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
    CMD python -c "import pathlib,sys; sys.exit(0 if pathlib.Path('/app/data/schedule.db').exists() else 1)"

CMD ["python", "main.py"]
