FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/playlists /app/m3u

ENV PORT=8080 \
    REFRESH_HOURS=6 \
    PYTHONUNBUFFERED=1 \
    REFRESH_TOKEN="" \
    MAX_WORKERS=10 \
    PLAYLIST_BASE_URL="" \
    CACHE_TTL=480 \
    GENRE_FILTER=1 \
    GENRE_BLOCK="NEWS,TAMIL,TELUGU,MALAYALAM,KANNADA,MARATHI,GUJARATI,PUNJABI,URDU,FRENCH,ARABIC,FILIPINO,CARIBBEAN"

EXPOSE 8080

HEALTHCHECK --interval=120s --timeout=10s --start-period=300s --retries=5 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1

# proxy.py is the live-link-redirect service. Set PLAYLIST_BASE_URL env
# (e.g. https://myapp.onrender.com) on deploy for NEVER-expiring playlist URLs.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "600", "proxy:app"]
