FROM python:3.11-slim

# librosa/libsndfile 需要的系统库
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend /app/backend
COPY frontend /app/frontend

ENV PYTHONPATH=/app/backend \
    DATA_DIR=/data \
    DB_PATH=/data/review.db \
    FRONTEND_DIR=/app/frontend \
    MUSIC21_SETTINGS_AUTO_DOWNLOAD=0

RUN mkdir -p /data/files
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--app-dir", "/app/backend", \
     "--host", "0.0.0.0", "--port", "8000"]
