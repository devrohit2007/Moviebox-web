"""MovieBox provider client (Python).

Implements request signing, visitor session, host failover, search, homepage,
details, seasons, stream resolution (direct + DASH) and subtitles.
See THIRD_PARTY_NOTICES.md for licensing of the original work this is based on.
"""
import base64
import hashlib
import hmac
import json
import os
import random
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse

import http.cookiejar
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter

HOSTS = [
    "https://api6.aoneroom.com",
    "https://api5.aoneroom.com",
    "https://api4.aoneroom.com",
    "https://api4sg.aoneroom.com",
    "https://api3.aoneroom.com",
    "https://api6sg.aoneroom.com",
    "https://api.inmoviebox.com",
]
RETRY_STATUS = {403, 406, 407, 429, 500, 502, 503, 504}
SECRET = bytes.fromhex("efa891974eecd3148df63aa611602defd101259ba521022c57ae0566bd8e")
BODY_MAX = 102_400
STREAM_REFERER = "https://sportslive.wine"
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "session.json")


# ---------------------------------------------------------------- crypto
def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def client_token(ts: int) -> str:
    s = str(ts)
    return f"{s},{md5_hex(s[::-1].encode())}"


def sorted_query(url: str) -> str:
    q = urlparse(url).query
    if not q:
        return ""
    params = {}
    for k, v in parse_qsl(q, keep_blank_values=True):
        params.setdefault(k, []).append(v)
    return "&".join(f"{k}={v}" for k in sorted(params) for v in params[k])


def canonical_string(method, accept, content_type, url, body, ts) -> str:
    path = urlparse(url).path
    query = sorted_query(url)
    canon_url = f"{path}?{query}" if query else path
    if body is not None:
        raw = body.encode()
        body_hash, body_len = md5_hex(raw[:BODY_MAX]), str(len(raw))
    else:
        body_hash, body_len = "", ""
    return "\n".join(
        [method.upper(), accept or "", content_type or "", body_len, str(ts), body_hash, canon_url]
    )


def signature(method, accept, content_type, url, body, ts) -> str:
    canon = canonical_string(method, accept, content_type, url, body, ts)
    mac = hmac.new(SECRET, canon.encode(), hashlib.md5).digest()
    return f"{ts}|2|{base64.b64encode(mac).decode()}"


def _hex(n):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def gen_client_info():
    android = random.choice(
        [
            ("9", "PQ3A.190605.03081104"),
            ("10", "QP1A.191005.007.A3"),
            ("11", "RP1A.200720.011"),
            ("12", "S1B.220414.015"),
            ("13", "TQ2A.230405.003"),
        ]
    )
    device = random.choice(
        ["23078RKD5C", "2201117TY", "2201117TG", "22101316G", "21121210G", "M2012K11AG", "M2007J20CG"]
    )
    vcode = random.choice([50020117, 50020118, 50020119, 50020120, 50020121])
    net = random.choice(["NETWORK_WIFI", "NETWORK_MOBILE"])
    tz = random.choice(["Asia/Kolkata", "Asia/Shanghai", "Asia/Tokyo", "America/New_York", "Europe/London"])
    gaid = f"{_hex(8)}-{_hex(4)}-{_hex(4)}-{_hex(4)}-{_hex(12)}"
    ua = (
        f"com.community.oneroom/{vcode} (Linux; U; Android {android[0]}; en_US; "
        f"{device}; Build/{android[1]}; Cronet/135.0.7012.3)"
    )
    info = json.dumps(
        {
            "package_name": "com.community.oneroom",
            "version_name": "4.0.01.0813.03",
            "version_code": vcode,
            "os": "android",
            "os_version": android[0],
            "install_ch": "ps",
            "device_id": _hex(32),
            "install_store": "ps",
            "gaid": gaid,
            "brand": "Redmi",
            "model": device,
            "system_language": "en",
            "net": net,
            "region": "US",
            "timezone": tz,
            "sp_code": "40401",
            "X-Play-Mode": "2",
        },
        separators=(",", ":"),
    )
    return ua, info


