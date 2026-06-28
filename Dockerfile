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

# Single process: the API serves /api/... and, with RUN_SCHEDULER=1, also runs
# the nightly pipeline in-process (APScheduler) so one machine owns the volume.
# Shell form so ${PORT} from fly.toml is honored.
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8080}
