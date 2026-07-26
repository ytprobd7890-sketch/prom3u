"""
Live-link proxy — keeps Stalker streams alive forever.

Player hits  /w/<portal_name>/<ch_id>
Server      → checks TTL cache (< 8 min old)
            → if miss: re-auth if needed, runs create_link via StalkerClient
            → returns HTTP 302 to a FRESH m3u8 URL
            → player follows redirect and plays

This means playlists never ship expired tokens. The .m3u8 files contain
stable /w/... URLs that work forever.
"""
import os, re, sys, time, json, threading, logging, random, subprocess
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
import requests as _req
from flask import (Flask, redirect, abort, Response, jsonify, request,
                   send_from_directory, stream_with_context)

sys.path.insert(0, str(Path(__file__).parent))
from generate import StalkerClient, _merged_proxies, _env_portal_url, _env_mac_addr, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

# ---------- Hacker ASCII error page (KOBIR skull) ----------
_ERROR_ART = r"""
                 ;i.

                  M$L                    .;i.

                  M$Y;                .;iii;;.

                 ;$YY$i._           .;iiii;;;;;

                .iiiYYYYYYiiiii;;;;i;iii;; ;;;

              .;iYYYYYYiiiiiiYYYiiiiiii;;  ;;;

           .YYYY$$$$YYYYYYYYYYYYYYYYiii;; ;;;;

         .YYY$$$$$$YYYYYY$$$$iiiY$$$$$$$ii;;;;

        :YYYF`,  TYYYYY$$$$$YYYYYYYi$$$$$iiiii;

        Y$MM: \  :YYYY$$P"````"T$YYMMMMMMMMiiYY.

     `.;$$M$$b.,dYY$$Yi; .(     .YYMMM$$$MMMMYY

   .._$MMMMM$!YYYYYYYYYi;.`"  .;iiMMM$MMMMMMMYY

    ._$MMMP` ```""4$$$$$iiiiiiii$MMMMMMMMMMMMMY;

     MMMM$:       :$$$$$$$MMMMMMMMMMM$$MMMMMMMYYL

    :MMMM$$.    .;PPb$$$$MMMMMMMMMM$$$$MMMMMMiYYU:

     iMM$$;;: ;;;;i$$$$$$$MMMMM$$$$MMMMMMMMMMYYYYY

     `$$$$i .. ``:iiii!*"``.$$$$$$$$$MMMMMMM$YiYYY

      :Y$$iii;;;.. ` ..;;i$$$$$$$$$MMMMMM$$YYYYiYY:

       :$$$$$iiiiiii$$$$$$$$$$$MMMMMMMMMMYYYYiiYYYY.

        `$$$$$$$$$$$$$$$$$$$$MMMMMMMM$YYYYYiiiYYYYYY

         YY$$$$$$$$$$$$$$$$MMMMMMM$$YYYiiiiiiYYYYYYY

        :YYYYYY$$$$$$$$$$$$$$$$$$YYYYYYYiiiiYYYYYYi'
""".strip("\n")

_ERROR_MESSAGES = [
    "ACCESS DENIED — tui ki churi korar cheshta korchis?",
    "ERROR 403 — ei jaigai tui dhukte parish na, bondhu.",
    "HACKER DETECTED — KOBIR er server e dhuke ki pabi re?",
    "NOT FOUND — vhul link e eshechosh, vai.",
    "INVALID REQUEST — credential churi korar try korchho? Bhul jaygay.",
    "BLOCKED — ei path ta private, ferot jao.",
]

def _error_page(code, msg=None):
    art = _ERROR_ART
    pick = msg or random.choice(_ERROR_MESSAGES)
    try:
        bd_now = datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Dhaka")).strftime("%Y-%m-%d %I:%M:%S %p BD")
    except Exception:
        bd_now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>[{code}] ACCESS DENIED</title>
