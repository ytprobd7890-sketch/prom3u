#!/usr/bin/env python3
"""
TataTV IPTV Playlist Generator (fixed)
Changes:
  - Proxy no longer hardcoded; provide via env SOCKS_PROXY / config.json
  - Connection chain: try DIRECT first; if 403/Cloudflare, try proxy; retry
  - Fixes host='none' bug when portal detection fails
  - Removes dead EPG URLs (empty by default; supply your own)
  - Faster timeouts + better error messages
  - Multiple proxies supported (failover list)
"""
import hashlib, json, os, re, sys, time, gzip, random, urllib.parse, xml.etree.ElementTree as traceback_ET
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
OUT_DIR = ROOT / "playlists"
M3U_DIR = ROOT / "m3u"
OUT_DIR.mkdir(exist_ok=True); M3U_DIR.mkdir(exist_ok=True)

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "12"))
TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "12"))
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
# If set, each channel URL becomes:  {PLAYLIST_BASE_URL}/w/<portal>/<ch_id>
# and a companion proxy.py (Flask app) redirects to a fresh token-URL every hit.
# This is REQUIRED because stalker create_link tokens expire in ~15-20 minutes.
# Leave empty to bake direct (expiring) URLs into the m3u8 (for local use).
PLAYLIST_BASE_URL = os.environ.get("PLAYLIST_BASE_URL", "").rstrip("/")
# -------- GENRE FILTERS --------
# ORDER OF PRECEDENCE:
#   1. allowed_genres (config.json) or GENRE_ALLOW (env, comma-separated) — whitelist.
#      If non-empty, ONLY genres whose title contains ANY of these keywords (case-insensitive)
#      are processed. Others are skipped entirely (huge time/proxy savings).
#   2. If no allowlist, genre BLOCKLIST applies: genres whose title contains any of the
#      GENRE_BLOCK keywords are dropped. Set GENRE_FILTER=0 to disable blocklist.
GENRE_FILTER = os.environ.get("GENRE_FILTER", "1") == "1"
GENRE_BLOCK_STR = os.environ.get(
    "GENRE_BLOCK",
    "NEWS,TAMIL,TELUGU,MALAYALAM,KANNADA,MARATHI,GUJARATI,PUNJABI,URDU,FRENCH,ARABIC,FILIPINO,CARIBBEAN"
).upper()
_GENRE_BLOCK_KW = [k.strip() for k in GENRE_BLOCK_STR.split(",") if k.strip()] if GENRE_FILTER else []
GENRE_ALLOW_STR = os.environ.get("GENRE_ALLOW", "").upper()
_GENRE_ALLOW_ENV = [k.strip() for k in GENRE_ALLOW_STR.split(",") if k.strip()]

def _genre_blocked(title: str) -> bool:
    t = title.upper()
    return any(kw in t for kw in _GENRE_BLOCK_KW)

def _genre_allowed(title: str, allowlist) -> bool:
    """allowlist: list of uppercase keywords from config+env. Empty list = no whitelist."""
    if not allowlist:
        return not _genre_blocked(title)
    t = title.upper()
    return any(kw in t for kw in allowlist)

MAG_UA = ("Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
          "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3")
LOGO_FALLBACK = ""  # no remote logo; rely on EPG

# Hardcoded defaults so env vars are OPTIONAL
_DEFAULT_PORTAL_URL = "http://tatatv.cc/stalker_portal/c/"
_DEFAULT_MAC_ADDR   = "00:1A:79:00:8C:32"

# Fallback proxy list (if env SOCKS_PROXY/HTTP_PROXY not set, try these; remove any dead ones)
_FALLBACK_PROXIES = [
    # "socks5h://test:test@203.96.226.98:1088",  # known flaky from GitHub Actions IPs
]

def _env_portal_url():
    v = (os.environ.get("PORTAL_URL") or "").strip()
    return v or _DEFAULT_PORTAL_URL

def _env_mac_addr():
    v = (os.environ.get("MAC_ADDR") or "").strip()
    return v or _DEFAULT_MAC_ADDR

