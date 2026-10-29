"""Fallback sources, tried when MovieBox has nothing playable for a title.

  4khdhub  - web index of movie/series releases, mirrors resolved lazily
  dramachi - JSON API (mostly Asian dramas)
  addons   - Stremio-style addons (Cinemeta metadata + the stream addons you add)
  live TV  - M3U playlists you add (separate page, not a fallback)

See THIRD_PARTY_NOTICES.md for licensing of the original work this is based on.
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import quote, unquote, urljoin, urlparse, parse_qs

import requests

import moviebox as mb

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
CONFIG_FILE = os.path.join(CACHE_DIR, "config.json")
BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Mobile Safari/537.36"
)
POOL = ThreadPoolExecutor(8)
http = mb.pooled_session(32)

try:  # HTML parsing for 4KHDHub (pure Python: `pip install beautifulsoup4`)
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover
    BeautifulSoup = None


class NotAvailable(Exception):
    """The provider does not have this title / episode."""


# ---------------------------------------------------------------- config
DEFAULT_CONFIG = {
    "enabled": {"moviebox": True, "4khdhub": True, "dramachi": True, "addons": True},
    "preferred": "moviebox",   # tried first
    "on_missing": "ask",       # when the preferred provider lacks a title: "ask" which to switch to, or "auto" try the rest
    "addons": [],          # Stremio addon manifest URLs that serve streams
    "tv_playlists": [],    # M3U playlist URLs
    "fourkhdhub_url": "",  # optional override when the site moves
}
_cfg_lock = threading.Lock()
_cfg = None


def _private_host(host: str) -> bool:
    host = (host or "").lower().strip("[]")
    if not host or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not ip.is_global


def clean_url(raw, allow_http=True):
    """Return a normalised http(s) URL, or None when it is not acceptable (private/loopback hosts are refused)."""
    u = (raw or "").strip().strip("\"'")
    if u.startswith("stremio://"):
        u = "https://" + u[len("stremio://"):]
    if not u:
        return None
    p = urlparse(u)
    if p.scheme not in (("http", "https") if allow_http else ("https",)) or not p.hostname or _private_host(p.hostname):
        return None
    return u


def load_config():
    global _cfg
    with _cfg_lock:
        if _cfg is None:
            data = {}
            try:
                with open(CONFIG_FILE) as f:
                    data = json.load(f)
            except Exception:
                pass
            cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            if isinstance(data, dict):
                cfg["enabled"].update({k: bool(v) for k, v in (data.get("enabled") or {}).items() if k in cfg["enabled"]})
                for k in ("addons", "tv_playlists"):
                    cfg[k] = [u for u in (data.get(k) or []) if isinstance(u, str)]
                cfg["fourkhdhub_url"] = data.get("fourkhdhub_url") or ""
                if data.get("preferred") in cfg["enabled"]:
                    cfg["preferred"] = data["preferred"]
                if data.get("on_missing") in ("ask", "auto"):
                    cfg["on_missing"] = data["on_missing"]
            _cfg = cfg
        return _cfg


def save_config(patch):
    """Merge a partial update from the settings page. Bad URLs are dropped."""
    cfg = load_config()
    with _cfg_lock:
        if isinstance(patch.get("enabled"), dict):
            for k, v in patch["enabled"].items():
                if k in cfg["enabled"]:
                    cfg["enabled"][k] = bool(v)
        for k in ("addons", "tv_playlists"):
            if isinstance(patch.get(k), list):
                seen, out = set(), []
                for raw in patch[k][:20]:
                    u = clean_url(raw if isinstance(raw, str) else "")
                    if u and u not in seen:
                        seen.add(u)
                        out.append(u)
                cfg[k] = out
        if patch.get("preferred") in cfg["enabled"]:
            cfg["preferred"] = patch["preferred"]
        if patch.get("on_missing") in ("ask", "auto"):
            cfg["on_missing"] = patch["on_missing"]
        if "fourkhdhub_url" in patch:
            u = clean_url(patch.get("fourkhdhub_url") or "", allow_http=False)
            cfg["fourkhdhub_url"] = u or ""
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cfg, f)
            os.replace(tmp, CONFIG_FILE)
        except OSError:
            pass
    _invalidate_caches()
    return cfg


# ---------------------------------------------------------------- small caches
class _TTL:
    def __init__(self, ttl, max_items=64):
        self.ttl, self.max, self.d, self.lock = ttl, max_items, {}, threading.Lock()

    def get(self, key):
        with self.lock:
            e = self.d.get(key)
            if e and time.time() - e[0] < self.ttl:
                return e[1]
        return None

    def put(self, key, val):
        with self.lock:
            if len(self.d) >= self.max:
                self.d.clear()
            self.d[key] = (time.time(), val)
        return val

    def clear(self):
        with self.lock:
            self.d.clear()


_pages = _TTL(60)
_searches = _TTL(600, 200)


def _invalidate_caches():
    _pages.clear()
    _searches.clear()
    _channels.clear()


# ---------------------------------------------------------------- text helpers
_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)")


def norm_title(t):
    t = (t or "").lower()
    t = _TAGS.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(w for w in t.split() if w not in ("the", "a", "an"))


def year4(text):
    m = re.search(r"(?<!\d)(19|20)\d{2}(?!\d)", text or "")
    return m.group(0) if m else ""


def score_candidate(c, title, year, typ):
    a, b = norm_title(title), norm_title(c.get("title"))
    if not a or not b:
        return 0
    if a == b:
        s = 100
    else:
        wa, wb = set(a.split()), set(b.split())
        jac = len(wa & wb) / max(1, len(wa | wb))
        s = 85 * jac if jac >= 0.6 else 0
        if s and (a in b or b in a):
            s += 5
    if not s:
        return 0
    cy = year4(str(c.get("year") or ""))
    if year and cy:
        s += 15 if year == cy else (5 if abs(int(year) - int(cy)) == 1 else -40)
    if typ and c.get("type") and c["type"] != typ:
        s -= 15
    return s


def pick_best(cands, title, year, typ):
    best, bs = None, 0
    for c in cands:
        s = score_candidate(c, title, year, typ)
        if s > bs:
            best, bs = c, s
    return best if bs >= 75 else None


def parse_size_bytes(text):
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*([KMGT]i?B)\b", text or "", re.I):
        n, unit = float(m.group(1)), m.group(2).upper()[0]
        return int(n * {"K": 1024, "M": 1048576, "G": 1073741824, "T": 1099511627776}[unit])
    return None


def parse_season_episode(text):
    m = re.search(r"S(\d+)\s*E(\d+)", (text or ""), re.I)
    return (int(m.group(1)), int(m.group(2))) if m else None


def detect_quality(v):
    low = (v or "").lower().replace("4khdhub", " ")   # the site's own name in file names is not "4K"
    for n in (2160, 1080, 720, 480):
        if re.search(rf"(?<!\d){n}(?!\d)", low):
            return f"{n}p"
    if re.search(r"\b(4k|uhd)\b", low):
        return "2160p"
    if re.search(r"\bfhd\b", low):
        return "1080p"
    if re.search(r"\bhd\b", low):
        return "720p"
    if re.search(r"\bsd\b", low):
        return "480p"
    return None


def detect_codec(v):
    v = (v or "").lower()
    if "av1" in v:
        return "AV1"
    if any(k in v for k in ("h.265", "h265", "x265")):
        return "H.265"
    if "hevc" in v:
        return "HEVC"
    if any(k in v for k in ("h.264", "h264", "x264", "avc")):
        return "H.264"
    if "remux" in v:
        return "REMUX"
    return None


_LANGS = [
    ("Hindi", ("hindi", "hin")), ("English", ("english", "eng")), ("Tamil", ("tamil", "tam")),
    ("Telugu", ("telugu", "tel")), ("Malayalam", ("malayalam", "mal")), ("Kannada", ("kannada", "kan")),
    ("Bengali", ("bengali", "ben", "bangla")), ("Marathi", ("marathi", "mar")), ("Punjabi", ("punjabi", "pan")),
    ("Japanese", ("japanese", "jpn")), ("Korean", ("korean", "kor")), ("Chinese", ("chinese", "chi", "mandarin")),
    ("Spanish", ("spanish", "spa")), ("French", ("french", "fre", "fra")), ("German", ("german", "ger", "deu")),
]


def detect_language(v):
    low = (v or "").lower()
    found = []
    for name, pats in _LANGS:
        for p in pats:
            m = re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low)
            if m:
                found.append((m.start(), name))
                break
    if found:
        return ", ".join(n for _, n in sorted(found))
    if "multi audio" in low or "multi-audio" in low:
        return "Multi Audio"
    if "dual audio" in low or "dual-audio" in low:
        return "Dual Audio"
    return None


def _release(provider, filename, quality, size, season, episode, url=None, headers=None, mirrors=None,
             codec=None, language=None, rid=None):
    res = 0
    if quality:
        q = str(quality).strip().lower()
        res = 2160 if q in ("4k", "uhd") else (int(re.sub(r"\D", "", q) or 0) if re.search(r"\d", q) else 0)
    lab = " · ".join(x for x in (f"{res}p" if res else None, codec, language) if x) or "Direct"
    return {
        "provider": provider, "filename": filename, "quality": f"{res}p" if res else None, "res": res,
        "codec": codec, "size": size, "season": season or None, "episode": episode or None,
        "resource_id": rid, "url": url, "headers": headers or {}, "mirrors": mirrors or [],
        "multi": False, "label": lab,
    }


# ================================================================ 4KHDHub
SEL_DOWNLOAD = ("a#download, a.btn-primary, a.btn-success, a.btn[href*='/download/'], a[href*='/download/'], "
                "a[href*='gamerxyt.com'], a[href*='hubcloud.php']")


def _text(node):
    return " ".join(node.get_text(" ").split()) if node else ""


def _meta_row(soup, label):
    for item in soup.select(".metadata-item"):
        if _text(item.select_one(".metadata-label")) == label:
            return _text(item.select_one(".metadata-value")) or None
    return None


def parse_search_4k(base, html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a.movie-card"):
        href = a.get("href")
        if not href:
            continue
        url = urlparse(urljoin(base, href))
        if url.hostname != urlparse(base).hostname:
            continue
        title = _text(a.select_one(".movie-card-title"))
        if not title:
            continue
        meta = _text(a.select_one(".movie-card-meta"))
        img = a.select_one("img")
        out.append({
            "pid": url.path, "title": title, "year": year4(meta),
            "type": "series" if "-series-" in href else "movie",
            "poster": img.get("src") if img else None,
        })
    return out


def _is_archive(name):
    low = name.lower()
    return low.endswith(".zip") or "complete season" in low or "season pack" in low


def parse_releases_4k(html, season, episode):
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select("#episodes .episode-download-item" if season > 0 else ".download-item")
    title_sel = ".episode-file-title" if season > 0 else ".file-title"
    page_lang = detect_language(_meta_row(soup, "Audios:") or "")
    grouped = {}
    for item in items:
        fname = _text(item.select_one(title_sel))
        if not fname or _is_archive(fname):
            continue
        se_ep = parse_season_episode(fname)
        if season > 0 and se_ep != (season, episode):
            continue
        mirrors = []
        for a in item.select("a[href]"):
            href = a.get("href", "")
            if not href.startswith("https://") or "logout" in href:
                continue
            mirrors.append({"label": _text(a) or "Source", "url": href})
        if not mirrors:
            continue
        size = None
        for n in item.select(".badge-size, .badge"):
            size = parse_size_bytes(_text(n))
            if size:
                break
        key = re.sub(r"[^a-z0-9]", "", fname.lower())
        rel = grouped.get(key)
        if rel is None:
            rel = grouped[key] = _release(
                "4khdhub", fname, detect_quality(fname), size, se_ep[0] if se_ep else 0, se_ep[1] if se_ep else 0,
                codec=detect_codec(fname), language=detect_language(fname) or page_lang)
            rel["mirrors"] = []
        for m in mirrors:
            if not any(x["url"] == m["url"] for x in rel["mirrors"]):
                rel["mirrors"].append(m)
    rels = list(grouped.values())
    # best for a browser first: 1080p, 720p, 480p, then 4K; H.264 before HEVC; smaller (faster to start) before bigger
    rank = {1080: 0, 720: 1, 480: 2, 2160: 3}
    rels.sort(key=lambda r: (rank.get(r["res"], 4), 0 if r["codec"] == "H.264" else 1, r["size"] or 10 ** 15))
    return rels


def _norm_playback_url(raw):
    """https only, public host, not an archive/login page. Spaces and brackets in the path are encoded."""
    try:
        p = urlparse(raw)
        host = (p.hostname or "").lower()
    except ValueError:
        return None
    if p.scheme != "https" or not host or _private_host(host):
        return None
    path = p.path
    if re.search(r"[^A-Za-z0-9/:@!$&'()*+,;=\-._~%]", path):
        path = quote(path, safe="/:@!$&'()*+,;=-._~%")
    low = path.lower()
    if low.endswith(".zip") or "login.php" in low or "logout" in low or "greenmotors." in host or "greenmountmotors." in host:
        return None
    return p._replace(path=path).geturl()


def _score(url, label):
    v = f"{url} {label}".lower()
    if any(k in v for k in ("pixel.hubcloud.", "googleusercontent.com", "googlevideo.com", "cloudflarestorage.com",
                            "fsl server", "10gbps", "r2.dev", "watch online")):
        return 0
    if any(k in v for k in ("storage.googleapis.com", "hubcloud.cx/re/", "hubcloud.fans/re/")):
        return 1
    if "pixeldrain" in v:
        return 2
    if any(k in v for k in ("testzip.php", "vcloud.php", "drive.php", "gpdl.")):
        return 3
    return 4


def _pixeldrain_api(raw):
    try:
        p = urlparse(raw)
    except ValueError:
        return None
    if "pixeldrain." not in (p.hostname or ""):
        return None
    path = p.path
    if path.startswith("/u/"):
        fid = path[3:].strip("/")
    elif path.startswith("/api/file/"):
        fid = path[len("/api/file/"):].strip("/")
    else:
        return None
    if not fid or not re.fullmatch(r"[A-Za-z0-9_-]+", fid):
        return None
    return f"https://{p.hostname}/api/file/{fid}?download"


def _script_pixeldrain(html):
    urls = []
    for prefix in ("https://pixeldrain.dev/u/", "https://pixeldrain.com/u/",
                   "https://pixeldrain.dev/api/file/", "https://pixeldrain.com/api/file/"):
        for m in re.finditer(re.escape(prefix) + r"[^\"'\s<\\]*", html):
            u = _pixeldrain_api(m.group(0))
            if u and u not in urls:
                urls.append(u)
    return urls


def _unwrap_watch_online(raw):
    try:
        p = urlparse(raw)
        if "pages.dev" in (p.hostname or ""):
            tok = (parse_qs(p.query).get("u") or [""])[0]
            dec = base64.b64decode(tok + "=" * (-len(tok) % 4)).decode()
            if dec.startswith("https://"):
                return dec
    except Exception:
        pass
    return None


def _fetch(url, ua=BROWSER_UA, timeout=(5, 12), **kw):
    r = http.get(url, headers={"User-Agent": ua}, timeout=timeout, **kw)
    r.raise_for_status()
    return r


def _resolve_hubcloud(url):
    u = urlparse(url)
    if u.scheme != "https" or "hubcloud." not in (u.hostname or "") or not u.path.startswith("/drive/"):
        raise ValueError("bad hubcloud url")
    drive = BeautifulSoup(_fetch(url).text, "html.parser")
    resolver = next((a["href"] for a in drive.select(SEL_DOWNLOAD) if a.get("href", "").startswith("https://")), None)
    if not resolver:
        raise ValueError("HubCloud resolver link missing")
    html = _fetch(resolver).text
    cands = [(_score(u, "PixelDrain"), u, "PixelDrain") for u in _script_pixeldrain(html)]
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        href, label = a["href"], " ".join(a.get_text(" ").split())
        un = _unwrap_watch_online(href)
        if un and _norm_playback_url(un):
            v = _norm_playback_url(un)
            cands.append((_score(v, "Watch Online"), v, "Watch Online"))
            continue
        v = _norm_playback_url(href)
        if v:
            v = _pixeldrain_api(v) or v
            cands.append((_score(v, label), v, label or "Direct"))
    cands.sort(key=lambda c: c[0])
    out, seen = [], set()
    for _, u, label in cands:
        if u not in seen:
            seen.add(u)
            out.append((u, label))
    if not out:
        raise ValueError("no candidates found")
    return out


def _resolve_hubdrive(url):
    u = urlparse(url)
    if u.scheme != "https" or "hubdrive." not in (u.hostname or "") or not u.path.startswith("/file/"):
        raise ValueError("bad hubdrive url")
    soup = BeautifulSoup(_fetch(url).text, "html.parser")
    for a in soup.select("a[href]"):
        p = urlparse(a["href"])
        if "hubcloud." in (p.hostname or "") and p.path.startswith("/drive/"):
            return _resolve_hubcloud(a["href"])
    raise ValueError("HubDrive HubCloud mirror missing")


def _rot13(s):
    return s.translate(str.maketrans(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "nopqrstuvwxyzabcdefghijklmNOPQRSTUVWXYZABCDEFGHIJKLM"))


def _b64d(s):
    return base64.b64decode(s + "=" * (-len(s) % 4)).decode()


def _unpack_greenmotors(html):
    m = re.search(r"""s\(\s*['"]o['"]\s*,\s*(['"])(.*?)\1""", html, re.S)
    if not m:
        return None
    try:
        step = _b64d(_rot13(_b64d(_b64d(m.group(2)))))
        return _b64d(json.loads(step)["o"])
    except Exception:
        return None


def _resolve_greenmotors(url):
    host = urlparse(url).hostname or ""
    if "greenmotors." not in host and "greenmountmotors." not in host:
        raise ValueError("bad greenmotors url")
    target = _unpack_greenmotors(_fetch(url).text)
    if not target:
        raise ValueError("GreenMotors mirror target missing")
    if "hubcloud." in target:
        return _resolve_hubcloud(target)
    if "hubdrive." in target:
        return _resolve_hubdrive(target)
    v = _norm_playback_url(target)
    if not v:
        raise ValueError("bad direct url")
    return [(v, "Direct")]


def _resolve_mirror(m):
    u = m["url"]
    if "hubcloud." in u:
        return _resolve_hubcloud(u)
    if "hubdrive." in u:
        return _resolve_hubdrive(u)
    if "greenmotors." in u or "greenmountmotors." in u:
        return _resolve_greenmotors(u)
    v = _norm_playback_url(u)
    if not v:
        raise ValueError("unplayable direct url")
    return [(v, m.get("label") or "Direct")]


_BAD_BODY = ("failed to extract link", "token expired", "file not found", "404 not found", "link has expired",
             "expired", "access denied", "downloadquotaexceeded", "generate link again")


def _preflight(url, headers):
    """Range-probe a candidate. Returns (final_url, seekable); raises when it is not a media file."""
    if not _norm_playback_url(url):
        raise ValueError("invalid url")
    r = http.get(url, headers={**headers, "Range": "bytes=0-8191"}, timeout=(3, 4), stream=True)
    try:
        r.raise_for_status()
        seekable = r.status_code == 206
        final = r.url
        ctype = (r.headers.get("Content-Type") or "").lower()
        if any(t in ctype for t in ("text/html", "application/zip", "text/plain")):
            body = r.content[:20000].decode("utf-8", "ignore").lower()
            if any(b in body for b in _BAD_BODY):
                raise ValueError("expired link")
            wrapped = (parse_qs(urlparse(final).query).get("link") or [""])[0]
            if not wrapped.startswith("https://") or not _norm_playback_url(wrapped):
                raise ValueError(f"not media: {ctype}")
            r.close()
            r = http.get(wrapped, headers={**headers, "Range": "bytes=0-8191"}, timeout=(3, 4), stream=True)
            r.raise_for_status()
            seekable = r.status_code == 206
            final = r.url
            if any(t in (r.headers.get("Content-Type") or "").lower() for t in ("text/html", "application/zip", "text/plain")):
                raise ValueError("not media")
        return final, seekable
    finally:
        r.close()


def resolve_4k_mirrors(mirrors, referer):
    """Pick one playable file out of a release's mirrors -> (url, headers, label). Raises when none works."""
    ex = ThreadPoolExecutor(8)
    try:
        futs = {ex.submit(_resolve_mirror, m): m for m in mirrors}
        done, _ = wait(futs, timeout=9)
        cands, seen = [], set()
        for f in done:
            try:
                for url, label in f.result():
                    if url not in seen:
                        seen.add(url)
                        cands.append((_score(url, label), url, label))
            except Exception:
                continue
        cands.sort(key=lambda c: c[0])
        if not cands:
            raise LookupError("no working mirror links")
        hdrs = {"Referer": referer.rstrip("/"), "User-Agent": BROWSER_UA}
        pf = {ex.submit(_preflight, url, hdrs): (sc, url, label) for sc, url, label in cands[:6]}
        fallbacks, pending = [], set(pf)
        deadline = time.time() + 10
        while pending and time.time() < deadline:
            done, pending = wait(pending, timeout=max(0.1, deadline - time.time()), return_when=FIRST_COMPLETED)
            for f in done:
                sc, url, label = pf[f]
                try:
                    final, seekable = f.result()
                except Exception:
                    continue
                sc = min(sc, _score(final, label))
                if seekable and sc <= 1:
                    return final, hdrs, label
                fallbacks.append((not seekable, sc, final, label))
        if not fallbacks:
            raise LookupError("all mirrors failed")
        fallbacks.sort(key=lambda x: (x[0], x[1]))
        _, _, final, label = fallbacks[0]
        return final, hdrs, label
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


