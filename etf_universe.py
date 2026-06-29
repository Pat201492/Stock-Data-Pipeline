"""
etf_universe.py — Build the ETF universe + holdings + a fundamentals-weighted score
====================================================================================
1. DISCOVER ETF tickers (NASDAQ ETF screener API; fallback to a built-in list)
2. ENRICH each via yfinance: overview (.info) + top holdings (.funds_data)
3. SCORE   weighted score = Σ(weight · holding score_composite) / Σ(weight covered)
           using our own valuations table (only holdings in our stock universe count)

Writes the `etfs` + `etf_holdings` tables. Incremental: skips ETFs refreshed in
the last STALE_DAYS. Run after model.py (needs valuations populated).

Run:  python etf_universe.py
"""
import json, os, time, warnings
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

import yfinance as yf
from database import SessionLocal, ETF, ETFHolding, Valuation, init_db, upsert

DATA_DIR    = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
CACHE_FILE  = os.path.join(DATA_DIR, "etf_cache.json")
SLEEP_SEC   = 1.0
STALE_DAYS  = 7

NASDAQ_ETF_URL = "https://api.nasdaq.com/api/screener/etf?tableonly=true&limit=10000&offset=0&download=true"

# Fallback set of widely-held ETFs (used if discovery is blocked / thin).
FALLBACK_ETFS = [
    "SPY","VOO","IVV","VTI","QQQ","QQQM","DIA","IWM","VEA","VWO","IEFA","IEMG",
    "VUG","VTV","VIG","VYM","SCHD","SCHX","SCHB","SCHG","SCHF","SPYG","SPYV",
    "XLK","XLF","XLE","XLV","XLI","XLY","XLP","XLU","XLB","XLRE","XLC",
    "VGT","VFH","VHT","VDE","VNQ","SMH","SOXX","IGV","IBB","XBI","KRE","KBE",
    "ARKK","ARKG","ARKW","IWF","IWD","IWB","IWN","IWO","MDY","IJH","IJR","VB","VO",
    "AGG","BND","BNDX","LQD","HYG","TLT","IEF","SHY","TIP","MUB","VCIT","VCSH",
    "GLD","SLV","IAU","GDX","USO","DBC","PDBC",
    "EFA","EEM","VXUS","ACWI","VT","BKLN","JEPI","JEPQ","DGRO","NOBL","RSP",
    "VTEB","VGK","EWJ","EWZ","FXI","MCHI","INDA","EWT","EWY","EWG","EWU",
    "ITOT","SPLG","SPTM","FNDX","FNDA","DFAC","AVUV","AVDV","COWZ","MOAT",
    "VBR","VOE","VONG","VONV","SPDW","SPEM","SPSB","SPIB","USFR","SGOV","BIL",
    "TQQQ","SQQQ","SOXL","UPRO","SH","PSQ","VXX","UVXY",
]


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_cache(c):
    with open(CACHE_FILE, "w") as f:
        json.dump(c, f, indent=2)


def discover():
    """ETF tickers from the NASDAQ ETF screener API (data.rows). Fallback list on failure."""
    try:
        from urllib.request import Request, urlopen
        req = Request(NASDAQ_ETF_URL, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urlopen(req, timeout=45).read().decode("utf-8", "ignore")).get("data", {})
        rows = data.get("rows") or data.get("table", {}).get("rows", [])
        found = []
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if sym and sym.isalpha() and len(sym) <= 5:
                found.append(sym)
        if len(found) >= 50:
            print(f"  NASDAQ ETF API: {len(found)} tickers")
            return list(dict.fromkeys(found))
        print(f"  NASDAQ ETF API thin ({len(found)}) — using fallback list")
    except Exception as e:
        print(f"  NASDAQ ETF API failed ({e}) — using fallback list")
    return list(dict.fromkeys(FALLBACK_ETFS))


def _fresh(db):
    cutoff = datetime.utcnow() - timedelta(days=STALE_DAYS)
    return {t for (t,) in db.query(ETF.ticker).filter(ETF.last_updated >= cutoff).all()}


