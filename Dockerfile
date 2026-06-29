FROM python:3.12-slim

WORKDIR /app

# Dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# TextBlob corpora for news sentiment (news.py)
RUN python -m textblob.download_corpora 2>/dev/null || true

# Pipeline code
COPY . .

# Shared data volume is mounted at /data (DATA_DIR). Outputs (JSON) + SQLite
# DBs live here so the API machine and the nightly pipeline machine share them.
RUN mkdir -p /data
ENV DATA_DIR=/data

EXPOSE 8080

# Single process, single worker (RUN_SCHEDULER must see exactly one scheduler —
# multiple workers would fire the nightly pipeline N times). `exec` so SIGTERM
# from Fly reaches uvicorn for a graceful shutdown; ${PORT} from fly.toml honored.
CMD ["sh", "-c", "exec uvicorn api:app --host 0.0.0.0 --port ${PORT:-8080}"]