class FourKHub:
    id, name = "4khdhub", "4KHDHub"

    def __init__(self, cfg):
        self.base = (os.environ.get("MB_FOURKHDHUB_URL") or cfg.get("fourkhdhub_url") or "https://4khdhub.one/").strip()
        if not self.base.endswith("/"):
            self.base += "/"

    def ready(self):
        return (True, "") if BeautifulSoup else (False, "needs: pip install beautifulsoup4")

    def search(self, q):
        r = _fetch(self.base, timeout=(5, 15), params={"s": q})
        return parse_search_4k(self.base, r.text)

    def _page(self, pid):
        url = urljoin(self.base, pid.lstrip("/"))
        if urlparse(url).hostname != urlparse(self.base).hostname:
            raise ValueError("bad id")
        html = _pages.get(url)
        if html is None:
            html = _pages.put(url, _fetch(url, timeout=(5, 18)).text)
        return html

    def releases(self, pid, se, ep):
        rels = parse_releases_4k(self._page(pid), se, ep)
        for r in rels:
            r["referer"] = self.base
        return rels


# ================================================================ Dramachi
class Dramachi:
    id, name = "dramachi", "Dramachi"
    API = "https://api.nodeobjects.com/"
    CDN = "https://static.nodeobjects.com/thumbnail/"

    def __init__(self, cfg):
        pass

    def ready(self):
        return True, ""

    def _get(self, **params):
        r = http.get(self.API, params=params, headers={"User-Agent": BROWSER_UA}, timeout=(5, 15))
        r.raise_for_status()
        return r.json()

    def search(self, q):
        data = self._get(interface="search", q=q, filter="all", page=1)
        out = []
        for it in (data.get("data") or []):
            is_movie = str(it.get("content") or "").lower() in ("movie", "movies")
            thumb = (it.get("thumb") or "").strip()
            out.append({"pid": str(it.get("id")), "title": it.get("title") or "", "year": it.get("year") or "",
                        "type": "movie" if is_movie else "series", "poster": self.CDN + thumb if thumb else None})
        return out

    def _episodes(self, title_id, rip):
        key = ("dc-eps", title_id, rip)
        eps = _pages.get(key)
        if eps is None:
            data = self._get(interface="eplist", season=rip, id=title_id)
            eps = data.get("episode_list") or []
            eps.sort(key=lambda e: _ep_num(e.get("f_title")) or 10 ** 9)
            _pages.put(key, eps)
        return eps

    def releases(self, pid, se, ep):
        title_id, _, rip = pid.partition("::")
        title_id, rip = title_id.strip(), rip.strip()
        if not title_id:
            raise NotAvailable("no id")
        if not rip:
            data = self._get(interface="title_v2", id=title_id)
            seasons = data.get("seasons") or {}
            if se > 0:
                g = seasons.get(f"Season {se:02d}") or {}
                vs = g.get("versions") or []
                rip = vs[0]["rip"] if vs else ""
            if not rip:
                for g in seasons.values():
                    vs = g.get("versions") or []
                    if vs:
                        rip = vs[0]["rip"]
                        break
            rip = rip or (f"Season {se:02d}" if se > 0 else "hd Rip")
        eps = self._episodes(title_id, rip)
        if not eps:
            raise NotAvailable("no episodes")
        if se == 0 and ep == 0:
            matched = eps
        else:
            matched = [e for e in eps if _ep_num(e.get("f_title")) == ep]
            if not matched and 0 < ep <= len(eps):
                matched = [eps[ep - 1]]
        futs = [POOL.submit(self._file, e) for e in matched if e.get("fid") and e.get("disk")]
        rels = []
        for e, f in zip([e for e in matched if e.get("fid") and e.get("disk")], futs):
            try:
                url, fname = f.result(timeout=20)
            except Exception:
                continue
            rels.append(_release("dramachi", fname, e.get("quality") or "", parse_size_bytes(e.get("size") or ""), se, ep,
                                 url=url, headers={"User-Agent": BROWSER_UA}, rid=e["fid"]))
        rels.sort(key=lambda r: r["res"], reverse=True)
        return rels

    def _file(self, e):
        data = self._get(interface="getFile", fid=e["fid"], findex=e["disk"])
        host = (data.get("hostInfo") or {}).get("host") or ""
        info = (data.get("fileInfo") or [None])[0]
        if not host or not info or not info.get("url"):
            raise NotAvailable("no file")
        url = f"https://{host.rstrip('/')}/cdn/{info['url'].lstrip('/')}"
        return url, info.get("filename") or info.get("f_title") or f"{e['fid']}.mkv"


