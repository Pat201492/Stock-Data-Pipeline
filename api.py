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

import json, os, time
from typing import Optional
from fastapi import FastAPI, HTTPException, Query

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TTL = 300  # re-read JSON at most every 5 min

app = FastAPI(title="Stock Data Pipeline API", version="1.0")

_cache = {}  # filename -> (expires, data)


def _load(name):
    path = os.path.join(SCRIPT_DIR, name)
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


@app.get("/health")
def health():
    have = {f: os.path.exists(os.path.join(SCRIPT_DIR, f))
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
