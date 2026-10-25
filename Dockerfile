# The bot as one always-on container (Railway). Local dev doesn't need this.
# Railway sets PORT; the database lives on a volume mounted at /data.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY pyproject.toml ./
COPY app ./app
# Install the dependencies only; the code runs from /srv so clients/ is found next to app/.
RUN pip install --no-cache-dir . && pip uninstall -y dealer-whatsapp-agent
COPY clients ./clients

ENV PYTHONUNBUFFERED=1 \
    APP_ENV=railway \
    SQLITE_PATH=/data/app.sqlite3 \
    ALERTS_EVERY_MINUTES=60

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
