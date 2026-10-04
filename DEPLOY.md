# Deploy — Stock Data Pipeline on Fly.io

**One always-on machine** serves the read-only API *and* runs the nightly
pipeline in-process (APScheduler). Single machine because a Fly volume attaches
to exactly one machine — so the API and the cron must live together to share
`/data`.

```
   ┌──────────────────────────────────────────┐
   │  Fly machine (always-on, min=1)           │
   │   ├─ uvicorn api:app   → serves /api/...   │
   │   └─ APScheduler 02:00 UTC → full night    │
   │            writes ↓        ↑ reads         │
   │        ┌─────────────────────────┐         │
   │        │  /data volume (*.json,  │         │
   │        │  *.db)                  │         │
   │        └─────────────────────────┘         │
   └──────────────────────────────────────────┘
```

Still one collector — no second box. The machine stays up (shared-cpu-1x is
cheap) because the in-process scheduler needs to be alive at 02:00 UTC.

The 02:00 job runs the **full nightly sequence** — `run.py --skip-validate`, then
`pol_refresh.py`, then `validate.py --strict` (`scheduler.run_all`, #48) — not
`run.py` alone. On startup, if the volume has **no `fundamentals.json` yet**, the
API kicks one such run in the background so a fresh deploy serves data within the
hour instead of waiting for 02:00 (the cold-volume *bootstrap*, #49). Set
`BOOTSTRAP_ON_EMPTY=0` to disable it. The bootstrap and the cron share one lock,
so they never run at once.

## First-time setup
```bash
fly launch --no-deploy --copy-config        # uses fly.toml; app = stock-data-pipeline-pat
fly volumes create pipeline_data --size 2 --region ewr

# Secrets (never commit). Both degrade SILENTLY if unset — set them before deploy:
fly secrets set FRED_API_KEY=xxxxx                 # /api/macro is 503 without it
fly secrets set SEC_USER_AGENT="YourApp you@example.com"  # no as-filed XBRL Magic
                                                   # Formula without it (yfinance fallback)
# Google service-account JSON for sheets.py (if used):
#   fly secrets set GOOGLE_SA_JSON="$(cat stock-stracker-*.json)"  # sheets.py reads it from env

fly deploy
fly status

# Verify the deploy in one command (checks /health config, /api/stocks, macro,
# integrity; exits non-zero on any failure). --allow-empty while the bootstrap runs:
python smoke_deploy.py https://stock-data-pipeline-pat.fly.dev
```

`/health` reports `config: {sec_user_agent, fred_api_key, run_scheduler}` as
booleans (never the values) plus `data_dir`, so you can confirm both secrets
landed without exposing them. `smoke_deploy.py` fails loudly if either is false.

`RUN_SCHEDULER=1` (set in fly.toml) turns on the in-process nightly run. To run
the API without the cron (e.g. a read replica), deploy a second app config with
`RUN_SCHEDULER` unset.

## Host on a Windows PC (no Fly)

Runs the same API + in-process nightly job on this machine, as a logon
Scheduled Task (the same durability pattern as study-hall). The API listens on
`http://127.0.0.1:8000` only, which is Trader-Screener's default API base.

```powershell
pip install -r requirements.txt
setx FRED_API_KEY <key>                     # once; SEC_USER_AGENT falls back to EDGAR_USER_AGENT
powershell -ExecutionPolicy Bypass -File deploy\windows\install-scheduled-task.ps1
Start-ScheduledTask -TaskName StockDataPipeline
python smoke_deploy.py http://127.0.0.1:8000 --allow-empty   # while the first fill runs
```

- **Data:** `%USERPROFILE%\.stock-data-pipeline\data` (outside the repo, so
  switching branches never touches it). **Log:** `...\.stock-data-pipeline\logs\api.log`.
- **First start** on an empty data dir runs one full pipeline in the background
  (bootstrap). `/api/stocks` is empty until it finishes.
- **A PC is not always on.** The nightly job (`PIPELINE_HOUR_UTC`, default 2 =
  22:00 EDT) still runs if the PC wakes within `PIPELINE_MISFIRE_GRACE_HOURS`
  (default 6). Otherwise, the next start runs a catch-up when the last run is older
  than `CATCHUP_AFTER_HOURS` (default 26). `CATCHUP_ON_START=0` disables it.
- **Built for a PC that sleeps:** a run waits up to 10 min for the network after
  wake (`NETWORK_WAIT_SECONDS`), holds Windows awake only while it runs
  (`KEEP_AWAKE_DURING_RUN=0` to disable), and `run-local.ps1` restarts the API
  if it ever exits (every exit is logged in `api.log`).
- The code runs from the repo checkout, so `git pull` + restarting the task
  (`Stop-ScheduledTask` / `Start-ScheduledTask`) deploys a new version.
- Remove: `install-scheduled-task.ps1 -Uninstall`.

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
- `FRED_API_KEY` — macro series (`fred.py`); `/api/macro` is 503 without it.
- `SEC_USER_AGENT` — enables as-filed XBRL Magic Formula (`fundamentals.py`);
  falls back to yfinance when unset.
- `YF_SLEEP`, `YF_BATCH`, `YF_RETRIES`, `YF_BACKOFF` — yfinance pacing (one set
  of canonical defaults, applied to universe enrich and `run_batches` alike).
- `OPTIONS_TOP_N`, `OPTIONS_MAX_EXP` — options scope (`options.py`).
- `EXPOSURE_COMPUTE` — enable computed exposure betas (`commodity_exposure.py`).
- `RUN_SCHEDULER` — in-process nightly cron + cold-volume bootstrap (`api.py`).
- `BOOTSTRAP_ON_EMPTY` — set to `0` to disable the cold-volume bootstrap (`api.py`).

`news.py` is DB-only (`DB_PATH`).

## ⚠️ Remaining before go-live
- [ ] Set real secrets (`FRED_API_KEY`, `SEC_USER_AGENT`, Google SA) in `fly secrets`.
- [ ] First deploy — the cold-volume bootstrap (#49) starts one run automatically;
      API returns empty until it finishes (use `smoke_deploy.py --allow-empty` meanwhile).
- [ ] Run `python smoke_deploy.py https://stock-data-pipeline-pat.fly.dev` — all PASS.
- [ ] Confirm the OLD app's cron is OFF after cutover (no double-collect).
- [ ] (Optional) JSON-on-volume vs DB-backed API reads — see FOLLOWUPS.md.