def _clean_ep_title(t):
    t = (t or "").strip()
    for suf in (" 720p", " 1080p", " 540p", " 480p", " 360p", " DUB", " hi DUB", " ENG Subbed Full"):
        if t.endswith(suf):
            t = t[: -len(suf)]
    return t


def _ep_num(f_title):
    up = _clean_ep_title(f_title).upper()
    for prefix in ("EPISODE", "EP.", "EP", "E"):
        start = 0
        while True:
            pos = up.find(prefix, start)
            if pos < 0:
                break
            prev = up[pos - 1] if pos else ""
            ok = (not prev) or (not prev.isalpha()) or (prefix == "E" and prev.isdigit())
            rest = up[pos + len(prefix):].lstrip(" -._")
            m = re.match(r"\d+", rest)
            if ok and m:
                return int(m.group(0))
            start = pos + len(prefix)
    last = (_clean_ep_title(f_title).split() or [""])[-1]
    return int(last) if last.isdigit() else None


# ================================================================ Stremio addons
CINEMETA = "https://v3-cinemeta.strem.io"


def _addon_base(url):
    u = url.strip()
    if u.startswith("stremio://"):
        u = "https://" + u[len("stremio://"):]
    u = u.split("?")[0].split("#")[0]
    if u.endswith("/manifest.json"):
        u = u[: -len("/manifest.json")]
    return u.rstrip("/")