def _env_proxies():
    plist = []
    for k in ("SOCKS_PROXY", "SOCKS5_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        v = (os.environ.get(k) or "").strip()
        if v and v not in plist: plist.append(v)
    # also accept PROXIES as comma-separated list
    multi = (os.environ.get("PROXIES") or "").strip()
    if multi:
        for v in [p.strip() for p in multi.split(",") if p.strip()]:
            if v not in plist: plist.append(v)
    if not plist: plist = [p for p in _FALLBACK_PROXIES if p]
    return plist

def _merged_proxies(portal_proxies):
    """Merge portal's own proxy list with env proxies (env first). Returned list has no duplicates."""
    out = []
    for p in list(_env_proxies()) + list(portal_proxies or []):
        if isinstance(p, str): p = p.strip()
        if p and p not in out: out.append(p)
    return out

DEFAULT_CONFIG = {
    # allowed_genres: whitelist (applies to all portals if per-portal list missing).
    # Empty list / missing = fall back to GENRE_BLOCK blocklist.
    "allowed_genres": [],
    "portals": [
        {
            "name": "tatatv",
            "url": _env_portal_url(),
            "mac": _env_mac_addr(),
            "proxies": [],
            "epg_id_prefix": "",
        },
        {
            "name": "jiotv",
            "url": os.environ.get("JIOTV_URL",  "http://jiotv.be/stalker_portal/c"),
            "mac": os.environ.get("JIOTV_MAC",  "00:1A:79:E6:6E:90"),
            "proxies": [],
            "epg_id_prefix": "",
        },
    ],
    # 🔥 Working EPG sources (2026-07-26 tested). Merged in order: first hit wins id,
    #    later sources fill in missing logos / extra display-name aliases.
    "epg_sources": [
        "https://www.open-epg.com/files/india.xml.gz",         # 1184 IN ch, 100% logos
        "https://avkb.short.gy/epg.xml.gz",                    # 1514 ch, ~99.7% logos
        "https://epgshare01.online/epgshare01/epg_ripper_IN1.xml.gz",  # 1166 IN ch, multi logo
    ],
    "aliases": {
        # Extra name → {id, logo} mappings to help fuzzy matching
        "STAR JALSHA MOVIES HD": {"id": "StarJalshaMovies.in", "logo": "https://images.open-epg.com/x1882.png"},
        "STAR JALSHA MOVIES":    {"id": "StarJalshaMovies.in", "logo": "https://images.open-epg.com/x1882.png"},
        "ANANDA TV BANGLA HD":   {"id": "AnandaTV.in",         "logo": ""},
        "ANANDA BANGLA":         {"id": "AnandaTV.in",         "logo": ""},
        "COLORS BANGLA CINEMA":  {"id": "ColorsBanglaCinema.in","logo": ""},
        "ZEE BANGLA":            {"id": "ZeeBangla.in",        "logo": "https://images.open-epg.com/30078.png"},
        "STAR BHARAT HD":        {"id": "StarBharat.in",       "logo": "https://images.open-epg.com/x743.png"},
        "&TV HD":                {"id": "AndTV.in",            "logo": "https://images.open-epg.com/x1758.png"},
        "&TV":                   {"id": "AndTV.in",            "logo": "https://images.open-epg.com/x1758.png"},
        "SONY ENTERTAINMENT TELEVISION HD": {"id": "SET.in",    "logo": "https://images.open-epg.com/x1152.png"},
        "SONY TV HD":            {"id": "SET.in",              "logo": "https://images.open-epg.com/x1152.png"},
        "STAR SPORTS FIRST HD":  {"id": "StarSportsFirst.in",  "logo": ""},
        "SPORTS 18 HINDI":       {"id": "Sports18Khel.in",     "logo": ""},
        "BANGLA TOP 10":         {"id": "BanglaTop10.in",      "logo": ""},
    },
    "favorites": {
        "Bangla_Trending": ["ZEE BANGLA 4K", "STAR JALSHA HD", "COLORS BANGLA HD", "AAKAASH AATH", "SONY AATH"],
        "Sports": ["STAR SPORTS 1 HD", "STAR SPORTS SELECT 1", "SONY SPORTS TEN 1", "SPORTS - CRICKET"],
    },
}

FOLDER_MAP = [
    ("BANGLA","01_Bangla"),("HINDI","02_Hindi"),("URDU","03_Urdu"),("PUNJABI","04_Punjabi"),
    ("TAMIL","05_Tamil"),("TELUGU","06_Telugu"),("MARATHI","07_Marathi"),("GUJARATI","08_Gujarati"),
    ("MALAYALAM","09_Malayalam"),("KANNADA","10_Kannada"),("SRI LANKA","11_Sri_Lanka"),
    ("ENGLISH","12_English"),("KIDS","13_Kids"),("ANIMATION","13_Kids"),
    ("SPORTS","14_Sports"),("CRICKET","14_Sports"),("FIFA","14_Sports"),("UFC","14_Sports"),
    ("NBA","14_Sports"),("NHL","14_Sports"),("MLB","14_Sports"),("NFL","14_Sports"),
    ("EPL","14_Sports"),("MLS","14_Sports"),("FORMULA 1","14_Sports"),("F1","14_Sports"),
    ("TENNIS","14_Sports"),("GOLF","14_Sports"),("SOCCER","14_Sports"),("RACING","14_Sports"),
    ("PPV","15_PPV"),("4K","16_4K"),("ADULT 18+","17_Adult"),
    ("RELIGIOUS","18_Religious"),("NEWS","19_News"),("MUSIC","20_Music"),("MOVIE","21_Movies"),
]


def classify_folder(title: str) -> str:
    up = title.upper().strip()
    if up in ("ALL", "*"): return "00_All"
    # Bengali keyword mapping (portal uses "BENGALI" or "BANGLA")
    if ("BANGLA" in up) or ("BENGALI" in up):
        return "01_Bangla"
    for kw, f in FOLDER_MAP:
        if kw in up: return f
    return "22_Others"


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60] or "channel"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in DEFAULT_CONFIG.items(): cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            print(f"[!] config.json parse failed ({e}) — defaults used")
    return DEFAULT_CONFIG


