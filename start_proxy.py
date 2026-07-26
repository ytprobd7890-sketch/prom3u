#!/usr/bin/env python3
"""
Run the live-link proxy AND generate-on-startup.

Use this on Render/Railway/local to serve forever-working playlist + stream URLs.

Set PLAYLIST_BASE_URL to your server's public URL BEFORE generating playlists so
the generated .m3u8 files contain /w/... URLs pointing back at this proxy.
"""
import os, sys, threading, time, logging
from pathlib import Path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("start")

def bg_generate():
    """Run generate.py once at boot so /playlists/ is populated."""
    time.sleep(3)
    import subprocess
    try:
        log.info("[boot] running generate.py to build initial playlists...")
        r = subprocess.run([sys.executable, str(ROOT/"generate.py")],
                           cwd=str(ROOT), timeout=30*60,
                           env={**os.environ, "PYTHONUNBUFFERED":"1"})
        log.info(f"[boot] generate.py exited with code {r.returncode}")
    except Exception as e:
        log.error(f"[boot] generate.py failed: {e}")

if __name__ == "__main__":
    from proxy import app, PORT
    threading.Thread(target=bg_generate, daemon=True).start()
    log.info(f"Starting proxy on 0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
