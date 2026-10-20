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

import json, os, sys, time, subprocess, threading
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import config
from database import SessionLocal, News, PriceHistory, ETF, ETFHolding
from serializers import db_columns
from sqlalchemy import and_, func, not_, or_
from politicians_database import InsiderTrade, SessionLocal as PolSession

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Where the pipeline writes its outputs. On Fly this is the /data volume, shared
# in-process by the API and the nightly scheduler (single machine — Fly volumes
# attach to one machine only, so API + cron live together). Resolved once in config.py.
DATA_DIR = config.DATA_DIR
TTL = 300  # re-read JSON at most every 5 min

app = FastAPI(title="Stock Data Pipeline API", version="1.0")


# One lock guards every in-process pipeline run — the 02:00 cron job AND the
# cold-volume bootstrap below. Held for the whole run so the two can never overlap
# (a double-collect would contend on the DB and the /data volume).
_pipeline_lock = threading.Lock()


def _run_pipeline():
    """The nightly APScheduler job (also the bootstrap thread's target). Runs the
    SAME sequence as the CLI scheduler (market, political, validate) in-process —
    not run.py alone (#48). Holds _pipeline_lock so a cron fire and a bootstrap
    never run at once. A failing step must never raise out of here, or APScheduler
    drops the job."""
    import scheduler
    with _pipeline_lock:
        try:
            ok, results = scheduler.run_all()
            print(f"nightly pipeline finished ok={ok} steps={results}", flush=True)
        except Exception as e:
            print(f"nightly pipeline crashed: {e}", flush=True)


def _maybe_bootstrap():
    """Cold-volume bootstrap: a fresh /data volume serves nothing until the first
    02:00 run. If there's no fundamentals.json yet, kick one run now in a background
    thread so the API has data to serve within the hour. BOOTSTRAP_ON_EMPTY=0 turns
    it off. The thread target is _run_pipeline, so it holds _pipeline_lock and can
    never overlap the cron job. Returns the started thread, or None."""
    if os.environ.get("BOOTSTRAP_ON_EMPTY") == "0":
        return None
    if os.path.exists(os.path.join(DATA_DIR, "fundamentals.json")):
        return None
    print("bootstrap: no fundamentals.json on the volume — starting one "
          "pipeline run in the background", flush=True)
    t = threading.Thread(target=_run_pipeline, name="bootstrap", daemon=True)
    t.start()
    app.state.bootstrap_thread = t
    return t


def _pipeline_tz():
    """The nightly job's zone: PIPELINE_TZ (default UTC; "local" = this
    machine's zone, DST-aware)."""
    tz = os.environ.get("PIPELINE_TZ") or "UTC"
    if tz == "local":
        from tzlocal import get_localzone
        return get_localzone()
    from zoneinfo import ZoneInfo
    return ZoneInfo(tz)


def _pipeline_hour():
    return int(os.environ.get("PIPELINE_HOUR") or os.environ.get("PIPELINE_HOUR_UTC") or 2)


def _grace_hours():
    return float(os.environ.get("PIPELINE_MISFIRE_GRACE_HOURS") or 6)