# ---------- EPG ----------
class EPGIndex:
    def __init__(self, sources):
        self.ids: dict = {}
        self.by_name: dict = {}
        ok = 0
        total_loaded = 0
        sess = requests.Session()
        ua = {"User-Agent": "Mozilla/5.0"}
        for src in sources or []:
            before = len(self.ids)
            try:
                r = sess.get(src, timeout=60, allow_redirects=True, headers=ua)
                r.raise_for_status()
                data = r.content
                if src.endswith(".gz") or (len(data) > 2 and data[:2] == b"\x1f\x8b"):
                    data = gzip.GzipFile(fileobj=BytesIO(data)).read()
                root = ET.fromstring(data)
                added = 0; merged = 0; logo_filled = 0
                for ch in root.findall("channel"):
                    cid = ch.get("id", "").strip()
                    logos = [e.get("src","") for e in ch.findall("icon") if e.get("src")]
                    logo = logos[0] if logos else ""
                    names = [(e.text or "").strip() for e in ch.findall("display-name") if e.text]
                    if not cid: continue
                    if cid in self.ids:
                        # merge: add missing name aliases, fill logo if missing
                        merged += 1
                        entry = self.ids[cid]
                        for dn in names:
                            if dn and dn not in entry["names"]:
                                entry["names"].append(dn)
                                self._add_name(dn, cid)
                        if (not entry.get("logo")) and logo:
                            entry["logo"] = logo
                            logo_filled += 1
                        continue
                    self.ids[cid] = {"names": list(names), "logo": logo}
                    added += 1
                    for dn in names:
                        self._add_name(dn, cid)
                ok += 1
                total_loaded = len(self.ids)
                print(f"[epg] ✔ {src} (+{added} new, {merged} merged, {logo_filled} logo-filled → {total_loaded} total)")
            except Exception as e:
                print(f"[epg] ✖ {src}: {e}")
        print(f"[epg] {len(self.ids)} unique channels indexed ({ok}/{len(sources or [])} sources ok)")

    def _add_name(self, dn, cid):
        # Store multiple normalized key variants for higher match rate
        variants = set()
        raw = dn.upper()
        variants.add(raw.strip())
        # remove common noise words
        for stop in [r'\bHD\b', r'\bFHD\b', r'\bUHD\b', r'\b4K\b', r'\bTV\b', r'\bLIVE\b',
                     r'\bCHANNEL\b', r'\bINDIA\b', r'\bIN\b']:
            raw = re.sub(stop, '', raw)
        raw = re.sub(r'[^A-Z0-9&+]+', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
        variants.add(raw)
        # ampersand variants: & <-> AND
        variants.add(raw.replace('&',' AND ').replace('  ',' ').strip())
        variants.add(raw.replace(' AND ',' & ').replace('  ',' ').strip())
        # compact no-space
        variants.add(re.sub(r'\s+','', raw))
        variants.add(re.sub(r'\s+','', raw.replace('&','AND')))
        for k in variants:
            if k and k not in self.by_name:
                self.by_name[k] = cid

    def match(self, name, aliases=None):
        """Return (tvg_id, logo). aliases=dict from config['aliases'].
        Logo priority: alias logo > EPG-matched logo > ''."""
        if aliases and name in aliases:
            a = aliases[name]
            return a.get("id",""), a.get("logo","")
        raw = name.upper()
        # try every normalization variant
        candidates = [raw.strip()]
        for stop in [r'\bHD\b', r'\bFHD\b', r'\bUHD\b', r'\b4K\b', r'\bTV\b', r'\bLIVE\b',
                     r'\bCHANNEL\b', r'\bINDIA\b', r'\bIN\b']:
            raw = re.sub(stop, '', raw)
        raw = re.sub(r'[^A-Z0-9&+]+', ' ', raw)
        raw = re.sub(r'\s+', ' ', raw).strip()
        candidates += [raw,
                       raw.replace('&',' AND ').replace('  ',' ').strip(),
                       raw.replace(' AND ',' & ').replace('  ',' ').strip(),
                       re.sub(r'\s+','', raw),
                       re.sub(r'\s+','', raw.replace('&','AND'))]
        for k in candidates:
            if k and k in self.by_name:
                cid = self.by_name[k]
                return cid, self.ids[cid].get("logo","")
        # fuzzy (costly, do last with high cutoff)
        import difflib
        keys = list(self.by_name.keys())
        for k in candidates:
            if len(k) < 4: continue
            best = difflib.get_close_matches(k, keys, n=1, cutoff=0.88)
            if best:
                cid = self.by_name[best[0]]
                return cid, self.ids[cid].get("logo","")
        # no EPG match -> use slug (no epg-prefix needed for open-epg which uses readable ids)
        return slugify(name), ""


# ---------- Stalker client ----------
class StalkerClient:
    def __init__(self, name, portal_url, mac, proxies=None, epg_prefix=""):
        self.name = name
        self.mac = mac
        self.epg_prefix = epg_prefix
        self.proxy_list = []
        if proxies:
            for p in (proxies if isinstance(proxies, list) else [proxies]):
                p = p.strip() if isinstance(p, str) else p
                if p: self.proxy_list.append(p)
        # include None as "direct" attempt
        self.attempts = [None] + self.proxy_list

        p = urlparse(portal_url)
        if not p.scheme or not p.hostname:
            raise ValueError(f"Invalid portal URL: {portal_url}")
        self.proto = p.scheme
        self.host = p.hostname
        self.port = p.port or (443 if self.proto == "https" else 80)
        parsed_path = p.path or "/"
        # normalize: ensure trailing slash
        if not parsed_path.endswith("/"): parsed_path += "/"
        base_host = f"{self.proto}://{self.host}:{self.port}"

        self.endpoint = None
        self.portal_version = "5.6.10"

        # detect endpoint trying each proxy; paths relative to HOST root
        last_err = None
        vjs_paths = [
            # (endpoint_path_from_root, version_js_url_from_root)
            ("stalker_portal/server/load.php", "/stalker_portal/c/version.js"),
            ("server/load.php",                "/c/version.js"),            # portal already at /stalker_portal/
            ("portal.php",                     "/c/version.js"),
        ]
        for prox in self.attempts:
            s = self._newsess(prox)
            for ep, vjs in vjs_paths:
                try:
                    r = s.get(base_host + vjs, timeout=10)
                    r.raise_for_status()
                    m = re.search(r"var ver = ['\\\"]([^'\\\"]+)['\\\"];", r.text)
                    if m or "version" in r.text.lower():
                        self.endpoint = ep
                        if m: self.portal_version = m.group(1)
                        self.active_proxy = prox
                        break
                except Exception as e:
                    last_err = e
            if self.endpoint: break
        if not self.endpoint:
            print(f"[{self.name}] ! portal detection failed; defaulting to stalker_portal/server/load.php")
            self.endpoint = "stalker_portal/server/load.php"
            self.active_proxy = None

        # Compute base_url: the prefix to prepend to endpoint so the full URL is
        #   scheme://host:port/<right base>/endpoint
        # The endpoint already contains any necessary stalker_portal/ prefix when detected
        # against the host root. So base_url must be the HOST ROOT only (not duplicating
        # stalker_portal/). However the user-supplied parsed_path may include a subfolder
        # (e.g. /stalker_portal/c/), in which case endpoint should already contain the
        # matching prefix. To prevent double-pathing: if endpoint starts with the directory
        # of parsed_path, strip that directory from base_url.
        self.base_url = base_host + "/"
        # if user supplied a sub-path that endpoint already accounts for, keep base at root
        # (endpoint path is authoritative from detection)
        if parsed_path not in ("/", ""):
            # sub = the directory before "c/" e.g. /stalker_portal/
            sub = parsed_path.rstrip("/")
            if sub.endswith("/c"): sub = sub[:-2]
            if sub.endswith("/stalker_portal"):
                # endpoint already starts with stalker_portal/ — don't repeat
                pass
            elif sub and sub != "/":
                if not self.endpoint.startswith(sub.lstrip("/")):
                    self.base_url = base_host + sub.rstrip("/") + "/"
        if self.host in (None, "", "none"):
            raise RuntimeError(f"Hostname detection failed for {portal_url}")

        self.sn = hashlib.md5(mac.encode()).hexdigest().upper()[:13]
        self.did  = hashlib.sha256(self.sn.encode()).hexdigest().upper()
        self.did2 = hashlib.sha256(mac.encode()).hexdigest().upper()
        self.hv2  = hashlib.sha1(mac.encode()).hexdigest()
        self.cookies = {"adid": self.hv2, "debug": "1", "device_id2": self.did2,
                        "device_id": self.did, "hw_version": "1.7-BD-00", "mac": mac,
                        "sn": self.sn, "stb_lang": "en", "timezone": "Asia/Dhaka"}
        self.base_headers = {"User-Agent": MAG_UA, "Accept-Encoding": "identity",
                             "Accept": "*/*", "Connection": "keep-alive"}
        self._handshake()

    def _newsess(self, proxy=None):
        s = requests.Session()
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
            s.trust_env = False
        adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50,
            max_retries=Retry(total=2, backoff_factor=0.3,
                status_forcelist=[429,500,502,503,504], allowed_methods=["GET"]))
        s.mount("http://", adapter); s.mount("https://", adapter)
        return s

    def _do(self, url, tries=3):
        last = None
        s = self.session
        s.headers.update(self.auth_headers); s.cookies.update(self.auth_cookies)
        # Inject Bearer token for authenticated calls
        if getattr(self, "token", None):
            s.headers["Authorization"] = f"Bearer {self.token}"
        for i in range(tries):
            try:
                r = s.get(url, timeout=TIMEOUT)
                if r.status_code == 200:
                    try: return r.json()
                    except Exception: return r
                if r.status_code in (401, 403):
                    # token may have expired; re-handshake once and retry
                    if i == 0:
                        try: self._handshake()
                        except Exception: pass
                        s = self.session
                        s.headers.update(self.auth_headers); s.cookies.update(self.auth_cookies)
                        continue
                    return r  # fall through
            except Exception as e:
                last = e; time.sleep(0.3*(i+1))
        raise ConnectionError(f"All connection attempts failed for {url}: {last}")

    def _get(self, url, tries=3):
        # Preferred path: use the already-authenticated session
        try:
            return self._do(url, tries=tries)
        except Exception as first_err:
            # Failover: retry across all proxies (re-auth per proxy path)
            last = first_err
            for prox in self.attempts:
                try:
                    s = self._newsess(prox)
                    r = s.get(url, timeout=TIMEOUT)
                    if r.status_code == 200:
                        try: return r.json()
                        except Exception: return r
                except Exception as e:
                    last = e; time.sleep(0.3)
            raise ConnectionError(f"All connection attempts failed for {url}: {last}")

    def _handshake(self):
        self.token = self.tr = None
        url = f"{self.base_url}{self.endpoint}?action=handshake&type=stb&JsHttpRequest=1-xml"
        for prox in self.attempts:
            s = self._newsess(prox)
            try:
                r = s.get(url, cookies=self.cookies, headers=self.base_headers, timeout=TIMEOUT)
                r.raise_for_status()
                js = r.json().get("js", {})
                if not js.get("token"): continue
                self.token = js["token"]; self.tr = js.get("random")
                self.active_proxy = prox
                # second-step
                if self.tr:
                    sig = hashlib.sha256(self.tr.encode()).hexdigest().upper()
                    metrics = {"mac": self.mac, "sn": self.sn, "type": "STB",
                               "model": "MAG250", "uid": self.did, "random": self.tr}
                    enc = urllib.parse.quote(json.dumps(metrics))
                    s.headers.update({**self.base_headers,
                                      "Authorization": f"Bearer {self.token}",
                                      "X-Random": str(self.tr)})
                    s.cookies.update(self.cookies)
                    purl = (f"{self.base_url}{self.endpoint}?type=stb&action=get_profile&hd=1"
                            f"&ver=ImageDescription: 0.2.18-r23-250; ImageDate: Wed Aug 29 10:49:53 EEST 2018; "
                            f"PORTAL version: {self.portal_version}; API Version: JS API version: 343; "
                            f"STB API version: 146; Player Engine version: 0x58c&num_banks=2&sn={self.sn}"
                            f"&stb_type=MAG250&client_type=STB&image_version=218&video_out=hdmi"
                            f"&device_id={self.did2}&device_id2={self.did2}&sig={sig}"
                            f"&auth_second_step=1&hw_version=1.7-BD-00&not_valid_token=0"
                            f"&metrics={enc}&hw_version_2={self.hv2}"
                            f"&timestamp={round(time.time())}&api_sig=262&prehash=0")
                    s.get(purl, timeout=TIMEOUT)
                self.session = s
                self.auth_cookies = dict(s.cookies); self.auth_cookies.update(self.cookies)
                self.auth_cookies["token"] = self.token
                self.auth_headers = dict(s.headers)
                print(f"[{self.name}] ✅ auth via {prox or 'DIRECT'} endpoint={self.endpoint} v={self.portal_version}")
                return
            except Exception as e:
                print(f"[{self.name}] auth attempt proxy={prox} fail: {e.__class__.__name__}")
                continue
        raise RuntimeError(f"[{self.name}] ❌ auth failed on all proxies/direct. Check proxy/MAC/URL.")

    def genres(self):
        j = self._get(f"{self.base_url}{self.endpoint}?type=itv&action=get_genres&JsHttpRequest=1-xml")
        data = j.get("js", []) if isinstance(j, dict) else []
        out = [{"id":str(g["id"]),"title":(g.get("title") or "").strip()} for g in data if isinstance(g, dict)]
        out.sort(key=lambda x:(x["id"]!="*", x["title"]))
        return out

    def _page(self, gid, p):
        u = (f"{self.base_url}{self.endpoint}?type=itv&action=get_ordered_list&genre={urllib.parse.quote(str(gid))}"
             f"&p={p}&JsHttpRequest=1-xml")
        j = self._get(u)
        return j.get("js",{}) if isinstance(j,dict) else {}

    def channels(self, gid, progress=None):
        js0 = self._page(gid, 0)
        total = int(js0.get("total_items",0))
        d0 = js0.get("data",[]) or []
        ipp = int(js0.get("max_page_items") or 0) or max(len(d0),1)
        pages = (total+ipp-1)//ipp if total else 1
        allc = list(d0)
        self._prime_cmd_cache(d0)
        if callable(progress): progress(1,pages)
        for p in range(1, pages):
            try:
                data = self._page(gid,p).get("data",[]) or []
                self._prime_cmd_cache(data)
                for ch in data: allc.append(ch)
            except Exception as e:
                print(f"[{self.name}] page {p} of genre {gid} failed: {e}")
            if callable(progress): progress(p+1,pages)
        uniq={}
        for ch in allc:
            cid = str(ch.get("id") or "")
            if cid and cid not in uniq: uniq[cid]=ch
        return sorted(uniq.values(), key=lambda x:x.get("name","").lower())

    def _prime_cmd_cache(self, data):
        if not hasattr(self, "_cmd_cache"): self._cmd_cache = {}
        for ch in data or []:
            cid = str(ch.get("id") or "")
            cmds = ch.get("cmds") or []
            if cid and cmds and cmds[0].get("id"):
                self._cmd_cache[cid] = str(cmds[0]["id"])

    def _resolve_cmd_id(self, ch_id):
        """Map ch.id -> cmds[0].id. create_link on ministra often needs the STREAM cmd id
        (cmds[0].id), not the channel id. Cache indefinitely; lazy-load by scanning genres."""
        if not hasattr(self, "_cmd_cache"): self._cmd_cache = {}
        cid = str(ch_id)
        if cid in self._cmd_cache: return self._cmd_cache[cid]
        # Lazy scan all genres once to build ch.id -> cmd.id map
        try:
            for g in self.genres():
                if g["id"] == "*": continue
                try:
                    js0 = self._page(g["id"], 0)
                    total = int(js0.get("total_items",0))
                    ipp = int(js0.get("max_page_items") or 0) or max(len(js0.get("data",[]) or []),1)
                    pages = (total+ipp-1)//ipp if total else 1
                    self._prime_cmd_cache(js0.get("data",[]))
                    for p in range(1, pages):
                        data = self._page(g["id"],p).get("data",[]) or []
                        self._prime_cmd_cache(data)
                except Exception:
                    pass
                if cid in self._cmd_cache: break
        except Exception as e:
            print(f"[{self.name}] cmd-id lookup scan issue: {e}")
        return self._cmd_cache.get(cid, cid)

    def create_link(self, ch_id, cmd_id=None, raw_cmd=None):
        """Create a fresh playable m3u8 URL.
          ch_id   - channel id (required, used as fallback key)
          cmd_id  - cmds[0].id (preferred by Ministra tatatv-style portals)
          raw_cmd - complete 'ffrt http://localhost/ch/<id>' string from ch['cmd']; bypasses guesswork
        Returns a playable URL (after following redirects) or None."""
        if raw_cmd:
            targets = [raw_cmd]
        else:
            cid = str(cmd_id) if cmd_id else str(self._resolve_cmd_id(ch_id))
            targets = [f"{p}http://localhost/ch/{cid}" for p in ("ffrt ","ff ")]
            # also try raw ch.id in case portal uses that
            if cid != str(ch_id):
                targets += [f"{p}http://localhost/ch/{ch_id}" for p in ("ffrt ","ff ")]
        last_err = None
        for cmd in targets:
            for _ in range(2):
                try:
                    u = (f"{self.base_url}{self.endpoint}?type=itv&action=create_link"
                         f"&cmd={urllib.parse.quote(cmd)}&JsHttpRequest=1-xml")
                    j = self._get(u)
                    if not isinstance(j, dict):
                        link = None
                    else:
                        link = j.get("js",{}).get("cmd")
                        err  = j.get("js",{}).get("error")
                    if link and link.startswith("http"):
                        # Don't follow/HEAD the link — some portals bind the token to
                        # the first request path, and following 302 here can 404 the
                        # actual stream in players. Return the URL verbatim; the player
                        # will follow redirects with its own UA.
                        return link
                    last_err = err
                except Exception as e:
                    last_err = e
                    self._handshake(); time.sleep(0.3)
        return None


