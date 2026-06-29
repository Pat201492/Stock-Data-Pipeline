# Follow-ups

Tracked items from the PR #1 review (commodities/options/API). Status updated as addressed.

## Fixed in PR #2 (review-followups)
- [x] **#1 `chg_ytd` mislabeled** — was 1-year change. Now computes true YTD from the dated index (anchor = first close of current calendar year); also added explicit `chg_1y`. `commodities.py`.
- [x] **#2 `iv_mean` polluted by garbage IV** — yfinance reports absurd IV on the wings. `iv_mean` now averages only near-the-money strikes (±20% of spot) within a sane band (`0.01 < iv < 5.0`); ATM IV unaffected. Verified `1.15 → 0.69`. `options.py`.
- [x] **#4 `fast_info.get` can raise / lose a ticker** — added fallback to last daily close (`history(period="1d")`) when fast_info has no price. `options.py`.
- [x] **#5 No CORS** — added `CORSMiddleware` (GET, all origins for now) so browser web clients can read the API. `api.py`.
- [x] **minor:** moved `import math` to module top in `commodities.py`.

## Deferred (not yet done)
- [ ] **#3 ATM IV picks call-side first within 3% band** — if multiple strikes sit inside the band it grabs the first iterated (a call), not the strike strictly nearest spot. Low-impact approximation. Fix: choose `min(abs(K-spot))` across both call+put.
- [ ] **CORS tightening** — `allow_origins=["*"]` is fine for a private deploy; restrict to the app domains before any public exposure.
- [ ] **API auth / rate-limit** — read-only and serves all data; intended as a private data service. Add auth/limits if ever exposed publicly. (Cutover hosting decision.)
- [ ] **Bad `sort` field on `/api/stocks`** silently no-ops (all-None sort). Could return `400` instead. Low.
- [ ] **Term structure (contango/backwardation)** — yfinance gives only continuous front-month; needs explicit contract months. Tracked in Trader-Screener `Commodities.md`.
- [ ] **DB persistence** for commodities/options — currently JSON-only (mirrors universe/model). Add SQLAlchemy models if the API moves to DB-backed reads.
- [ ] **IV rank / IV percentile** — needs accumulated history (the snapshot append exists for commodities; add for options).

## Cutover Phase 1 (deploy — in progress)
- [x] Fly config for the data service: `Dockerfile` (uvicorn `api:app`), `fly.toml`, `.dockerignore`, `DEPLOY.md`.
- [x] `DATA_DIR` env honored by `api.py`, `commodities.py`, `options.py` (shared `/data` volume).
- [x] **Single-machine model** — API + in-process APScheduler cron (`RUN_SCHEDULER=1`) on one always-on machine, since Fly volumes attach to one machine only. (Was wrongly drawn as two machines sharing a volume.)
- [x] **Retrofit inherited collectors to `DATA_DIR`** — `universe.py`, `fundamentals.py`, `model.py`, `etf_universe.py` now read/write/cache under `DATA_DIR` (chain intact: model→fundamentals→universe). `news.py` is DB-only (`DB_PATH` already on `/data`), no change. API on the volume now sees full data.
- [ ] Set real secrets in `fly secrets` (`FRED_API_KEY`, Google SA for sheets).
- [ ] Schedule the nightly `run.py` machine; confirm old app's cron is OFF after cutover.

## Related (other repos)
- [ ] Cutover phases 1–5 — see Trader-Screener `CUTOVER.md`.
- [ ] Wire Trader-Screener mobile client to `/api/...` endpoints.
