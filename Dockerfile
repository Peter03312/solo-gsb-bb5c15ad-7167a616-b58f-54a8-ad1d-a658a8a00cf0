FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite 数据库文件目录（EVAC_DB_PATH 默认 /data/evacuation.db）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 worker：编排变更由进程内 asyncio 锁串行化，保证容量/事务一致性
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