# ---------- Writers ----------
def esc(s):
    if s is None: return ""
    s = str(s)
    # Escape XML/HTML-dangerous and M3U-breaking chars so channel names can't inject
    # #EXTINF lines or XSS into our generated index.html / m3u8.
    repl = {
        '"': "'",
        "\n": " ", "\r": " ", "\t": " ",
        "<": "&lt;", ">": "&gt;",
        "\\": "/",
    }
    for k,v in repl.items(): s = s.replace(k,v)
    # strip other control chars (0x00-0x1f except space)
    s = "".join(c if (c == " " or ord(c) >= 0x20) else "" for c in s)
    return s.strip()
def extinf(ch, group, url):
    attrs=[]
    if ch.get("tvg_id"): attrs.append(f'tvg-id="{esc(ch["tvg_id"])}"')
    if ch.get("tvg_name"): attrs.append(f'tvg-name="{esc(ch["tvg_name"])}"')
    if ch.get("logo"): attrs.append(f'tvg-logo="{esc(ch["logo"])}"')
    if ch.get("group"): attrs.append(f'group-title="{esc(ch["group"])}"')
    attrs.append('catchup="flussonic"')
    attrs.append(f'catchup-source="{esc(url.rsplit("/",1)[0])}/"')
    n = esc(ch.get("name","Channel")) or "Channel"
    return f"#EXTINF:-1 {' '.join(attrs)},{n}\n{esc(url)}\n"