class Addons:
    id, name = "addons", "Addons"

    def __init__(self, cfg):
        self.urls = [u for u in cfg.get("addons", []) if clean_url(u)]

    def ready(self):
        return (True, "") if self.urls else (False, "add a Stremio addon URL in Sources")

    def search(self, q, typ=None):
        out = []
        for t in ([typ] if typ in ("movie", "series") else ["movie", "series"]):
            try:
                r = http.get(f"{CINEMETA}/catalog/{t}/top/search={quote(q.strip(), safe='')}.json",
                             headers={"User-Agent": BROWSER_UA}, timeout=(5, 10))
                r.raise_for_status()
                for m in (r.json().get("metas") or []):
                    if str(m.get("id", "")).startswith("tt"):
                        out.append({"pid": f"{t}:{m['id']}", "title": m.get("name") or "",
                                    "year": year4(str(m.get("releaseInfo") or m.get("year") or "")),
                                    "type": t, "poster": m.get("poster")})
            except Exception:
                continue
        return out

    def releases(self, pid, se, ep):
        typ, _, imdb = pid.partition(":")
        if not imdb:
            typ, imdb = "movie", pid
        sid = f"{imdb}:{se}:{ep}" if typ == "series" and ep else imdb

        def one(url):
            r = http.get(f"{_addon_base(url)}/stream/{typ}/{quote(sid, safe=':')}.json",
                         headers={"User-Agent": BROWSER_UA}, timeout=(4, 7))
            r.raise_for_status()
            j = r.json()
            return j.get("streams") if isinstance(j, dict) else j

        futs = [POOL.submit(one, u) for u in self.urls]
        rels, seen = [], set()
        for f in futs:
            try:
                streams = f.result(timeout=9) or []
            except Exception:
                continue
            for s in streams:
                rel = _addon_release(s, se, ep)
                if rel and rel["url"].split("?")[0] not in seen:
                    seen.add(rel["url"].split("?")[0])
                    rels.append(rel)
        rels.sort(key=lambda r: (r["res"], r["size"] or 0), reverse=True)
        return rels


