"""
api.py — Read-only data API for the Stock Data Pipeline
========================================================
The single data service. Consumer apps (Stock-App, Trader-Screener) are thin
clients that read from these endpoints — they do NOT collect data themselves.

Serves the pipeline's JSON outputs (universe / fundamentals / model /
commodities / options). App-only concerns (auth, accounts, static pages,
notifications) stay in the consumer apps, NOT here.

Run:   uvicorn api:app --reload --port 8000
Files are re-read with a short TTL so a nightly pipeline run is picked up
without a server restart.
"""

import json, os, sys, time, subprocess
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import config
from database import SessionLocal, News, PriceHistory, ETF, ETFHolding
from serializers import db_columns

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Where the pipeline writes its outputs. On Fly this is the /data volume, shared
# in-process by the API and the nightly scheduler (single machine — Fly volumes
# attach to one machine only, so API + cron live together). Resolved once in config.py.
DATA_DIR = config.DATA_DIR
TTL = 300  # re-read JSON at most every 5 min

app = FastAPI(title="Stock Data Pipeline API", version="1.0")


@app.on_event("startup")
def _start_scheduler():
    """Run the nightly pipeline in-process so the API and cron share one machine
    (and therefore one Fly volume). Enable with RUN_SCHEDULER=1; off by default
    so local dev / tests don't kick off a full pipeline run.

    NOTE: run a SINGLE uvicorn worker when RUN_SCHEDULER=1 — every worker process
    executes this hook, so N workers would fire the pipeline N times concurrently
    (double-collect + DB contention). The Dockerfile CMD uses one worker."""
    if os.environ.get("RUN_SCHEDULER") != "1":
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    def _run_pipeline():
        subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "run.py")],
                       cwd=SCRIPT_DIR,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(_run_pipeline, CronTrigger(hour=2, minute=0),
                  id="nightly_pipeline", max_instances=1, coalesce=True)
    sched.start()
    app.state.scheduler = sched

# Read-only data API → browser web clients need CORS. Allow all origins for now
# (data is non-sensitive market data); tighten to the app domains before any
# public deploy. See FOLLOWUPS.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_cache = {}     # filename -> (expires, data)  — JSON exports
_db_cache = {}  # query-key -> (expires, data)  — DB-only datasets (news/history/ETFs)


def _db_cached(key, builder):
    """Same short-TTL read-through cache as JSON `_load`, but for the DB-only
    datasets the API must query directly (news / price history / ETFs are
    authoritative in `stocks.db` with no JSON export — see README "Persistence").
    A nightly run is picked up within TTL without a server restart."""
    hit = _db_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    data = builder()
    _db_cache[key] = (time.time() + TTL, data)
    return data


def _serialize(obj, model, drop=("last_updated",)):
    """Project a SQLAlchemy row onto its model's columns (schema declared once in
    database.py, read via serializers.db_columns), jsonifying datetimes and
    dropping DB-only bookkeeping columns."""
    out = {}
    for c in db_columns(model):
        if c in drop:
            continue
        v = getattr(obj, c)
        if isinstance(v, datetime):
            v = v.isoformat()
        out[c] = v
    return out


def _load(name):
    path = os.path.join(DATA_DIR, name)
    hit = _cache.get(name)
    if hit and hit[0] > time.time():
        return hit[1]
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    _cache[name] = (time.time() + TTL, data)
    return data


def _rows(name, key="stocks"):
    d = _load(name)
    if d is None:
        return []
    if isinstance(d, list):
        return d
    return d.get(key, [])


@app.get("/api/integrity")
def integrity():
    """Data-integrity snapshot: freshness, coverage, quality distribution,
    model completeness, commodity fetch gaps."""
    u = _load("universe.json") or {}
    f = _load("fundamentals.json") or {}
    m = _load("model.json") or {}
    c = _load("commodities.json") or {}
    opt = _load("options.json") or {}
    exp = _load("exposure.json") or {}

    # commodity fetch gaps vs the configured set
    expected, missing = [], []
    try:
        from commodities import COMMODITIES
        expected = list(COMMODITIES.keys())
        have = {r["root"] for r in c.get("commodities", [])}
        missing = [{"root": r, "name": COMMODITIES[r][0]} for r in expected if r not in have]
    except Exception:
        pass

    return {
        "generated": {
            "universe": u.get("generated"),
            "fundamentals": f.get("generated"),
            "model": m.get("generated"),
            "commodities": c.get("asof"),
        },
        "coverage": {
            "universe": u.get("total"),
            "fundamentals": f.get("total"),
            "model": m.get("total"),
            "options_tickers": len(opt),
            "exposure_tickers": len(exp),
        },
        "quality_summary": f.get("quality_summary"),
        "model_completeness": {
            "total": m.get("total"),
            "dcf": m.get("dcf_computed"),
            "comps": m.get("comps_computed"),
            "m3": m.get("m3_computed"),
            "all3": m.get("all3_computed"),
        },
        "commodities": {
            "fetched": c.get("count"),
            "expected": len(expected),
            "missing": missing,
        },
    }