AUTHOR_NAME = os.environ.get("AUTHOR_NAME", "KOBIR")
PLAYLIST_TITLE = os.environ.get("PLAYLIST_TITLE", "KOBIR IPTV PRO")

# 8 random ASCII banners (one picked at random each build)
_ARTS = [
    '88      a8P    ,ad8888ba,    88888888ba   88  88888888ba\n88    ,88\'    d8"\'    `"8b   88      "8b  88  88      "8b\n88  ,88"     d8\'        `8b  88      ,8P  88  88      ,8P\n88,d88\'      88          88  88aaaaaa8P\'  88  88aaaaaa8P\'\n8888"88,     88          88  88""""""8b,  88  88""""88\'\n88P   Y8b    Y8,        ,8P  88      `8b  88  88`    8b\n88     "88,   Y8a.    .a8P   88      a8P  88  88     `8b\n88       Y8b   `"Y8888Y"\'    88888888P"   88  88`     8b',
    '888b    888 8888888888 888     88 8888888888 8888888b.\n8888b   888 888        888     888 888     888   Y88b\n88888b  888 888        888     888 888     888    888\n888Y88b 888 8888888    888     888 8888888 888   d88P\n888 Y88b888 888        Y88b   d88P 888     8888888P"\n888  Y88888 888         Y88b d88P  888     888 T88b\n888   Y8888 888          Y88o88P   888     888  T88b\n888    Y888 8888888888    Y8P     8888888 888   T88b',
    '  ______ ________  ____  __ ___ ______\n /_  __//  _/ __ \\/ __ )/ //_//_  __/\n  / /   / // /_/ / __  / ,<    / /\n / /  _/ // _, _/ /_/ / /| |  / /\n/_/  /___/_/ |_|\\____/_/ |_| /_/',
    ' __  __    ___    ____   ___   ____\n/\\ \\/ /   /\\_ \\  / __  \\/\\_ \\ / ___\\\n\\ \\  "-. \\//\\ \\/\\_\\L\\ \\//\\ /\\ \\__/\n \\ \\_\\ \\_\\ \\ \\/_/_ _ < \\ \\ \\ \\__  \\\n  \\/_/\\/_/  \\___\\____/ \\___\\/____/  PRO',
    "oooo oooo  .oooo.    ooooooooo. oooo ooooooooo.\n`88  `8P  d8P'`Y8b  `888  `Y8b `88  `888   `Y88.\n 888 d8'  888   888  888    888 888 888   .d88'\n88888[   888   888  888ooo88  888 888ooo88P'\n88`88b.  888   888  888  `88b  888 888`88b.\n888 `88b.`88b d88'  888   .88P 888 888  `88b.\no888oo888o `Y888P  o888bood8P o888oo888o  o888o",
    '`7MMF\'`YMM\'  .g8""8q.  `7MM"""Yp,`7MMF\'`7MM"""Mq.\n  MM  .M\'  .dP\'   `YM.  MM   Yb   MM   MM   `MM.\n  MM .d"   dM\'     `MM  MM   dP   MM   MM   ,M9\n  MMMMM.   MM       MM  MM""bg.   MM   MMmmdM9\n  MM  VMA  MM.     ,MP  MM   `Y   MM   MM  YM.\n  MM  `MM.`Mb.  ,dP\'  MM   ,9   MM   MM   `Mb.\n.JMML. MMb. `"bmmd"\'  .JMMmmmd9 .JMML.JMML. JMM.',
    '888   d8P .d8888b. 88888b.  8888888 88888b.\n888  d8P d88P" "Y88b888 `88b   888  888   Y88b\n888d8P   888   888 888 .88P   888  888   d88P\n88888[   888   888 888888K    888  8888888P"\n88`88b.  888   888 888 `Y8b   888  888 T88b\n888 `88b Y88b. 88P 888  d88P  888  888  T88b\n888  `88b `Y8888P" 8888888P" 8888888 888  T88b',
    '#   #  ####### ######  ### ######\n#  #   #     # #    #  #  #     #\n# #    #     # #    #  #  #     #\n###    #     # ######  #  ######\n#  #   #     # #   #   #  #   #\n#   #  #     # #    #  #  #    #\n#    # ####### #     # ### #     #'
]

