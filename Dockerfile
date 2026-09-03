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

COPY app/ ./app/
COPY scripts/ ./scripts/

# Run as a non-root user; the ./data bind mount (docker-compose.yml) must be
# writable by it -- it holds the SQLite DB and the MSAL token cache.
RUN groupadd --system --gid 1000 linebot \
    && useradd --system --uid 1000 --gid linebot --home-dir /app --shell /usr/sbin/nologin linebot \
    && mkdir -p /app/data \
    && chown -R linebot:linebot /app

USER linebot

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