def _last_slot(now):
    """The most recent nightly slot (PIPELINE_HOUR:00 in PIPELINE_TZ) at or
    before `now`, as an aware datetime."""
    from datetime import timedelta
    local = now.astimezone(_pipeline_tz())
    slot = local.replace(hour=_pipeline_hour(), minute=0, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    return slot


def _last_run_times():
    """(started_at, finished_at) of the last recorded run, either None."""
    from datetime import datetime
    try:
        with open(os.path.join(DATA_DIR, "last_run.json"), encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return None, None                 # missing / unreadable: never ran here
    out = []
    for k in ("started_at", "finished_at"):
        try:
            out.append(datetime.fromisoformat(rec[k]))
        except Exception:
            out.append(None)
    return tuple(out)


def _missed_slot(now):
    """The nightly slot this host slept through, or None: the latest slot is
    within the misfire grace window and no run has started since it (#63 --
    asleep 19:35-22:08 on 2026-10-19, so the 22:00 run never began)."""
    from datetime import timedelta
    slot = _last_slot(now)
    if now - slot > timedelta(hours=_grace_hours()):
        return None
    started, _ = _last_run_times()
    if started is not None and started >= slot:
        return None
    return slot


def _nightly():
    """The cron job's target. After a sleep the scheduler's timer fires late --
    by then catch-up may already have run tonight's pipeline, so a run that
    started since the slot (or one in progress) means there is nothing to do."""
    from datetime import datetime, timezone
    if _pipeline_lock.locked() or _missed_slot(datetime.now(timezone.utc)) is None:
        print("nightly: tonight's run already started -- skipping", flush=True)
        return
    _run_pipeline()


def _maybe_catch_up(now=None):
    """Catch-up for a host that is not always on (a PC that was asleep or off at
    the nightly hour). Start one run in the background when the volume has data
    and either:
      * tonight's slot was missed (#63): it passed within the misfire grace
        window and no run has started since; or
      * the last run finished more than CATCHUP_AFTER_HOURS ago (default 26),
        or no run was ever recorded.
    Same target and lock as the cron job, so it can never overlap it.
    CATCHUP_ON_START=0 turns it off. Returns the thread, or None."""
    from datetime import datetime, timezone, timedelta
    if os.environ.get("CATCHUP_ON_START") == "0":
        return None
    if _pipeline_lock.locked():
        return None                       # a run is in progress; it will write last_run.json
    if not os.path.exists(os.path.join(DATA_DIR, "fundamentals.json")):
        return None                       # empty volume: _maybe_bootstrap's job
    now = now or datetime.now(timezone.utc)
    hours = float(os.environ.get("CATCHUP_AFTER_HOURS") or 26)
    _, last = _last_run_times()
    missed = _missed_slot(now)
    if missed is None and last is not None and now - last < timedelta(hours=hours):
        return None
    why = (f"missed the {missed.isoformat()} slot" if missed is not None else
           f"last run {last.isoformat() if last else 'never recorded'} is older than {hours:g}h")
    print(f"catch-up: {why} -- starting one pipeline run now", flush=True)
    t = threading.Thread(target=_run_pipeline, name="catch-up", daemon=True)
    t.start()
    app.state.catchup_thread = t
    return t


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

    # The nightly hour, in PIPELINE_TZ (default UTC; "local" = this machine's
    # zone, DST-aware). PIPELINE_HOUR, falling back to the older PIPELINE_HOUR_UTC,
    # default 2. The PC host sets PIPELINE_TZ=local / PIPELINE_HOUR=22 in
    # run-local.ps1: a fixed 02:00 UTC drifted from 22:00 to 21:00 Eastern when
    # DST ends. misfire_grace_time: APScheduler's default is ~1s, so a host
    # asleep at the hour silently skipped the night; within the grace window
    # (PIPELINE_MISFIRE_GRACE_HOURS, default 6) a late wake still runs it.
    tz = _pipeline_tz()
    hour = _pipeline_hour()
    grace = int(_grace_hours() * 3600)
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(_nightly, CronTrigger(hour=hour, minute=0, timezone=tz),
                  id="nightly_pipeline", max_instances=1, coalesce=True,
                  misfire_grace_time=grace)
    # Catch-up check every 15 minutes: a PC asleep at the hour used to skip the
    # night -- start-up catch-up only fires when the API starts, and the cron
    # timer itself runs late after a sleep. Same rule as at start-up (a missed
    # slot, or the last run older than CATCHUP_AFTER_HOURS; nothing running).
    # CATCHUP_HOURLY=0 turns it off.
    if os.environ.get("CATCHUP_HOURLY") != "0":
        from apscheduler.triggers.interval import IntervalTrigger
        sched.add_job(_maybe_catch_up, IntervalTrigger(minutes=15), id="catch_up_check",
                      max_instances=1, coalesce=True)
    sched.start()
    app.state.scheduler = sched

    if _maybe_bootstrap() is None:
        _maybe_catch_up()

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
        # Per-step upstream empty-response rates (issue #38). Written by the
        # yfinance fetch layer; lets a human / Stock-App #109 see when a step
        # came back mostly empty (source throttled/down) rather than clean.
        "empty_rates": _load("empty_rates.json") or {},
    }


@app.get("/health")
def health():
    have = {f: os.path.exists(os.path.join(DATA_DIR, f))
            for f in ("universe.json", "fundamentals.json", "model.json",
                      "commodities.json", "options.json")}
    # last_run.json is written by scheduler.run_all after each night. Report it
    # (null before the first run). Status stays 200 even when the last run failed,
    # so a bad night does not fail Fly's health check and restart the machine.
    last_run = None
    path = os.path.join(DATA_DIR, "last_run.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                last_run = json.load(f)
        except Exception:
            last_run = None
    # Config report — BOOLEANS ONLY, never the secret values. A silent-degrade
    # catcher: SEC_USER_AGENT unset → yfinance fallback (no as-filed XBRL);
    # FRED_API_KEY unset → /api/macro 503. data_dir is a path, not a secret.
    return {
        "ok": True,
        "outputs": have,
        "last_run": last_run,
        "data_dir": DATA_DIR,
        "config": {
            "sec_user_agent": bool(os.environ.get("SEC_USER_AGENT", "").strip()),
            "fred_api_key":   bool(os.environ.get("FRED_API_KEY", "").strip()),
            "run_scheduler":  os.environ.get("RUN_SCHEDULER") == "1",
        },
    }


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
            "roc_nwc_floored": f.get("roc_nwc_floored"),
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


# ── Macro regime (FRED) ───────────────────────────────────────────────────────
@app.get("/api/macro")
def macro():
    """VIX term structure + 10y-3m spread for the dashboard's macro-regime soft
    gates (Trader-Screener #39). Shape matches its pipeline-mock contract:
    {vix, vixcls, vix9d, vix3m, t10y3m, t10y3m_monthly, asof}.

    Each series is fetched independently via fred.latest_with_change (so the
    existing cache + 429 retry apply); a series that fails or is missing is null
    on its own and does not fail the whole response. vix9d has no FRED source, so
    it is always null. 503 when FRED_API_KEY is unset."""
    import fred
    if not fred.configured():
        return JSONResponse(status_code=503, content={"error": "FRED_API_KEY not set"})

    dates = []

    def latest(series_id):
        try:
            r = fred.latest_with_change(series_id)
        except Exception:
            return None   # per-series failure stays null, response still 200
        if not r:
            return None
        if r.get("asof"):
            dates.append(r["asof"])
        return r["value"]

    vixcls = latest("VIXCLS")
    vix3m  = latest("VXVCLS")
    t10y3m = latest("T10Y3M")
    t10y3m_monthly = latest("T10Y3MM")

    return {
        "vix":            vixcls,
        "vixcls":         vixcls,
        "vix9d":          None,   # no FRED source
        "vix3m":          vix3m,
        "t10y3m":         t10y3m,
        "t10y3m_monthly": t10y3m_monthly,
        "asof":           max(dates) if dates else None,
    }


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


# ---------------------------------------------------------------------------
# Insider trades (#57, #58): the market-wide Form 4 table in politicians.db.
# SEC full-text search returns at most the newest 100 filings; this serves all
# of them, filterable, with impossible source rows kept out of the default view.
# ---------------------------------------------------------------------------

# A title matches a role when it contains any of these (case-insensitive).
_ROLE_WORDS = {
    "officer": ("officer", "chief", "ceo", "cfo", "coo", "president",
                "vp", "evp", "svp"),
    "director": ("director",),
    "ten_pct": ("10%",),
}


def _parse_day(value, name):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}")


def _insider_window(from_, to):
    """(from, to) dates; `to` defaults to today and `from` to 30 days before it."""
    end = _parse_day(to, "to") if to else date.today()
    start = _parse_day(from_, "from") if from_ else end - timedelta(days=30)
    if start > end:
        raise HTTPException(400, f"from {start} is after to {end}")
    return start, end


# No insider stake on the market is worth this much; a row implying one has a
# bad price (IHT: a ~$2 stock "sold" at $50,410.80, a $400bn stake after).
# Checked on a year of live rows: it flags exactly those IHT rows. A check
# against stocks.db closes was tried and dropped (#61) -- it flagged option
# exercises at strike, foreign insiders' home-market share prices against the
# US ADR close, and rows on a ticker whose stored close was wrong.
MAX_HOLDING_VALUE = 3e11


def _suspect_expr():
    """SQL for the bad-row guard (#57, #61). A row is suspect when:
      * price > $100,000, or value > $5bn;
      * shares x price is more than 10x away from the stated value;
      * shares owned after x price exceeds MAX_HOLDING_VALUE.
    NULLs read as 0, so a missing figure never makes a row suspect (and the
    expression is never NULL -- which a NOT would silently drop)."""
    p = func.coalesce(InsiderTrade.price_per_share, 0)
    v = func.coalesce(InsiderTrade.total_value, 0)
    s = func.coalesce(InsiderTrade.shares, 0)
    after = func.coalesce(InsiderTrade.shares_owned_after, 0)
    implied = p * s
    rules = [p > 100_000, v > 5e9,
             and_(implied > 0, v > 0, or_(v > implied * 10, implied > v * 10)),
             after * p > MAX_HOLDING_VALUE]
    return or_(*rules)


def _insider_filters(start, end, role=None, include_suspect=False):
    conds = [InsiderTrade.transaction_date >= start,
             InsiderTrade.transaction_date <= end]
    if role:
        words = _ROLE_WORDS.get(role)
        if words is None:
            raise HTTPException(400, f"role must be one of {sorted(_ROLE_WORDS)}")
        conds.append(or_(*[InsiderTrade.insider_title.ilike(f"%{w}%") for w in words]))
    if not include_suspect:
        conds.append(not_(_suspect_expr()))
    return conds


def _insider_row(r, suspect):
    out = _serialize(r, InsiderTrade, drop=())
    for k, v in out.items():
        if isinstance(v, date):
            out[k] = v.isoformat()
    url = r.source_url or ""
    out["sec_url"] = url[len("mirror:"):] if url.startswith("mirror:") else url
    out["suspect"] = bool(suspect)
    return out


@app.get("/api/insider-trades")
def insider_trades(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    type: Optional[str] = None,
    ticker: Optional[str] = None,
    insider: Optional[str] = None,
    role: Optional[str] = None,
    min_value: Optional[float] = None,
    sort: str = "date",
    limit: int = Query(200, ge=1),
    offset: int = Query(0, ge=0),
    include_suspect: int = 0,
):
    """Insider trades by transaction date (default the last 30 days), newest
    first or largest first (`sort=value`). `total` counts every match before
    limit/offset; suspect rows are neither returned nor counted unless
    `include_suspect=1`."""
    start, end = _insider_window(from_, to)
    if sort not in ("date", "value"):
        raise HTTPException(400, "sort must be date or value")
    limit = min(limit, 2000)
    codes = sorted({c.strip().upper() for c in (type or "").split(",") if c.strip()})
    t = (ticker or "").strip().upper()
    who = (insider or "").strip()
    key = (f"insider:{start}:{end}:{','.join(codes)}:{t}:{who.lower()}:{role}:"
           f"{min_value}:{sort}:{limit}:{offset}:{int(bool(include_suspect))}")

    def build():
        db = PolSession()
        try:
            conds = _insider_filters(start, end, role, bool(include_suspect))
            if codes:
                conds.append(InsiderTrade.transaction_type.in_(codes))
            if t:
                conds.append(func.upper(InsiderTrade.ticker) == t)
            if who:
                conds.append(InsiderTrade.insider_name.ilike(f"%{who}%"))
            if min_value is not None:
                conds.append(InsiderTrade.total_value >= min_value)
            q = db.query(InsiderTrade, _suspect_expr()).filter(*conds)
            total = db.query(func.count(InsiderTrade.filing_id)).filter(*conds).scalar()
            if sort == "value":
                q = q.order_by(InsiderTrade.total_value.desc().nullslast(),
                               InsiderTrade.transaction_date.desc())
            else:
                q = q.order_by(InsiderTrade.transaction_date.desc(),
                               InsiderTrade.filing_id)
            rows = [_insider_row(r, sus) for r, sus in q.offset(offset).limit(limit).all()]
            return {"count": len(rows), "total": total, "rows": rows}
        finally:
            db.close()

    return _db_cached(key, build)


@app.get("/api/insider-trades/rollups")
def insider_rollups(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    role: Optional[str] = None,
    include_suspect: int = 0,
    cluster_min_value: float = 100_000,
    cluster_min_avg: float = 10_000,
):
    """The three market reads: the 25 largest buys, the 25 tickers insiders
    sold most (by value), and cluster buys -- 3+ different insiders buying one
    ticker within any 14-day window in the range, together at least
    `cluster_min_value` dollars and averaging at least `cluster_min_avg` per
    insider (#60: TSM's 30 share-plan buys totalled $121k, ~$4k each)."""
    start, end = _insider_window(from_, to)
    if cluster_min_value < 0 or cluster_min_avg < 0:
        raise HTTPException(400, "cluster_min_value and cluster_min_avg must be >= 0")
    key = (f"insider-rollups:{start}:{end}:{role}:{int(bool(include_suspect))}:"
           f"{cluster_min_value}:{cluster_min_avg}")

    def build():
        db = PolSession()
        try:
            conds = _insider_filters(start, end, role, bool(include_suspect))
            # Rollups are per company: a row with no real ticker (blank, or the
            # source placeholder NONE) cannot be one.
            conds.append(func.upper(func.coalesce(InsiderTrade.ticker, "")).notin_(["", "NONE"]))
            top = (db.query(InsiderTrade, _suspect_expr())
                   .filter(*conds, InsiderTrade.transaction_type == "P")
                   .order_by(InsiderTrade.total_value.desc().nullslast())
                   .limit(25).all())
            sold = (db.query(InsiderTrade.ticker,
                             func.max(InsiderTrade.company_name),
                             func.sum(func.coalesce(InsiderTrade.total_value, 0)),
                             func.count(InsiderTrade.filing_id),
                             func.count(func.distinct(InsiderTrade.insider_name)))
                    .filter(*conds, InsiderTrade.transaction_type == "S")
                    .group_by(InsiderTrade.ticker)
                    .order_by(func.sum(func.coalesce(InsiderTrade.total_value, 0)).desc())
                    .limit(25).all())
            buys = (db.query(InsiderTrade.ticker, InsiderTrade.company_name,
                             InsiderTrade.insider_name, InsiderTrade.transaction_date,
                             InsiderTrade.total_value)
                    .filter(*conds, InsiderTrade.transaction_type == "P")
                    .order_by(InsiderTrade.ticker, InsiderTrade.transaction_date).all())
            return {
                "top_buys": [_insider_row(r, sus) for r, sus in top],
                "most_sold": [{"ticker": tk, "company_name": cn, "sell_value": sv,
                               "sell_count": sc, "insiders": ni}
                              for tk, cn, sv, sc, ni in sold],
                "clusters": _clusters(buys, min_value=cluster_min_value,
                                      min_avg=cluster_min_avg),
            }
        finally:
            db.close()

    return _db_cached(key, build)


def _clusters(buys, window_days=14, min_insiders=3, min_value=0, min_avg=0):
    """Per ticker, the 14-day window holding the most distinct buyers among
    windows with at least 3 of them, at least `min_value` bought in total and
    at least `min_avg` per buyer. `buys` is (ticker, company, insider, date,
    value) sorted by ticker then date."""
    by_ticker = {}
    for b in buys:
        if b[0]:
            by_ticker.setdefault(b[0], []).append(b)
    out = []
    for tk, rows in by_ticker.items():
        best = None
        for i, first in enumerate(rows):
            stop = first[3] + timedelta(days=window_days)
            win = [r for r in rows[i:] if r[3] <= stop]
            people = {r[2] for r in win if r[2]}
            value = sum(r[4] or 0 for r in win)
            if len(people) >= min_insiders and value >= min_value \
                    and value >= min_avg * len(people) \
                    and (best is None or len(people) > best[0]):
                best = (len(people), win)
        if best:
            n, win = best
            out.append({"ticker": tk, "company_name": win[0][1], "insiders": n,
                        "buy_value": sum(r[4] or 0 for r in win),
                        "first_date": win[0][3].isoformat(),
                        "last_date": win[-1][3].isoformat()})
    out.sort(key=lambda c: (c["insiders"], c["buy_value"]), reverse=True)
    return out
