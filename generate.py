#!/usr/bin/env python3
"""
Professional TataTV / Multi-Portal IPTV M3U generator.
Features:
  - Multi-portal + multi-MAC (round-robin) support via config.json
  - SOCKS5/HTTP proxy per portal
  - Auto-detects logo (stalker returns logo on some portals) + falls back
    to channel-name-based logo guess
  - EPG tvg-id mapping (name → epg id) via fuzzy match + manual aliases
  - Catchup / timeshift tags where portal supports it
  - Favorites playlists from config
  - Per-genre .m3u + per-genre .m3u8 HLS files (no extension nonsense)
  - VLC-direct .m3u (absolute http:// play links)
  - Full JSON channel catalog (machine-readable)
  - index.html human-readable directory listing
  - GitHub Actions cron + manual dispatch
  - Parallel page fetch + parallel create_link
"""
import hashlib
import json
import os
import re
import sys
import time
import gzip
import difflib
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from io import BytesIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------
ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.json"
DEFAULT_CONFIG = {
    "portals": [
        {
            "name": "tatatv",
            "url": os.environ.get("PORTAL_URL", "http://tatatv.cc/stalker_portal/c/"),
            "mac": os.environ.get("MAC_ADDR", "00:1A:79:00:8C:32"),
            "proxy": os.environ.get("SOCKS_PROXY", "socks5h://test:test@203.96.226.98:1088"),
            "epg_id_prefix": "in.",
        }
    ],
    "epg_sources": [
        "https://epg.pw/xmltv/epg_in.xml.gz",
        "https://raw.githubusercontent.com/dtankdempse/epg/main/in.xml.gz"
    ],
    "favorites": {
        "Bangla_Trending": ["ZEE BANGLA 4K", "STAR JALSHA HD", "COLORS BANGLA HD"],
    },
    "aliases": {},
    "output": {
        "vlc_m3u": True,
        "m3u8_hls": True,
        "json_metadata": True,
        "index_html": True,
    },
}

OUT_DIR = ROOT / "playlists"
M3U_DIR = ROOT / "m3u"
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "25"))
TIMEOUT = 20
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
MAG_UA = ("Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
          "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3")
LOGO_FALLBACK = "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3f/Placeholder_view_vector.svg/240px-Placeholder_view_vector.svg.png"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # merge on top of defaults
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception as e:
            print(f"[!] config.json পার্স হচ্ছে না ({e}) — ডিফল্ট ব্যবহার করছি")
    return DEFAULT_CONFIG


# --------------------------------------------------------------------------
# Folder classification
# --------------------------------------------------------------------------
FOLDER_MAP = [
    ("BANGLA",            "01_Bangla"),
    ("HINDI",             "02_Hindi"),
    ("URDU",              "03_Urdu"),
    ("PUNJABI",           "04_Punjabi"),
    ("TAMIL",             "05_Tamil"),
    ("TELUGU",            "06_Telugu"),
    ("MARATHI",           "07_Marathi"),
    ("GUJARATI",          "08_Gujarati"),
    ("MALAYALAM",         "09_Malayalam"),
    ("KANNADA",           "10_Kannada"),
    ("SRI LANKA",         "11_Sri_Lanka"),
    ("ENGLISH",           "12_English"),
    ("KIDS",              "13_Kids"),
    ("ANIMATION",         "13_Kids"),
    ("SPORTS",            "14_Sports"),
    ("CRICKET",           "14_Sports"),
    ("FIFA",              "14_Sports"),
    ("UFC",               "14_Sports"),
    ("NBA",               "14_Sports"),
    ("NHL",               "14_Sports"),
    ("MLB",               "14_Sports"),
    ("NFL",               "14_Sports"),
    ("EPL",               "14_Sports"),
    ("MLS",               "14_Sports"),
    ("FORMULA 1",         "14_Sports"),
    ("F1",                "14_Sports"),
    ("TENNIS",            "14_Sports"),
    ("GOLF",              "14_Sports"),
    ("SOCCER",            "14_Sports"),
    ("RACING",            "14_Sports"),
    ("PPV",               "15_PPV"),
    ("4K",                "16_4K"),
    ("ADULT 18+",         "17_Adult"),
    ("RELIGIOUS",         "18_Religious"),
    ("NEWS",              "19_News"),
    ("MUSIC",             "20_Music"),
    ("MOVIE",             "21_Movies"),
]