def spoofed_ip():
    prefix = random.choice(
        ["103.241", "49.36", "117.195", "106.198", "122.162", "157.32", "182.70", "103.58", "27.60", "59.90"]
    )
    return f"{prefix}.{random.randint(1, 253)}.{random.randint(1, 253)}"


def jwt_claims(token: str):
    parts = token.split(".")
    if len(parts) < 2:
        return None, None
    try:
        p = parts[1]
        p += "=" * (-len(p) % 4)
        data = json.loads(base64.urlsafe_b64decode(p))
    except Exception:
        return None, None
    uid = data.get("userId") or data.get("uid") or data.get("sub")
    exp = data.get("exp")
    try:
        exp = int(exp) if exp is not None else None
    except (TypeError, ValueError):
        exp = None
    return (str(uid) if uid is not None else None), exp


# ---------------------------------------------------------------- client
class ApiError(Exception):
    pass


class _NoCookies(http.cookiejar.CookiePolicy):
    """Never store cookies: sessions are only used for connection reuse."""
    netscape = True
    rfc2965 = hide_cookie2 = False

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

    def domain_return_ok(self, domain, request):
        return False

    def path_return_ok(self, path, request):
        return False


def pooled_session(maxsize=64):
    """requests.Session with a large keep-alive pool and no cookie jar."""
    s = requests.Session()
    s.cookies.set_policy(_NoCookies())
    adapter = HTTPAdapter(pool_connections=16, pool_maxsize=maxsize, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


# shared worker pool for independent upstream calls (kept small for low-power devices)
POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mb")


class MovieBoxClient:
    def __init__(self):
        self.http = pooled_session(32)
        self.ua, self.client_info = gen_client_info()
        self.ip = spoofed_ip()
        self.host_idx = 0
        self.session = None  # dict(token, uid, exp, created)
        self.lock = threading.Lock()
        self._load_session()

    # -- session
    def _session_valid(self, s):
        if not s or not s.get("token", "").strip():
            return False
        now = time.time()
        if s.get("exp"):
            return now + 60 < s["exp"]
        return now < s.get("created", 0) + 7 * 86400

    def _load_session(self):
        try:
            with open(SESSION_FILE) as f:
                s = json.load(f)
            if self._session_valid(s):
                self.session = s
        except Exception:
            pass

    def _set_session(self, token, uid=None):
        j_uid, exp = jwt_claims(token)
        self.session = {"token": token, "uid": uid or j_uid, "exp": exp, "created": time.time()}
        try:
            os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
            with open(SESSION_FILE, "w") as f:
                json.dump(self.session, f)
        except Exception:
            pass

    def _invalidate(self):
        self.session = None
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass

    def ensure_session(self) -> str:
        if self._session_valid(self.session):
            return self.session["token"]
        with self.lock:
            if self._session_valid(self.session):
                return self.session["token"]
            val = self._request_hosts("POST", "/wefeed-mobile-bff/user-api/visitor-login", "{}", None)
            token = (val or {}).get("token") if isinstance(val, dict) else None
            if not token or not str(token).strip():
                raise ApiError("Missing token")
            uid = val.get("uid") or val.get("userId")
            self._set_session(token, str(uid) if uid is not None else None)
            return token

    # -- http
    def _headers(self, method, url, body, token):
        ts = int(time.time() * 1000)
        accept = ctype = "application/json"
        h = {
            "User-Agent": self.ua,
            "Accept": accept,
            "Content-Type": ctype,
            "Connection": "keep-alive",
            "x-client-token": client_token(ts),
            "x-tr-signature": signature(method, accept, ctype, url, body, ts),
            "x-client-info": self.client_info,
            "x-client-status": "0",
            "x-forwarded-for": self.ip,
        }
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _absorb_x_user(self, resp):
        raw = resp.headers.get("x-user")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        token = data.get("token")
        # only act on a *new* token: re-saving an unchanged session on every response = a disk write per API call
        if token and not (self.session and self.session.get("token") == token):
            uid = data.get("uid") or data.get("userId")
            self._set_session(token, str(uid) if uid is not None else None)

    def _request_hosts(self, method, path, body, token):
        start = self.host_idx
        backoff = 0.05
        for i in range(len(HOSTS)):
            if i > 0:
                time.sleep(backoff)
                backoff = 0.05
            idx = (start + i) % len(HOSTS)
            url = HOSTS[idx] + path
            headers = self._headers(method, url, body, token)
            try:
                resp = self.http.request(
                    method, url, headers=headers,
                    data=body.encode() if body is not None else None,
                    timeout=(3, 12),
                )
            except requests.RequestException:
                self.host_idx = (idx + 1) % len(HOSTS)
                continue
            self._absorb_x_user(resp)
            if resp.status_code in RETRY_STATUS:
                self.host_idx = (idx + 1) % len(HOSTS)
                if resp.status_code == 429:
                    try:
                        backoff = min(int(resp.headers.get("Retry-After", "")), 3)
                    except ValueError:
                        backoff = 0.4
                continue
            self.host_idx = idx
            if not resp.ok:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            if isinstance(payload, dict) and "data" in payload:
                return payload["data"]
            return payload
        raise ApiError("All hosts exhausted")

    def request(self, method, path, body_obj=None):
        body = None
        if body_obj is not None:
            body = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)
        token = self.ensure_session()
        try:
            return self._request_hosts(method, path, body, token)
        except ApiError:
            self._invalidate()
            token = self.ensure_session()
            return self._request_hosts(method, path, body, token)

    def get(self, path):
        return self.request("GET", path)

    # -- endpoints
    def search(self, query, page=1):
        return self.request(
            "POST",
            "/wefeed-mobile-bff/subject-api/search/v2",
            {"keyword": query, "page": page, "perPage": 15, "subjectType": 0},
        )

    def details(self, subject_id):
        d = self.get(f"/wefeed-mobile-bff/subject-api/get?subjectId={subject_id}")
        stype = None
        if isinstance(d, dict):
            inner = d.get("subject") if isinstance(d.get("subject"), dict) else d
            stype = inner.get("subjectType", inner.get("stype"))
        if stype == 2:
            try:
                info = self.get(f"/wefeed-mobile-bff/subject-api/season-info?subjectId={subject_id}")
                if isinstance(d, dict):
                    d["seasons"] = info
            except ApiError:
                pass
        return d

    def homepage(self, tab_id="2", page=1):
        return self.get(f"/wefeed-mobile-bff/tab-operating?page={page}&tabId={tab_id}&version=")

    def play_info(self, subject_id, season, episode):
        if season == 0 and episode == 0:
            path = f"/wefeed-mobile-bff/subject-api/play-info/v2?subjectId={subject_id}"
        else:
            path = (
                f"/wefeed-mobile-bff/subject-api/play-info/v2?subjectId={subject_id}"
                f"&se={season}&ep={episode}"
            )
        return self.get(path)

    def episode_resources(self, subject_id, season, episode):
        return self.get(
            "/wefeed-mobile-bff/subject-api/resource/v2?"
            f"subjectId={subject_id}&page=1&perPage=20&all=0&startPosition=1&endPosition=1"
            f"&pagerMode=2&resolution=0&se={season}&epFrom={episode}&epTo={episode}&isVip=true"
        )

    def resource_page(self, subject_id, season, episode, page=1):
        if season == 0 and episode == 0:
            path = f"/wefeed-mobile-bff/subject-api/resource?subjectId={subject_id}&page={page}&perPage=20"
        else:
            path = (
                f"/wefeed-mobile-bff/subject-api/resource?subjectId={subject_id}"
                f"&se={season}&ep={episode}&page={page}&perPage=20"
            )
        res = self.get(path)
        return (res.get("list") or []) if isinstance(res, dict) else []

    def resource_position(self, subject_id, resource_id, failed_url):
        q = urlencode(
            [
                ("subjectId", subject_id),
                ("resourceId", resource_id),
                ("failUrl", failed_url),
                ("failCode", "404"),
                ("resourceNum", "0"),
                ("isVip", "true"),
            ]
        )
        return self.get(f"/wefeed-mobile-bff/subject-api/resource-position/v2?{q}")

    def ext_captions(self, subject_id, resource_id):
        return self.get(
            f"/wefeed-mobile-bff/subject-api/get-ext-captions?subjectId={subject_id}&resourceId={resource_id}"
        )


