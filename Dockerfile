FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MMPRED_CONFIG=/app/config/mmpredictions.json \
    MMPRED_DB_PATH=/var/lib/mmpredictions/mmpredictions.sqlite3 \
    MMPRED_SYNC_ON_STARTUP=0

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /var/lib/mmpredictions /app/config /app/mmpredictions \
    && chown -R app:app /var/lib/mmpredictions /app

WORKDIR /app

# Runtime uses Python stdlib only. Mount /app/config/mmpredictions.json from
# your secret/config system, or rely on the checked-in example for local smoke tests.
COPY config/mmpredictions.example.json /app/config/mmpredictions.example.json
COPY mmpredictions /app/mmpredictions
RUN chown -R app:app /app

EXPOSE 8080
HEALTHCHECK CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2).status==200 else 1)"]

USER app
CMD ["python", "-m", "mmpredictions.app"]
