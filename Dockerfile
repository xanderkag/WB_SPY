# ─────────────────────────────────────────────────────────────────────
# WB Spy — Python 3.11 (FastAPI web + collector/bot worker)
# Один образ, две роли (см. docker-compose): web и worker.
# Playwright/Chromium НЕ ставим (тяжёлый) — wb_browser импортируется
# лениво и нужен только при USE_PLAYWRIGHT=1.
# ─────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/wb.sqlite3

# ca-certificates — для TLS к WB/Telegram; curl_cffi тащит свой libcurl-impersonate
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY wbp ./wbp

# каталог под SQLite (монтируется volume)
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# дефолт — web; worker переопределяет command в compose
CMD ["uvicorn", "wbp.web:app", "--host", "0.0.0.0", "--port", "8000"]
