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

## Configuration & data paths
**`config.py` is the single source of the env-var contract.** All environment
tunables and path resolution live there — read it rather than trusting a list
here that can rot. `DATA_DIR` is resolved once (default = code folder; `/data`
on Fly), and every JSON output, cache, DB, and `run.log` derives from it, so a
full `run.py` writes everything to the volume the API serves — nothing escapes
to the container FS.

Env vars honored (all defined in `config.py`):
- `DATA_DIR` — writable root for outputs/caches/DBs/`run.log`.
- `DB_PATH`, `POL_DB_PATH` — SQLite paths (default under `DATA_DIR`).
- `FRED_API_KEY` — macro series (`fred.py`).
- `YF_SLEEP`, `YF_BATCH`, `YF_RETRIES`, `YF_BACKOFF` — yfinance pacing (one set
  of canonical defaults, applied to universe enrich and `run_batches` alike).
- `OPTIONS_TOP_N`, `OPTIONS_MAX_EXP` — options scope (`options.py`).
- `EXPOSURE_COMPUTE` — enable computed exposure betas (`commodity_exposure.py`).
- `RUN_SCHEDULER` — in-process nightly cron (`api.py`).

`news.py` is DB-only (`DB_PATH`).

## ⚠️ Remaining before go-live
- [ ] Set real secrets (`FRED_API_KEY`, Google SA) in `fly secrets`.
- [ ] First deploy + first `run.py` (cold volume → API returns 503/empty until
      the first nightly completes; trigger once manually if needed).
- [ ] Confirm the OLD app's cron is OFF after cutover (no double-collect).
- [ ] (Optional) JSON-on-volume vs DB-backed API reads — see FOLLOWUPS.md.