# ---------------------------------------------------------------- adapters
def _s(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return str(int(v))
    return str(v)


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def year_of(raw):
    m = re.search(r"[12]\d{3}", raw or "")
    return m.group(0) if m else ""


def _img_url(v):
    if isinstance(v, dict):
        return v.get("url") or v.get("src")
    return v if isinstance(v, str) else None


def subject_to_item(s, banner_image=None):
    sid = _s(s.get("subjectId", s.get("id")))
    if not sid:
        return None
    stype = s.get("subjectType", s.get("stype", 1))
    poster = _img_url(s.get("cover")) or s.get("coverUrl") or s.get("poster") or s.get("pic")
    backdrop = banner_image or _img_url(s.get("stills")) or _img_url(s.get("backdrop")) or _img_url(s.get("banner"))
    desc = s.get("description") or s.get("intro") or ""
    return {
        "id": sid,
        "title": s.get("title") or s.get("name") or "Unknown",
        "type": "series" if stype == 2 else "movie",
        "year": year_of(str(s.get("releaseDate") or s.get("year") or s.get("releaseInfo") or "")),
        "poster": poster,
        "backdrop": backdrop,
        "rating": s.get("imdbRatingValue"),
        "description": (desc[:260] + "…") if len(desc) > 260 else desc,
    }


def search_to_items(payload):
    subjects = None
    if isinstance(payload, dict):
        results = payload.get("results") or (payload.get("data") or {}).get("results")
        if isinstance(results, list) and results:
            subjects = results[0].get("subjects")
        if subjects is None:
            subjects = payload.get("list") or (payload.get("data") or {}).get("list")
    out = []
    for s in subjects or []:
        it = subject_to_item(s)
        if it:
            out.append(it)
    return out


def homepage_to_sections(payload):
    """Group homepage payload into titled rows: [{title, items, featured?}]."""
    groups = payload.get("items") if isinstance(payload, dict) else payload
    seen, sections = set(), []
    for g in groups or []:
        entries = []  # (subject, banner_image)
        banners = (g.get("banner") or {}).get("banners") or []
        for b in banners:
            if b.get("subject"):
                entries.append((b["subject"], _img_url(b.get("image")) or _img_url(b.get("cover"))))
        for c in ((g.get("customData") or {}).get("items") or []):
            if c.get("subject"):
                entries.append((c["subject"], _img_url(c.get("image"))))
        for sub in g.get("subjects") or []:
            entries.append((sub, None))
        items = []
        for sub, img in entries:
            it = subject_to_item(sub, img)
            if it and it["id"] not in seen:
                seen.add(it["id"])
                items.append(it)
        if items:
            title = g.get("title") or g.get("name") or g.get("opTitle") or ("Trending" if not sections else f"Picks {len(sections) + 1}")
            sec = {"title": str(title), "items": items}
            if banners:
                sec["featured"] = True
            sections.append(sec)
    return sections


def homepage_to_items(payload):
    groups = payload.get("items") if isinstance(payload, dict) else payload
    seen, out = set(), []
    for g in groups or []:
        subs = []
        for b in ((g.get("banner") or {}).get("banners") or []):
            if b.get("subject"):
                subs.append(b["subject"])
        for c in ((g.get("customData") or {}).get("items") or []):
            if c.get("subject"):
                subs.append(c["subject"])
        subs.extend(g.get("subjects") or [])
        for s in subs:
            it = subject_to_item(s)
            if it and it["id"] not in seen:
                seen.add(it["id"])
                out.append(it)
    return out


def details_to_media(payload):
    subject = payload
    if isinstance(payload, dict):
        subject = (payload.get("data") or {}).get("subject") or payload.get("subject") or payload
    sid = _s(subject.get("subjectId", subject.get("id")))
    if not sid:
        raise ApiError("Not found")
    stype = subject.get("subjectType", subject.get("stype", 1))
    cover = subject.get("cover")
    poster = (cover.get("url") if isinstance(cover, dict) else None) or subject.get("coverUrl")
    dur = subject.get("duration")
    if isinstance(dur, int) and dur > 0:
        dur = f"{dur // 60}m"
    elif not isinstance(dur, str) or dur.strip() in ("", "0", "0m"):
        dur = None
    genres = subject.get("genre") or subject.get("genres") or []
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",") if g.strip()]

    seasons = []
    raw = subject.get("seasons")
    if raw is None and isinstance(payload, dict):
        raw = payload.get("seasons")
    if isinstance(raw, dict):
        raw = raw.get("seasons")
    for s in raw or []:
        se = _int(s.get("se")) or 1
        nums = s.get("episodeNumbers")
        if isinstance(nums, list):
            eps = [int(n) for n in nums if _int(n) is not None]
        else:
            eps = list(range(1, (_int(s.get("maxEp")) or 0) + 1))
        seasons.append({"number": se, "episodes": eps})

    dubs = []
    for d in subject.get("dubs") or []:
        did = _s(d.get("subjectId", d.get("id"))) or ""
        lang = d.get("lanName") or d.get("language") or d.get("lang") or "Unknown"
        dubs.append({"id": did, "language": lang, "label": d.get("title") or d.get("name") or lang})

    return {
        "id": sid,
        "title": subject.get("title") or "Unknown",
        "type": "series" if stype == 2 else "movie",
        "year": year_of(str(subject.get("releaseDate") or subject.get("year") or "")),
        "description": subject.get("description") or subject.get("intro"),
        "tagline": subject.get("tagline"),
        "rating": subject.get("imdbRatingValue") or subject.get("rating"),
        "director": subject.get("director"),
        "stars": subject.get("stars"),
        "poster": poster,
        "duration": dur,
        "genres": genres,
        "seasons": seasons,
        "dubs": dubs,
    }


