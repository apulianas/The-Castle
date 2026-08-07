FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ravens_bot ./ravens_bot

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-m", "ravens_bot"]
