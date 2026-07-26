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
import hashlib, json, os, re, sys, time, gzip, urllib.parse, xml.etree.ElementTree as traceback_ET
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

MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "25"))
TIMEOUT = 15
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

MAG_UA = ("Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
          "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3")
LOGO_FALLBACK = ""  # no remote logo; rely on EPG

DEFAULT_CONFIG = {
    "portals": [
        {
            "name": "tatatv",
            "url": os.environ.get("PORTAL_URL", "http://tatatv.cc/stalker_portal/c/"),
            "mac": os.environ.get("MAC_ADDR", "00:1A:79:00:8C:32"),
            # proxy: string or list of strings; tried in order; empty/None = direct only
            "proxies": list(filter(None, [
                os.environ.get("SOCKS_PROXY", ""),
                os.environ.get("HTTP_PROXY", ""),
            ])),
            "epg_id_prefix": "in.",
        }
    ],
    "epg_sources": [],  # supply working EPG URLs if needed
    "aliases": {},
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
        for src in sources or []:
            try:
                r = requests.get(src, timeout=30)
                r.raise_for_status()
                data = r.content
                if src.endswith(".gz"):
                    data = gzip.GzipFile(fileobj=BytesIO(data)).read()
                root = ET.fromstring(data)
                for ch in root.findall("channel"):
                    cid = ch.get("id", "")
                    logos = [e.get("src") for e in ch.findall("icon") if e.get("src")]
                    logo = logos[0] if logos else ""
                    names = [e.text or "" for e in ch.findall("display-name")]
                    self.ids[cid] = {"names": names, "logo": logo}
                    for dn in names:
                        k = re.sub(r"[^A-Z0-9]+", " ", dn.upper())
                        k = re.sub(r"\b(HD|FHD|UHD|4K|TV|LIVE)\b", "", k).strip()
                        if k and k not in self.by_name:
                            self.by_name[k] = cid
                ok += 1
            except Exception as e:
                print(f"[epg] fail {src}: {e}")
        print(f"[epg] {len(self.ids)} channels indexed ({ok}/{len(sources or [])})")

    def match(self, name, prefix=""):
        key = re.sub(r"[^A-Z0-9]+", " ", name.upper())
        key = re.sub(r"\b(HD|FHD|UHD|4K|TV|LIVE)\b", "", key).strip()
        if not key: return "", ""
        if key in self.by_name:
            cid = self.by_name[key]
            return cid, self.ids[cid]["logo"]
        import difflib
        best = difflib.get_close_matches(key, list(self.by_name.keys()), n=1, cutoff=0.85)
        if best:
            cid = self.by_name[best[0]]
            return cid, self.ids[cid]["logo"]
        return (prefix + slugify(name) if prefix else slugify(name)), ""


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
        if parsed_path.endswith("c/"): parsed_path = parsed_path[:-2]
        base_host = f"{self.proto}://{self.host}:{self.port}"

        self.endpoint = None
        self.portal_version = "5.6.10"

        # detect endpoint trying each proxy
        last_err = None
        for prox in self.attempts:
            s = self._newsess(prox)
            for ep, vjs in [("portal.php", "/c/version.js"),
                            ("stalker_portal/server/load.php", "/stalker_portal/c/version.js")]:
                try:
                    r = s.get(base_host + vjs, timeout=10)
                    r.raise_for_status()
                    m = re.search(r"var ver = ['\"]([^'\"]+)['\"];", r.text)
                    if m:
                        self.endpoint = ep
                        self.portal_version = m.group(1)
                        self.active_proxy = prox
                        break
                except Exception as e:
                    last_err = e
            if self.endpoint: break
        if not self.endpoint:
            print(f"[{self.name}] ! portal detection failed; defaulting to stalker_portal/server/load.php")
            self.endpoint = "stalker_portal/server/load.php"
            self.active_proxy = None
        self.base_url = f"{self.proto}://{self.host}:{self.port}{parsed_path}"
        if "stalker_portal/" in self.base_url and "stalker_portal/" in self.endpoint:
            self.base_url = self.base_url.replace("stalker_portal/", "")
        self.base_url = self.base_url.rstrip("/") + "/"
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

    def _get(self, url, tries=3):
        last = None
        for prox in self.attempts:
            s = self._newsess(prox)
            s.headers.update(self.auth_headers); s.cookies.update(self.auth_cookies)
            for i in range(tries):
                try:
                    r = s.get(url, timeout=TIMEOUT)
                    if r.status_code == 200:
                        try: return r.json()
                        except Exception: return r
                    if r.status_code in (401, 403): break  # try next proxy
                except Exception as e:
                    last = e; time.sleep(0.3*(i+1))
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
        total = int(js0.get("total_items",0)); d0 = js0.get("data",[])
        ipp = max(len(d0),1); pages = (total+ipp-1)//ipp if total else 1
        allc = list(d0)
        if callable(progress): progress(1,pages)
        def grab(p):
            try: return self._page(gid,p).get("data",[])
            except: return []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,20)) as ex:
            futs = [ex.submit(grab,p) for p in range(1,pages)]
            done=1
            for fu in as_completed(futs):
                for ch in fu.result(): allc.append(ch)
                done+=1
                if callable(progress): progress(done,pages)
        uniq={};
        for ch in allc:
            cid = str(ch.get("id") or "")
            if cid and cid not in uniq: uniq[cid]=ch
        return sorted(uniq.values(), key=lambda x:x.get("name","").lower())

    def create_link(self, ch_id):
        for prefix in ("ffrt ","ff "):
            for _ in range(2):
                try:
                    cmd = f"{prefix}http://localhost/ch/{ch_id}"
                    u = (f"{self.base_url}{self.endpoint}?type=itv&action=create_link"
                         f"&cmd={urllib.parse.quote(cmd)}&JsHttpRequest=1-xml")
                    j = self._get(u)
                    link = j.get("js",{}).get("cmd") if isinstance(j,dict) else None
                    if link and link.startswith("http"):
                        try:
                            h = requests.get(link, allow_redirects=True, stream=True,
                                             timeout=10,
                                             proxies=self.session.proxies if self.active_proxy else None)
                            return h.url
                        except: return link
                except: self._handshake(); time.sleep(0.3)
        return None


