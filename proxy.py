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
import os, re, sys, time, json, threading, logging, random
from pathlib import Path
from urllib.parse import quote
from datetime import datetime
import requests as _req
from flask import Flask, redirect, abort, Response, jsonify, request

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

# ---------- Shared state ----------
_clients = {}          # portal_name -> StalkerClient
_client_born = {}      # portal_name -> timestamp
_cache = {}            # (portal, ch_id) -> {"url":..., "ts":...}
_locks = {}            # portal_name -> threading.Lock (serialize create_link per portal)
_lock = threading.Lock()


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
    # require a magic header? No — let root be a friendly JSON (still shows ASCII if they hit wrong)
    return jsonify({
        "service": "iptv-live-proxy",
        "usage": "/w/<portal>/<ch_id>",
        "example": "/w/tatatv/11154  (ZEE BANGLA 4K)",
        "portals": [pc.get("name") for pc in load_config().get("portals", [])],
        "by": "KOBIR",
    })


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "cached": len(_cache), "portals": list(_clients.keys())})


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


@app.route("/m3u8/<portal>/<path:rest>")
@app.route("/m3u/<portal>/<path:rest>")
def serve_static_gen(portal, rest):
    """Serve the generated playlists (read from playlists/ or m3u/).
    When PLAYLIST_BASE_URL is set, the generator already wrote /w/ links;
    otherwise we serve as-is."""
    from flask import send_from_directory
    base = OUT_DIR = Path(__file__).parent / ("playlists" if request.path.startswith("/m3u8") else "m3u")
    target = (base / rest).resolve()
    if not str(target).startswith(str(base.resolve())): abort(404)
    if not target.exists(): abort(404)
    return send_from_directory(base, rest)


def main():
    log.info(f"[proxy] starting on port {PORT}  CACHE_TTL={CACHE_TTL}s  PLAYLIST_BASE_URL={PLAYLIST_BASE_URL}")
    # Pre-warm clients in background
    def warm():
        time.sleep(2)
        for pc in load_config().get("portals", []):
            if pc.get("enabled", True) is False: continue
            try: _get_client(pc["name"])
            except Exception as e: log.warning(f"[proxy] warmup failed for {pc['name']}: {e}")
    import threading
    threading.Thread(target=warm, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
