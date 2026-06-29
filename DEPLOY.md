# Deploy — Stock Data Pipeline on Fly.io

**One always-on machine** serves the read-only API *and* runs the nightly
pipeline in-process (APScheduler). Single machine because a Fly volume attaches
to exactly one machine — so the API and the cron must live together to share
`/data`.

```
   ┌──────────────────────────────────────────┐
   │  Fly machine (always-on, min=1)           │
   │   ├─ uvicorn api:app   → serves /api/...   │
   │   └─ APScheduler 02:00 UTC → run.py        │
   │            writes ↓        ↑ reads         │
   │        ┌─────────────────────────┐         │
   │        │  /data volume (*.json,  │         │
   │        │  *.db)                  │         │
   │        └─────────────────────────┘         │
   └──────────────────────────────────────────┘
```

Still one collector — no second box. The machine stays up (shared-cpu-1x is
cheap) because the in-process scheduler needs to be alive at 02:00 UTC.

## First-time setup
```bash
fly launch --no-deploy --copy-config        # uses fly.toml; app = stock-data-pipeline-pat
fly volumes create pipeline_data --size 2 --region ewr

# Secrets (never commit):
fly secrets set FRED_API_KEY=xxxxx
# Google service-account JSON for sheets.py (if used):
#   fly secrets set GOOGLE_SA_JSON="$(cat stock-stracker-*.json)"  # sheets.py reads it from env

fly deploy
fly status
curl https://stock-data-pipeline-pat.fly.dev/health
```

`RUN_SCHEDULER=1` (set in fly.toml) turns on the in-process nightly run. To run
the API without the cron (e.g. a read replica), deploy a second app config with
`RUN_SCHEDULER` unset.

## Local run
```bash
pip install -r requirements.txt
export DATA_DIR=$(pwd)          # outputs land next to the code locally
python run.py                   # daily update (universe → … → commodities → options)
uvicorn api:app --port 8000     # serve the API (scheduler OFF unless RUN_SCHEDULER=1)
```

> **If you keep the daily run on your local machine** (instead of the Fly
> scheduler): run `run.py` locally, then push the outputs to the Fly volume so
> the API serves them — `fly ssh sftp shell` / `fly sftp put` into `/data`, or
> switch the API to object storage. In that case leave `RUN_SCHEDULER` unset on
> Fly. Pick ONE place to run the pipeline; don't double-collect.

## Data paths
All collectors honor `DATA_DIR` (default = code folder; `/data` on Fly):
`universe.py`, `fundamentals.py`, `model.py`, `etf_universe.py`, `commodities.py`,
`options.py` read/write/cache there; `news.py` is DB-only (`DB_PATH` on `/data`).
So a full `run.py` writes everything to the volume the API serves.

## ⚠️ Remaining before go-live
- [ ] Set real secrets (`FRED_API_KEY`, Google SA) in `fly secrets`.
- [ ] First deploy + first `run.py` (cold volume → API returns 503/empty until
      the first nightly completes; trigger once manually if needed).
- [ ] Confirm the OLD app's cron is OFF after cutover (no double-collect).
- [ ] (Optional) JSON-on-volume vs DB-backed API reads — see FOLLOWUPS.md.