def is_notice_url(url: str) -> bool:
    low = (url or "").lower()
    return (
        "1c7de0bd3393702d9191801f15f88f8d" in low
        or "9a0461bc39da389663bf3dbb17091d3f" in low
        or "b164fbfb4347792950bdfbfb563d39d9" in low
        or "/notice.mp4" in low
        or ("macdn.aoneroom.com" in low and "/other/" in low)
    )


def _to_mpd(url):
    base = url.rstrip("*").rstrip("/")
    if base.startswith(("http://", "https://")) and "/dash/" in base:
        return base + "/index.mpd"
    return None


def _b64(s):
    s += "=" * (-len(s) % 4)
    try:
        return base64.b64decode(s)
    except Exception:
        return None


def dash_from_cookie(sign_cookie: str):
    for part in sign_cookie.split(";"):
        part = part.strip()
        i = part.find("urlprefix=")
        if i >= 0:
            tok = part[i + len("urlprefix="):].split(":")[0].strip()
            raw = _b64(tok.replace("-", "+").replace("_", "/"))
            if raw:
                try:
                    mpd = _to_mpd(raw.decode())
                except UnicodeDecodeError:
                    mpd = None
                if mpd:
                    return mpd
        if part.startswith("CloudFront-Policy="):
            pol = part[len("CloudFront-Policy="):].strip()
            raw = _b64(pol.replace("-", "+").replace("_", "=").replace("~", "/"))
            if not raw:
                continue
            try:
                res = json.loads(raw)["Statement"][0]["Resource"]
            except Exception:
                continue
            mpd = _to_mpd(res)
            if mpd:
                return mpd
    return None


