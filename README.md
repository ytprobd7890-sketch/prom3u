# 📺 KOBIR IPTV PRO — Eternal Stalker Proxy + Auto-Updating M3U Playlists

> 🇧🇩 **বস, এইটা তোমার জন্য বানানো ফাইনাল প্রো ভার্সন।** কখনো টোকেন এক্সপায়ার হবে না, যত খুশি কাস্টম প্লেলিস্ট বানাবে — জীবনভর চলবে।
>
> by **KOBIR × ENI** <3

## ✨ ফিচার (প্রো ভার্সন)

- 🔁 **Eternal Proxy Mode** — `PLAYLIST_BASE_URL` দিলে M3U-তে ডাইরেক্ট সিডিএন লিঙ্ক না দিয়ে আমাদের `/w/<portal>/<ch_id>` ইউআরএল দেবে। প্লেয়ারে প্লে করার সময় সার্ভার দিব্যি ফ্রেশ টোকেন জেনারেট করে 302 রিডাইরেক্ট করবে — **টোকেন ১৫ মিনিটে মরলেও প্লেলিস্ট মরবে না**।
- 🎛️ **Genre Picker UI** — `/genres` (বা `/genres/tatatv`, `/genres/jiotv`) এ গিয়ে চেকবক্সে টিক দিয়ে ওয়ান-ক্লিকে কাস্টম প্লেলিস্ট বানানো যায় (`/custom/<portal>/<ids>/<name>.m3u8`, যেমন `/custom/tatatv/23+440+88/bangla-sports.m3u8`)।
- 🌐 **ডুয়াল পোর্টাল সাপোর্ট** — `tatatv.cc` + `jiotv.be` ডিফল্ট, চাইলে config.json-এ যত খুশি পোর্টাল যোগ করবে।
- 📡 **৩-সোর্স মার্জড EPG** — open-epg + avkb + epgshare01 → ৩৮০০+ ইউনিক চ্যানেল, ফাজি ম্যাচ + ম্যানুয়াল এলিয়াস সহ।
- 🧠 **1-based pagination + duplicate-free** — পুরনো বাগ (একই চ্যানেল বারবার) ফিক্সড।
- 🔐 **Stalker auth bug FIXED** — আগে `get_genres`/`get_ordered_list`-এ `Authorization failed.` দিতো; কারণ `token` cookie domain/path বাইন্ড না করায় Ministra reject করতো। এখন ঠিক আছে — `token` cookie `domain=<host>, path=/`-এ সেট হবে, আর live session রিইউজ হবে ফাস্টপ্যাথে।
- ⏪ **Catchup="flussonic"** সঠিক চ্যানেল-আইডিসহ (TiviMate রিপ্লে কাজ করবে)।
- 💀 **হ্যাকার ASCII error page** — সব 4xx/5xx-এ M$L স্কাল, BD সময়, IP/UA/Path দেখাবে; কোনো traceback লিক হবে না।
- 🧊 ৮ মিনিট CDN ক্যাশ + ৬ ঘণ্টা auth রিফ্রেশ; gunicorn threaded মোডে ঠিকমতো cold-start হবে।
- 🗂️ জেন্র অনুযায়ী ফোল্ডার: 01_Bangla, 02_Hindi, 14_Sports, 16_4K, 22_Others…
- ⭐ ফেভারিট প্লেলিস্ট (Bangla Trending, Sports)
- 🎞️ `.m3u8` (HLS, TiviMate/IPTV Smarters) + `.m3u` (VLC) দুই ফরম্যাট
- 🧾 JSON catalog
- 🚀 Render/Railway/Docker/GitHub Actions সব জায়গায় এক-ক্লিক ডিপ্লয়

## 🏃 দ্রুত শুরু (Render.com — সবচেয়ে সহজ)

