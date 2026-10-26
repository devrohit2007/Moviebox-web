#!/usr/bin/env python3
"""MovieBox Web — local-network web GUI.  Run:  python server.py [port]"""
import base64
import os
import re
import secrets
import socket
import sys
import threading
import time
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

import moviebox as mb

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
client = mb.MovieBoxClient()

# ---------------------------------------------------------------- token store
_tokens = {}
_tok_lock = threading.Lock()
TOKEN_TTL = 6 * 3600
TOKEN_MAX = 2000


def register(entry) -> str:
    now = time.time()
    with _tok_lock:
        if len(_tokens) > TOKEN_MAX:
            for k in [k for k, v in _tokens.items() if now - v["t"] > TOKEN_TTL]:
                _tokens.pop(k, None)
            while len(_tokens) > TOKEN_MAX:
                _tokens.pop(next(iter(_tokens)))
        tok = secrets.token_urlsafe(9)
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


# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


@app.route("/manifest.json")
def manifest():
    return jsonify(
        {
            "name": "MovieBox Web",
            "short_name": "MovieBox",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#0e0f13",
            "theme_color": "#0e0f13",
            "icons": [
                {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
    )


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(os.path.join(HERE, "static"), name)


# ---------------------------------------------------------------- API
@app.route("/api/home")
def api_home():
    tab = request.args.get("tab", "2")
    page = request.args.get("page", "1")
    try:
        payload = client.homepage(tab, int(page))
        return jsonify({"items": mb.homepage_to_items(payload), "sections": mb.homepage_to_sections(payload)})
    except Exception as e:
        return err(e)


@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"items": []})
    try:
        return jsonify({"items": mb.search_to_items(client.search(q, int(request.args.get("page", 1))))})
    except Exception as e:
        return err(e)


@app.route("/api/details")
def api_details():
    try:
        return jsonify(mb.details_to_media(client.details(request.args["id"])))
    except Exception as e:
        return err(e)


@app.route("/api/streams")
def api_streams():
    sid = request.args["id"]
    se = int(request.args.get("se", 0))
    ep = int(request.args.get("ep", 0))
    try:
        releases = mb.get_releases(client, sid, se, ep)
    except Exception as e:
        return err(e)
    if not releases:
        return err("No stream sources available", 404)

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
            }
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
    return jsonify({"streams": items})


@app.route("/api/captions")
def api_captions():
    sid = request.args.get("id", "")
    rid = request.args.get("rid", "")
    se = int(request.args.get("se", 0))
    ep = int(request.args.get("ep", 0))
    sibs = [s for s in request.args.get("sibs", "").split(",") if s]
    try:
        caps = mb.get_captions(client, sid, rid, sibs, se, ep)
    except Exception as e:
        return err(e)
    out = []
    for c in caps:
        tok = register({"sub": c["url"]})
        out.append({"name": c["name"], "src": f"/sub/{tok}.vtt"})
    return jsonify({"captions": out})


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


@app.route("/sub/<tok>.vtt")
def sub(tok):
    e = lookup(tok)
    if not e or "sub" not in e:
        return "not found", 404
    try:
        r = requests.get(e["sub"], headers={"User-Agent": client.ua}, timeout=10)
        r.raise_for_status()
    except requests.RequestException as ex:
        return f"subtitle fetch failed: {ex}", 502
    return Response(to_vtt(r.content), mimetype="text/vtt", headers={"Cache-Control": "max-age=3600"})


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
    if not e or "url" not in e:
        return "not found", 404

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
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]
    try:
        up = requests.get(upstream, headers=headers, stream=True, timeout=(5, 30))
    except requests.RequestException as ex:
        return f"upstream error: {ex}", 502

    low = upstream.split("?")[0].lower()
    if low.endswith((".mpd", ".m3u8")) and up.status_code == 200:
        text = up.text
        up.close()
        text = rewrite_mpd(text, e) if low.endswith(".mpd") else rewrite_m3u8(text, tok)
        return Response(
            text,
            mimetype="application/dash+xml" if low.endswith(".mpd") else "application/vnd.apple.mpegurl",
            headers={"Cache-Control": "no-store"},
        )

    def gen():
        try:
            for chunk in up.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            up.close()

    resp = Response(stream_with_context(gen()), status=up.status_code)
    for h in PASS_HEADERS:
        if h in up.headers:
            resp.headers[h] = up.headers[h]
    if "Accept-Ranges" not in resp.headers:
        resp.headers["Accept-Ranges"] = "bytes"
    return resp


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


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    ip = lan_ip()
    print(f"\n  Open on this device:     http://127.0.0.1:{port}")
    if ip:
        print(f"  Open on other devices:   http://{ip}:{port}   (same Wi-Fi)\n")
    else:
        print("  Open on other devices:   use the 'Running on http://<ip>:%d' line below (not 127.0.0.1)\n" % port)
    app.run(host="0.0.0.0", port=port, threaded=True)