def clean_title(raw: str) -> str:
    t = (raw or "").strip()
    while t.startswith("["):
        close = t.find("]")
        rest = t[close + 1:].strip() if close >= 0 else ""
        if close < 0 or not rest:
            break
        t = rest
    pos = t.find("[")
    if pos > 0:
        t = t[:pos].strip()
    return t.strip("-:_. ") or (raw or "").strip()


def play_info_to_releases(payload, season, episode, ua):
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    title = clean_title(data.get("title") or "MovieBox Stream")
    out = []
    for st in data.get("streams") or []:
        stream_id = _s(st.get("id"))
        fmt = st.get("format") or "MP4"
        codec = st.get("codecName") or st.get("codec")
        size = _int(st.get("size"))
        res_str = st.get("resolutions") or data.get("displayResolutions") or "1080,720,480"
        cookie = st.get("signCookie") or ""
        url = st.get("url") or ""
        playable = dash_from_cookie(cookie)
        if not playable and not is_notice_url(url) and url.startswith("http"):
            playable = url
        if not playable:
            continue
        headers = {"Referer": STREAM_REFERER, "User-Agent": ua}
        if cookie:
            headers["Cookie"] = "; ".join(p.strip() for p in cookie.rstrip(";").split(";") if p.strip())
        resolutions = sorted({int(x) for x in str(res_str).split(",") if x.strip().isdigit()}, reverse=True) or [1080]
        top = resolutions[0]
        disp = codec or fmt
        for res in resolutions:
            scaled = size
            if size and res < top:
                scaled = int(size * min(1.0, max(0.15, (res / top) ** 1.6)))
            out.append(
                {
                    "filename": f"{title} S{season:02d}E{episode:02d} {res}p {disp}"
                    if season > 0 and episode > 0
                    else f"{title} {res}p {disp}",
                    "quality": f"{res}p",
                    "res": res,
                    "codec": codec,
                    "size": scaled,
                    "season": season or None,
                    "episode": episode or None,
                    "resource_id": stream_id,
                    "url": playable,
                    "headers": headers,
                    "multi": True,  # one manifest holds all resolutions (adaptive)
                    "label": f"{res}p {disp}",
                }
            )
    return out


