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

from yf_client import yf_ticker, is_empty, EmptyUpstreamResponse, tolerate_empties
from fundamentals import _eps_surprise, _next_earnings_from_df

DB = "stocks.db"
WORKERS = 3          # low — yfinance throttles bursts and silently returns None
RETRIES = 2
MAX_EMPTY_RATE = 0.20  # earnings data expected at 80%+ hit rate


def fetch(ticker):
    """Fetch earnings_dates with backoff. Raises EmptyUpstreamResponse on empty,
    or returns (ticker, eps_surprise, next_earnings) on success."""
    edf = None
    for attempt in range(RETRIES):
        try:
            edf = yf_ticker(ticker).get_earnings_dates(limit=12)
            if not is_empty(edf):
                break
        except Exception:
            edf = None
        time.sleep(1.0 * (attempt + 1))   # 1s, 2s backoff
    time.sleep(0.2)                        # gentle pacing between tickers
    if is_empty(edf):
        raise EmptyUpstreamResponse(f"no earnings_dates for {ticker}", symbol=ticker, label="earnings_dates")
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

    done = filled = empty = 0
    futures = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for ticker in tickers:
            futures[ex.submit(fetch, ticker)] = ticker
        for future in futures:
            try:
                ticker, surp, nxt = future.result()
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
                    print(f"  {done}/{len(tickers)} — {filled} filled, {empty} empty")
            except EmptyUpstreamResponse:
                done += 1
                empty += 1
    con.commit()

    tolerate_empties("backfill_eps", len(tickers), empty, MAX_EMPTY_RATE)
    con.close()
    print(f"Done — {filled}/{len(tickers)} got an eps_surprise value ({empty} empty responses)")


if __name__ == "__main__":
    main()