def _pick_art() -> str:
    return random.choice(_ARTS).strip("\n")


_BOX_W = 72

def _box_line(text=""):
    t = text[:_BOX_W-2]
    return f"# {t}{' '*(_BOX_W-2-len(t))} #"


def m3u_header(title, epg_urls=None, extra=None):
    """Build a pro-styled # header block: ASCII art, BD update time, meta, then #EXTM3U.
    Lines starting with '#' are comments ignored by M3U players but show nicely in editors.
    """
    try:
        from zoneinfo import ZoneInfo
        now_bd = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Dhaka"))
        update_str = now_bd.strftime("%Y-%m-%d  %I:%M:%S %p  BD")
    except Exception:
        update_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    W = _BOX_W - 2
    lines = []
    top = "#" * (_BOX_W + 2)
    lines.append(top)
    lines.append(_box_line(""))
    art = _pick_art().split("\n")
    for row in art:
        lines.append(_box_line(row))
    lines.append(_box_line(""))
    lines.append(_box_line(f"::  {PLAYLIST_TITLE}"))
    lines.append(_box_line(""))
    meta = [("Last Updated (BD)", update_str),
            ("Author",           AUTHOR_NAME),
            ("Playlist",         title)]
    if extra:
        for k, v in extra.items():
            meta.append((str(k), str(v)))
    for k, v in meta:
        if len(k) > 20: k = k[:19] + "…"
        key = k + ":"
        val = v[:W-2-len(key)-1]
        lines.append(_box_line(f"  {key.ljust(20)} {val}"))
    lines.append(_box_line(""))
    epg_count = len(epg_urls) if epg_urls else 0
    lines.append(_box_line(f"  !! Stream links auto-refreshed | EPG: {epg_count} source(s) merged"))
    lines.append(_box_line("  <3 Made in Bangladesh - KOBIR x ENI (Eternal Proxy Mode)"))
    lines.append(_box_line(""))
    lines.append("#" * (_BOX_W + 2))
    lines.append("")
    attrs = []
    if epg_urls:
        seen = set(); urls = []
        for u in epg_urls:
            if u and u not in seen:
                seen.add(u); urls.append(u)
        if urls:
            attrs.append(f'url-tvg="{",".join(urls)}"')
    attrs.append('refresh="21600"')
    lines.append(f'#EXTM3U {" ".join(attrs)}')
    lines.append(f'#PLAYLIST:{title}')
    return "\n".join(lines) + "\n"