def resource_item_to_release(item, ua):
    res = item.get("resolution")
    res_i = _int(res)
    link = item.get("resourceLink") or item.get("url") or ""
    link = "" if (not link or is_notice_url(link)) else link
    return {
        "filename": item.get("fileName") or item.get("title") or "Unknown Release",
        "quality": f"{res_i}p" if res_i else (str(res) if res else None),
        "res": res_i or 0,
        "codec": item.get("codecName") or item.get("codec"),
        "size": _int(item.get("size")),
        "season": _int(item.get("se")),
        "episode": _int(item.get("ep")),
        "resource_id": _s(item.get("resourceId", item.get("id"))),
        "url": link,
        "headers": {"User-Agent": ua},
        "multi": False,
        "label": f"{res_i}p {item.get('codecName') or ''}".strip() if res_i else (item.get("fileName") or "Direct"),
        "_origin": item.get("resourceLink") or item.get("url"),
    }


def get_releases(client: MovieBoxClient, subject_id, season=0, episode=0):
    """Return playable releases sorted best-first. Each has url + headers."""
    releases, seen = [], set()
    # the two upstream lookups are independent: run them side by side
    f_info = POOL.submit(client.play_info, subject_id, season, episode)
    f_res = POOL.submit(client.episode_resources, subject_id, season, episode)
    try:
        for r in play_info_to_releases(f_info.result(), season, episode, client.ua):
            releases.append(r)
            seen.add(r["url"].split("?")[0])
    except ApiError:
        pass

    items = []
    try:
        payload = f_res.result()
        items = (payload.get("list") or []) if isinstance(payload, dict) else []
    except ApiError:
        if not releases:
            try:
                items = client.resource_page(subject_id, season, episode, max(1, (episode - 1) // 20 + 1))
            except ApiError:
                items = []

    for item in items:
        rel = resource_item_to_release(item, client.ua)
        if (season or episode) and (rel["season"] != season or rel["episode"] != episode):
            continue
        if not rel["url"]:
            origin = item.get("resourceLink") or item.get("url")
            if not (rel["resource_id"] and origin and is_notice_url(origin)):
                continue
            try:
                pos = client.resource_position(subject_id, rel["resource_id"], origin)
            except ApiError:
                continue
            pdata = pos.get("data", pos) if isinstance(pos, dict) else {}
            for cand_item in pdata.get("list") or []:
                cand = resource_item_to_release(cand_item, client.ua)
                if (
                    cand["resource_id"] == rel["resource_id"]
                    and cand["season"] == rel["season"]
                    and cand["episode"] == rel["episode"]
                    and cand["url"].startswith(("http://", "https://"))
                ):
                    rel["url"] = cand["url"]
                    rel["headers"] = {"User-Agent": client.ua}
                    if cand["size"]:
                        rel["size"] = cand["size"]
                    break
            if not rel["url"]:
                continue
        base = rel["url"].split("?")[0]
        if base in seen:
            continue
        seen.add(base)
        releases.append(rel)

    if not releases:
        # play-info/resource only return a placeholder clip: fall back to the detector links on the subject details
        releases = detector_releases(client, subject_id, season, episode)

    releases.sort(key=lambda r: (r["res"], r["size"] or 0), reverse=True)
    return releases


def detector_releases(client, subject_id, season=0, episode=0):
    try:
        d = client.details(subject_id)
    except ApiError:
        return []
    if not isinstance(d, dict):
        return []
    inner = d.get("subject") if isinstance(d.get("subject"), dict) else {}
    dets = d.get("resourceDetectors") or inner.get("resourceDetectors") or []
    out, seen = [], set()
    for det in dets if isinstance(dets, list) else []:
        if not isinstance(det, dict):
            continue
        for y in det.get("resolutionList") or []:
            if not isinstance(y, dict):
                continue
            url = y.get("resourceLink") or y.get("url") or y.get("link") or ""
            if not url.startswith(("http://", "https://")) or is_notice_url(url):
                continue
            se, ep = _int(y.get("se", det.get("se"))), _int(y.get("ep", det.get("ep")))
            if (season or episode) and (se or ep) and (se != season or ep != episode):
                continue
            base = url.split("?")[0]
            if base in seen:
                continue
            seen.add(base)
            res = _int(y.get("resolution", y.get("res")))
            codec = y.get("codecName") or y.get("codec")
            title = clean_title(det.get("title") or inner.get("title") or "MovieBox")
            out.append({
                "filename": f"{title} {res}p {codec or ''}".strip(),
                "quality": f"{res}p" if res else None,
                "res": res or 0,
                "codec": codec,
                "size": _int(y.get("size")),
                "season": season or None,
                "episode": episode or None,
                "resource_id": _s(y.get("resourceId", y.get("id"))),
                "url": url,
                "headers": {"User-Agent": client.ua},  # no Referer: the CDN answers 429 to it
                "multi": False,
                "label": f"{res}p {codec or ''}".strip() if res else "Direct",
            })
    return out


# ---------------------------------------------------------------- subtitles
def captions_to_options(payload):
    caps = None
    if isinstance(payload, dict):
        caps = payload.get("extCaptions") or (payload.get("data") or {}).get("extCaptions")
    seen, out = set(), []
    for cap in caps or []:
        url = cap.get("url")
        if not url or "aa348f2541d13ffe" in url:
            continue
        size = _int(cap.get("size")) or 0
        if 0 < size <= 50:
            continue
        name = (cap.get("lanName") or "").strip() or cap.get("lan") or "Unknown"
        if name.lower() == "in" and size <= 100:
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"name": name, "url": url})
    return out


