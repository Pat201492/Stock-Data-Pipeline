# Stock Data Pipeline

**The single source of truth for market data.** This repo collects, enriches, scores, and stores all the data that the consumer apps read. It is the *only* writer. Extracted from the old Stock Tracker so that multiple front-ends share one collection pipeline instead of each running their own (collect once, serve many).

## Consumers
- **Stock-App** (old Stock Tracker — web + mobile) — reads this data.
- **Trader-Screener** (new) — reads the same data; adds the options-chain feed.

> Neither consumer collects data itself. They read what this pipeline produces. See each repo's `ARCHITECTURE.md`.

---

## The daily program (run locally)

`run.py` is the **daily local runner** — the program that updates the data each day. Run it on the local machine:

```bash
python run.py                 # full pipeline
python run.py --skip-universe # skip the weekly universe rebuild
python run.py --from model    # resume from a stage (model + news)
```

Pipeline order (each stage is incremental — only stale data is re-fetched):

```
universe → fundamentals → model → news → etf_universe
```

`scheduler.py` is the **Fly.io nightly wrapper** — a cron machine fires it ~2am UTC and it shells out to `run.py`. Same logic, hosted. Keep ONE collector (local daily run OR the Fly cron) to avoid double-collecting and double cost.

Political + macro refreshers run alongside (not in the core `run.py` chain):
`ingest_congress.py`, `ingest_senate.py`, `ingest_senate_efd.py`, `ingest_house.py`, `ingest_edgar.py`, `ingest_insider_mirror.py`, `ingest_committees.py`, `pol_refresh.py`, `fred.py`.

---

## What it collects

| Stage | Script | Source | Output |
|---|---|---|---|
| Discover universe | `universe.py` | NASDAQ Trader FTP (+ Wikipedia / iShares fallback) | `universe.json` |
| Fundamentals | `fundamentals.py` | yfinance (batched) | `fundamentals.json` (+ data_quality) |
| Valuation/score | `model.py` | DCF + Comps + EPV/Graham | `model.json` |
| News + sentiment | `news.py` | yfinance news + TextBlob | DB (News) |
| ETFs | `etf_universe.py` | yfinance / holdings | ETF data |
| Macro | `fred.py` | FRED (St. Louis Fed) | macro series |
| Congress/insider | `ingest_*`, `pol_refresh.py` | Senate/House disclosures, SEC EDGAR | `politicians.db` |
| Maintenance | `backfill_eps.py`, `validate.py`, `deepdive.py`, `sheets.py` | — | fixes / exports |

Shared modules: `database.py` (SQLAlchemy models — `stocks.db`), `politicians_database.py` (`politicians.db`), `data_utils.py` (robust yfinance field extraction).

---

## Outputs (what consumers read)
- **Databases:** `stocks.db`, `politicians.db`, `accounts.db` (gitignored — large, rebuilt).
- **JSON:** `universe.json`, `fundamentals.json`, `model.json`, ETF data (gitignored — generated).

These are **not** in git. They are produced by running the pipeline and shared with consumers at **runtime**.

## Consuming the data — runtime sharing (decide one)
| Model | How consumers read | Notes |
|---|---|---|
| **Service (API)** ⭐ | This repo also runs the FastAPI server; apps call `/api/...` | Cleanest. The old `server.py` API layer should migrate here. |
| Shared DB/volume | Apps query the same `stocks.db` / `politicians.db` | Couples apps to schema. |
| Published artifacts | Pipeline uploads JSON/DB to object storage; apps download | Simple, slightly stale. |

> **Recommended:** promote the data API (the read-only `/api/...` routes from the old `server.py`) into this repo so both consumers are thin clients. Tracked as an open item below.

---

## Setup
```bash
pip install -r requirements.txt
# secrets via env (never commit):
#   FRED_API_KEY=...                  # macro (fred.py)
#   Google service-account JSON       # sheets.py — keep out of git
python run.py
```

---

## ⚠️ Open Items
- [ ] **Runtime sharing mechanism** — pick Service (API) / shared DB / artifacts. Recommend Service.
- [ ] **Migrate the read-only data API** (`/api/stocks`, `/api/stocks/{ticker}`, news/fed/politicians routes) from the old `server.py` into this repo. App-only routes (auth/accounts/static) stay in Stock-App.
- [ ] **Hosting** — one collector only (local daily `run.py` OR Fly cron `scheduler.py`), not both. Cost-driven.
- [ ] **Secrets** — move the Google service-account key (`sheets.py`) and `FRED_API_KEY` to env/secret manager; confirm the key was never committed.
- [ ] **Options-chain feed** — Trader-Screener's `options.py` should be added HERE (shared pipeline) so the old app gets it too.
- [ ] **Cutover** — repoint Stock-App + Trader-Screener at this repo, then delete the duplicated pipeline code from the old app. (Not yet done — see Stock-App working tree has uncommitted changes.)
- [ ] **Deploy drift** — the live Fly source may be ahead of this extracted copy; reconcile before deploying.
