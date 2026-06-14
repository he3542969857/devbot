# devbot 镜像(放到 /home/ubuntu/apps/devbot/Dockerfile,构建上下文=该目录)
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
COPY devbot /app/devbot
COPY devbot_eval /app/devbot_eval
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1
EXPOSE 8502
CMD ["uvicorn", "devbot.api.app:app", "--host", "0.0.0.0", "--port", "8502", "--workers", "2"]