LANG_ALIASES = {
    "العربية": "Arabic", "ara": "Arabic", "ar": "Arabic",
    "اردو": "Urdu", "ur": "Urdu", "urd": "Urdu",
    "বাংলা": "Bengali", "ben": "Bengali", "bn": "Bengali",
    "हिन्दी": "Hindi", "हिंदी": "Hindi", "hin": "Hindi", "hi": "Hindi",
    "filipino": "Filipino", "tagalog": "Filipino", "fil": "Filipino", "tl": "Filipino",
    "ind": "Indonesian", "id": "Indonesian", "in_id": "Indonesian",
    "eng": "English", "en": "English",
    "español": "Spanish", "spa": "Spanish", "es": "Spanish",
    "français": "French", "fra": "French", "fre": "French", "fr": "French",
    "deutsch": "German", "deu": "German", "ger": "German", "de": "German",
    "italiano": "Italian", "ita": "Italian", "it": "Italian",
    "português": "Portuguese", "por": "Portuguese", "pt": "Portuguese",
    "русский": "Russian", "rus": "Russian", "ru": "Russian",
    "türkçe": "Turkish", "tur": "Turkish", "tr": "Turkish",
    "tiếng việt": "Vietnamese", "vie": "Vietnamese", "vi": "Vietnamese",
    "中文": "Chinese", "zho": "Chinese", "chi": "Chinese", "zh": "Chinese",
    "日本語": "Japanese", "jpn": "Japanese", "ja": "Japanese",
    "한국어": "Korean", "kor": "Korean", "ko": "Korean",
    "ไทย": "Thai", "tha": "Thai", "th": "Thai",
}