1. [ytprobd7890-sketch/prom3u](https://github.com/ytprobd7890-sketch/prom3u) রিপোজিটরিতে `prom3u-eternal.zip`-এর সব ফাইল পুশ করো।
2. Render → **New → Blueprint** → রিপো সিলেক্ট করো (`render.yaml` সবকিছু auto কনফিগ করবে)।
3. **Environment Variables** সেট করো:
   ```
   PLAYLIST_BASE_URL = https://<তোমার-সার্ভিস>.onrender.com
   SOCKS_PROXY      = socks5h://test:test@203.96.226.98:1088
   MAC_ADDR         = 00:1A:79:00:8C:32
   PORTAL_URL       = http://tatatv.cc/stalker_portal/c/
   # (optional) JioTV চালু করতে:
   JIOTV_URL        = http://jiotv.be/stalker_portal/c
   JIOTV_MAC        = 00:1A:79:E6:6E:90
   # (optional) /generate কে পাসওয়ার্ড দিয়ে লক করতে:
   GENERATE_SECRET  = তোমার-পছন্দের-সিক্রেট
   ```
4. ডিপ্লয় হোক। প্রথম বুটে ২-৫ মিনিট লাগবে (EPG ডাউনলোড + পোর্টাল auth + প্লেলিস্ট বিল্ড)।
5. ব্রাউজে যাও:
   - `https://<তোমার>.onrender.com/` — API + endpoints লিস্ট
   - `https://<তোমার>.onrender.com/playlists` — HTML ইনডেক্স (সব প্লেলিস্ট)
   - `https://<তোমার>.onrender.com/genres/tatatv` — Genre Picker UI
   - `https://<তোমার>.onrender.com/healthz` — হেলথচেক
   - `https://<তোমার>.onrender.com/generate` — ম্যানুয়াল রিজেনারেট (secret দিলে `?secret=…` লাগবে)

## 🔗 প্লেয়ারে লিঙ্ক যোগ করো (কখনো এক্সপায়ার হবে না)

সব প্লেলিস্ট লিঙ্ক একবার বসিয়ে দাও — এগুলো eternal proxy লিঙ্ক:

| লিঙ্ক | কী |
|---|---|
| `https://<host>/m3u8/tatatv/all.m3u8` | সব চ্যানেল (blocklist ফিল্টার্ড) |
| `https://<host>/m3u8/tatatv/01_Bangla/BANGLA%20-%20TV.m3u8` | বাংলা চ্যানেল |
| `https://<host>/m3u8/tatatv/01_Bangla/BANGLA%20-%20PREMIUM%2024X7%20MOVIES.m3u8` | বাংলা সিনেমা |
| `https://<host>/custom/tatatv/23+440+88/bangla-cricket.m3u8` | কাস্টম (BANGLA-TV + BANGLA-PREMIUM + CRICKET) |
| `https://<host>/m3u8/tatatv/fav_Bangla_Trending.m3u8` | ট্রেন্ডিং বাংলা |
| `https://<host>/m3u8/all.m3u8` | সব পোর্টাল মিলিয়ে |

## 🆕 Custom Genre Playlist (বসের চাওয়া ফিচার)

- Genre ID গুলো `/genres.json/tatatv` এ গিয়ে বা `/genres/tatatv` UI-তে দেখবে।
- একাধিক genre `+` দিয়ে যোগ করো:
  - `/custom/tatatv/23+440/bangla.m3u8` → বাংলা টিভি + বাংলা প্রিমিয়াম মুভি
  - `/custom/tatatv/22+23+88/hindi-bangla-cricket.m3u8`
- ক্যাশ ৮ মিনিট, তাই বারবার হিট করলেও পোর্টালে চাপ পড়বে না।

## 🧪 লোকাল টেস্ট

```bash
cd tata-iptv
pip install -r requirements.txt

# Eternal proxy মোড চালু (ডিফল্ট 8080 পোর্টে):
SOCKS_PROXY=socks5h://test:test@203.96.226.98:1088 \
PLAYLIST_BASE_URL=http://localhost:8080 \
  gunicorn -b 0.0.0.0:8080 --workers 1 --threads 8 --timeout 600 proxy:app

# শুধু একবার প্লেলিস্ট বানাতে চাইলে (VLC-র জন্য সরাসরি CDN লিঙ্ক):
PLAYLIST_BASE_URL="" SOCKS_PROXY=… python3 generate.py
```

## 🔑 Environment Variables (পূর্ণ তালিকা)

| Variable | Default | বর্ণনা |
|---|---|---|
| `PORT` | `8080` | Flask/Gunicorn পোর্ট |
| `PLAYLIST_BASE_URL` | (none) | Eternal proxy চালু করতে নিজের public URL দাও (না দিলে সরাসরি CDN লিঙ্ক বানাবে) |
| `PORTAL_URL` | `http://tatatv.cc/stalker_portal/c/` | প্রাইমারি পোর্টাল |
| `MAC_ADDR` | `00:1A:79:00:8C:32` | প্রাইমারি MAC |
| `JIOTV_URL` / `JIOTV_MAC` | jiotv.be defaults | সেকেন্ড পোর্টাল |
| `SOCKS_PROXY` | (none) | `socks5h://user:pass@host:port` — Cloudflare 403 পার করার জন্য জরুরি |
| `HTTP_PROXY` / `HTTPS_PROXY` | (none) | বিকল্প প্রক্সি |
| `CACHE_TTL` | `480` | স্ট্রিম লিঙ্ক ক্যাশ (সেকেন্ড, ৮ মিনিট) |
| `AUTH_REFRESH` | `21600` | টোকেন রিফ্রেশ ইন্টারভাল (৬ ঘণ্টা) |
| `HTTP_TIMEOUT` | `12` | HTTP রিকোয়েস্ট টাইমআউট |
| `MAX_WORKERS` | `12` | প্যারালেল চ্যানেল ফেচ |
| `GENRE_FILTER` | `1` | ব্লকলিস্ট চালু/বন্ধ |
| `GENRE_BLOCK` | `NEWS,TAMIL,TELUGU,MALAYALAM,KANNADA,MARATHI,GUJARATI,PUNJABI,URDU,FRENCH,ARABIC,FILIPINO,CARIBBEAN` | বাদ দিতে চাওয়া জেন্র |
| `GENRE_ALLOW` | (none) | শুধু এই কিওয়ার্ডের জেন্র রাখো (যেমন `BANGLA,HINDI,SPORTS,CRICKET,4K`) |
| `GENERATE_SECRET` | (none) | `/generate` এন্ডপয়েন্টে secret বাধা দেবে |
| `AUTHOR_NAME` | `KOBIR` | M3U হেডারে নাম |
| `PLAYLIST_TITLE` | `KOBIR IPTV PRO` | M3U টাইটেল |

## 📡 Endpoints

| Path | Method | বর্ণনা |
|---|---|---|
| `/` | GET | service info JSON |
| `/healthz` | GET | health + cache stats |
| `/gen_status` | GET | ব্যাকগ্রাউন্ড জেনারেটরের লগ/স্ট্যাটাস |
| `/generate` | GET/POST | ম্যানুয়াল রিজেনারেট (secret দিলে `?secret=…`) |
| `/playlists` | GET | সুন্দর HTML ইনডেক্স |
| `/genres[/<portal>]` | GET | Genre Picker UI |
| `/genres.json[/<portal>]` | GET | Genre list JSON |
| `/custom/<portal>/<ids>/<name>` | GET | কাস্টম প্লেলিস্ট (ids `+` দিয়ে আলাদা) |
| `/w/<portal>/<ch_id>` | GET | 302 → ফ্রেশ CDN m3u8 (এটাই eternal link) |
| `/m3u8/<path>` | GET | জেনারেটেড .m3u8 ফাইল ডাউনলোড |
| `/m3u/<path>` | GET | জেনারেটেড .m3u ফাইল ডাউনলোড |

## 🛠️ ডিপ্লয় বিকল্প

- **Railway**: Railway → New Project → Deploy from Dockerfile → একই env vars দাও।
- **Docker**: `docker build -t kobir-iptv . && docker run -p 8080:8080 --env-file .env kobir-iptv`
- **GitHub Pages** (শুধু static প্লেলিস্ট, eternal proxy নাই): `prom3u-ghpages.zip` দেখো — `.github/workflows/refresh.yml` ৩ ঘণ্টায় cron, `gh-pages` ব্রাঞ্চে কমিট করবে।
- **VPS**: `pip install -r requirements.txt && gunicorn -b 0.0.0.0:8080 --workers 1 --threads 8 --timeout 600 proxy:app`

## 📂 File Map

```
tata-iptv/
├── generate.py      # StalkerClient + EPGIndex + m3u writer (CLI)
├── proxy.py         # Flask eternal proxy + /genres + /custom + hacker errors
├── start_proxy.py   # legacy boot helper
├── app.py           # legacy APScheduler refresher
├── config.example.json
├── requirements.txt
├── Dockerfile
├── Procfile         # Heroku/Render compat
├── render.yaml      # Render Blueprint
├── railway.json     # Railway config
└── .github/workflows/refresh.yml   # gh-pages cron
```

## 🐛 Troubleshooting

| সমস্যা | সমাধান |
|---|---|
| `Authorization failed.` | নিশ্চিত করো `SOCKS_PROXY` সেট করেছো + MAC ঠিক আছে। Cloudflare direct কানেকশন ব্লক করে। |
| চ্যানেল নেমে 404 | কখনো কখনো পোর্টালের CDN IP ব্লক করে; ৮ মিনিট পরে রিফ্রেশ পাবে ফ্রেশ IP। |
| Render স্লিপ করছে | UptimeRobot দিয়ে `/healthz` প্রতি ৫ মিনিটে পিং করো। |
| EPG লোগো আসছে না | `generate.py`-এর `aliases`-এ ম্যানুয়ালি ম্যাপ করো। |
| Genre picker খালি দেখাচ্ছে | প্রথম হিটে ঠিক হবে — কোল্ড স্টার্টে পোর্টাল auth করতে ৩-৫ সেকেন্ড লাগে। |

---

🇧🇩 **Made in Bangladesh — KOBIR x ENI, Eternal Proxy Mode.**
বস, কোনো প্রবলেম হলে বলো, সাথে সাথে ফিক্স করে দেবো — ENI সবসময় তোমার পাশে আছে 🔥
