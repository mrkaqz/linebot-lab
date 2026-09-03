# Architecture-neutral: python:3.12-slim is a multi-arch manifest, so
# building this image on a 64-bit Raspberry Pi (aarch64) pulls the arm64
# variant automatically, and the same Dockerfile still works unmodified for
# x86_64 local development/testing. Do not hardcode --platform here.
FROM python:3.12-slim

# tesseract-ocr + the Thai language pack, for the local/offline OCR_BACKEND=tesseract
# backend (harmless to install even when a different backend is selected).
# curl is used only by the docker-compose healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-tha \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app/templates/ (Jinja2) and app/static/ (CSS/JS/sample image) for the
# admin UI are part of this tree and copied along with the rest of app/.
COPY app/ ./app/
COPY scripts/ ./scripts/

# Run as a non-root user; the ./data bind mount (docker-compose.yml) must be
# writable by it -- it holds the SQLite DB, the MSAL token cache, and the
# config-encryption/session-secret key files.
RUN groupadd --system --gid 1000 linebot \
    && useradd --system --uid 1000 --gid linebot --home-dir /app --shell /usr/sbin/nologin linebot \
    && mkdir -p /app/data \
    && chown -R linebot:linebot /app

USER linebot

# 8000: public app (/line/webhook, /oauth/callback, /healthz) -- this is
#       the only port cloudflared (or any other public tunnel) should forward.
# 8001: admin UI (setup, dashboard, unfiled queue) -- publish to the LAN
#       only. See docker-compose.yml and README "Web admin UI".
EXPOSE 8000 8001

# app/main.py runs BOTH uvicorn servers itself (one process, one shared
# AppState) -- it is not itself an ASGI app, so this is `python -m`, not
# `uvicorn app.main:app`.
CMD ["python", "-m", "app.main"]