def lang_label(name: str) -> str:
    cleaned = re.sub("[ً-ٰٟۖ-ۭ]", "", name or "").strip()
    return LANG_ALIASES.get(cleaned.lower(), LANG_ALIASES.get(cleaned, cleaned))


def get_captions(client: MovieBoxClient, subject_id, resource_id, sibling_ids=(), season=0, episode=0):
    all_caps, seen = [], set()

    def add(opts):
        for o in opts:
            if o["url"] not in seen:
                seen.add(o["url"])
                all_caps.append(o)

    def primary():
        if subject_id and resource_id:
            return captions_to_options(client.ext_captions(subject_id, resource_id))
        return []

    def sibling(sib):
        page = (episode - 1) // 20 + 1 if episode > 0 else 1
        items = client.resource_page(sib, season, episode, page)
        match = None
        for it in items:
            if (season == 0 and episode == 0) or (_int(it.get("se")) == season and _int(it.get("ep")) == episode):
                match = it
                break
        rid = _s(match.get("resourceId", match.get("id"))) if match else None
        return captions_to_options(client.ext_captions(sib, rid)) if rid else []

    futures = [POOL.submit(primary)] + [
        POOL.submit(sibling, s) for s in [s for s in sibling_ids if s and s != subject_id][:3]
    ]
    for f in futures:           # primary first, then siblings, same order as before
        try:
            add(f.result(timeout=20))
        except Exception:
            continue

    dedup, langs = [], set()
    for c in all_caps:
        label = lang_label(c["name"])
        if label not in langs:
            langs.add(label)
            dedup.append({"name": label, "url": c["url"]})
    dedup.sort(key=lambda c: (c["name"].lower() != "english", c["name"]))
    return dedup
