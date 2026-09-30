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

import yf_client

try:
    import fred
    HAS_FRED = fred.configured()
except Exception:
    HAS_FRED = False

import config

OUT        = config.COMMODITIES_JSON            # /data volume on Fly
HIST       = config.COMMODITIES_HISTORY_JSON

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


_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]


def _monthly_and_seasonality(full):
    """full: pandas Close Series (up to 5y). Returns (monthly_avg, seasonality).
    monthly_avg = [[YYYY-MM, mean], …]; seasonality = [[MonthName, mean, index]]
    where index = month mean / overall mean × 100 (100 = average)."""
    monthly, seasonality = [], []
    try:
        m = full.resample("ME").mean().dropna()
        monthly = [[f"{idx.year:04d}-{idx.month:02d}", round(float(v), 4)] for idx, v in m.items()]
        overall = float(full.mean())
        by_month = full.groupby(full.index.month).mean()
        for mo in range(1, 13):
            if mo in by_month.index:
                avg = float(by_month.loc[mo])
                seasonality.append([_MONTHS[mo - 1], round(avg, 4),
                                    round(avg / overall * 100, 1) if overall else None])
    except Exception:
        pass
    return monthly, seasonality


def _metrics_from_history(hist):
    """hist: pandas DataFrame with a Close column (up to 5y daily). 1y metrics
    come from the tail; monthly averages + seasonality use the full span."""
    full = hist["Close"].dropna()
    series = full.tail(252)                 # ~1y for the headline metrics
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
    # Dated price history (~1y, downsampled to ≤130 points) for the detail chart.
    history = []
    try:
        s2 = series.iloc[-252:]
        step = max(1, len(s2) // 130)
        for i in range(0, len(s2), step):
            history.append([str(s2.index[i].date()), round(float(s2.iloc[i]), 4)])
    except Exception:
        history = []
    monthly, seasonality = _monthly_and_seasonality(full)
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
        "history":    history,
        "monthly":    monthly,
        "seasonality": seasonality,
    }


def _fred_overlay(series_id):
    if not (HAS_FRED and series_id):
        return None
    try:
        return fred.latest_with_change(series_id, limit=24)
    except Exception:
        return None


def main():
    with config.yfinance_lock():
        print("Commodities feed — fetching futures via yfinance …")
        symbols = list(COMMODITIES.keys())
        # 5y daily closes — 1y metrics come from the tail; monthly averages +
        # seasonality need the multi-year span (crop cycles, seasonal logistics).
        # Shared wrapper: paces the call and backs off on a rate-limit signal before
        # giving up. A persistent 429 raises through, and the empty-snapshot guard
        # below then keeps the existing commodities.json rather than clobbering it.
        try:
            data = yf_client.yf_download(symbols, period="5y", interval="1d",
                                         group_by="ticker", progress=False, threads=True)
        except Exception as e:
            print(f"  ⚠️  yfinance download failed: {e}")
            data = None

        snapshot = []
        asof = datetime.utcnow().strftime("%Y-%m-%d")
        if data is not None:
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