def write_m3u(path, title, channels, epg_urls=None, group=None, extra=None):
    """channels = list of dicts. If group is given, override group-title.
    extra = optional dict of meta lines to show in the header (Portal URL, etc.)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path,"w",encoding="utf-8") as f:
        f.write(m3u_header(title, epg_urls=epg_urls, extra=extra))
        for ch in channels:
            u = ch.get("hls") or ch.get("url")
            if not u: continue
            g = group or ch.get("group", "")
            attrs=[]
            if ch.get("tvg_id"): attrs.append(f'tvg-id="{esc(ch["tvg_id"])}"')
            if ch.get("tvg_name"): attrs.append(f'tvg-name="{esc(ch["tvg_name"])}"')
            if ch.get("logo"): attrs.append(f'tvg-logo="{esc(ch["logo"])}"')
            if g: attrs.append(f'group-title="{esc(g)}"')
            attrs.append('catchup="flussonic"')
            attrs.append(f'catchup-source="{esc(u.rsplit("/",1)[0])}/"')
            n = esc(ch.get("name","Channel")) or "Channel"
            f.write(f"#EXTINF:-1 {' '.join(attrs)},{n}\n{u}\n")

def write_index(path, sections):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows=[]
    for h, files in sections.items():
        rows.append(f"<h2>{esc(h)}</h2><ul>")
        for label, fp in files:
            rel = fp.relative_to(OUT_DIR.parent).as_posix()
            rows.append(f'<li><a href="{esc(rel)}">{esc(label)}</a></li>')
        rows.append("</ul>")
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>TataTV IPTV</title>
<style>body{{font-family:system-ui;background:#0f172a;color:#e2e8f0;max-width:880px;margin:2rem auto;padding:0 1rem}}
h1{{color:#38bdf8}}h2{{color:#a3e635;border-bottom:1px solid #334155}}a{{color:#fbbf24}}</style></head><body>
<h1>📺 TataTV IPTV</h1><small>Updated {now}</small>{''.join(rows)}</body></html>"""
    path.write_text(html, encoding="utf-8")


def process_portal(cfg, epg, aliases):
    pname = cfg["name"]
    proxies = _merged_proxies(cfg.get("proxies"))
    print(f"\n[{pname}] শুরু করছি... url={cfg.get('url')} mac={cfg.get('mac')}")
    client = StalkerClient(pname, cfg["url"], cfg["mac"],
                           proxies=proxies, epg_prefix=cfg.get("epg_id_prefix",""))
    genres = client.genres()

    # Build per-portal allowlist from config.json `allowed_genres` + env GENRE_ALLOW.
    # Precedence: per-portal allowed_genres > top-level allowed_genres > env GENRE_ALLOW.
    portal_allow_cfg = (cfg.get("allowed_genres")
                        or cfg.get("only_genres")
                        or [])
    if isinstance(portal_allow_cfg, str): portal_allow_cfg = [portal_allow_cfg]
    allow_kw = []
    for k in list(portal_allow_cfg) + list(_GENRE_ALLOW_ENV):
        ku = k.strip().upper() if isinstance(k,str) else str(k).strip().upper()
        if ku and ku not in allow_kw:
            allow_kw.append(ku)
    # Always keep special "*" (All) pseudo-genre — its contents will be re-filtered below
    star_g = next((g for g in genres if g.get("id") == "*"), None)
    if allow_kw:
        before = len(genres)
        genres = [g for g in genres if _genre_allowed(g.get("title",""), allow_kw)]
        if star_g and star_g not in genres: genres.insert(0, star_g)
        print(f"[{pname}] ✅ allowlist: {before} → {len(genres)} genres (only: {allow_kw})")
    elif _GENRE_BLOCK_KW:
        before = len(genres)
        genres = [g for g in genres if not _genre_blocked(g.get("title",""))]
        if star_g and star_g not in genres: genres.insert(0, star_g)
        print(f"[{pname}] 🚫 blocklist: {before} → {len(genres)} genres (dropped: {_GENRE_BLOCK_KW})")

    if DRY_RUN:
        bangla_kw = ("BANGLA","BENGALI","HINDI","SPORTS","CRICKET","4K")
        bgenres = [g for g in genres if any(k in g["title"].upper() for k in bangla_kw)]
        if bgenres: genres = bgenres[:2]  # limit to 2 genres for fast smoke
        print(f"[{pname}] DRY_RUN: {len(genres)} genre (smoke test)")
    print(f"[{pname}] {len(genres)} genre")
    out = OUT_DIR / sanitize(pname); m3u_out = M3U_DIR / sanitize(pname)
    grouped = {}; all_ch = []; lock = Lock()

    origin = f"{client.proto}://{client.host}"
    if client.port not in (80,443): origin += f":{client.port}"

    def enrich(name, logo_hint):
        # EPG/alias match first (gives proper logo when available)
        tid, elogo = epg.match(name, aliases=aliases)
        full_logo = ""
        if logo_hint:
            full_logo = logo_hint if logo_hint.startswith("http") else f"{origin}/stalker_portal/misc/avatar/{logo_hint.lstrip('/')}"
        # Prefer EPG logo, then alias logo, then portal-provided logo (highest quality first)
        chosen_logo = elogo or full_logo or LOGO_FALLBACK
        return tid, chosen_logo

    proxy_mode = bool(PLAYLIST_BASE_URL)
    def process(g):
        gid,gt = g["id"],g["title"]
        folder = "00_All" if str(gid)=="*" else classify_folder(gt)
        try: raw = client.channels(gid)
        except Exception as e:
            print(f"[{pname}] ! {gt} fail: {e}"); return folder,gt,[]
        result=[]
        if proxy_mode:
            # No per-channel create_link needed — proxy will resolve on first hit.
            # This is 20-50x faster and never ships expiring tokens.
            for ch in raw:
                nm = ch.get("name","").strip()
                if not nm: continue
                tid,tlogo = enrich(nm, ch.get("logo","") or "")
                u = f"{PLAYLIST_BASE_URL}/w/{pname}/{ch.get('id')}"
                result.append({"id":ch.get("id"),"name":nm,"cmd":ch.get("cmd",""),
                               "hls":u,"logo":tlogo,"tvg_id":tid,"tvg_name":nm,
                               "group":gt,"genre_id":gid,"folder":folder,"portal":pname})
        else:
            # Legacy: bake expiring direct URLs into the playlist (local, short-lived)
            def linkone(ch):
                cid = ch.get("id")
                raw_cmd = ch.get("cmd") or ""
                cmds = ch.get("cmds") or []
                cmd_id = cmds[0].get("id") if cmds and cmds[0].get("id") else None
                url = client.create_link(str(cid), cmd_id=str(cmd_id) if cmd_id else None,
                                         raw_cmd=raw_cmd if raw_cmd.startswith(('ff ','ffrt ')) else None) if cid else None
                return ch,url
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                for ch,u in ex.map(linkone, raw):
                    if not u: continue
                    nm = ch.get("name","").strip()
                    tid,tlogo = enrich(nm, ch.get("logo","") or "")
                    result.append({"id":ch.get("id"),"name":nm,"cmd":ch.get("cmd",""),
                                   "hls":u,"logo":tlogo,"tvg_id":tid,"tvg_name":nm,
                                   "group":gt,"genre_id":gid,"folder":folder,"portal":pname})
        result.sort(key=lambda x:x["name"].lower())
        return folder,gt,result

    for folder,gt,chans in map(process, genres):
        if not chans: continue
        # When expanding special "*" (All) pseudo-genre, drop channels whose group-title
        # does not match allowlist / is on blocklist (so all.m3u8 is also clean).
        if str(gt) in ("*","All","ALL"):
            def _keep_all(c):
                if allow_kw:
                    return _genre_allowed(c.get("group",""), allow_kw)
                return not _genre_blocked(c.get("group",""))
            chans = [c for c in chans if _keep_all(c)]
        if not chans: continue
        with lock:
            grouped.setdefault((folder,gt),[]).extend(chans)
            all_ch.extend(chans)
        print(f"[{pname}]   ✔ {gt}: {len(chans)}")

    seen={}; uniq=[]
    for ch in all_ch:
        k = ch["name"].upper()
        if k not in seen: seen[k]=ch; uniq.append(ch)
    all_ch = sorted(uniq, key=lambda x:x["name"].lower())

    epg_urls = cfg.get("epg_sources", [])
    all_extra = {
        "Portal URL": client.base_url,
        "MAC": cfg.get("mac",""),
        "Total Channels": len(all_ch),
        "Mode": "Eternal Proxy" if proxy_mode else "Direct",
    }
    for (folder,gt),chans in grouped.items():
        safe = sanitize(gt)
        write_m3u(out/folder/f"{safe}.m3u8", f"{pname} - {gt}", chans,
                  epg_urls=epg_urls, group=gt,
                  extra={"Portal URL": client.base_url, "Genre": gt, "Channels": len(chans)})
        write_m3u(m3u_out/folder/f"{safe}.m3u", f"{pname} - {gt}", chans,
                  epg_urls=epg_urls, group=gt,
                  extra={"Portal URL": client.base_url, "Genre": gt, "Channels": len(chans)})
    write_m3u(out/"all.m3u8", f"{pname} - All", all_ch,
              epg_urls=epg_urls, group="All", extra=all_extra)
    write_m3u(m3u_out/"all.m3u", f"{pname} - All", all_ch,
              epg_urls=epg_urls, group="All", extra=all_extra)
    (out/"catalog.json").write_text(json.dumps({"portal":pname,
        "generated_at":datetime.now(timezone.utc).isoformat(),"count":len(all_ch),
        "channels":all_ch}, indent=2, ensure_ascii=False), encoding="utf-8")
    for fn,pats in (cfg.get("favorites") or {}).items():
        fcs=[]
        for pat in pats:
            up=pat.upper()
            for ch in all_ch:
                if up in ch["name"].upper() and ch not in fcs: fcs.append(ch)
        if fcs:
            write_m3u(out/f"fav_{sanitize(fn)}.m3u8", f"{pname} - {fn}", fcs, epg_urls=epg_urls, group=fn)
            write_m3u(m3u_out/f"fav_{sanitize(fn)}.m3u", f"{pname} - {fn}", fcs, epg_urls=epg_urls, group=fn)
            print(f"[{pname}] ★ {fn}: {len(fcs)}")
    return all_ch


