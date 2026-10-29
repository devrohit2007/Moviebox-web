#!/usr/bin/env python3
"""MovieBox Web — local-network web GUI.  Run:  python server.py [port]"""
import base64
import gzip
import hashlib
import hmac
import http.cookiejar
import io
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from requests.adapters import HTTPAdapter

import moviebox as mb
import providers as pv

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
IMG_DIR = os.path.join(CACHE_DIR, "img")
STATIC_DIR = os.path.join(HERE, "static")
os.makedirs(IMG_DIR, exist_ok=True)

app = Flask(__name__, static_folder=None)
client = mb.MovieBoxClient()
media = mb.pooled_session(64)  # keep-alive pool for CDN media + images (no cookies)

try:  # optional: thumbnails need Pillow; everything works (unresized) without it
    from PIL import Image

    HAVE_PIL = True
except Exception:  # pragma: no cover
    HAVE_PIL = False


# ---------------------------------------------------------------- response cache
class _Flight:
    __slots__ = ("event", "value", "error")

    def __init__(self):
        self.event = threading.Event()
        self.value = None
        self.error = None


class Cache:
    """TTL cache with request coalescing and stale-while-revalidate.

    * fresh  : served straight from memory
    * stale  : served immediately, refreshed once in the background
    * missing: ONE caller fetches, everybody else waits for that result
    """

    def __init__(self, maxsize=600):
        self.d, self.inflight, self.maxsize = {}, {}, maxsize
        self.lock = threading.Lock()
        self.hits = self.misses = self.stale_hits = self.coalesced = 0

    def _put(self, key, value):
        with self.lock:
            self.d[key] = (value, time.time())
            if len(self.d) > self.maxsize:
                for k, _ in sorted(self.d.items(), key=lambda kv: kv[1][1])[: len(self.d) - self.maxsize]:
                    self.d.pop(k, None)

    def _flight(self, key, fn):
        with self.lock:
            fl = self.inflight.get(key)
            leader = fl is None
            if leader:
                fl = self.inflight[key] = _Flight()
        if leader:
            try:
                fl.value = fn()
                self._put(key, fl.value)
            except Exception as ex:  # noqa: BLE001
                fl.error = ex
            finally:
                with self.lock:
                    self.inflight.pop(key, None)
                fl.event.set()
        else:
            self.coalesced += 1
            fl.event.wait(timeout=45)
        if fl.error is not None:
            raise fl.error
        if not fl.event.is_set():
            raise TimeoutError("upstream timed out")
        return fl.value

    def get(self, key, fresh, stale, fn):
        now = time.time()
        with self.lock:
            e = self.d.get(key)
        if e:
            age = now - e[1]
            if age < fresh:
                self.hits += 1
                return e[0]
            if age < stale:
                self.stale_hits += 1
                with self.lock:
                    busy = key in self.inflight
                if not busy:
                    threading.Thread(target=self._refresh, args=(key, fn), daemon=True).start()
                return e[0]
        self.misses += 1
        return self._flight(key, fn)

    def _refresh(self, key, fn):
        try:
            self._flight(key, fn)
        except Exception:  # keep serving the stale copy
            pass


cache = Cache()


def pack(obj):
    """Serialise once; keep plain + gzip bytes so repeat requests cost nothing."""
    body = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    gz = gzip.compress(body, 5) if len(body) > 700 else None
    return {"body": body, "gz": gz, "etag": '"%s"' % hashlib.md5(body).hexdigest()[:16]}


def send_packed(p, max_age=60, swr=600):
    if request.headers.get("If-None-Match") == p["etag"]:
        r = Response(status=304)
    else:
        use_gz = p["gz"] is not None and "gzip" in request.headers.get("Accept-Encoding", "")
        r = Response(p["gz"] if use_gz else p["body"], mimetype="application/json")
        if use_gz:
            r.headers["Content-Encoding"] = "gzip"
    r.headers["ETag"] = p["etag"]
    r.headers["Vary"] = "Accept-Encoding"
    r.headers["Cache-Control"] = f"private, max-age={max_age}, stale-while-revalidate={swr}"
    return r


# ---------------------------------------------------------------- token store
_tokens = {}
_tok_lock = threading.Lock()
TOKEN_TTL = 6 * 3600
TOKEN_MAX = 3000


