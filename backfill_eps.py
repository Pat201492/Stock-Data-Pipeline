"""
backfill_eps.py — one-off backfill of eps_surprise (and next_earnings) for
existing Fundamentals rows.

fundamentals.py skips tickers whose DB row is still fresh (data_quality>=50,
< STALE_DAYS old), so the new earnings_dates-based eps_surprise never reaches
those rows on a normal run. This script fetches just the earnings_dates frame
per affected ticker and UPDATEs the two columns in place — no full re-fetch.

Run: PYTHONUTF8=1 .venv/Scripts/python backfill_eps.py
"""
import sqlite3, time
from concurrent.futures import ThreadPoolExecutor

from yf_client import yf_ticker
from fundamentals import _eps_surprise, _next_earnings_from_df

DB = "stocks.db"
WORKERS = 3          # low — yfinance throttles bursts and silently returns None
RETRIES = 2


def fetch(ticker):
    """Fetch earnings_dates with backoff. Distinguishes a real 'no data' from a
    throttle: only an empty/None frame after all retries returns None."""
    edf = None
    for attempt in range(RETRIES):
        try:
            edf = yf_ticker(ticker).get_earnings_dates(limit=12)
            if edf is not None and len(edf) > 0:
                break
        except Exception:
            edf = None
        time.sleep(1.0 * (attempt + 1))   # 1s, 2s backoff
    time.sleep(0.2)                        # gentle pacing between tickers
    return ticker, _eps_surprise(edf), _next_earnings_from_df(edf)


def main():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    rows = cur.execute(
        "select ticker from Fundamentals where eps_surprise is null "
        "and data_quality >= 30 order by ticker"
    ).fetchall()
    tickers = [r[0] for r in rows]
    print(f"{len(tickers)} tickers missing eps_surprise")

    done = filled = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for ticker, surp, nxt in ex.map(fetch, tickers):
            done += 1
            sets, args = [], []
            if surp is not None:
                sets.append("eps_surprise = ?"); args.append(surp)
                filled += 1
            if nxt:
                sets.append("next_earnings = ?"); args.append(nxt)
            if sets:
                args.append(ticker)
                cur.execute(f"update Fundamentals set {', '.join(sets)} where ticker = ?", args)
            if done % 200 == 0:
                con.commit()
                print(f"  {done}/{len(tickers)} — {filled} filled")
    con.commit()
    con.close()
    print(f"Done — {filled}/{len(tickers)} got an eps_surprise value")


if __name__ == "__main__":
    main()
