# 📺 TataTV IPTV — Auto-Updating M3U Playlists

প্রফেশনাল আইপিটিভি প্লেলিস্ট জেনারেটর। তিনভাবে ডিপ্লয় করতে পারো:

1. **GitHub Actions** (ফ্রি) — প্রতি ৬ ঘণ্টায় cron, raw M3U8 raw link serve করে
2. **Render.com** (free tier) — ডকার কন্টেইনারে ওয়েব সার্ভার + শিডিউল্ড রিফ্রেশ
3. **Railway.app** — একই ডকার ইমেজ, এক-ক্লিক ডিপ্লয়

## ✨ ফিচার
- 🔄 **অটো রিফ্রেশ** প্রতি ৬ ঘণ্টায় (Flask + APScheduler ওয়েব সার্ভারে)
- 📡 **মাল্টি-পোর্টাল / মাল্টি-MAC** — `config.json`-এ যত খুশি portal+MAC+প্রক্সি
- 🌐 **SOCKS5/HTTP প্রক্সি** প্রতি portal আলাদা (Cloudflare ব্লক পার করার জন্য)
- 📺 **EPG + TVG লোগো** — পোর্টাল logo + বাহিরের EPG সোর্স + fuzzy match + manual aliases
- ⏪ **Catchup/timeshift** (flussonic) ট্যাগ — Tivimate/Mytvonline-এ রিপ্লে কাজ করে
- ⭐ **ফেভারিট প্লেলিস্ট** (নিজের পছন্দের চ্যানেল আলাদা ফাইল)
- 🎞️  **.m3u (VLC)** + **.m3u8 (HLS)** দুই ফরম্যাট
- 📋 **JSON catalog** (API/Web UI-র জন্য)
- 🌍 **index.html** — ব্রাউজারে এক ক্লিকে প্লেলিস্ট বাছাই
- ⚡  Flask/Gunicorn ওয়েব সার্ভার সহ
- 🔐 `/refresh` এন্ডপয়েন্ট টোকেন দিয়ে সুরক্ষিত
- 💚 `/healthz` হেলথচেক

## 📂 আউটপুট স্ট্রাকচার
```
playlists/
├── index.html
├── all.m3u8
└── tatatv/
    ├── all.m3u8
    ├── catalog.json
    ├── fav_Bangla_Trending.m3u8
    ├── 00_All/00_All_Channels.m3u8
    └── 01_Bangla/BANGLA - TV.m3u8
m3u/                       ← একই, .m3u এক্সটেনশনে
```

## 🚀 ডিপ্লয়
### Render.com (সবচেয়ে সহজ)
1. এই repo তে Push করো
2. Render → New → Blueprint → repo সিলেক্ট করো (`render.yaml` auto-detect করবে)
3. Environment Variables সেট করো:
   - `MAC_ADDR` = তোমার MAC
   - `SOCKS_PROXY` = `socks5h://test:test@203.96.226.98:1088`
4. `REFRESH_TOKEN` auto-jenerate হবে
5. ডিপ্লয় হওয়ার পর ২-৫ মিনিটে প্রথম রিফ্রেশ কমপ্লিট হবে
6. ব্রাউজে `https://<service>.onrender.com/` এ গেলে playlist list পাবে

### Railway.app
1. New Project → Deploy from Dockerfile
2. repo সিলেক্ট করো (Dockerfile auto-detect)
3. Variables সেট করো (উপরের মতো MAC_ADDR, SOCKS_PROXY, PORT=8080)
4. ডিপ্লয় → `https://<app>.up.railway.app/`

### GitHub Actions
1. repo তে পুশ করো
2. Settings → Secrets এ `SOCKS_PROXY` দাও
3. Actions enable করো
4. প্রতি ৬ ঘণ্টায় auto-commit প্লেলিস্ট
5. (Optional) Settings → Pages → Source: GitHub Actions

### লোকালি / VPS
```bash
pip install -r requirements.txt
cp config.example.json config.json   # চাইলে এডিট করো
# ওয়েব সার্ভার:
gunicorn -b 0.0.0.0:8080 --workers 1 --threads 8 --timeout 600 app:app
# শুধু একবার জেনারেট:
python generate.py
```

## 🔑 Environment Variables
| Var | Default | বর্ণনা |
|---|---|---|
| `PORT` | `8080` | ওয়েব সার্ভার পোর্ট |
| `REFRESH_HOURS` | `6` | কত ঘণ্টা পর পর refresh |
| `REFRESH_TOKEN` | (none) | `/refresh` trigger করার টোকেন |
| `PORTAL_URL` | `http://tatatv.cc/stalker_portal/c/` | |
| `MAC_ADDR` | `00:1A:79:00:8C:32` | MAC address |
| `SOCKS_PROXY` | (none) | SOCKS5/HTTP proxy |
| `MAX_WORKERS` | `20` | কনকারেন্ট লিঙ্ক ফেচার |
| `DRY_RUN` | (unset) | শুধু BANGLA - TV জেনার টেস্ট করো |

## 🌐 Endpoints (web server)
| Path | বর্ণনা |
|---|---|
| `/` | index.html এ রিডাইরেক্ট |
| `/playlists/...` | HLS .m3u8 ফাইলসমূহ |
| `/m3u/...` | VLC .m3u ফাইলসমূহ |
| `/healthz` | JSON health status (`last_refresh` info সহ) |
| `/refresh?token=XXX` | ম্যানুয়াল রিফ্রেশ (ট্রিগার, background-এ চলে) |

## ⚙️ config.json (মাল্টি-পোর্টাল)
`config.example.json` কপি করে `config.json` বানাও:
```json
{
  "portals": [
    {
      "name": "tatatv",
      "url": "http://tatatv.cc/stalker_portal/c/",
      "mac": "00:1A:79:00:8C:32",
      "proxy": "socks5h://user:pass@host:port",
      "epg_id_prefix": "in."
    }
  ],
  "epg_sources": [],
  "aliases": {
    "ZEE BANGLA 4K": {"id": "zeebangla.in", "logo": "https://example.com/zb.png"}
  },
  "favorites": {
    "Bangla_Trending": ["ZEE BANGLA 4K", "STAR JALSHA HD", "COLORS BANGLA HD"]
  },
  "output": {"vlc_m3u": true, "m3u8_hls": true, "json_metadata": true, "index_html": true}
}
```

## 📝 নোট
- প্রথম রিফ্রেশ ২-৫ মিনিট লাগতে পারে (৭০০০+ চ্যানেল)
- টোকেন ৬-১২ ঘণ্টা বৈধ
- Catchup শুধুমাত্র Flussonic সার্ভারে কাজ করে (tatatv-তে ফ্লুসোনিক)
- কোনো চ্যানেল লোগো/epg মিসিং হলে `aliases`-এ ম্যাপ করো
- Free tier-এ Render/Railway কিছুক্ষণ ইনঅ্যাকটিভ হলে স্লিপ করে, প্রথম রিকোয়েস্টে জেগে ওঠে — UptimeRobot দিয়ে ৫ মিনিটে /healthz পিং করালে ২৪/৭ চলবে