_ALLOWED_HDR = {"user-agent", "referer", "origin", "accept", "accept-language"}


def _addon_release(s, se, ep):
    url = (s.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):  # torrents (infoHash) cannot be played in a browser
        return None
    text = " ".join(str(s.get(k) or "") for k in ("name", "title", "description"))
    if se and ep:
        pair = parse_season_episode(text)
        if pair and pair != (se, ep):
            return None
    hints = s.get("behaviorHints") or {}
    hdrs = {"User-Agent": BROWSER_UA}
    raw = hints.get("headers") or ((hints.get("proxyHeaders") or {}).get("request")) or {}
    for k, v in raw.items():
        if str(k).lower() in _ALLOWED_HDR:
            hdrs[k] = str(v)
    first = (s.get("title") or s.get("description") or s.get("name") or "Stream").splitlines()[0].strip()
    rel = _release("addons", first, detect_quality(text), hints.get("videoSize") or parse_size_bytes(text), se, ep,
                   url=url, headers=hdrs, codec=detect_codec(text), language=detect_language(text))
    host = urlparse(url).hostname or ""
    rel["label"] = f"{(s.get('name') or host).splitlines()[0].strip()[:24]} · {rel['label']}"
    return rel


# ================================================================ registry + orchestration
_CLASSES = [FourKHub, Dramachi, Addons]