def main():
    cfg = load_config()
    # Skip portals marked enabled:false
    cfg["portals"] = [pc for pc in cfg["portals"] if pc.get("enabled", True) is not False]
    print(f"[*] Portal configs: {len(cfg['portals'])}")
    for pc in cfg["portals"]:
        plist = _merged_proxies(pc.get("proxies"))
        pd = f"{len(plist)} proxies" if plist else "DIRECT ONLY"
        print(f"    - {pc.get('name')}: {pc.get('url')} mac={pc.get('mac')} proxies={pd} allowed={pc.get('allowed_genres') or '(blocklist)'}")
    epg = EPGIndex(cfg.get("epg_sources", []))
    all_all = []
    for pc in cfg["portals"]:
        try: all_all.extend(process_portal(pc, epg, cfg.get("aliases",{})))
        except Exception as e:
            import traceback
            print(f"[!] portal {pc.get('name')} FAILED: {e}")
            traceback.print_exc()
    if len(cfg["portals"]) > 1:
        write_m3u(OUT_DIR/"all.m3u8", "All Portals", all_all,
                  epg_urls=cfg.get("epg_sources",[]), group="All",
                  extra={"Portals": ", ".join(pc.get("name","") for pc in cfg["portals"]),
                         "Total Channels": len(all_all),
                         "Mode": "Eternal Proxy" if PLAYLIST_BASE_URL else "Direct"})
        write_m3u(M3U_DIR/"all.m3u", "All Portals", all_all,
                  epg_urls=cfg.get("epg_sources",[]), group="All",
                  extra={"Portals": ", ".join(pc.get("name","") for pc in cfg["portals"]),
                         "Total Channels": len(all_all),
                         "Mode": "Eternal Proxy" if PLAYLIST_BASE_URL else "Direct"})
    sec = {}
    def collect(base, heading):
        files = sorted(base.rglob("*.m3u8")) + sorted(base.rglob("*.m3u"))
        if files: sec[heading] = [(f.relative_to(base).as_posix(), f) for f in files]
    collect(OUT_DIR,"📂 HLS (.m3u8)")
    collect(M3U_DIR,"🎞️  VLC (.m3u)")
    jfs = sorted(OUT_DIR.rglob("catalog.json"))
    if jfs: sec["🧾 JSON"] = [(j.relative_to(OUT_DIR.parent).as_posix(),j) for j in jfs]
    write_index(OUT_DIR/"index.html",sec)
    write_index(M3U_DIR/"index.html",sec)
    print(f"\n[🏁] done! {len(all_all)} channels.")
    print(f"   HLS: {OUT_DIR}\n   M3U:  {M3U_DIR}")

if __name__ == "__main__":
    main()
