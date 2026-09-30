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
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

import config

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

_cache = {}  # filename -> (expires, data)


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
