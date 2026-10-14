FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schedule_bot/ ./schedule_bot/
COPY main.py .

RUN mkdir -p /app/data/pdf_cache && \
    useradd --create-home --uid 1000 bot && \
    chown -R bot:bot /app
USER bot

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import pathlib,sys,time; \
db=pathlib.Path('/app/data/schedule.db'); hb=pathlib.Path('/app/data/heartbeat'); \
sys.exit(0 if db.exists() and hb.exists() and time.time() - hb.stat().st_mtime < 180 else 1)"

CMD ["python", "main.py"]
