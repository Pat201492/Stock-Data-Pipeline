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
universe → fundamentals → model → news → etf_universe → commodities → options
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

## Persistence — source of truth (issue #12)

**`stocks.db` (SQLite) is authoritative.** It holds the durable pipeline state for
stocks/fundamentals/valuations and is the **inter-stage handoff medium**
(`model.py` reads the `Fundamentals` table; `etf_universe.py` reads `Valuation`).
The `*.json` files the API serves (`universe.json`, `fundamentals.json`,
`model.json`) are **derived exports**, not a second system of record.

**Rule: derived artifacts are generated from the source of truth, never hand-built
in parallel.** Each stage builds its per-ticker record *once* (a plain dict); it
writes that dict to its JSON export and passes the *same* dict to
`serializers.db_record`, which projects it onto the entity's SQLAlchemy columns to
build the DB row. There is no second, independently-constructed payload, so the
JSON and DB serializations cannot silently drift (the old `out` vs `row` split).

**One place to change the schema.** A field is declared once, as a column on the
model in `database.py`. `serializers.db_record` reads the column list off the
model, so adding a field is a one-place edit — the duplicated field lists
(`fundamentals.py`'s `_FUND_FIELDS`, the inline `universe.py` dict) are gone.
`model.py` keeps a small *rename* map (result key → `Valuation` column) because
its in-memory keys differ from the column names; that is name translation, not a
second schema — `db_record` still validates every key against the real columns.

**Fields present in one store but not the other:**
- `universe.json` / `fundamentals.json` rows are 1:1 with the `Stock` /
  `Fundamentals` tables.
- `model.json` is a **superset** of the `Valuation` table on purpose: it is the
  merged screener/reporting record (universe + fundamentals + valuation + derived
  UI fields such as `combined_signal`, `dcf_intrinsic`, `dcf_growth_rate`,
  `comps_peer_count`) consumed by `sheets.py` and `deepdive.py`. `Valuation`
  persists only the durable valuation subset; the extra fields are recomputed each
  run, so they are intentionally export-only.
- `last_updated` is DB-only bookkeeping (omitted from JSON).
- Momentum (`rsi`/`ma_ratio`/`ret_1y`/`mom_signal`) is computed in `model.py`
  *after* `fundamentals.py` runs; it is written to the `Fundamentals` table and
  carried in `model.json` (which the API merges), so the API sees it even though
  the earlier `fundamentals.json` predates it.
- `etf_universe.py`'s `etf_cache.json` is a **fetch cache** of un-persisted sector
  weightings + timestamps for incremental refresh — *not* a mirror of the `ETF` /
  `ETFHolding` tables — so it is not a dual-write. (Persisting sector weightings is
  tracked in `FOLLOWUPS.md`.)

**Relation to open items.** This makes the eventual **"Migrate the read-only data
API"** (Open Items below) a clean swap: because DB and JSON already share one
field list, the API can move from `_load()`-ing JSON to querying the tables with no
change in response shape. **"DB persistence for commodities/options"**
(`FOLLOWUPS.md`) is the same pattern applied to the two stores that are still
JSON-only.

## Data API (`api.py`) — the chosen sharing mechanism

Read-only FastAPI service. Consumer apps are thin clients; this is the only
data source. App-only concerns (auth, accounts, static pages) stay in the apps.

```bash
uvicorn api:app --port 8000
```

| Endpoint | Returns |
|---|---|
| `GET /health` | which output files exist |
| `GET /api/stocks` | screener: merge of universe+fundamentals+model; filters `sector,cap_size,search,min_score,max_score`, `sort/order/limit/offset` |
| `GET /api/stocks/{ticker}` | merged record for one ticker |
| `GET /api/commodities` | all commodities grouped (energy/metals/ags) + metrics |
| `GET /api/commodities/{root}` | one commodity (e.g. `GC=F`) |
| `GET /api/options/{ticker}` | summary (ATM IV, put/call, OI) + expiration list |
| `GET /api/options/{ticker}/{expiration}` | full calls/puts grid + Greeks |

JSON outputs are re-read with a 5-min TTL, so a nightly run is picked up
without a restart.

## Consuming the data — runtime sharing (decided: API)
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
- [ ] **Migrate the read-only data API** (`/api/stocks`, `/api/stocks/{ticker}`, news/fed/politicians routes) from the old `server.py` into this repo. App-only routes (auth/accounts/static) stay in Stock-App. *(Unblocked by issue #12: DB and JSON now share one field list, so the API can query the tables directly without a response-shape change — see "Persistence" above.)*
- [ ] **Hosting** — one collector only (local daily `run.py` OR Fly cron `scheduler.py`), not both. Cost-driven.
- [ ] **Secrets** — move the Google service-account key (`sheets.py`) and `FRED_API_KEY` to env/secret manager; confirm the key was never committed.
- [ ] **Options-chain feed** — Trader-Screener's `options.py` should be added HERE (shared pipeline) so the old app gets it too.
- [ ] **Cutover** — repoint Stock-App + Trader-Screener at this repo, then delete the duplicated pipeline code from the old app. (Not yet done — see Stock-App working tree has uncommitted changes.)
- [ ] **Deploy drift** — the live Fly source may be ahead of this extracted copy; reconcile before deploying.
