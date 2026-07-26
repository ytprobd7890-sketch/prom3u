"""
TataTV IPTV — Self-hosted web server for Render/Railway.
- Serves generated M3U/M3U8 playlists + index.html
- Auto-refreshes every 6 hours via APScheduler
- /refresh endpoint to trigger a manual refresh
- /healthz for uptime checks
"""
import os
import sys
import time
import logging
import threading
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, send_from_directory, redirect, jsonify, abort
from apscheduler.schedulers.background import BackgroundScheduler

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "playlists"
M3U_DIR = ROOT / "m3u"
OUT_DIR.mkdir(exist_ok=True)
M3U_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tata-iptv")

REFRESH_HOURS = int(os.environ.get("REFRESH_HOURS", "6"))
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")  # protect /refresh
PORT = int(os.environ.get("PORT", "8080"))

app = Flask(__name__, static_folder=None)

# Lock so concurrent /refresh doesn't race with the scheduler
_refresh_lock = threading.Lock()
_last_refresh = {"at": None, "channels": 0, "ok": False, "error": None}


def do_refresh():
    """Run generate.py as a subprocess (clean globals, fresh every refresh)."""
    import subprocess
    with _refresh_lock:
        _last_refresh["at"] = datetime.now(timezone.utc).isoformat()
        _last_refresh["error"] = None
        _last_refresh["channels"] = 0
        t0 = time.time()
        try:
            log.info("Refresh started")
            result = subprocess.run(
                [sys.executable, str(ROOT / "generate.py")],
                cwd=str(ROOT),
                capture_output=True, text=True, timeout=20*60,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            if result.returncode != 0:
                raise RuntimeError(f"generate.py exited {result.returncode}: {result.stderr[-500:]}")
            # Count channels from all catalog.json
            total = 0
            seen = set()
            for jf in OUT_DIR.rglob("catalog.json"):
                try:
                    data = __import__("json").loads(jf.read_text())
                    for ch in data.get("channels", []):
                        k = ch.get("name","").upper()
                        if k and k not in seen:
                            seen.add(k); total += 1
                except Exception as e:
                    log.warning(f"Cannot read {jf}: {e}")
            _last_refresh["channels"] = total
            _last_refresh["ok"] = True
            log.info(f"Refresh done in {time.time()-t0:.1f}s — {total} unique channels")
        except Exception as e:
            _last_refresh["ok"] = False
            _last_refresh["error"] = str(e)
            log.exception("Refresh failed")


def _safe_join(directory: Path, req_path: str) -> Path:
    base = directory.resolve()
    target = (directory / req_path.lstrip("/")).resolve()
    if not str(target).startswith(str(base)):
        abort(404)
    return target


@app.route("/")
def root():
    return redirect("/playlists/index.html")


@app.route("/healthz")
def health():
    return jsonify({
        "ok": _last_refresh.get("ok", True),
        "last_refresh": _last_refresh,
        "service": "tata-iptv",
    })


@app.route("/refresh", methods=["GET", "POST"])
def refresh():
    token = _last_refresh.get("token") or ""
    if REFRESH_TOKEN:
        auth = request.args.get("token", "") if "request" in globals() else ""
        # Lazy import for token auth
        from flask import request
        auth = request.args.get("token", "") or request.headers.get("X-Refresh-Token", "")
        if auth != REFRESH_TOKEN:
            abort(401)
    if _refresh_lock.locked():
        return jsonify({"status": "already_running"})
    # Run in a thread so HTTP returns immediately
    threading.Thread(target=do_refresh, daemon=True).start()
    return jsonify({"status": "started"})


def serve_dir(base: Path, url_prefix: str, name: str):
    @app.get(url_prefix, endpoint=f"{name}_root")
    @app.get(url_prefix + "/", endpoint=f"{name}_idx")
    def idx():
        idx_file = base / "index.html"
        if idx_file.exists():
            return send_from_directory(base, "index.html")
        return jsonify({"files": sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())})

    @app.get(url_prefix + "/<path:req_path>", endpoint=f"{name}_file")
    def files(req_path):
        target = _safe_join(base, req_path)
        if not target.exists() or not target.is_file():
            abort(404)
        return send_from_directory(base, req_path)

serve_dir(OUT_DIR, "/playlists", "playlists")
serve_dir(M3U_DIR, "/m3u", "m3u")


def main():
    log.info("Starting TataTV IPTV server on port %d", PORT)
    # Initial refresh (in background so server can start immediately)
    threading.Thread(target=do_refresh, daemon=True).start()
    # Scheduler: refresh every N hours
    sched = BackgroundScheduler(daemon=True, timezone="UTC")
    sched.add_job(do_refresh, "interval", hours=REFRESH_HOURS,
                  next_run_time=None, id="refresh", max_instances=1)
    sched.start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
