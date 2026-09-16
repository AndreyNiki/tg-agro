FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schedule_bot/ ./schedule_bot/
COPY main.py .

RUN mkdir -p /app/data/pdf_cache

CMD ["python", "main.py"]
