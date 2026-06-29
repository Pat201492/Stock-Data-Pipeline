"""
commodities.py — Commodities dashboard feed
============================================
Collects front-month futures for energy / metals / agriculture via yfinance,
derives momentum + range + realized-vol metrics, and overlays FRED macro
series (WTI, Brent, Henry Hub, retail gas) when FRED_API_KEY is configured.

Part of the SHARED pipeline — collect once, served to every consumer app.

Output:  commodities.json   (current snapshot + grouped cards)
History: commodities_history.json  (append one row per root per run, so
         %-change / range / vol stay computable over time)

Run:      python commodities.py
Schedule: nightly, after etf_universe (see run.py)
"""

import json, os, math, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

import yfinance as yf

try:
    import fred
    HAS_FRED = fred.configured()
except Exception:
    HAS_FRED = False

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.environ.get("DATA_DIR", SCRIPT_DIR)  # /data volume on Fly
OUT        = os.path.join(DATA_DIR, "commodities.json")
HIST       = os.path.join(DATA_DIR, "commodities_history.json")

# root: (display name, group, optional FRED overlay series for spot/official)
COMMODITIES = {
    # Energy
    "CL=F":  ("WTI Crude",     "Energy",      "DCOILWTICO"),
    "BZ=F":  ("Brent Crude",   "Energy",      "DCOILBRENTEU"),
    "NG=F":  ("Natural Gas",   "Energy",      "DHHNGSP"),
    "RB=F":  ("Gasoline",      "Energy",      None),
    "HO=F":  ("Heating Oil",   "Energy",      None),
    # Metals
    "GC=F":  ("Gold",          "Metals",      None),
    "SI=F":  ("Silver",        "Metals",      None),
    "PL=F":  ("Platinum",      "Metals",      None),
    "PA=F":  ("Palladium",     "Metals",      None),
    "HG=F":  ("Copper",        "Metals",      None),
    "ALI=F": ("Aluminum",      "Metals",      None),   # thinner on yfinance
    # Agriculture — grains & softs
    "ZC=F":  ("Corn",          "Agriculture", None),
    "ZW=F":  ("Wheat",         "Agriculture", None),
    "KE=F":  ("KC HRW Wheat",  "Agriculture", None),   # thinner on yfinance
    "RS=F":  ("Canola",        "Agriculture", None),   # thinner on yfinance
    "DC=F":  ("Milk (Class III)","Agriculture", None), # thinner on yfinance
    "ZS=F":  ("Soybeans",      "Agriculture", None),
    "ZL=F":  ("Soybean Oil",   "Agriculture", None),
    "ZM=F":  ("Soybean Meal",  "Agriculture", None),
    "ZO=F":  ("Oats",          "Agriculture", None),
    "ZR=F":  ("Rough Rice",    "Agriculture", None),
    "KC=F":  ("Coffee",        "Agriculture", None),
    "SB=F":  ("Sugar",         "Agriculture", None),
    "CC=F":  ("Cocoa",         "Agriculture", None),
    "CT=F":  ("Cotton",        "Agriculture", None),
    "OJ=F":  ("Orange Juice",  "Agriculture", None),   # thinner on yfinance
    "LBS=F": ("Lumber",        "Agriculture", None),   # thinner on yfinance
    # Livestock
    "LE=F":  ("Live Cattle",   "Livestock",   None),
    "GF=F":  ("Feeder Cattle", "Livestock",   None),
    "HE=F":  ("Lean Hogs",     "Livestock",   None),
}


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if (a is not None and b) else None


def _realized_vol(closes):
    """Annualized 20-day realized vol from a close series (list of floats)."""
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    rets = rets[-20:]
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var  = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return round((var ** 0.5) * (252 ** 0.5) * 100, 1)


def _metrics_from_history(hist):
    """hist: pandas DataFrame with a Close column (1y daily). Returns metric dict."""
    series = hist["Close"].dropna()
    closes = [float(c) for c in series.tolist()]
    if not closes:
        return None
    price = closes[-1]
    n = len(closes)
    def back(days):
        return closes[-(days + 1)] if n > days else (closes[0] if closes else None)
    # True YTD: anchor to the first close of the current calendar year (from the
    # dated index), not the first row of the 1y window.
    try:
        this_year = series.index[-1].year
        yr = series[series.index.year == this_year]
        ytd_anchor = float(yr.iloc[0]) if len(yr) else closes[0]
    except Exception:
        ytd_anchor = closes[0]
    hi = max(closes); lo = min(closes)
    rng = ((price - lo) / (hi - lo) * 100) if hi > lo else None
    return {
        "price":      round(price, 4),
        "chg_1d":     _pct(price, back(1)),
        "chg_1w":     _pct(price, back(5)),
        "chg_1m":     _pct(price, back(21)),
        "chg_1y":     _pct(price, closes[0]),
        "chg_ytd":    _pct(price, ytd_anchor),
        "high_52w":   round(hi, 4),
        "low_52w":    round(lo, 4),
        "range_pct":  round(rng, 1) if rng is not None else None,
        "vol_20d":    _realized_vol(closes),
        "spark":      [round(c, 4) for c in closes[-30:]],
    }


def _fred_overlay(series_id):
    if not (HAS_FRED and series_id):
        return None
    try:
        return fred.latest_with_change(series_id, limit=24)
    except Exception:
        return None


def main():
    print("Commodities feed — fetching futures via yfinance …")
    symbols = list(COMMODITIES.keys())
    # One batched download for all roots (1y daily closes).
    data = yf.download(symbols, period="1y", interval="1d",
                       group_by="ticker", progress=False, threads=True)

    snapshot = []
    asof = datetime.utcnow().strftime("%Y-%m-%d")
    for sym, (name, group, fred_series) in COMMODITIES.items():
        try:
            hist = data[sym] if len(symbols) > 1 else data
            m = _metrics_from_history(hist)
        except Exception as e:
            print(f"  ⚠️  {sym} {name}: {e}")
            m = None
        if not m:
            continue
        row = {"root": sym, "name": name, "group": group, "asof": asof, **m}
        overlay = _fred_overlay(fred_series)
        if overlay:
            row["fred"] = overlay  # official spot + change + sparkline
        snapshot.append(row)
        print(f"  {name:<14} {m['price']:>10}  1d {str(m['chg_1d']):>6}%  vol {m['vol_20d']}")

    # Guard: never clobber a good output with an empty one (e.g. a yfinance rate
    # limit fails every download). Leave the last snapshot in place and bail.
    if not snapshot:
        print("\n⚠️  0 commodities fetched (rate-limited or all delisted?) — "
              "keeping existing commodities.json, not overwriting.")
        return

    # group for the dashboard cards
    grouped = {}
    for r in snapshot:
        grouped.setdefault(r["group"], []).append(r)

    out = {"asof": asof, "count": len(snapshot), "groups": grouped, "commodities": snapshot}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n✅ Wrote {len(snapshot)} commodities → {OUT}")

    # append history (one row per root per run) — keeps vol/range honest over time
    history = []
    if os.path.exists(HIST):
        try:
            history = json.load(open(HIST))
        except Exception:
            history = []
    for r in snapshot:
        history.append({"asof": asof, "root": r["root"], "price": r["price"]})
    with open(HIST, "w") as f:
        json.dump(history, f)
    print(f"✅ Appended {len(snapshot)} rows → {HIST}")


if __name__ == "__main__":
    main()
