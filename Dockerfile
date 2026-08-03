FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CADDIE_HOST=0.0.0.0 \
    CADDIE_PORT=8766 \
    CADDIE_ALLOW_REMOTE=1 \
    CADDIE_DATA_DIR=/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY . .
RUN mkdir -p /data

EXPOSE 8766
VOLUME ["/data"]

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8766", "--proxy-headers"]