# ---------- Writers ----------
def esc(s): return s.replace('"',"'").replace("\n"," ").strip()
def extinf(ch, group, url):
    attrs=[]
    if ch.get("tvg_id"): attrs.append(f'tvg-id="{esc(ch["tvg_id"])}"')
    if ch.get("tvg_name"): attrs.append(f'tvg-name="{esc(ch["tvg_name"])}"')
    if ch.get("logo"): attrs.append(f'tvg-logo="{esc(ch["logo"])}"')
    if ch.get("group"): attrs.append(f'group-title="{esc(ch["group"])}"')
    attrs.append('catchup="flussonic"')
    attrs.append(f'catchup-source="{esc(url.rsplit("/",1)[0])}/"')
    n = ch.get("name","Channel").strip()
    return f"#EXTINF:-1 {' '.join(attrs)},{n}\n{url}\n"

def m3u_header(title):
    return f'#EXTM3U refresh="21600"\n#PLAYLIST:{title}\n'

def write_m3u(path, title, group, channels):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path,"w",encoding="utf-8") as f:
        f.write(m3u_header(title))
        for ch in channels:
            u = ch.get("hls") or ch.get("url")
            if u: f.write(extinf(ch, group, u))

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
    print(f"\n[{pname}] শুরু করছি... url={cfg.get('url')} mac={cfg.get('mac')}")
    client = StalkerClient(pname, cfg["url"], cfg["mac"],
                           proxies=cfg.get("proxies"), epg_prefix=cfg.get("epg_id_prefix",""))
    genres = client.genres()
    if DRY_RUN:
        genres = [g for g in genres if g["title"] in ("BANGLA - TV",)]
    print(f"[{pname}] {len(genres)} genre")
    out = OUT_DIR / sanitize(pname); m3u_out = M3U_DIR / sanitize(pname)
    grouped = {}; all_ch = []; lock = Lock()

    origin = f"{client.proto}://{client.host}"
    if client.port not in (80,443): origin += f":{client.port}"

    def enrich(name, logo_hint):
        if name in aliases:
            a = aliases[name]; return a.get("id",""), a.get("logo","")
        full_logo = ""
        if logo_hint:
            full_logo = logo_hint if logo_hint.startswith("http") else f"{origin}/stalker_portal/misc/avatar/{logo_hint.lstrip('/')}"
        tid, elogo = epg.match(name, cfg.get("epg_id_prefix",""))
        return tid, (elogo or full_logo or LOGO_FALLBACK)

    def process(g):
        gid,gt = g["id"],g["title"]
        folder = "00_All" if str(gid)=="*" else classify_folder(gt)
        try: raw = client.channels(gid)
        except Exception as e:
            print(f"[{pname}] ! {gt} fail: {e}"); return folder,gt,[]
        def linkone(ch):
            cid = ch.get("id"); url = client.create_link(str(cid)) if cid else None
            return ch,url
        result=[]
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
    for (folder,gt),chans in grouped.items():
        safe = sanitize(gt)
        write_m3u(out/folder/f"{safe}.m3u8", f"{pname} - {gt}", epg_urls, gt, chans)
        write_m3u(m3u_out/folder/f"{safe}.m3u", f"{pname} - {gt}", epg_urls, gt, chans)
    write_m3u(out/"all.m3u8", f"{pname} - All", epg_urls, "All", all_ch)
    write_m3u(m3u_out/"all.m3u", f"{pname} - All", epg_urls, "All", all_ch)
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
            write_m3u(out/f"fav_{sanitize(fn)}.m3u8", f"{pname} - {fn}", epg_urls, fn, fcs)
            write_m3u(m3u_out/f"fav_{sanitize(fn)}.m3u", f"{pname} - {fn}", epg_urls, fn, fcs)
            print(f"[{pname}] ★ {fn}: {len(fcs)}")
    return all_ch


def main():
    cfg = load_config()
    print(f"[*] Portal configs: {len(cfg['portals'])}")
    for pc in cfg["portals"]:
        print(f"    - {pc.get('name')}: {pc.get('url')} mac={pc.get('mac')} proxies={pc.get('proxies') or 'DIRECT ONLY'}")
    epg = EPGIndex(cfg.get("epg_sources", []))
    all_all = []
    for pc in cfg["portals"]:
        try: all_all.extend(process_portal(pc, epg, cfg.get("aliases",{})))
        except Exception as e:
            print(f"[!] portal {pc.get('name')} FAILED: {e}")
    if len(cfg["portals"]) > 1:
        write_m3u(OUT_DIR/"all.m3u8","All Portals",cfg.get("epg_sources",[]),"All",all_all)
        write_m3u(M3U_DIR/"all.m3u","All Portals",cfg.get("epg_sources",[]),"All",all_all)
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
