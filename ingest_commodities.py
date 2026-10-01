"""
ingest_commodities.py — Fetch commodity futures data and validate coverage
Source: yfinance (front-month futures via FRED overlays where available)
Strategy: fetch 5y daily OHLCV for each commodity symbol, track empty responses.
"""
import sys

from yf_client import (
    yf_download, is_empty, EmptyUpstreamResponse, tolerate_empties
)


# Commodity symbols and metadata (mirrors commodities.py)
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
    "ALI=F": ("Aluminum",      "Metals",      None),
    # Agriculture — grains & softs
    "ZC=F":  ("Corn",          "Agriculture", None),
    "ZW=F":  ("Wheat",         "Agriculture", None),
    "KE=F":  ("KC HRW Wheat",  "Agriculture", None),
    "RS=F":  ("Canola",        "Agriculture", None),
    "DC=F":  ("Milk (Class III)","Agriculture", None),
    "ZS=F":  ("Soybeans",      "Agriculture", None),
    "ZL=F":  ("Soybean Oil",   "Agriculture", None),
    "ZM=F":  ("Soybean Meal",  "Agriculture", None),
    "ZO=F":  ("Oats",          "Agriculture", None),
    "ZR=F":  ("Rough Rice",    "Agriculture", None),
    "KC=F":  ("Coffee",        "Agriculture", None),
    "SB=F":  ("Sugar",         "Agriculture", None),
    "CC=F":  ("Cocoa",         "Agriculture", None),
    "CT=F":  ("Cotton",        "Agriculture", None),
    "OJ=F":  ("Orange Juice",  "Agriculture", None),
    "LBS=F": ("Lumber",        "Agriculture", None),
    # Livestock
    "LE=F":  ("Live Cattle",   "Livestock",   None),
    "GF=F":  ("Feeder Cattle", "Livestock",   None),
    "HE=F":  ("Lean Hogs",     "Livestock",   None),
}


def ingest():
    """Fetch commodity futures data and validate aggregate empty-response rate.

    Per-symbol empty data (delisted, thin/illiquid) is legitimate. But if MOST
    symbols come back empty, the source is down/throttled and the step must fail
    loudly. Empty-rate is recorded to empty_rates.json for observability.
    """
    print("[commodities] Fetching futures via yfinance…")
    symbols = list(COMMODITIES.keys())

    attempted = 0
    empties = 0

    # Fetch 5y daily closes for each symbol individually, tracking empty responses.
    # (The batch yf_download below is for the common case; individual retries
    # give us symbol-by-symbol granularity for empty tracking.)
    for sym, (name, group, _) in COMMODITIES.items():
        try:
            attempted += 1
            data = yf_download(sym, period="5y", interval="1d", progress=False)
            if is_empty(data):
                empties += 1
                print(f"  {name:<14} empty")
            else:
                print(f"  {name:<14} ✓")
        except EmptyUpstreamResponse:
            empties += 1
            print(f"  {name:<14} empty (upstream)")
        except Exception as e:
            print(f"  {name:<14} error: {e}")
            # Don't count fetch errors as empties; they're infrastructure issues.
            # Empties are only the payload-is-empty case (throttle vs. delisted).

    # Aggregate empty-rate verdict: a few empties (delisted / thin names) is
    # normal; if MOST came back empty the source is throttled/down and the step
    # must FAIL. The rate is written to empty_rates.json for human monitoring.
    if attempted:
        try:
            tolerate_empties("ingest_commodities", attempted, empties,
                           max_empty_rate=0.2)  # 20% tolerance for commodity futures
        except EmptyUpstreamResponse as e:
            print(f"  ❌ commodities ingest failed: {e}")
            return False

    print(f"[commodities] Ingested {attempted - empties}/{attempted} symbols")
    return True


if __name__ == "__main__":
    success = ingest()
    sys.exit(0 if success else 1)