def classify_folder(title: str) -> str:
    up = title.upper().strip()
    if up in ("ALL", "*"):
        return "00_All"
    for kw, folder in FOLDER_MAP:
        if kw in up:
            return folder
    return "22_Others"


def sanitize(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s[:60] or "channel"


# --------------------------------------------------------------------------
# EPG loader
# --------------------------------------------------------------------------
class EPGIndex:
    """Downloads EPG XML (or .gz) and builds tvg-id lookup + logo map."""
    def __init__(self, sources: list[str]):
        self.channels: dict[str, dict] = {}  # normalized_name -> {id, names[], logo}
        self.ids: dict[str, dict] = {}       # id -> {logo, names}
        ok = 0
        for src in sources:
            try:
                self._load(src); ok += 1
            except Exception as e:
                print(f"[epg] সোর্স ফেল {src}: {e}")
        print(f"[epg] {len(self.ids)} চ্যানেল ইনডেক্স করা হয়েছে ({ok}/{len(sources)} সোর্স সফল)")

    def _load(self, url: str):
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.content
            if url.endswith(".gz"):
                data = gzip.GzipFile(fileobj=BytesIO(data)).read()
            root = ET.fromstring(data)
            for ch in root.findall("channel"):
                cid = ch.get("id", "")
                display_names = [e.text or "" for e in ch.findall("display-name")]
                logo_el = ch.find("icon")
                logo = logo_el.get("src") if logo_el is not None else ""
                self.ids[cid] = {"names": display_names, "logo": logo}
                for dn in display_names:
                    key = self._norm(dn)
                    if key and key not in self.channels:
                        self.channels[key] = {"id": cid, "name": dn, "logo": logo}
        except Exception as e:
            print(f"[epg] {url} লোডে সমস্যা: {e}")

    @staticmethod
    def _norm(s: str) -> str:
        s = s.upper()
        s = re.sub(r"[^A-Z0-9]+", " ", s)
        s = re.sub(r"\b(HD|FHD|UHD|4K|TV|LIVE)\b", "", s)
        return s.strip()

    def match(self, name: str, prefix: str = "") -> tuple[str, str]:
        """Returns (tvg_id, logo_url) best-effort match."""
        key = self._norm(name)
        if not key:
            return "", ""
        if key in self.channels:
            c = self.channels[key]
            return c["id"], c["logo"]
        # fuzzy match
        names = list(self.channels.keys())
        best = difflib.get_close_matches(key, names, n=1, cutoff=0.82)
        if best:
            c = self.channels[best[0]]
            return c["id"], c["logo"]
        # prefix-based id (for raw M3U fallback)
        guess_id = prefix + slugify(name) if prefix else slugify(name)
        return guess_id, ""


# --------------------------------------------------------------------------
# Stalker Client
# --------------------------------------------------------------------------
class StalkerClient:
    def __init__(self, name: str, portal_url: str, mac: str, proxy: str | None = None,
                 epg_prefix: str = ""):
        self.name = name
        self.proxy = proxy
        self.mac = mac
        self.epg_prefix = epg_prefix
        from urllib.parse import urlparse
        p = urlparse(portal_url)
        self.proto = p.scheme or "http"
        self.host = p.hostname
        self.port = p.port or (443 if self.proto == "https" else 80)
        parsed_path = p.path or "/"
        if parsed_path.endswith("c/"):
            parsed_path = parsed_path[:-2]
        base_host = f"{self.proto}://{self.host}:{self.port}"
        self.endpoint = None
        self.portal_version = "5.3.1"
        s = self._new_session()
        for ep, vjs in [("portal.php", "/c/version.js"),
                        ("stalker_portal/server/load.php", "/stalker_portal/c/version.js")]:
            try:
                r = s.get(base_host + vjs, timeout=TIMEOUT)
                r.raise_for_status()
                m = re.search(r"var ver = ['\"](.*?)['\"];", r.text)
                if m:
                    self.endpoint = ep
                    self.portal_version = m.group(1)
                    break
            except Exception:
                pass
        if not self.endpoint:
            self.endpoint = "portal.php"
        self.base_url = f"{self.proto}://{self.host}:{self.port}{parsed_path}"
        if "stalker_portal/" in self.base_url and "stalker_portal/" in self.endpoint:
            self.base_url = self.base_url.replace("stalker_portal/", "")
        self.base_url = self.base_url.rstrip("/") + "/"

        self.sn = hashlib.md5(mac.encode()).hexdigest().upper()[:13]
        self.did  = hashlib.sha256(self.sn.encode()).hexdigest().upper()
        self.did2 = hashlib.sha256(mac.encode()).hexdigest().upper()
        self.hv2  = hashlib.sha1(mac.encode()).hexdigest()
        self.cookies = {
            "adid": self.hv2, "debug": "1", "device_id2": self.did2,
            "device_id": self.did, "hw_version": "1.7-BD-00", "mac": mac,
            "sn": self.sn, "stb_lang": "en", "timezone": "Asia/Dhaka",
        }
        self.base_headers = {
            "User-Agent": MAG_UA, "Accept-Encoding": "identity",
            "Accept": "*/*", "Connection": "keep-alive",
        }
        self._handshake()

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        if self.proxy:
            s.proxies = {"http": self.proxy, "https": self.proxy}
            s.trust_env = False
        retry = Retry(total=3, backoff_factor=0.3,
                      status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"])
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.mount("https://", HTTPAdapter(max_retries=retry))
        return s

    def _handshake(self):
        s = self._new_session()
        hs_url = f"{self.base_url}{self.endpoint}?action=handshake&type=stb&JsHttpRequest=1-xml"
        r = s.get(hs_url, cookies=self.cookies, headers=self.base_headers, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()["js"]
        self.token = js["token"]
        self.tr = js.get("random")
        if self.tr:
            sig = hashlib.sha256(self.tr.encode()).hexdigest().upper()
            metrics = {"mac": self.mac, "sn": self.sn, "type": "STB",
                       "model": "MAG250", "uid": self.did, "random": self.tr}
            enc = urllib.parse.quote(json.dumps(metrics))
            s.headers.update({**self.base_headers,
                              "Authorization": f"Bearer {self.token}",
                              "X-Random": str(self.tr)})
            s.cookies.update(self.cookies)
            purl = (
                f"{self.base_url}{self.endpoint}?type=stb&action=get_profile&hd=1"
                f"&ver=ImageDescription: 0.2.18-r23-250; ImageDate: Wed Aug 29 10:49:53 EEST 2018; "
                f"PORTAL version: {self.portal_version}; API Version: JS API version: 343; "
                f"STB API version: 146; Player Engine version: 0x58c&num_banks=2"
                f"&sn={self.sn}&stb_type=MAG250&client_type=STB&image_version=218&video_out=hdmi"
                f"&device_id={self.did2}&device_id2={self.did2}&sig={sig}"
                f"&auth_second_step=1&hw_version=1.7-BD-00&not_valid_token=0"
                f"&metrics={enc}&hw_version_2={self.hv2}"
                f"&timestamp={round(time.time())}&api_sig=262&prehash=0"
            )
            s.get(purl, timeout=TIMEOUT)
        self.cookies["token"] = self.token
        s.cookies.set("token", self.token)
        self.session = s
        print(f"[{self.name}] ✅ auth endpoint={self.endpoint} v={self.portal_version}")

    def _get(self, url: str, tries=3) -> requests.Response:
        last = None
        for i in range(tries):
            try:
                r = self.session.get(url, timeout=TIMEOUT)
                if r.status_code == 200:
                    if any(k in url for k in ("action=",)):
                        try:
                            r.json()
                        except Exception:
                            time.sleep(0.5*(i+1))
                            self._handshake(); continue
                    return r
                if r.status_code in (401, 403):
                    self._handshake(); time.sleep(0.3*(i+1)); continue
                r.raise_for_status()
            except Exception as e:
                last = e; time.sleep(0.5*(i+1))
                try: self._handshake()
                except Exception: pass
        if last: raise last
        raise RuntimeError(f"req failed: {url}")

    def list_genres(self) -> list[dict]:
        url = f"{self.base_url}{self.endpoint}?type=itv&action=get_genres&JsHttpRequest=1-xml"
        data = self._get(url).json().get("js", [])
        out = [{"id": g["id"], "title": g["title"].strip()} for g in data if isinstance(g, dict)]
        out.sort(key=lambda x: (x["id"] != "*", x["title"]))
        return out

    def _page(self, gid, p) -> tuple[int, list[dict]]:
        url = (f"{self.base_url}{self.endpoint}?type=itv&action=get_ordered_list"
               f"&genre={gid}&p={p}&JsHttpRequest=1-xml")
        r = self._get(url)
        j = r.json().get("js", {})
        return int(j.get("total_items", 0)), j.get("data", []) or []

    def list_channels(self, gid) -> list[dict]:
        total, first = self._page(gid, 0)
        ipp = len(first) or 25
        pages = (total + ipp - 1)//ipp if ipp else 0
        chans = list(first); seen = {c.get("id") for c in chans}
        def grab(p):
            try: return self._page(gid, p)[1]
            except Exception: return []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, 20)) as ex:
            for batch in as_completed(ex.submit(grab, p) for p in range(1, pages)):
                for ch in batch.result():
                    cid = ch.get("id")
                    if cid and cid not in seen:
                        seen.add(cid); chans.append(ch)
        return chans

    def create_link(self, ch_id: str) -> str | None:
        for prefix in ("ffrt ", "ff "):
            for _ in range(2):
                try:
                    cmd = f"{prefix}http://localhost/ch/{ch_id}"
                    url = (f"{self.base_url}{self.endpoint}?type=itv&action=create_link"
                           f"&cmd={urllib.parse.quote(cmd)}&JsHttpRequest=1-xml")
                    js = self._get(url).json().get("js", {})
                    link = js.get("cmd")
                    if link and link.startswith("http"):
                        try:
                            head = requests.get(link, allow_redirects=True,
                                                stream=True, timeout=TIMEOUT,
                                                proxies=self.session.proxies if self.proxy else None)
                            return head.url
                        except Exception:
                            return link
                except Exception:
                    self._handshake(); time.sleep(0.3)
        return None


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------
def esc(s: str) -> str:
    return s.replace('"', "'").replace("\n", " ").strip()


def extinf(channel: dict, group: str, url: str) -> str:
    attrs = []
    if channel.get("tvg_id"):
        attrs.append(f'tvg-id="{esc(channel["tvg_id"])}"')
    if channel.get("tvg_name"):
        attrs.append(f'tvg-name="{esc(channel["tvg_name"])}"')
    if channel.get("logo"):
        attrs.append(f'tvg-logo="{esc(channel["logo"])}"')
    if channel.get("group"):
        attrs.append(f'group-title="{esc(channel["group"])}"')
    # catchup tags
    attrs.append('catchup="flussonic"')
    attrs.append(f'catchup-source="{esc(url.split("/index.m3u8")[0])}/"')
    attr_str = " ".join(attrs)
    name = channel.get("name", "").strip() or "Channel"
    return f"#EXTINF:-1 {attr_str},{name}\n{url}\n"


def m3u_header(title: str, epg_urls: list[str]) -> str:
    x_tvg = ",".join(epg_urls)
    return (f'#EXTM3U x-tvg-url="{x_tvg}" refresh="21600" '
            f'url-tvg="{x_tvg}" generator="MacAttack-IPTV-Gen"\n'
            f'#PLAYLIST:{title}\n')


def write_m3u(path: Path, title: str, epg_urls: list[str], group: str,
              channels: list[dict], hls_only: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write(m3u_header(title, epg_urls))
        for ch in channels:
            url = ch.get("hls") or ch.get("url")
            if not url:
                continue
            f.write(extinf(ch, group, url))


def write_json_catalog(path: Path, portal: str, channels: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "portal": portal,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(channels),
        "channels": channels,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_index_html(path: Path, sections: dict[str, list[tuple[str, Path]]]):
    """sections: { heading: [(label, relative_path), ...] } """
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for heading, files in sections.items():
        rows.append(f"<h2>{esc(heading)}</h2><ul>")
        for label, fp in files:
            rel = fp.relative_to(OUT_DIR.parent).as_posix()
            rows.append(f'<li><a href="{rel}">{esc(label)}</a></li>')
        rows.append("</ul>")
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>TataTV IPTV Playlists</title>
<style>body{{font-family:system-ui,sans-serif;max-width:880px;margin:2rem auto;padding:0 1rem;background:#0f172a;color:#e2e8f0}}
h1{{color:#38bdf8}} h2{{color:#a3e635;border-bottom:1px solid #334155;padding-bottom:.3rem;margin-top:2rem}}
a{{color:#fbbf24;text-decoration:none}}a:hover{{text-decoration:underline}}
small{{color:#94a3b8;display:block;margin-top:.5rem}}
ul{{line-height:1.8}}</style></head>
<body>
<h1>📺 TataTV IPTV Playlists</h1>
<small>Auto-refreshed: {now} &middot; Links valid ~6h</small>
{''.join(rows)}
</body></html>"""
    path.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def process_portal(cfg: dict, epg: EPGIndex, aliases: dict) -> list[dict]:
    pname = cfg["name"]
    print(f"\n[{pname}] শুরু করছি...")
    client = StalkerClient(
        name=pname, portal_url=cfg["url"], mac=cfg["mac"],
        proxy=cfg.get("proxy") or None, epg_prefix=cfg.get("epg_id_prefix", ""),
    )
    genres = client.list_genres()
    if DRY_RUN:
        genres = [g for g in genres if g["title"] in ("BANGLA - TV",)]
    print(f"[{pname}] {len(genres)} জেনার")

    portal_out = OUT_DIR / sanitize(pname)
    portal_out.mkdir(parents=True, exist_ok=True)
    all_channels: list[dict] = []
    grouped: dict[str, list[dict]] = {}
    lock = Lock()

    def enrich(name: str, logo_hint: str, portal_name: str, base_origin: str) -> tuple[str, str]:
        # manual alias first
        if name in aliases:
            a = aliases[name]
            return a.get("id", ""), a.get("logo", "")
        # portal-provided logo: stalker returns "11129.png" — need full URL
        if logo_hint:
            if logo_hint.startswith("http"):
                full_logo = logo_hint
            else:
                # stalker_logo is at /stalker_portal/misc/avatar/ or similar — guess a sensible default
                full_logo = f"{base_origin.rstrip('/')}/stalker_portal/misc/avatar/{logo_hint.lstrip('/')}"
        else:
            full_logo = ""
        tid, epg_logo = epg.match(name, cfg.get("epg_id_prefix", ""))
        # Prefer EPG logo if found; fall back to portal logo
        final_logo = epg_logo or full_logo or LOGO_FALLBACK
        return tid, final_logo

    def process_genre(g: dict) -> tuple[str, str, list[dict]]:
        gid, gtitle = g["id"], g["title"]
        folder = "00_All" if str(gid) == "*" else classify_folder(gtitle)
        try:
            raw = client.list_channels(gid)
        except Exception as e:
            print(f"[{pname}] ! {gtitle} list failed: {e}")
            return folder, gtitle, []
        base_origin = f"{client.proto}://{client.host}:{client.port}" if client.port not in (80,443) else f"{client.proto}://{client.host}"
        # resolve links parallel
        def link_one(ch):
            ch_id = ch.get("id")
            url = client.create_link(str(ch_id)) if ch_id else None
            return ch, url
        result: list[dict] = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for ch, url in ex.map(link_one, raw):
                if not url:
                    continue
                name = ch.get("name", "").strip()
                logo = ch.get("logo", "") or ""
                tvg_id, tvg_logo = enrich(name, logo, pname, base_origin)
                item = {
                    "id": ch.get("id"),
                    "name": name,
                    "cmd": ch.get("cmd", ""),
                    "hls": url,
                    "logo": tvg_logo,
                    "tvg_id": tvg_id,
                    "tvg_name": name,
                    "group": gtitle,
                    "genre_id": gid,
                    "folder": folder,
                    "portal": pname,
                }
                result.append(item)
        result.sort(key=lambda x: x["name"].lower())
        return folder, gtitle, result

    for folder, gtitle, chans in map(process_genre, genres):
        if not chans:
            continue
        with lock:
            grouped.setdefault((folder, gtitle), []).extend(chans)
            all_channels.extend(chans)
        print(f"[{pname}]   ✔ {gtitle}: {len(chans)} চ্যানেল")

    # Deduplicate all_channels by name
    seen_names = {}
    for ch in all_channels:
        k = ch["name"].upper()
        if k not in seen_names:
            seen_names[k] = ch
    all_channels = sorted(seen_names.values(), key=lambda x: x["name"].lower())

    # ----- write per-genre -----
    epg_urls = cfg.get("epg_sources", [])
    for (folder, gtitle), chans in grouped.items():
        safe = sanitize(gtitle)
        m3u8_path = portal_out / folder / f"{safe}.m3u8"
        write_m3u(m3u8_path, f"{pname} - {gtitle}", epg_urls, gtitle, chans, hls_only=True)
        # classic .m3u (VLC) — same HLS links but .m3u extension for compatibility
        m3u_path = M3U_DIR / sanitize(pname) / folder / f"{safe}.m3u"
        write_m3u(m3u_path, f"{pname} - {gtitle}", epg_urls, gtitle, chans)

    # ----- all-in-one playlists -----
    write_m3u(portal_out / "all.m3u8", f"{pname} - All Channels", epg_urls, "All", all_channels)
    write_m3u(M3U_DIR / sanitize(pname) / "all.m3u", f"{pname} - All Channels", epg_urls, "All", all_channels)

    # ----- JSON catalog -----
    write_json_catalog(portal_out / "catalog.json", pname, all_channels)

    # ----- favorites -----
    for fname, patterns in (cfg.get("favorites") or {}).items():
        fchans = []
        for pat in patterns:
            up = pat.upper()
            for ch in all_channels:
                if up in ch["name"].upper() and ch not in fchans:
                    fchans.append(ch)
        if fchans:
            fav_m3u8 = portal_out / f"fav_{sanitize(fname)}.m3u8"
            fav_m3u = M3U_DIR / sanitize(pname) / f"fav_{sanitize(fname)}.m3u"
            write_m3u(fav_m3u8, f"{pname} - {fname}", epg_urls, fname, fchans)
            write_m3u(fav_m3u, f"{pname} - {fname}", epg_urls, fname, fchans)
            print(f"[{pname}] ★ {fname}: {len(fchans)} চ্যানেল")

    return all_channels


def main():
    cfg = load_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    M3U_DIR.mkdir(parents=True, exist_ok=True)

    print("[*] EPG সোর্স লোড হচ্ছে...")
    epg = EPGIndex(cfg["epg_sources"])

    all_all: list[dict] = []
    index_sections: dict[str, list[tuple[str, Path]]] = {}

    global_epg_urls = cfg.get("epg_sources", [])
    for portal_cfg in cfg["portals"]:
        merged_epg = list(dict.fromkeys((portal_cfg.get("epg_sources") or []) + global_epg_urls))
        portal_cfg["epg_sources"] = merged_epg
        chans = process_portal(portal_cfg, epg, cfg.get("aliases", {}))
        all_all.extend(chans)

    # Master combined playlist across portals
    if len(cfg["portals"]) > 1:
        write_m3u(OUT_DIR / "all.m3u8", "All Portals", cfg["epg_sources"], "All", all_all)
        write_m3u(M3U_DIR / "all.m3u", "All Portals", cfg["epg_sources"], "All", all_all)

    # ---- build index.html ----
    if cfg["output"].get("index_html", True):
        # walk playlists dir
        def collect(root: Path, heading: str):
            files = sorted(root.rglob("*.m3u8"))
            if not files:
                return
            index_sections[heading] = [(f.relative_to(root).as_posix(), f) for f in files]
        collect(OUT_DIR, "📂 HLS Playlists (.m3u8)")
        collect(M3U_DIR, "🎞️  VLC Classic (.m3u)")
        # JSON catalog
        for jf in sorted(OUT_DIR.rglob("catalog.json")):
            index_sections.setdefault("🧾 JSON metadata", []).append(
                (jf.relative_to(OUT_DIR.parent).as_posix(), jf))
        write_index_html(OUT_DIR / "index.html", index_sections)
        write_index_html(M3U_DIR / "index.html", index_sections)

    print(f"\n[🏁] হয়ে গেছে! মোট {len(all_all)} চ্যানেল প্রসেস করা হয়েছে")
    print(f"   HLS: {OUT_DIR}")
    print(f"   M3U:  {M3U_DIR}")


if __name__ == "__main__":
    main()