@app.get("/health")
def health():
    have = {f: os.path.exists(os.path.join(DATA_DIR, f))
            for f in ("universe.json", "fundamentals.json", "model.json",
                      "commodities.json", "options.json")}
    return {"ok": True, "outputs": have}


# ── Stocks screener ───────────────────────────────────────────────────────────
@app.get("/api/stocks")
def list_stocks(
    sector:  Optional[str] = None,
    cap_size: Optional[str] = None,
    search:  Optional[str] = None,
    sort:    str = "rank",
    order:   str = "asc",
    limit:   int = Query(100, le=1000),
    offset:  int = 0,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
):
    """Merge universe + fundamentals + model by ticker, then filter/sort/page."""
    fund = {r["ticker"]: r for r in _rows("fundamentals.json")}
    model = {r["ticker"]: r for r in _rows("model.json")}
    out = []
    for u in _rows("universe.json"):
        t = u["ticker"]
        f = fund.get(t, {}); m = model.get(t, {})
        out.append({
            "ticker": t,
            "name": u.get("name"),
            "sector": u.get("sector"),
            "cap_size": u.get("cap_size"),
            "mkt_cap": u.get("mkt_cap"),
            "price": u.get("price"),
            "rank": u.get("rank"),
            "pe": f.get("pe"),
            "roic": f.get("roic"),
            "roc_greenblatt": f.get("roc_greenblatt"),
            "ebit_ev_yield": f.get("ebit_ev_yield"),
            "magic_source": f.get("magic_source"),
            "magic_period_end": f.get("magic_period_end"),
            "magic_accession": f.get("magic_accession"),
            "magic_derived": f.get("magic_derived"),
            "score": m.get("score_composite") or m.get("score"),
            "upside": m.get("avg_upside") or m.get("upside"),
        })

    if sector:   out = [r for r in out if r["sector"] == sector]
    if cap_size: out = [r for r in out if r["cap_size"] == cap_size]
    if search:
        s = search.lower()
        out = [r for r in out if s in (r["ticker"] or "").lower()
               or s in (r["name"] or "").lower()]
    if min_score is not None:
        out = [r for r in out if (r["score"] or -1e9) >= min_score]
    if max_score is not None:
        out = [r for r in out if (r["score"] or 1e9) <= max_score]

    rev = (order == "desc")
    out.sort(key=lambda r: (r.get(sort) is None, r.get(sort)), reverse=rev)
    total = len(out)
    return {"total": total, "stocks": out[offset:offset + limit]}


@app.get("/api/stocks/{ticker}")
def get_stock(ticker: str):
    t = ticker.upper()
    merged = {}
    for name in ("universe.json", "fundamentals.json", "model.json"):
        for r in _rows(name):
            if r.get("ticker") == t:
                merged.update(r)
    if not merged:
        raise HTTPException(404, f"{t} not found")
    return merged


# ── Per-stock commodity exposure ──────────────────────────────────────────────
@app.get("/api/stocks/{ticker}/commodities")
def stock_commodities(ticker: str):
    """What commodities drive this stock — curated economic links + (optional)
    computed return-regression betas."""
    d = _load("exposure.json") or {}
    rec = d.get(ticker.upper())
    if not rec:
        raise HTTPException(404, f"no commodity exposure mapped for {ticker.upper()}")
    return {"ticker": ticker.upper(), **rec}


# ── Commodities ───────────────────────────────────────────────────────────────
@app.get("/api/commodities")
def commodities():
    d = _load("commodities.json")
    if d is None:
        raise HTTPException(503, "commodities.json not generated yet — run commodities.py")
    return d


@app.get("/api/commodities/{root}")
def commodity(root: str):
    d = _load("commodities.json")
    if d is None:
        raise HTTPException(503, "commodities.json not generated yet")
    for r in d.get("commodities", []):
        if r["root"].lower() == root.lower():
            try:
                from commodity_trade import for_root
                trade = for_root(r["root"])
                if trade:
                    r = {**r, "trade": trade}   # top exporters / importers
            except Exception:
                pass
            return r
    raise HTTPException(404, f"{root} not found")


# ── Options ───────────────────────────────────────────────────────────────────
@app.get("/api/options/{ticker}")
def options_summary(ticker: str):
    d = _load("options.json") or {}
    rec = d.get(ticker.upper())
    if not rec:
        raise HTTPException(404, f"no options for {ticker.upper()}")
    # summary + expiration dates only (lightweight)
    return {"ticker": ticker.upper(), "asof": rec.get("asof"),
            "summary": rec.get("summary"),
            "expirations": [e["expiration"] for e in rec.get("expirations", [])]}