def _score_map(db, tickers):
    """holding ticker -> score_composite from our valuations (stocks.db)."""
    if not tickers:
        return {}
    rows = db.query(Valuation.ticker, Valuation.score_composite).filter(
        Valuation.ticker.in_(list(tickers))
    ).all()
    return {t: s for t, s in rows if s is not None}


def enrich_one(ticker, db):
    t = yf.Ticker(ticker)
    info = {}
    try:
        info = t.info or {}
    except Exception:
        info = {}

    # Holdings (top ~10) via funds_data
    holdings = []
    sector_w = {}
    try:
        fd = t.funds_data
        th = fd.top_holdings  # DataFrame: index=ticker, cols Name, Holding Percent
        if th is not None and len(th):
            for sym, row in th.iterrows():
                holdings.append({
                    "ticker": str(sym).upper(),
                    "name":   str(row.get("Name", "")),
                    "weight": float(row.get("Holding Percent") or 0),
                })
        try:
            sector_w = dict(fd.sector_weightings or {})
        except Exception:
            sector_w = {}
    except Exception:
        pass

    # Weighted score over holdings we have a score for
    smap = _score_map(db, [h["ticker"] for h in holdings])
    cov_w = sum(h["weight"] for h in holdings if h["ticker"] in smap)
    wsum  = sum(h["weight"] * smap[h["ticker"]] for h in holdings if h["ticker"] in smap)
    weighted = round(wsum / cov_w, 1) if cov_w > 0 else None

    yld = info.get("yield")
    ytd = info.get("ytdReturn")
    # yfinance expense ratio is already a percentage number (SPY -> 0.0945 == 0.09%),
    # unlike yield/ytd which are fractions. Don't scale it.
    exp = info.get("annualReportExpenseRatio") or info.get("netExpenseRatio")

    etf_row = {
        "ticker":         ticker,
        "name":           info.get("longName") or info.get("shortName") or ticker,
        "category":       info.get("category", ""),
        "asset_class":    (info.get("legalType") or info.get("quoteType") or "").lower(),
        "aum":            info.get("totalAssets"),
        "expense_ratio":  round(exp, 3) if isinstance(exp, (int, float)) else None,
        "yield_pct":      round(yld * 100, 3) if isinstance(yld, (int, float)) and yld < 1 else yld,
        "ytd_return":     round(ytd * 100, 2) if isinstance(ytd, (int, float)) and abs(ytd) < 5 else ytd,
        "price":          info.get("navPrice") or info.get("regularMarketPrice") or info.get("currentPrice") or 0,
        "weighted_score": weighted,
        "covered_weight": round(cov_w, 4),
        "holdings_count": len(holdings),
        "last_updated":   datetime.utcnow(),
    }
    return etf_row, holdings, sector_w


def main():
    init_db()
    db = SessionLocal()
    tickers = discover()
    fresh = _fresh(db)
    todo = [t for t in tickers if t not in fresh]
    print(f"ETF universe: {len(tickers)} discovered, {len(fresh)} fresh, {len(todo)} to enrich")

    cache = load_cache()
    done = 0
    for i, ticker in enumerate(todo):
        if i % 25 == 0:
            print(f"  {i}/{len(todo)} …")
        try:
            etf_row, holdings, sector_w = enrich_one(ticker, db)
            if not etf_row["holdings_count"] and not etf_row["aum"]:
                continue  # not a real/known ETF
            upsert(db, ETF, etf_row)
            db.query(ETFHolding).filter(ETFHolding.etf_ticker == ticker).delete()
            for h in holdings:
                db.add(ETFHolding(
                    etf_ticker=ticker, holding_ticker=h["ticker"],
                    holding_name=h["name"], weight=h["weight"],
                ))
            db.commit()
            cache[ticker] = {"sectors": sector_w, "ts": datetime.utcnow().isoformat()}
            done += 1
        except Exception as e:
            db.rollback()
            print(f"    [warn] {ticker}: {type(e).__name__}: {str(e)[:80]}")
        time.sleep(SLEEP_SEC)

    save_cache(cache)
    total = db.query(ETF).count()
    db.close()
    print(f"Done — enriched {done}, {total} ETFs in DB.")


if __name__ == "__main__":
    main()