def instances():
    cfg = load_config()
    return {c.id: c(cfg) for c in _CLASSES}


def provider_list():
    cfg = load_config()
    out = [{"id": "moviebox", "name": "MovieBox", "enabled": bool(cfg["enabled"].get("moviebox", True)), "ready": True, "note": ""}]
    for inst in instances().values():
        ok, note = inst.ready()
        out.append({"id": inst.id, "name": inst.name, "enabled": bool(cfg["enabled"].get(inst.id)), "ready": ok, "note": note})
    return out


def search(provider, q, typ=None):
    inst = instances().get(provider)
    if inst is None:
        raise NotAvailable("unknown provider")
    key = (provider, q.lower(), typ or "")
    hit = _searches.get(key)
    if hit is not None:
        return hit
    res = inst.search(q, typ) if isinstance(inst, Addons) else inst.search(q)
    return _searches.put(key, res)


def releases_for(provider, pid, se, ep):
    inst = instances().get(provider)
    if inst is None:
        raise NotAvailable("unknown provider")
    rels = inst.releases(pid, se, ep)
    if not rels:
        raise NotAvailable("no files for this title")
    return rels


def auto_releases(provider, title, year, typ, se, ep):
    """Find the same title on `provider` and return its releases. Raises NotAvailable when it isn't there."""
    title = mb.clean_title(title or "")
    if not title:
        raise NotAvailable("no title")
    inst = instances().get(provider)
    if inst is None:
        raise NotAvailable("unknown provider")
    ok, note = inst.ready()
    if not ok:
        raise NotAvailable(note)
    queries = [title]
    short = re.split(r"\s*[:\-–]\s+", title, maxsplit=1)[0].strip()
    if short and short != title:
        queries.append(short)
    best = None
    for q in queries:
        try:
            cands = search(provider, q, typ)
        except NotAvailable:
            raise
        except Exception as e:
            raise NotAvailable(f"{inst.name} unreachable: {e.__class__.__name__}")
        best = pick_best(cands, title, year, typ)
        if best:
            break
    if not best:
        raise NotAvailable(f"{inst.name} does not have this title")
    return best, releases_for(provider, best["pid"], se, ep)