@app.get("/api/options/{ticker}/{expiration}")
def options_chain(ticker: str, expiration: str):
    d = _load("options.json") or {}
    rec = d.get(ticker.upper())
    if not rec:
        raise HTTPException(404, f"no options for {ticker.upper()}")
    for e in rec.get("expirations", []):
        if e["expiration"] == expiration:
            return {"ticker": ticker.upper(), **e}
    raise HTTPException(404, f"expiration {expiration} not found for {ticker.upper()}")


# ── News + sentiment (DB-only dataset) ────────────────────────────────────────
@app.get("/api/news")
def news(ticker: Optional[str] = None, limit: int = Query(50, le=500)):
    """Latest news headlines + sentiment. Optionally scoped to one ticker.
    Served from the authoritative `news` table (no JSON export)."""
    t = ticker.upper() if ticker else None

    def build():
        db = SessionLocal()
        try:
            q = db.query(News)
            if t:
                q = q.filter(News.ticker == t)
            rows = q.order_by(News.published_at.desc()).limit(limit).all()
            return [_serialize(n, News, drop=("id", "last_updated")) for n in rows]
        finally:
            db.close()

    items = _db_cached(f"news:{t}:{limit}", build)
    return {"ticker": t, "count": len(items), "news": items}


@app.get("/api/stocks/{ticker}/news")
def stock_news(ticker: str, limit: int = Query(50, le=500)):
    """News + sentiment for one ticker (alias of /api/news?ticker=)."""
    return news(ticker=ticker, limit=limit)


# ── Price history (DB-only dataset) ───────────────────────────────────────────
@app.get("/api/stocks/{ticker}/history")
def stock_history(ticker: str, limit: int = Query(365, le=5000)):
    """Daily close/volume series for one ticker, oldest→newest. Defaults to the
    most recent ~1y (365 rows). Served from the authoritative `price_history`
    table (no JSON export)."""
    t = ticker.upper()

    def build():
        db = SessionLocal()
        try:
            # take the most recent `limit` rows, then return ascending by date
            rows = (db.query(PriceHistory)
                    .filter(PriceHistory.ticker == t)
                    .order_by(PriceHistory.date.desc())
                    .limit(limit).all())
            return [{"date": r.date, "close": r.close, "volume": r.volume}
                    for r in reversed(rows)]
        finally:
            db.close()

    series = _db_cached(f"history:{t}:{limit}", build)
    if not series:
        raise HTTPException(404, f"no price history for {t}")
    return {"ticker": t, "count": len(series), "history": series}


# ── ETFs / holdings (DB-only dataset) ─────────────────────────────────────────
@app.get("/api/etfs")
def etfs(
    asset_class: Optional[str] = None,
    category:    Optional[str] = None,
    search:      Optional[str] = None,
    sort:        str = "aum",
    order:       str = "desc",
    limit:       int = Query(100, le=1000),
    offset:      int = 0,
):
    """List ETFs (without holdings — use /api/etfs/{ticker} for those). Served
    from the authoritative `etfs` table (no JSON export)."""
    def build():
        db = SessionLocal()
        try:
            return [_serialize(e, ETF) for e in db.query(ETF).all()]
        finally:
            db.close()

    out = list(_db_cached("etfs:all", build))
    if asset_class:
        out = [r for r in out if r.get("asset_class") == asset_class]
    if category:
        out = [r for r in out if r.get("category") == category]
    if search:
        s = search.lower()
        out = [r for r in out if s in (r.get("ticker") or "").lower()
               or s in (r.get("name") or "").lower()]

    rev = (order == "desc")
    out.sort(key=lambda r: (r.get(sort) is None, r.get(sort)), reverse=rev)
    total = len(out)
    return {"total": total, "etfs": out[offset:offset + limit]}


@app.get("/api/etfs/{ticker}")
def etf_detail(ticker: str):
    """One ETF with its top holdings (weight-ranked)."""
    t = ticker.upper()

    def build():
        db = SessionLocal()
        try:
            e = db.query(ETF).filter(ETF.ticker == t).first()
            if not e:
                return None
            rec = _serialize(e, ETF)
            holdings = (db.query(ETFHolding)
                        .filter(ETFHolding.etf_ticker == t)
                        .order_by(ETFHolding.weight.desc()).all())
            rec["holdings"] = [
                {"ticker": h.holding_ticker, "name": h.holding_name,
                 "weight": h.weight}
                for h in holdings
            ]
            return rec
        finally:
            db.close()

    rec = _db_cached(f"etf:{t}", build)
    if rec is None:
        raise HTTPException(404, f"{t} not found")
    return rec