<style>
html,body{{background:#000;color:#00ff41;margin:0;padding:0;height:100%;font-family:'Courier New',Consolas,monospace;overflow:hidden;}}
body{{display:flex;align-items:center;justify-content:center;flex-direction:column;}}
pre{{color:#ff0033;text-shadow:0 0 8px #ff0033,0 0 18px #ff0033;font-size:11px;line-height:1.05;white-space:pre;margin:0 auto;text-align:center;}}
.box{{border:1px solid #ff0033;padding:22px 28px;background:rgba(20,0,0,0.85);box-shadow:0 0 30px #ff0033,inset 0 0 20px #200;max-width:95vw;}}
.msg{{color:#ffcc00;text-shadow:0 0 6px #ffcc00;margin-top:14px;font-weight:bold;font-size:15px;text-align:center;letter-spacing:1px;}}
.blink{{animation:bl 1s steps(2,start) infinite;}}
@keyframes bl{{to{{visibility:hidden;}}}}
.meta{{color:#888;margin-top:12px;font-size:11px;text-align:center;}}
.scan{{position:fixed;top:0;left:0;width:100%;height:6px;background:linear-gradient(to bottom, rgba(0,255,65,0),rgba(0,255,65,.18),rgba(0,255,65,0));animation:scan 4s linear infinite;pointer-events:none;z-index:99;}}
@keyframes scan{{0%{{transform:translateY(-10px);}}100%{{transform:translateY(100vh);}}}}
.warn{{color:#00ff41;font-size:12px;text-align:center;margin-top:8px;letter-spacing:2px;}}
.ip{{color:#00ffff;font-size:11px;margin-top:6px;}}
</style></head><body>
<div class="scan"></div>
<div class="box">
<pre>{art}</pre>
<div class="warn">&gt;&gt; KOBIR SYSTEMS — UNAUTHORIZED ACCESS &lt;&lt;<span class="blink">_</span></div>
<div class="msg">[{code}] {pick}</div>
<div class="ip">IP: {request.remote_addr if request else '0.0.0.0'} &nbsp;|&nbsp; PATH: {request.path if request else '/'} &nbsp;|&nbsp; AGENT: {(request.user_agent.string[:60] if request else '-')}</div>
<div class="meta">[{bd_now}] &mdash; by KOBIR &mdash; PROTECTED BY STALKER SHIELD</div>
</div>
</body></html>"""
    resp = Response(html, status=code, mimetype="text/html")
    resp.headers["X-Powered-By"] = "KOBIR-SHIELD/1.0"
    resp.headers["X-Deny-Reason"] = "hacker_detected"
    resp.headers["Refresh"] = "5;url=/"
    return resp

CACHE_TTL = int(os.environ.get("CACHE_TTL", "480"))     # 8 minutes (safely below 15-20 min expiry)
AUTH_REFRESH = int(os.environ.get("AUTH_REFRESH", "21600"))  # re-handshake every 6 hours
PORT = int(os.environ.get("PORT", "8080"))
PLAYLIST_BASE_URL = os.environ.get("PLAYLIST_BASE_URL", "").rstrip("/")

app = Flask(__name__)

_boot_done = {"done": False}
@app.before_request
def _boot_once():
    # Run once on first request (works under gunicorn which never calls main()).
    if _boot_done["done"]: return
    _boot_done["done"] = True
    log.info(f"[proxy] cold start — CACHE_TTL={CACHE_TTL}s PLAYLIST_BASE_URL={PLAYLIST_BASE_URL}")
    _startup_generate()
    def warm():
        time.sleep(2)
        for pc in load_config().get("portals", []):
            if pc.get("enabled", True) is False: continue
            try: _get_client(pc["name"])
            except Exception as e: log.warning(f"[proxy] warmup failed for {pc['name']}: {e}")
    threading.Thread(target=warm, daemon=True).start()

# ---------- Shared state ----------
_clients = {}          # portal_name -> StalkerClient
_client_born = {}      # portal_name -> timestamp
_cache = {}            # (portal, ch_id) -> {"url":..., "ts":...}
_locks = {}            # portal_name -> threading.Lock (serialize create_link per portal)
_lock = threading.Lock()

# Auto-generate state
_gen_lock = threading.Lock()
_gen_state = {"running": False, "started": 0, "finished": 0, "rc": None, "log": ""}

# Genre cache: portal_name -> {"ts":..., "genres":[{id,title}]}
_genre_cache = {}
_genre_lock = threading.Lock()

# Custom playlists cache: key -> {"ts":..., "m3u8":str, "count":int, "title":str}
_custom_lock = threading.Lock()
_custom_cache = {}
CUSTOM_TTL = int(os.environ.get("CUSTOM_TTL", str(CACHE_TTL)))  # reuse cache TTL (8 min)

GENERATE_SECRET = (os.environ.get("GENERATE_SECRET") or "").strip()
ROOT = Path(__file__).parent
PLAYLISTS_DIR = ROOT / "playlists"
M3U_DIR = ROOT / "m3u"
PLAYLISTS_DIR.mkdir(exist_ok=True); M3U_DIR.mkdir(exist_ok=True)


def _run_generate(reason="manual"):
    """Run generate.py in a subprocess. Caller must hold _gen_lock."""
    global _gen_state
    _gen_state = {"running": True, "started": time.time(), "finished": 0, "rc": None, "log": ""}
    def _bg():
        global _gen_state
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        # Force PLAYLIST_BASE_URL to our own origin so generated m3u8 files link back to us.
        env["PLAYLIST_BASE_URL"] = PLAYLIST_BASE_URL or ""
        try:
            proc = subprocess.run([sys.executable, str(ROOT/"generate.py")], cwd=str(ROOT),
                                  env=env, capture_output=True, text=True, timeout=60*40)
            _gen_state = {"running": False, "started": _gen_state["started"],
                          "finished": time.time(), "rc": proc.returncode,
                          "log": (proc.stdout or "") + ("\n[STDERR]\n"+proc.stderr if proc.stderr else "")}
            log.info(f"[proxy] generate.py finished rc={proc.returncode} ({reason})")
        except Exception as e:
            _gen_state = {"running": False, "started": _gen_state["started"],
                          "finished": time.time(), "rc": -1, "log": str(e)}
            log.error(f"[proxy] generate.py crashed: {e}")
    threading.Thread(target=_bg, daemon=True).start()

def _startup_generate():
    """Generate playlists once on boot if none exist."""
    try:
        any_m3u8 = list(PLAYLISTS_DIR.rglob("*.m3u8")) + list(M3U_DIR.rglob("*.m3u"))
        if not any_m3u8:
            with _gen_lock:
                if not _gen_state.get("running"):
                    log.info("[boot] no playlists found — running generate.py in background")
                    _run_generate(reason="startup")
    except Exception as e:
        log.warning(f"[boot] startup generate failed: {e}")


def _get_client(name):
    """Return a healthy StalkerClient for `name`; re-creates if TTL expired or dead."""
    with _lock:
        cli = _clients.get(name)
        born = _client_born.get(name, 0)
        if cli is None or (time.time() - born) > AUTH_REFRESH:
            cfg = _load_portal_cfg(name)
            log.info(f"[proxy] creating client for portal={name} url={cfg['url']}")
            cli = StalkerClient(name, cfg["url"], cfg["mac"], proxies=_merged_proxies(cfg.get("proxies")))
            _clients[name] = cli
            _client_born[name] = time.time()
            _locks.setdefault(name, threading.Lock())
        return cli


def _load_portal_cfg(name):
    """Load a portal config from config.json (or defaults)."""
    cfg = load_config()
    for pc in cfg.get("portals", []):
        if pc.get("name") == name and pc.get("enabled", True) is not False:
            return pc
    # Fallback: allow default portals by name using env
    if name == "tatatv":
        return {"name":"tatatv","url":_env_portal_url(),"mac":_env_mac_addr(),"proxies":[]}
    if name == "jiotv":
        return {"name":"jiotv","url":os.environ.get("JIOTV_URL","http://jiotv.be/stalker_portal/c"),
                "mac":os.environ.get("JIOTV_MAC","00:1A:79:E6:6E:90"),"proxies":[]}
    abort(404, f"Unknown portal: {name}")


def _fresh_link(name, ch_id):
    """Return a fresh playable URL for ch_id. Uses cache if fresh enough."""
    key = (name, str(ch_id))
    now = time.time()
    cached = _cache.get(key)
    if cached and (now - cached["ts"]) < CACHE_TTL and cached.get("url"):
        return cached["url"]
    cli = _get_client(name)
    plock = _locks[name]
    with plock:
        # double-checked
        cached = _cache.get(key)
        if cached and (now - cached["ts"]) < CACHE_TTL and cached.get("url"):
            return cached["url"]
        # re-auth if client is stale
        if (now - _client_born.get(name,0)) > AUTH_REFRESH:
            try: cli._handshake()
            except Exception:
                # force new client next attempt
                _clients.pop(name, None)
                cli = _get_client(name)
        url = None
        try:
            url = cli.create_link(str(ch_id))
        except Exception as e:
            log.warning(f"[proxy] create_link failed for {name}/{ch_id}: {e}")
            # retry once with new client
            try:
                _clients.pop(name, None)
                cli = _get_client(name)
                url = cli.create_link(str(ch_id))
            except Exception as e2:
                log.error(f"[proxy] retry failed for {name}/{ch_id}: {e2}")
        if not url:
            abort(502, "Failed to obtain stream link from portal")
        _cache[key] = {"url": url, "ts": time.time()}
        log.info(f"[proxy] {name}/ch/{ch_id} -> {url[:90]}...")
        return url


# ---------- Playlist rewriter ----------
def rewrite_playlist(text: str, base: str) -> str:
    """Replace direct stream URLs in an existing .m3u8/.m3u with /w/ links pointing at us."""
    def sub(m):
        return m.group(0)  # we don't know portal/ch_id from arbitrary URLs; rely on generator
    return text


# ---------- Helpers for genres + custom playlists ----------
def _base_url():
    """Return our own public base URL (PLAYLIST_BASE_URL or guess from request)."""
    if PLAYLIST_BASE_URL: return PLAYLIST_BASE_URL.rstrip("/")
    return request.url_root.rstrip("/")

def _portal_genres(name):
    """Return [{"id":...,"title":...}] for a portal (cached 6h)."""
    now = time.time()
    with _genre_lock:
        c = _genre_cache.get(name)
        if c and (now - c["ts"]) < AUTH_REFRESH:
            return c["genres"]
    cli = _get_client(name)
    genres = []
    try:
        genres = cli.genres()
    except Exception as e:
        log.warning(f"[genres] {name} genres failed: {e}")
        # return stale cache if any
        with _genre_lock:
            c = _genre_cache.get(name)
            if c: return c["genres"]
        raise
    with _genre_lock:
        _genre_cache[name] = {"ts": now, "genres": genres}
    return genres

def _parse_ids(raw):
    """Accept comma/plus-separated id list like '23+440+88' or '23,440,*'."""
    if not raw: return []
    # support + , and space-separated
    parts = re.split(r"[+,;\s]+", str(raw))
    return [p.strip() for p in parts if p.strip()]

def _m3u_header(title, count, genres_text=""):
    import html as _h
    now_bd = datetime.now(__import__("zoneinfo").ZoneInfo("Asia/Dhaka")).strftime("%Y-%m-%d %I:%M:%S %p BD")
    lines = []
    attrs = ['url-tvg="https://www.open-epg.com/files/india.xml.gz,https://avkb.short.gy/epg.xml.gz,https://epgshare01.online/epgshare01/epg_ripper_IN1.xml.gz"',
             'refresh="600"', 'catchup="flussonic"']
    lines.append(f'#EXTM3U {" ".join(attrs)}')
    lines.append(f'#PLAYLIST:{title}')
    lines.append(f'#EXTGENRE:{genres_text}')
    lines.append(f'#AUTOR:KOBIR')
    lines.append(f'#BUILT:{now_bd}')
    lines.append(f'#CHANNELS:{count}')
    return "\n".join(lines) + "\n"

def _esc_attr(s):
    if s is None: return ""
    return str(s).replace('"',"'").replace("\n"," ").replace("\r"," ").strip()


# ---------- Routes ----------
@app.errorhandler(400)
def _e400(e): return _error_page(400)
@app.errorhandler(403)
def _e403(e): return _error_page(403)
@app.errorhandler(404)
def _e404(e): return _error_page(404)
@app.errorhandler(405)
def _e405(e): return _error_page(405)
@app.errorhandler(500)
def _e500(e): return _error_page(500)
@app.errorhandler(502)
def _e502(e): return _error_page(502)
@app.errorhandler(Exception)
def _e_all(e):
    # Last-resort catch-all so portal/auth errors never leak tracebacks.
    log.warning(f"[proxy] unhandled error: {e.__class__.__name__}: {e}")
    code = getattr(e, "code", 500)
    if not isinstance(code, int) or code < 400 or code > 599:
        code = 500
    return _error_page(code)


@app.route("/")
def root():
    # Build clickable list of generated m3u8 files
    m3u8 = sorted(p.relative_to(PLAYLISTS_DIR).as_posix()
                  for p in PLAYLISTS_DIR.rglob("*.m3u8"))
    m3u  = sorted(p.relative_to(M3U_DIR).as_posix()
                  for p in M3U_DIR.rglob("*.m3u"))
    return jsonify({
        "service": "iptv-live-proxy",
        "by": "KOBIR",
        "usage": "/w/<portal>/<ch_id>  (302 redirect to fresh stream)",
        "example": "/w/tatatv/11154  (ZEE BANGLA 4K)",
        "portals": [pc.get("name") for pc in load_config().get("portals", [])],
        "playlists": {
            "m3u8": m3u8[:40],
            "m3u":  m3u[:40],
        },
        "endpoints": {
            "stream": "/w/<portal>/<ch_id>",
            "download_m3u8": "/m3u8/<portal>/<path>",
            "download_m3u":  "/m3u/<portal>/<path>",
            "all_playlist": "/m3u8/tatatv/all.m3u8",
            "regenerate": "/generate (POST ?secret=...)",
            "status": "/gen_status",
        },
    })


@app.route("/gen_status")
def gen_status():
    return jsonify({
        "running": _gen_state.get("running"),
        "started": _gen_state.get("started"),
        "finished": _gen_state.get("finished"),
        "rc": _gen_state.get("rc"),
        "log_tail": (_gen_state.get("log") or "")[-2000:],
        "playlist_counts": {
            "m3u8": len(list(PLAYLISTS_DIR.rglob("*.m3u8"))),
            "m3u":  len(list(M3U_DIR.rglob("*.m3u"))),
        }
    })


@app.route("/generate", methods=["GET","POST"])
def gen_trigger():
    """Regenerate playlists. Protect with GENERATE_SECRET if set."""
    if GENERATE_SECRET:
        provided = (request.args.get("secret") or request.headers.get("X-Secret") or "")
        if provided != GENERATE_SECRET:
            return _error_page(403, "GENERATE key ta vhul — tui ke re?")
    with _gen_lock:
        if _gen_state.get("running"):
            return jsonify({"ok": False, "msg": "already running", "state": _gen_state})
        _run_generate(reason="web trigger")
    return jsonify({"ok": True, "msg": "generate started", "status": "/gen_status"})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "cached": len(_cache), "portals": list(_clients.keys()),
                    "custom": len(_custom_cache)})


@app.route("/genres")
@app.route("/genres/<portal>")
def genres_page(portal=None):
    """Human-friendly genre browser with checkbox builder for custom playlists."""
    import html as _h
    cfg = load_config()
    portals = [pc for pc in cfg.get("portals",[]) if pc.get("enabled", True) is not False]
    portal_names = [pc["name"] for pc in portals]
    if not portal: portal = portal_names[0] if portal_names else "tatatv"
    try:
        glist = _portal_genres(portal)
    except Exception as e:
        glist = []
    # separate the "*" (All) pseudo genre
    star = next((g for g in glist if g["id"]=="*"), None)
    rest = [g for g in glist if g["id"]!="*"]
    # build options
    opts = ""
    if star:
        opts += f'<label class="chip all"><input type="checkbox" class="gid" value="*" data-name="{_h.escape(star["title"])}"> ⭐ {_h.escape(star["title"])} (ALL)</label>\n'
    import re as _re
    def folder_of(t):
        up=t.upper()
        if "BANGLA" in up or "BENGALI" in up: return "01_Bangla"
        for kw,f in [("HINDI","02_Hindi"),("URDU","03_Urdu"),("PUNJABI","04_Punjabi"),
                     ("TAMIL","05_Tamil"),("TELUGU","06_Telugu"),("MARATHI","07_Marathi"),
                     ("GUJARATI","08_Gujarati"),("MALAYALAM","09_Malayalam"),("KANNADA","10_Kannada"),
                     ("ENGLISH","12_English"),("KIDS","13_Kids"),("ANIMATION","13_Kids"),
                     ("SPORTS","14_Sports"),("CRICKET","14_Sports"),
                     ("4K","16_4K"),("ADULT","17_Adult"),("NEWS","19_News"),("MOVIE","21_Movies")]:
            if kw in up: return f"{f}"
        return "22_Others"
    # group by folder
    from collections import defaultdict
    buckets = defaultdict(list)
    for g in rest: buckets[folder_of(g["title"])].append(g)
    for folder in sorted(buckets.keys()):
        opts += f'<h4>📁 {_h.escape(folder)}</h4>\n<div class="grid">\n'
        for g in sorted(buckets[folder], key=lambda x:x["title"].upper()):
            opts += (f'  <label class="chip"><input type="checkbox" class="gid" value="{g["id"]}" '
                     f'data-name="{_h.escape(g["title"])}"> <code>{g["id"]}</code> {_h.escape(g["title"])}</label>\n')
        opts += "</div>\n"
    portal_tabs = "".join(
        f'<a class="tab {"on" if p==portal else ""}" href="/genres/{_h.escape(p)}">{_h.escape(p)}</a>'
        for p in portal_names)
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>KOBIR — Genres / Custom Builder</title>
<style>
body{{background:#0b0f19;color:#e2e8f0;font-family:system-ui;max-width:1100px;margin:1.5rem auto;padding:0 1rem}}
h1{{color:#fbbf24;margin-bottom:.3rem}}h2{{color:#a3e635}}h3{{color:#38bdf8}}h4{{color:#f472b6;margin:1rem 0 .4rem}}
a{{color:#38bdf8}}
.tabs{{margin:.6rem 0 1rem;display:flex;gap:6px;flex-wrap:wrap}}
.tab{{background:#1e293b;color:#cbd5e1;padding:6px 14px;border-radius:999px;text-decoration:none}}
.tab.on{{background:#fbbf24;color:#000;font-weight:bold}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:6px}}
.chip{{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:7px 10px;display:flex;gap:8px;align-items:center;font-size:.9rem;cursor:pointer;user-select:none}}
.chip:hover{{border-color:#fbbf24}}
.chip.all{{background:#332a00;border-color:#fbbf24;color:#fde047;font-weight:bold}}
.chip input{{accent-color:#fbbf24}}
code{{color:#fbbf24;font-size:.8rem}}
.toolbar{{position:sticky;top:0;background:#0b0f19e6;backdrop-filter:blur(6px);padding:10px 0;border-bottom:1px solid #1e293b;margin-bottom:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}}
.btn{{background:#fbbf24;color:#000;padding:10px 18px;border-radius:8px;font-weight:bold;border:0;cursor:pointer;text-decoration:none;display:inline-block}}
.btn.sec{{background:#334155;color:#e2e8f0}}
.btn.ban{{background:#ef4444;color:#fff}}
.mini{{color:#94a3b8;font-size:.85rem;margin-left:auto}}
.preview{{background:#0f172a;border:1px dashed #334155;border-radius:8px;padding:10px;font-family:ui-monospace,monospace;font-size:.82rem;word-break:break-all;margin-top:.6rem;display:none}}
.note{{color:#94a3b8;font-size:.88rem}}
b.kw{{color:#fbbf24}}
</style></head><body>
<h1>🎛️ KOBIR — Genre Picker &amp; Custom Playlist Builder</h1>
<p class="note">Genre-gulo te tick mark diye <b class="kw">Build Custom Playlist</b> e click korun — selected genre er channel niye ektai <code>.m3u8</code> banabe.
Example: <code>23+440+88</code> (BANGLA-TV + BANGLA-PREMIUM + CRICKET). Link-ta TiviMate-e direct add korun — kokhono expire hobe na.</p>
<div class="tabs">{portal_tabs}
   <a class="tab" href="/playlists">📂 My Playlists</a>
   <a class="tab" href="/">🏠 API</a>
</div>
<div class="toolbar">
  <button class="btn" id="build">🔗 Build Custom Playlist</button>
  <button class="btn sec" id="selbangla">🇧🇩 Bangla</button>
  <button class="btn sec" id="selsports">🏏 Sports/Cricket</button>
  <button class="btn sec" id="selhindi">🎬 Hindi</button>
  <button class="btn ban" id="selnone">✖ Clear</button>
  <span class="mini" id="count">0 genres selected</span>
</div>
<div class="preview" id="preview"></div>
<h2>Genres — {_h.escape(portal)} ({len(rest)+(1 if star else 0)})</h2>
{opts}
<script>
const portal = {_h.escape(json.dumps(portal))};
function updateCount(){{
  const n=[...document.querySelectorAll('.gid:checked')].length;
  document.getElementById('count').textContent = n+' genres selected';
}}
document.querySelectorAll('.gid').forEach(c=>c.addEventListener('change',updateCount));
function ids(){{ return [...document.querySelectorAll('.gid:checked')].map(c=>c.value).join('+'); }}
document.getElementById('build').addEventListener('click',()=>{{
  const g=ids(); if(!g){{alert('Ekta genre to select korun!'); return;}}
  const url = '/custom/'+portal+'/'+g+'/custom.m3u8';
  const full = location.origin+url;
  const pv=document.getElementById('preview');
  pv.style.display='block';
  pv.innerHTML='<b style="color:#a3e635">✅ Ready — TiviMate-e ei link add korun:</b><br><br>'+
               '<a href="'+url+'" style="color:#38bdf8">'+full+'</a>'+
               '<br><br><span class="note">Open in TiviMate: copy this URL, add as M3U playlist. Channel count live dekhabe jokhon build complete hobe.</span>';
  window.open(url,'_blank');
}});
document.getElementById('selnone').addEventListener('click',()=>{{document.querySelectorAll('.gid').forEach(c=>c.checked=false);updateCount();}});
function selectByKeywords(kws){{
  document.querySelectorAll('.gid').forEach(c=>{{
    const n=(c.dataset.name||'').toUpperCase();
    c.checked = kws.some(k=>n.includes(k));
  }}); updateCount();
}}
document.getElementById('selbangla').addEventListener('click',()=>selectByKeywords(['BANGLA','BENGALI']));
document.getElementById('selhindi').addEventListener('click',()=>selectByKeywords(['HINDI']));
document.getElementById('selsports').addEventListener('click',()=>selectByKeywords(['SPORTS','CRICKET','4K']));
updateCount();
</script>
<hr><small>🇧🇩 KOBIR x ENI · Eternal Proxy · Channels load on first hit (8-min cache)</small>
</body></html>"""
    return Response(page, mimetype="text/html")


@app.route("/genres.json")
@app.route("/genres.json/<portal>")
def genres_json(portal=None):
    cfg = load_config()
    portals = [pc["name"] for pc in cfg.get("portals",[]) if pc.get("enabled", True) is not False]
    if not portal: portal = portals[0] if portals else "tatatv"
    try:
        return jsonify({"portal": portal, "genres": _portal_genres(portal)})
    except Exception as e:
        return jsonify({"portal": portal, "error": str(e), "genres": []}), 502


def _build_custom_m3u8(portal, id_list):
    """Actually fetch channels for given genre ids and build an m3u8 string."""
    cli = _get_client(portal)
    base = _base_url()
    channels = []
    fetched_genres = []
    target_ids = set(id_list)
    include_all = "*" in target_ids
    try:
        all_genres = cli.genres()
    except Exception as e:
        raise
    # If "*", expand to all genres (filtered by blocklist is already server's job;
    # we just use all genres except blocklisted-by-user ones can be left to /generate)
    if include_all:
        target_genres = [g for g in all_genres if g["id"]!="*"]
    else:
        target_genres = [g for g in all_genres if g["id"] in target_ids]
    seen = set()
    for g in target_genres:
        try:
            raw = cli.channels(g["id"])
        except Exception as e:
            log.warning(f"[custom] {portal} genre {g['id']} ({g['title']}) failed: {e}")
            continue
        for ch in raw:
            cid = str(ch.get("id") or "")
            if not cid or cid in seen: continue
            seen.add(cid)
            nm = (ch.get("name") or "").strip()
            if not nm: continue
            logo_hint = ch.get("logo") or ""
            origin = f"{cli.proto}://{cli.host}" + (f":{cli.port}" if cli.port not in (80,443) else "")
            logo = (logo_hint if logo_hint.startswith("http")
                    else f"{origin}/stalker_portal/misc/avatar/{logo_hint.lstrip('/')}") if logo_hint else ""
            tvg = ""
            channels.append({
                "name": nm,
                "logo": logo,
                "group": g["title"],
                "url": f"{base}/w/{portal}/{cid}",
            })
        fetched_genres.append(g["title"])
    channels.sort(key=lambda c: c["name"].lower())
    # build m3u8
    genre_text = ", ".join(fetched_genres)[:180]
    out = [_m3u_header(f"KOBIR — {portal} custom [{genre_text}]", len(channels), genre_text)]
    for c in channels:
        # catchup-source MUST end with the channel's own base path + a trailing slash
        # (TiviMate appends ?utc=...&duration=... to whatever is here). Using /w/<portal>/
        # without the ch_id breaks catchup — it needs /w/<portal>/<ch_id>/ so the server
        # can see which channel's timeshift to serve.
        catchup_base = c["url"].rstrip("/") + "/"
        attrs = [f'tvg-name="{_esc_attr(c["name"])}"',
                 f'group-title="{_esc_attr(c["group"])}"',
                 'catchup="flussonic"',
                 f'catchup-source="{_esc_attr(catchup_base)}"']
        if c["logo"]: attrs.append(f'tvg-logo="{_esc_attr(c["logo"])}"')
        out.append(f'#EXTINF:-1 {" ".join(attrs)},{_esc_attr(c["name"])}\n{c["url"]}\n')
    return "".join(out), channels, fetched_genres


@app.route("/custom/<portal>/<ids>/<path:name>")
def custom_playlist(portal, ids, name="custom.m3u8"):
    """Dynamically build a custom combined playlist from selected genre IDs.
    URLs are /w/... proxy links — never expire.
    Example: /custom/tatatv/23+440+88/bangla.m3u8
    """
    # reject unknown portals (but allow names from config)
    cfg = load_config()
    ok_names = {pc["name"] for pc in cfg.get("portals",[]) if pc.get("enabled", True) is not False}
    if portal not in ok_names: abort(404)
    id_list = _parse_ids(ids)
    if not id_list: abort(400, "no genre ids given")
    # normalize name -> force .m3u8 suffix
    safe_name = name or "custom.m3u8"
    if not (safe_name.lower().endswith(".m3u8") or safe_name.lower().endswith(".m3u")):
        safe_name += ".m3u8"
    mime = "audio/x-mpegurl" if safe_name.lower().endswith(".m3u") else "application/vnd.apple.mpegurl"
    key = (portal, "+".join(sorted(id_list)))
    now = time.time()
    with _custom_lock:
        cc = _custom_cache.get(key)
        if cc and (now - cc["ts"]) < CUSTOM_TTL:
            m3u = cc["m3u8"]
        else:
            m3u = None
    if m3u is None:
        try:
            m3u, channels, gnames = _build_custom_m3u8(portal, id_list)
        except Exception as e:
            log.error(f"[custom] build failed for {portal}/{ids}: {e}")
            return _error_page(502, f"Custom playlist build failed: {e}"), 502
        with _custom_lock:
            _custom_cache[key] = {"ts": time.time(), "m3u8": m3u}
    resp = Response(m3u, mimetype=mime)
    # Cache on proxy (8 min) but let player re-request reasonably quickly
    resp.headers["Cache-Control"] = f"public, max-age={CACHE_TTL//2}"
    resp.headers["X-Custom-Portal"] = portal
    resp.headers["X-Custom-Genres"] = ids[:200]
    resp.headers["Content-Disposition"] = f'inline; filename="{safe_name}"'
    return resp


@app.route("/w/<portal>/<ch_id>")
def watch(portal, ch_id):
    """Redirect to a fresh stream URL."""
    # ch_id may look like "11154" or "ch/11154" or "11154-video" — strip non-numeric
    m = re.search(r"(\d+)", ch_id)
    if not m: abort(400, "bad ch_id")
    cid = m.group(1)
    url = _fresh_link(portal, cid)
    # 302 (temporary) so players always re-hit us, getting fresh token
    return redirect(url, code=302)


@app.route("/m3u8/<path:rest>")
def serve_m3u8(rest):
    base = PLAYLISTS_DIR
    target = (base / rest).resolve()
    if not str(target).startswith(str(base.resolve())): abort(404)
    if not target.exists(): abort(404)
    return send_from_directory(base, rest, mimetype="application/vnd.apple.mpegurl")

@app.route("/m3u/<path:rest>")
def serve_m3u(rest):
    base = M3U_DIR
    target = (base / rest).resolve()
    if not str(target).startswith(str(base.resolve())): abort(404)
    if not target.exists(): abort(404)
    return send_from_directory(base, rest, mimetype="audio/x-mpegurl")

@app.route("/playlists")
def list_playlists():
    """Human-friendly HTML index of all playlists, grouped by folder (genre/category)."""
    import html as _html
    def collect(base):
        """Return {folder: [(label, rel_url, count)]}"""
        groups = {}
        for p in sorted(base.rglob("*.m3u8"), key=lambda x: x.as_posix().lower()):
            rel = p.relative_to(base).as_posix()
            parts = rel.split("/")
            folder = parts[1] if len(parts) >= 3 else "(root)"
            portal = parts[0] if parts else ""
            try:
                count = sum(1 for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.startswith("#EXTINF"))
            except Exception:
                count = -1
            label = parts[-1]
            groups.setdefault((portal, folder), []).append((label, rel, count))
        return groups
    m3u8_groups = collect(PLAYLISTS_DIR)
    m3u_groups  = collect(M3U_DIR)
    def render_groups(groups, url_prefix):
        if not groups:
            return "<p style='color:#94a3b8'>Kichui nei — <a href='/generate'>/generate</a> diye build koren first-e.</p>"
        out=[]
        portals = sorted({p for p,_ in groups.keys()})
        for portal in portals:
            out.append(f"<h3>🔹 {_html.escape(portal)}</h3>")
            for (po,folder),items in sorted(groups.items(), key=lambda kv:kv[0][1]):
                if po!=portal: continue
                out.append(f"<h4>📁 {_html.escape(folder)} <span style='color:#64748b;font-weight:normal'>({len(items)})</span></h4><ul>")
                for label,rel,count in items:
                    cnt = f"{count} ch" if count>=0 else "?"
                    out.append(f"<li><a href='{_html.escape(url_prefix)}/{_html.escape(rel)}'>{_html.escape(label)}</a> <span style='color:#64748b;font-size:.85em'>[{_html.escape(cnt)}]</span></li>")
                out.append("</ul>")
        return "".join(out)
    running = _gen_state.get("running", False)
    status_color = '#ef4444' if running else '#22c55e'
    status_text  = '🔄 GENERATING…' if running else '✅ READY'
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>KOBIR IPTV — Playlists</title>
<style>
body{{background:#0b0f19;color:#e2e8f0;font-family:system-ui;max-width:960px;margin:2rem auto;padding:0 1rem}}
h1{{color:#fbbf24}}h2{{color:#a3e635;border-bottom:1px solid #334155;padding-bottom:6px}}
h3{{color:#38bdf8;margin-top:1.4rem}}h4{{color:#f472b6;margin-bottom:4px}}
a{{color:#38bdf8;text-decoration:none}}a:hover{{text-decoration:underline;color:#fde047}}
.status{{background:#1e293b;padding:12px 14px;border-radius:8px;margin:1rem 0;font-family:ui-monospace,monospace;display:flex;align-items:center;gap:10px}}
.dot{{width:10px;height:10px;border-radius:50%;background:{status_color};box-shadow:0 0 10px {status_color};{'animation:pulse 1s infinite' if running else ''}}}
.btn{{background:#fbbf24;color:#000;padding:8px 14px;border-radius:6px;font-weight:bold;margin-right:8px;display:inline-block}}
.btn.sec{{background:#334155;color:#e2e8f0}}
ul{{margin:.3rem 0 1rem 1.2rem;padding:0}}li{{margin:3px 0}}
small{{color:#64748b}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.3}}}}
hr{{border:0;border-top:1px dashed #334155;margin:2rem 0}}
</style></head><body>
<h1>📺 KOBIR IPTV PRO  <small style='color:#64748b'>Eternal Proxy Mode</small></h1>
<div class='status'><span class='dot'></span><b>{status_text}</b>
   <a class='btn' href='/generate'>🔄 Regenerate</a>
   <a class='btn sec' href='/gen_status'>Status log</a>
   <a class='btn sec' href='/healthz'>Health</a>
   <a class='btn sec' href='/'>API</a>
</div>
<p style='color:#94a3b8'><b>PLAYLIST_BASE_URL:</b> {_html.escape(PLAYLIST_BASE_URL or "(not set — direct/expiring links)")}</p>
<p style='color:#94a3b8'>Eta diye TiviMate / Perfect Player / VLC-te add korun. Link-gula kokhono expire hobe na — proxy live token serve korbe.</p>
<h2>🎬 HLS (.m3u8) <small>— TiviMate / Perfect Player / IPTV Smarters</small></h2>
{render_groups(m3u8_groups, '/m3u8')}
<hr>
<h2>🎞️ VLC (.m3u) <small>— desktop VLC / Kodi</small></h2>
{render_groups(m3u_groups, '/m3u')}
<hr><small>🇧🇩 Made in Bangladesh by KOBIR x ENI · Shob channel sorted by genre · Proxy auto-refresh 8 min</small>
</body></html>"""
    return Response(page, mimetype="text/html")


def main():
    log.info(f"[proxy] starting on port {PORT}  CACHE_TTL={CACHE_TTL}s  PLAYLIST_BASE_URL={PLAYLIST_BASE_URL}")
    # (warm clients + startup-generate are triggered via @app.before_request to work
    #  under gunicorn which imports the module rather than calling main().)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