# ================================================================ live TV (M3U)
_channels = _TTL(24 * 3600, 8)
MAX_PLAYLIST = 15 * 1024 * 1024


def _attr(line, name):
    m = re.search(name + r"""=(["'])(.*?)\1""", line)
    return m.group(2) if m else ""


def parse_m3u(text):
    out, cur = [], {}
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            cur = {"id": _attr(line, "tvg-id"), "logo": _attr(line, "tvg-logo"), "group": _attr(line, "group-title")}
            quote_ch, comma = None, -1
            for i, ch in enumerate(line):
                if quote_ch:
                    if ch == quote_ch:
                        quote_ch = None
                elif ch in "\"'":
                    quote_ch = ch
                elif ch == ",":
                    comma = i
                    break
            if comma < 0:
                comma = line.find(",")
            cur["name"] = line[comma + 1:].strip() if comma >= 0 else ""
        elif not line.startswith("#") and cur is not None and cur:
            cur["url"] = line
            cur["id"] = cur["id"] or cur.get("name", "")
            out.append(cur)
            cur = {}
    return out


def _load_playlist(url):
    path_dir = os.path.join(CACHE_DIR, "tv")
    os.makedirs(path_dir, exist_ok=True)
    path = os.path.join(path_dir, hashlib.md5(url.encode()).hexdigest() + ".m3u")
    try:
        if time.time() - os.path.getmtime(path) < 24 * 3600:
            with open(path, encoding="utf-8", errors="ignore") as f:
                chans = parse_m3u(f.read())
            if chans:
                return chans
    except OSError:
        pass
    r = http.get(url, headers={"User-Agent": BROWSER_UA}, timeout=(8, 30), stream=True)
    r.raise_for_status()
    data = b""
    for chunk in r.iter_content(64 * 1024):
        data += chunk
        if len(data) > MAX_PLAYLIST:
            r.close()
            raise ValueError("playlist larger than 15 MB")
    text = data.decode("utf-8", "ignore")
    chans = parse_m3u(text)
    if chans:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    return chans


def tv_channels():
    cached = _channels.get("all")
    if cached is not None:
        return cached
    chans, errors = [], []
    for url in load_config()["tv_playlists"]:
        try:
            for c in _load_playlist(url):
                if c.get("url", "").startswith(("http://", "https://")):
                    chans.append(c)
        except Exception as e:
            errors.append(f"{urlparse(url).hostname}: {e.__class__.__name__}")
    for i, c in enumerate(chans):
        c["i"] = i
    res = {"channels": chans, "errors": errors}
    return _channels.put("all", res) if chans or not errors else res
