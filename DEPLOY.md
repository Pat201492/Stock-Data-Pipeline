# Deploy — Stock Data Pipeline on Fly.io

The data service: one app machine serves the read-only API; a separate
**scheduled** machine runs the nightly pipeline. Both share one `/data` volume.

```
   ┌─────────────┐   reads /data    ┌──────────────────────┐
   │  API machine │ ◄─────────────── │  /data volume        │
   │  uvicorn     │                  │  *.json + *.db       │
   │  api:app     │                  └──────────▲───────────┘
   └─────────────┘                     writes    │
                                  ┌───────────────┴───────┐
                                  │ nightly machine        │
                                  │ python run.py (cron)   │
                                  └────────────────────────┘
```

One collector only — respects the cost constraint (no second always-on box;
the nightly machine spins up, runs, exits).

## First-time setup
```bash
fly launch --no-deploy --copy-config        # uses fly.toml; app = stock-data-pipeline-pat
fly volumes create pipeline_data --size 2 --region ewr

# Secrets (never commit):
fly secrets set FRED_API_KEY=xxxxx
# Google service-account JSON for sheets.py (if used):
#   fly secrets set GOOGLE_SA_JSON="$(cat stock-stracker-*.json)"  # then sheets.py reads it from env

fly deploy
fly status
curl https://stock-data-pipeline-pat.fly.dev/health
```

## Nightly pipeline (the daily update program)
Run `run.py` on a scheduled machine sharing the same image + volume:
```bash
# 02:00 UTC daily — writes outputs to /data, which the API then serves
fly machine run . \
  --schedule daily \
  --volume pipeline_data:/data \
  --region ewr \
  --env DATA_DIR=/data \
  -- python run.py
```
(`scheduler.py` is the in-image wrapper that shells to `run.py`; either entrypoint works.)

## Local run
```bash
pip install -r requirements.txt
export DATA_DIR=$(pwd)          # outputs land next to the code locally
python run.py                   # daily update
uvicorn api:app --port 8000     # serve the API
```

## ⚠️ Before this fully works — remaining Phase-1 task
The API reads outputs from `DATA_DIR`. `api.py`, `commodities.py`, `options.py`
already honor `DATA_DIR`. The **inherited collectors still write to their own
folder** and must be pointed at `DATA_DIR` too, or the API on the volume sees
partial data:

- [ ] Retrofit `universe.py`, `fundamentals.py`, `model.py`, `news.py`,
      `etf_universe.py` (and `database.py` DB path is already env-driven via
      `DB_PATH`) to write outputs under `DATA_DIR`.
- [ ] Decide JSON-on-volume vs DB-backed API reads (FOLLOWUPS.md). Current API
      is JSON-backed; DBs already go to `/data` via `DB_PATH`/`POL_DB_PATH`.
- [ ] Set real secrets (`FRED_API_KEY`, Google SA) in `fly secrets`.
- [ ] Confirm the OLD app's cron is turned OFF after cutover (no double-collect).