def register(entry, key=None) -> str:
    """Store an entry and return its token. A `key` makes the token stable (same input -> same token)."""
    now = time.time()
    tok = hashlib.sha1(key.encode()).hexdigest()[:14] if key else secrets.token_urlsafe(9)
    with _tok_lock:
        old = _tokens.get(tok)
        if old is not None and key:
            old["t"] = now
            return tok
        if len(_tokens) > TOKEN_MAX:
            for k in [k for k, v in _tokens.items() if now - v["t"] > TOKEN_TTL]:
                _tokens.pop(k, None)
            while len(_tokens) > TOKEN_MAX:
                _tokens.pop(next(iter(_tokens)))
        entry["t"] = now
        _tokens[tok] = entry
    return tok


def lookup(tok):
    with _tok_lock:
        e = _tokens.get(tok)
    if e and time.time() - e["t"] > TOKEN_TTL * 4:
        return None
    return e


# ---------------------------------------------------------------- helpers
def err(e, code=502):
    return jsonify({"error": str(e) or e.__class__.__name__}), code


def kind_of(url: str) -> str:
    path = url.split("?")[0].lower()
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith(".m3u8"):
        return "hls"
    return "mp4"


def b64u(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def unb64u(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()


# ---------------------------------------------------------------- signed image URLs
def _load_secret():
    path = os.path.join(CACHE_DIR, "secret.key")
    try:
        with open(path, "rb") as f:
            k = f.read()
            if len(k) >= 16:
                return k
    except OSError:
        pass
    k = secrets.token_bytes(24)
    try:
        with open(path, "wb") as f:
            f.write(k)
    except OSError:
        pass
    return k


SECRET = _load_secret()


def _sign(u: str) -> str:
    return hmac.new(SECRET, u.encode(), hashlib.sha256).hexdigest()[:16]


def img_ref(u):
    """Original image URL -> local, signed, resizable URL (the page appends &w=<px>)."""
    if not u or not isinstance(u, str) or not u.startswith(("http://", "https://")):
        return u
    return f"/img?u={b64u(u)}&s={_sign(u)}"


def localize(obj):
    """Point poster/backdrop of items (or sections of items) at the local image proxy."""
    if isinstance(obj, dict):
        for k in ("poster", "backdrop"):
            if k in obj:
                obj[k] = img_ref(obj[k])
        for v in obj.values():
            if isinstance(v, (dict, list)):
                localize(v)
    elif isinstance(obj, list):
        for v in obj:
            localize(v)
    return obj


# ---------------------------------------------------------------- static files (gzip + validators, in memory)
_static_cache = {}


def serve_static(name, no_cache=False):
    path = os.path.normpath(os.path.join(STATIC_DIR, name))
    if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
        return "not found", 404
    mtime = os.path.getmtime(path)
    ent = _static_cache.get(path)
    if not ent or ent["m"] != mtime:
        with open(path, "rb") as f:
            raw = f.read()
        ctype = {
            ".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "application/javascript",
            ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json",
        }.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
        textual = ctype.startswith(("text/", "application/javascript", "application/json")) or ctype == "image/svg+xml"
        ent = _static_cache[path] = {
            "m": mtime, "raw": raw, "ctype": ctype,
            "gz": gzip.compress(raw, 6) if textual and len(raw) > 700 else None,
            "etag": '"%s"' % hashlib.md5(raw).hexdigest()[:16],
        }
    if request.headers.get("If-None-Match") == ent["etag"]:
        r = Response(status=304)
    else:
        use_gz = ent["gz"] is not None and "gzip" in request.headers.get("Accept-Encoding", "")
        r = Response(ent["gz"] if use_gz else ent["raw"], mimetype=ent["ctype"].split(";")[0])
        r.headers["Content-Type"] = ent["ctype"]
        if use_gz:
            r.headers["Content-Encoding"] = "gzip"
    r.headers["ETag"] = ent["etag"]
    r.headers["Vary"] = "Accept-Encoding"
    r.headers["Cache-Control"] = "no-cache" if no_cache else "public, max-age=86400"
    return r


@app.route("/")
def index():
    return serve_static("index.html", no_cache=True)


@app.route("/manifest.json")
def manifest():
    return jsonify(
        {
            "name": "MovieBox Web",
            "short_name": "MovieBox",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0b0b0f",
            "theme_color": "#0b0b0f",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@app.route("/static/<path:name>")
def static_files(name):
    return serve_static(name)


# ---------------------------------------------------------------- API (cached + coalesced)
def _home(tab, page, lite):
    payload = client.homepage(tab, page)
    sections = mb.homepage_to_sections(payload)
    if page == 1 and tab == "2":   # remember what the first screen needs, so it can be pre-built (see _warm_loop)
        global _warm_jobs
        jobs = []
        feat = next((s for s in sections if s.get("featured")), None)
        for it in (feat["items"][:6] if feat else []):
            if it.get("backdrop") or it.get("poster"):
                jobs.append((it.get("backdrop") or it["poster"], (780, 1280, 1920)))
        for s in [s for s in sections if s is not feat][:4]:
            for it in s["items"][:12]:
                if it.get("poster"):
                    jobs.append((it["poster"], (320, 480)))
        _warm_jobs = jobs
    out = {"sections": sections}
    if not lite:  # legacy flat list, kept for older pages
        out["items"] = mb.homepage_to_items(payload)
    return pack(localize(out))


@app.route("/api/home")
def api_home():
    tab = request.args.get("tab", "2")
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    lite = request.args.get("lite") == "1"
    try:
        p = cache.get(("home", tab, page, lite), 600, 6 * 3600, lambda: _home(tab, page, lite))
        return send_packed(p, 60, 600)
    except Exception as e:
        return err(e)


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return send_packed(pack({"items": []}))
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        p = cache.get(
            ("search", q.lower(), page), 600, 3600,
            lambda: pack(localize({"items": mb.search_to_items(client.search(q, page))})),
        )
        return send_packed(p, 120, 600)
    except Exception as e:
        return err(e)


@app.route("/api/details")
def api_details():
    sid = request.args["id"]
    try:
        p = cache.get(
            ("details", sid), 1800, 24 * 3600,
            lambda: pack(localize(mb.details_to_media(client.details(sid)))),
        )
        return send_packed(p, 300, 3600)
    except Exception as e:
        return err(e)


def _streams(sid, se, ep):
    releases = mb.get_releases(client, sid, se, ep)
    if not releases:
        raise LookupError("No stream sources available")
    out, groups = [], {}
    for r in releases:
        kind = kind_of(r["url"])
        if r["multi"] and kind in ("dash", "hls"):
            g = groups.get(r["url"])
            if g is None:
                g = groups[r["url"]] = {"rel": r, "res": []}
            g["res"].append(r["res"])
            continue
        out.append((r, kind, [r["res"]] if r["res"] else []))
    adaptive = [(g["rel"], kind_of(g["rel"]["url"]), sorted(g["res"], reverse=True)) for g in groups.values()]
    items = []
    for r, kind, res in adaptive + out:
        name = "index.mpd" if kind == "dash" else "index.m3u8" if kind == "hls" else "video.mp4"
        tok = register(
            {
                "url": r["url"],
                "base": r["url"].split("?")[0].rsplit("/", 1)[0] + "/",
                "headers": r["headers"],
                "name": name,
            },
            key=r["url"] + "|" + json.dumps(r["headers"], sort_keys=True),
        )
        items.append(
            {
                "label": ("Auto " + "/".join(f"{x}p" for x in res)) if kind != "mp4" and res else r["label"],
                "kind": kind,
                "src": f"/px/{tok}/{name}",
                "resolutions": res,
                "size": r["size"],
                "resource_id": r["resource_id"],
                "filename": r["filename"],
            }
        )
    return pack({"streams": items})


def _fallback_streams(prov, title, year, typ, se, ep, pid):
    """Streams for the same title from another provider (or a specific item when `pid` is given)."""
    if pid:
        rels, found = pv.releases_for(prov, pid, se, ep), pid
    else:
        best, rels = pv.auto_releases(prov, title, year, typ, se, ep)
        found = best["title"]
    pname = {p["id"]: p["name"] for p in pv.provider_list()}.get(prov, prov)
    items = []
    for r in rels[:14]:
        if r.get("mirrors"):  # resolved on first play (see _ensure_resolved): listing stays instant
            tok = register(
                {"mirrors": r["mirrors"], "referer": r.get("referer", ""), "headers": {}, "name": "video.mp4",
                 "base": "", "lk": threading.Lock()},
                key="lazy|" + json.dumps(r["mirrors"], sort_keys=True),
            )
            kind, name = "mp4", "video.mp4"
        else:
            kind = kind_of(r["url"])
            name = "index.mpd" if kind == "dash" else "index.m3u8" if kind == "hls" else "video.mp4"
            tok = register(
                {"url": r["url"], "base": r["url"].split("?")[0].rsplit("/", 1)[0] + "/", "headers": r["headers"], "name": name},
                key=r["url"] + "|" + json.dumps(r["headers"], sort_keys=True),
            )
        items.append({
            "label": f"{pname}: {r['label']}", "kind": kind, "src": f"/px/{tok}/{name}",
            "resolutions": [r["res"]] if r["res"] else [], "size": r["size"], "resource_id": None,
            "filename": r["filename"], "provider": prov,
        })
    return pack({"streams": items, "provider": prov, "found": found})


def _ensure_resolved(e):
    """Lazy 4KHDHub-style entries: pick a working mirror the first time the player asks for the file."""
    if "mirrors" not in e:
        return True
    with e["lk"]:
        if e.get("url") and time.time() - e.get("resolved", 0) < 1200:
            return True
        if e.get("fail") and time.time() - e["fail"] < 45:
            return False
        try:
            url, headers, _label = pv.resolve_4k_mirrors(e["mirrors"], e.get("referer") or "https://4khdhub.one/")
        except Exception as ex:
            print("[mirror] none playable:", ex.__class__.__name__, str(ex)[:120], flush=True)
            e["fail"] = time.time()
            return False
        e.pop("fail", None)
        e.update(url=url, headers=headers, base=url.split("?")[0].rsplit("/", 1)[0] + "/", resolved=time.time())
    return True


@app.route("/api/providers")
def api_providers():
    cfg = pv.load_config()
    return jsonify({"providers": pv.provider_list(), "preferred": cfg["preferred"], "on_missing": cfg["on_missing"]})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        pv.save_config(request.get_json(silent=True) or {})
    cfg = pv.load_config()
    return jsonify({"providers": pv.provider_list(), "addons": cfg["addons"], "tv_playlists": cfg["tv_playlists"],
                    "fourkhdhub_url": cfg["fourkhdhub_url"], "bs4": pv.BeautifulSoup is not None,
                    "preferred": cfg["preferred"], "on_missing": cfg["on_missing"]})


@app.route("/api/psearch")
def api_psearch():
    prov, q = request.args.get("p", ""), request.args.get("q", "").strip()
    if not q:
        return jsonify({"items": []})
    try:
        if prov == "moviebox":
            items = [{"pid": i["id"], "title": i["title"], "year": i.get("year"), "type": i.get("type")}
                     for i in mb.search_to_items(client.search(q, 1))]
        else:
            items = pv.search(prov, q)
    except Exception as e:
        return err(e)
    return jsonify({"items": [{"pid": i["pid"], "title": i["title"], "year": i.get("year"), "type": i.get("type")} for i in items[:30]]})


# ---------------------------------------------------------------- live TV
@app.route("/api/tv/channels")
def api_tv_channels():
    data = pv.tv_channels()
    q = request.args.get("q", "").strip().lower()
    group = request.args.get("group", "")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    chans = data["channels"]
    groups = sorted({c["group"] for c in chans if c.get("group")})
    sel = [c for c in chans if (not group or c.get("group") == group) and (not q or q in c["name"].lower())]
    per = 60
    items = [{"i": c["i"], "name": c["name"], "group": c.get("group", ""),
              "logo": img_ref(c["logo"]) if c.get("logo", "").startswith(("http://", "https://")) else ""}
             for c in sel[(page - 1) * per: page * per]]
    return jsonify({"items": items, "total": len(sel), "groups": groups, "errors": data["errors"],
                    "configured": bool(pv.load_config()["tv_playlists"]), "more": page * per < len(sel)})


@app.route("/api/tv/play")
def api_tv_play():
    try:
        c = pv.tv_channels()["channels"][int(request.args.get("i", -1))]
    except (ValueError, IndexError):
        return err("channel not found", 404)
    url = c["url"]
    hls = ".m3u8" in url.lower()
    name = "index.m3u8" if hls else "video.mp4"
    tok = register({"url": url, "base": url.split("?")[0].rsplit("/", 1)[0] + "/",
                    "headers": {"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"}, "name": name}, key="tv|" + url)
    return jsonify({"streams": [{"label": c["name"], "kind": "hls" if hls else "mp4", "src": f"/px/{tok}/{name}",
                                 "resolutions": [], "size": None, "resource_id": None, "filename": c["name"]}]})


@app.route("/api/streams")
def api_streams():
    sid = request.args.get("id", "")
    se = int(request.args.get("se", 0))
    ep = int(request.args.get("ep", 0))
    prov = request.args.get("p", "moviebox")
    if prov != "moviebox":
        a = request.args
        try:
            p = cache.get(
                ("fstreams", prov, a.get("title", ""), a.get("year", ""), a.get("type", ""), se, ep, a.get("pid", "")),
                300, 0, lambda: _fallback_streams(prov, a.get("title", ""), a.get("year", ""), a.get("type", ""), se, ep, a.get("pid", "")),
            )
        except pv.NotAvailable as e:
            return jsonify({"error": str(e), "none": True}), 404
        except Exception as e:
            print("[fallback]", prov, e.__class__.__name__, str(e)[:160], flush=True)
            return jsonify({"error": f"{prov}: {e.__class__.__name__}", "none": True}), 404
        return send_packed(p, 0, 0)
    try:
        # short TTL: stream links are signed and expire; no stale serving
        p = cache.get(("streams", sid, se, ep), 120, 0, lambda: _streams(sid, se, ep))
    except LookupError as e:
        try:
            diag = json.dumps(_probe_data(sid, se, ep), separators=(",", ":"))[:1800]
        except Exception:
            diag = ""
        print("[streams] none for", sid, se, ep, diag, flush=True)
        return jsonify({"error": str(e), "diag": diag}), 404
    except Exception as e:
        return err(e)
    return send_packed(p, 0, 0)


def _captions(sid, rid, se, ep, sibs):
    caps = mb.get_captions(client, sid, rid, sibs, se, ep)
    out = []
    for c in caps:
        tok = register({"sub": c["url"]}, key="sub|" + c["url"])
        out.append({"name": c["name"], "src": f"/sub/{tok}.vtt"})
    return pack({"captions": out})


@app.route("/api/captions")
def api_captions():
    sid = request.args.get("id", "")
    rid = request.args.get("rid", "")
    se = int(request.args.get("se", 0))
    ep = int(request.args.get("ep", 0))
    sibs = tuple(s for s in request.args.get("sibs", "").split(",") if s)
    try:
        p = cache.get(("caps", sid, rid, se, ep, sibs), 1800, 0, lambda: _captions(sid, rid, se, ep, sibs))
    except Exception as e:
        return err(e)
    return send_packed(p, 600, 0)


# ---------------------------------------------------------------- image proxy (resize + disk cache)
IMG_WIDTHS = (160, 240, 320, 480, 780, 1280, 1920)
_resize_slots = threading.Semaphore(2)  # at most 2 resizes at once: protects slow CPUs
_img_writes = 0


def _sniff(b: bytes):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


def _prune_images(limit=400 * 1024 * 1024, target=300 * 1024 * 1024):
    try:
        files = [(f.stat().st_mtime, f.stat().st_size, f.path) for f in os.scandir(IMG_DIR) if f.is_file()]
        total = sum(s for _, s, _ in files)
        if total <= limit:
            return
        for _, size, path in sorted(files):
            os.remove(path)
            total -= size
            if total <= target:
                break
    except OSError:
        pass


def _download_image(url):
    r = media.get(url, headers={"User-Agent": client.ua, "Accept-Encoding": "identity"}, timeout=(5, 20))
    r.raise_for_status()
    data = r.content
    if len(data) > 25 * 1024 * 1024:
        raise ValueError("image too large")
    return data


def _build_image(url, width, path, data=None):
    global _img_writes
    if data is None:
        data = _download_image(url)
    out = data
    if HAVE_PIL:
        with _resize_slots:
            try:
                im = Image.open(io.BytesIO(data))
                if im.format == "JPEG":
                    im.draft("RGB", (width * 2, width * 3))  # decode at reduced size: much cheaper
                if im.mode not in ("RGB", "L"):
                    im = im.convert("RGB")
                if im.width > width:
                    im.thumbnail((width, 100000), Image.BILINEAR)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=78)
                if buf.tell() < len(data) or im.width != width:
                    out = buf.getvalue()
            except Exception:
                out = data  # not decodable: pass it through untouched
    tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "wb") as f:
        f.write(out)
    os.replace(tmp, path)
    _img_writes += 1
    if _img_writes % 150 == 0:
        threading.Thread(target=_prune_images, daemon=True).start()
    return path


def _img_path(u, width):
    return os.path.join(IMG_DIR, f"{hashlib.sha1(u.encode()).hexdigest()[:24]}_{width}.img")


def prewarm_images(jobs, pause=0.03):
    """Build the thumbnails the home page is about to ask for, in the background.

    jobs: [(original_url, (widths...))]. Each image is downloaded once and resized to every width, so the
    first device after a (re)start finds ready-made thumbnails instead of waiting on download + resize."""
    for u, widths in jobs:
        todo = [w for w in widths if not os.path.exists(_img_path(u, w))]
        if not todo:
            continue
        try:
            data = _download_image(u)
            for w in todo:
                p = _img_path(u, w)
                cache.get(("img", p), 0, 0, lambda w=w, p=p: _build_image(u, w, p, data))
        except Exception:
            pass
        time.sleep(pause)  # leave CPU for real requests on slow devices


@app.route("/img")
def img():
    try:
        u = unb64u(request.args["u"])
    except Exception:
        return "bad request", 400
    if not hmac.compare_digest(_sign(u), request.args.get("s", "")):
        return "forbidden", 403
    try:
        want = int(request.args.get("w", "320"))
    except ValueError:
        want = 320
    width = next((w for w in IMG_WIDTHS if w >= want), IMG_WIDTHS[-1])
    path = _img_path(u, width)
    if not os.path.exists(path):
        try:
            # coalesced: 10 devices asking for one poster cause ONE download + resize
            # fresh=0: only coalesce concurrent builds; the DISK is the cache (files can be pruned at any time)
            cache.get(("img", path), 0, 0, lambda: _build_image(u, width, path))
        except Exception:
            return "image unavailable", 404
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:  # pruned between the check and the read
        return "image unavailable", 404
    resp = send_file(path, mimetype=_sniff(head), max_age=7 * 86400, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return resp


# ---------------------------------------------------------------- subtitles
TS_RE = re.compile(r"(\d{1,2}:\d{2}:\d{2}),(\d{3})")


def ass_to_vtt(text: str) -> str:
    fmt, out = None, ["WEBVTT", ""]

    def t(x):
        h, m, s = x.strip().split(":")
        s, cs = (s.split(".") + ["0"])[:2]
        return f"{int(h):02d}:{m}:{int(s):02d}.{(cs + '00')[:2]}0"

    for line in text.splitlines():
        if line.lower().startswith("format:") and fmt is None and "Start" in line:
            fmt = [x.strip() for x in line.split(":", 1)[1].split(",")]
        elif line.lower().startswith("dialogue:") and fmt:
            parts = line.split(":", 1)[1].split(",", len(fmt) - 1)
            d = dict(zip(fmt, parts))
            try:
                body = re.sub(r"\{[^}]*\}", "", d["Text"]).replace("\\N", "\n").replace("\\n", "\n").strip()
                out += [f"{t(d['Start'])} --> {t(d['End'])}", body, ""]
            except Exception:
                continue
    return "\n".join(out)


def to_vtt(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.lstrip().startswith("WEBVTT"):
        return text
    if "[Script Info]" in text or "Dialogue:" in text:
        return ass_to_vtt(text)
    return "WEBVTT\n\n" + TS_RE.sub(r"\1.\2", text)


def _fetch_vtt(url):
    r = media.get(url, headers={"User-Agent": client.ua}, timeout=10)
    r.raise_for_status()
    return pack_text(to_vtt(r.content))


def pack_text(text):
    body = text.encode()
    return {"body": body, "gz": gzip.compress(body, 5) if len(body) > 700 else None}


@app.route("/sub/<tok>.vtt")
def sub(tok):
    e = lookup(tok)
    if not e or "sub" not in e:
        return "not found", 404
    try:
        p = cache.get(("vtt", e["sub"]), 3600, 0, lambda: _fetch_vtt(e["sub"]))
    except Exception as ex:
        return f"subtitle fetch failed: {ex}", 502
    use_gz = p["gz"] is not None and "gzip" in request.headers.get("Accept-Encoding", "")
    r = Response(p["gz"] if use_gz else p["body"], mimetype="text/vtt")
    if use_gz:
        r.headers["Content-Encoding"] = "gzip"
    r.headers["Vary"] = "Accept-Encoding"
    r.headers["Cache-Control"] = "max-age=3600"
    return r


# ---------------------------------------------------------------- media proxy
PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag")


def rewrite_mpd(text: str, e: dict) -> str:
    m = re.search(r"<BaseURL>\s*(https?://[^<\s]+)\s*</BaseURL>", text)
    if m:
        base = m.group(1)
        e["base"] = base if base.endswith("/") else base.rsplit("/", 1)[0] + "/"
        text = text.replace(m.group(1), "")
    return text


def rewrite_m3u8(text: str, tok: str) -> str:
    def proxied(u):
        return f"/px/{tok}/__abs/{b64u(u)}"

    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            ln = re.sub(r'URI="(https?://[^"]+)"', lambda m: f'URI="{proxied(m.group(1))}"', ln)
        elif s.startswith(("http://", "https://")):
            ln = proxied(s)
        lines.append(ln)
    return "\n".join(lines) + "\n"


@app.route("/px/<tok>/<path:rest>")
def proxy(tok, rest):
    e = lookup(tok)
    if not e or ("url" not in e and "mirrors" not in e):
        return "not found", 404
    if not _ensure_resolved(e):
        return "no working mirror for this file right now", 502

    if rest == e["name"]:
        upstream = e["url"]
    elif rest.startswith("__abs/"):
        try:
            upstream = unb64u(rest[6:])
        except Exception:
            return "bad url", 400
        if not upstream.startswith(("http://", "https://")):
            return "bad url", 400
    else:
        upstream = urljoin(e["base"], rest)
        if request.query_string:
            upstream += ("&" if "?" in upstream else "?") + request.query_string.decode()

    headers = dict(e["headers"])
    headers["Accept-Encoding"] = "identity"
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]
    try:
        up = media.get(upstream, headers=headers, stream=True, timeout=(5, 30))  # pooled: reuses TLS connections
    except requests.RequestException as ex:
        return f"upstream error: {ex}", 502

    low = upstream.split("?")[0].lower()
    is_m3u8 = low.endswith(".m3u8") or (rest == e["name"] and e["name"].endswith(".m3u8") and "mpegurl" in up.headers.get("Content-Type", "").lower())
    if (low.endswith(".mpd") or is_m3u8) and up.status_code == 200:
        text = up.text  # fully read: the connection goes back to the pool by itself
        text = rewrite_mpd(text, e) if low.endswith(".mpd") else rewrite_m3u8(text, tok)
        return Response(
            text,
            mimetype="application/dash+xml" if low.endswith(".mpd") else "application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    def gen():
        done = False
        try:
            for chunk in up.iter_content(64 * 1024):
                if chunk:
                    yield chunk
            done = True  # fully read: connection returns to the pool for the next Range request
        finally:
            if not done:  # player seeked / left: drop the half-read connection
                up.close()

    resp = Response(stream_with_context(gen()), status=up.status_code)
    for h in PASS_HEADERS:
        if h in up.headers:
            resp.headers[h] = up.headers[h]
    if "Accept-Ranges" not in resp.headers:
        resp.headers["Accept-Ranges"] = "bytes"
    return resp


# ---------------------------------------------------------------- diagnostics
def _probe_data(sid, se, ep):
    """Diagnose 'No stream sources': what the provider actually returned."""

    def mask(u):
        u = u or ""
        return {"host": u.split("/")[2] if u.startswith("http") and u.count("/") > 2 else "",
                "ext": u.split("?")[0].rsplit(".", 1)[-1][:5], "notice": mb.is_notice_url(u)}

    out = {"id": sid, "se": se, "ep": ep}
    try:
        pi = client.play_info(sid, se, ep)
        d = pi.get("data", pi) if isinstance(pi, dict) else {}
        out["play_info"] = {
            "keys": sorted(d.keys())[:20] if isinstance(d, dict) else str(type(d)),
            "streams": [{"fmt": s.get("format"), "res": s.get("resolutions"), "url": mask(s.get("url")),
                         "cookie": bool(s.get("signCookie")), "dash": bool(mb.dash_from_cookie(s.get("signCookie") or ""))}
                        for s in (d.get("streams") or [])],
        }
    except Exception as e:
        out["play_info_error"] = f"{e.__class__.__name__}: {e}"[:300]
    try:
        rs = client.episode_resources(sid, se, ep)
        items = (rs.get("list") or []) if isinstance(rs, dict) else []
        out["resource_v2"] = [{"se": i.get("se"), "ep": i.get("ep"), "res": i.get("resolution"),
                               "url": mask(i.get("resourceLink") or i.get("url"))} for i in items[:12]]
    except Exception as e:
        out["resource_v2_error"] = f"{e.__class__.__name__}: {e}"[:300]
    try:
        items = (rs.get("list") or []) if isinstance(rs, dict) else []
        if items:
            it0 = items[0]
            origin = it0.get("resourceLink") or it0.get("url")
            out["item0_keys"] = sorted(it0.keys())[:24]
            rid = mb._s(it0.get("resourceId", it0.get("id")))
            out["item0_rid"] = rid
            pos = client.resource_position(sid, rid, origin)
            pd = pos.get("data", pos) if isinstance(pos, dict) else {}
            out["position"] = {"keys": sorted(pos.keys())[:12] if isinstance(pos, dict) else str(type(pos)),
                               "list": [{"rid": mb._s(c.get("resourceId", c.get("id"))), "se": c.get("se"), "ep": c.get("ep"),
                                         "res": c.get("resolution"), "url": mask(c.get("resourceLink") or c.get("url"))}
                                        for c in (pd.get("list") or [])[:6]] if isinstance(pd, dict) else []}
    except Exception as e:
        out["position_error"] = f"{e.__class__.__name__}: {e}"[:300]
    try:
        d = client.details(sid)
        inner = d.get("subject") if isinstance(d, dict) and isinstance(d.get("subject"), dict) else d
        dets = (d.get("resourceDetectors") if isinstance(d, dict) else None) or (inner.get("resourceDetectors") if isinstance(inner, dict) else None) or []
        out["detectors"] = [{"keys": sorted(x.keys())[:14],
                             "rl": [{"keys": sorted(y.keys())[:14], "url": mask(y.get("resourceLink") or y.get("url") or y.get("link"))}
                                    for y in ((x.get("resolutionList") or [])[:3])]} for x in dets[:2] if isinstance(x, dict)]
        out["details_keys"] = sorted(d.keys())[:30] if isinstance(d, dict) else str(type(d))
    except Exception as e:
        out["details_error"] = f"{e.__class__.__name__}: {e}"[:300]
    try:
        out["final_releases"] = len(mb.get_releases(client, sid, se, ep))
    except Exception as e:
        out["final_error"] = f"{e.__class__.__name__}: {e}"[:300]
    return out


@app.route("/api/_probe")
def probe():
    return jsonify(_probe_data(request.args["id"], int(request.args.get("se", 0)), int(request.args.get("ep", 0))))


@app.route("/api/_stats")
def stats():
    return jsonify(
        {
            "cache": {"hits": cache.hits, "stale": cache.stale_hits, "misses": cache.misses,
                      "coalesced": cache.coalesced, "entries": len(cache.d)},
            "tokens": len(_tokens),
            "pillow": HAVE_PIL,
        }
    )


# ---------------------------------------------------------------- main
def lan_ip():
    for target in ("8.8.8.8", "1.1.1.1", "10.255.255.255", "192.168.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    return None


_warm_jobs = []


def _warm_loop():
    """Keep the home page (and its thumbnails) warm so the first device after idle never waits."""
    while True:
        try:
            cache.get(("home", "2", 1, True), 0, 0, lambda: _home("2", 1, True))
            prewarm_images(list(_warm_jobs))
        except Exception:
            pass
        time.sleep(480)


def serve(host="0.0.0.0", port=8000):
    import logging

    if os.environ.get("MB_LOG") != "1":   # a log line per request floods Termux and costs real CPU on phones
        logging.getLogger("werkzeug").setLevel(logging.ERROR)
    threading.Thread(target=_prune_images, daemon=True).start()
    threading.Thread(target=_warm_loop, daemon=True).start()
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    ip = lan_ip()
    print(f"\n  Open on this device:     http://127.0.0.1:{port}")
    if ip:
        print(f"  Open on other devices:   http://{ip}:{port}   (same Wi-Fi)\n")
    else:
        print("  Open on other devices:   use the 'Running on http://<ip>:%d' line below (not 127.0.0.1)\n" % port)
    if not HAVE_PIL:
        print("  Tip: 'pkg install python-pillow' makes posters load much faster (small thumbnails).\n")
    serve("0.0.0.0", port)
