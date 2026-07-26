# 📺 TataTV IPTV — Auto-Updating M3U Playlists

প্রফেশনাল গ্রেড আইপিটিভি প্লেলিস্ট জেনারেটর। GitHub Actions দিয়ে প্রতি **৬ ঘণ্টায়** স্বয়ংক্রিয়ভাবে tatatv.cc (এবং config-এ যত খুশি portal/MAC) থেকে কাজ করা HLS m3u8 লিঙ্ক বের করে, EPG/TVG লোগো ম্যাচ করায়, ফেভারিট প্লেলিস্ট বানায়, আর সুন্দর index.html সহ সাজিয়ে রাখে।

## ✨ ফিচার
- 🔄 **অটো রিফ্রেশ** — প্রতি ৬ ঘণ্টায় (cron) + ম্যানুয়াল ডিসপ্যাচ
- 📡 **মাল্টি-পোর্টাল / মাল্টি-MAC** — `config.json` এ যত খুশি portal + MAC যোগ করো, একসাথে সব স্ক্র্যাপ হবে
- 🌐 **SOCKS5/HTTP প্রক্সি** প্রতি portal এ আলাদা করে দেয়া যায় (Cloudflare ব্লক পার করতে)
- 📺 **EPG + TVG লোগো** — dtankdempse/epg সোর্স থেকে স্বয়ংক্রিয় fuzzy ম্যাচ, ম্যানুয়াল alias-এর সাপোর্ট
- ⏪ **Catchup/Timeshift** ট্যাগ (flussonic) — Tivimate এ রিপ্লে চালু থাকে
- ⭐ **ফেভারিট প্লেলিস্ট** — নিজের পছন্দের চ্যানেল লিস্ট বানিয়ে আলাদা .m3u8/.m3u
- 🎞️  **VLC-র জন্য .m3u** + HLS-র জন্য .m3u8 — দুই ফরম্যাটেই আউটপুট
- 📋 **JSON ক্যাটালগ** — সব মেটাডেটা এক ফাইলে (API-র জন্য)
- 🌍 **index.html** — ব্রাউজারে গিয়ে এক ক্লিকে প্লেলিস্ট বাছাই করার জন্য ওয়েব পেজ
- ⚡  **দ্রুত** — ২৫টা কনকারেন্ট ওয়ার্কার, ১ মিনিটেই সব লিঙ্ক রেডি

## 📂 আউটপুট স্ট্রাকচার
```
playlists/
├── index.html                  ← ওয়েব ডিরেক্টরি
├── all.m3u8                    ← সব portal-এর চ্যানেল একসাথে
├── tatatv/
│   ├── all.m3u8                ← tatatv-র সব চ্যানেল
│   ├── catalog.json            ← JSON metadata
│   ├── 00_All/00_All_Channels.m3u8
│   ├── 01_Bangla/BANGLA - TV.m3u8
│   ├── fav_Bangla_Trending.m3u8
│   └── ...
m3u/                           ← একই, কিন্তু .m3u এক্সটেনশন (VLC-friendly)
```

## 🚀 সেটআপ
### ১. রিপো বানাও
এই ফোল্ডারটা GitHub-এ নতুন repo-তে পুশ করো।

### ২. Secrets / Variables সেট করো
Repo → Settings → Secrets and variables → Actions এ যাও:

**Variables:**
| Name | মান |
|---|---|
| `PORTAL_URL` | `http://tatatv.cc/stalker_portal/c/` |
| `MAC_ADDR` | `00:1A:79:00:8C:32` (বা তোমার MAC) |

**Secrets:**
| Name | মান |
|---|---|
| `SOCKS_PROXY` | `socks5h://user:pass@host:port` (Cloudflare ব্লক থাকলে বাধ্যতামূলক) |

`config.json`-এ সরাসরিও লিখতে পারো — সেক্ষেত্রে env-এর দরকার নেই।

### ৩. Pages চালু করো (optional)
Settings → Pages → Source: **GitHub Actions**, তারপর `https://<user>.github.io/<repo>/playlists/index.html` এ ব্রাউজ করো — সুন্দর লিস্ট পাবে।

### ৪. প্লেয়ারে লিঙ্ক যোগ করো
```
https://raw.githubusercontent.com/<user>/<repo>/main/playlists/tatatv/01_Bangla/BANGLA%20-%20TV.m3u8
https://raw.githubusercontent.com/<user>/<repo>/main/playlists/tatatv/fav_Bangla_Trending.m3u8
```

Tivimate / IPTV Smarters / Perfect Player / VLC — সব প্লেয়ারে চলবে। EPG URL হিসেবে:
```
https://raw.githubusercontent.com/dtankdempse/epg/master/merged.xml.gz
```

## ⚙️  config.json কাস্টমাইজ
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
  "epg_sources": [
    "https://raw.githubusercontent.com/dtankdempse/epg/master/merged.xml.gz"
  ],
  "aliases": {
    "ZEE BANGLA 4K": {"id": "zeebangla.in", "logo": "https://example.com/zeebangla.png"}
  },
  "favorites": {
    "Bangla_Trending": ["ZEE BANGLA 4K", "STAR JALSHA HD", "COLORS BANGLA HD"]
  }
}
```

## 🧪 লোকাল টেস্ট
```bash
pip install -r requirements.txt

# শুধু বাংলা জেনার টেস্ট
DRY_RUN=1 python generate.py

# পুরো জেনারেট
python generate.py
```

## 📝 নোট
- টোকেন ৬-১২ ঘণ্টা বৈধ থাকে, তাই cron ঠিক আছে
- Catchup/simeshift শুধুমাত্র Flussonic সার্ভারে কাজ করবে (এই পোর্টালে ফ্লুসোনিক)
- লোগো EPG সোর্সে না পাওয়া গেলে জেনেরিক প্লেসহোল্ডার ব্যবহার করবে
- কোনো চ্যানেল না মিললে `aliases`-এ ম্যানুয়ালি ম্যাপ করে দাও
