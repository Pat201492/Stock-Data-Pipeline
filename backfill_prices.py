"""
backfill_prices.py -- load several years of daily closes into price_history (#65).

The nightly news step keeps one year current. Research that needs longer
windows (the dealer-hedging test's 60-day issuance windows and horizons)
backfills once with this. Idempotent: rows already present are skipped, so a
second run adds nothing.

  python backfill_prices.py                     # INDEX_PROXIES, 3 years
  python backfill_prices.py --years 5 SPY NVDA  # any tickers
"""
import argparse

import config
from database import SessionLocal, init_db
from news import fetch_and_store_prices
from yf_client import EmptyUpstreamResponse


def backfill(tickers, years=3, db=None, fetch=fetch_and_store_prices):
    """{ticker: rows_added}; a ticker whose source is empty maps to None."""
    own = db is None
    if own:
        init_db()
        db = SessionLocal()
    out = {}
    try:
        for t in tickers:
            try:
                out[t] = fetch(t, db, period=f"{int(years)}y")
            except EmptyUpstreamResponse:
                out[t] = None
    finally:
        if own:
            db.close()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tickers", nargs="*", help="default: config.INDEX_PROXIES")
    ap.add_argument("--years", type=int, default=3)
    a = ap.parse_args(argv)
    tickers = [t.upper() for t in a.tickers] or config.INDEX_PROXIES
    for t, n in backfill(tickers, a.years).items():
        print(f"{t}: {'empty upstream' if n is None else f'{n} row(s) added'}")


if __name__ == "__main__":
    main()
