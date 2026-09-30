"""
yf_client.py — the single yfinance access layer
================================================
Every yfinance call in the pipeline routes through here, so pacing, rate-limit
backoff, and 429 logging are applied in ONE place instead of re-invented per
collector. Direct ``yf.download`` / ``yf.Ticker`` / ``yf.Tickers`` calls live
only in this module (and ``data_utils.run_batches``, which builds on the same
``pace`` / ``backoff`` primitives).

Thin wrappers
-------------
  yf_download(*a, **kw)  → yf.download, retried on a rate-limit signal
  yf_ticker(symbol)      → yf.Ticker  (construction paced)
  yf_tickers(symbols)    → yf.Tickers (accepts a list or a space-joined string)

Pacing knobs come from config.py (the canonical YF_* defaults) — no collector
keeps its own duplicate. ``pace()`` keeps at least ``config.YF_SLEEP`` seconds
between network calls; ``backoff()`` is the hard sleep of ``config.YF_BACKOFF``
seconds on a rate-limit signal, and run_batches calls the very same primitive
when a whole batch fails.
"""

import threading
import time

import yfinance as yf

import config

__all__ = [
    "yf_download", "yf_ticker", "yf_tickers",
    "pace", "backoff", "is_rate_limited",
]

# Monotonic timestamp of the last real yfinance call, guarded so the threaded
# callers (e.g. backfill_eps.py) pace against one shared clock.
_last_call = [0.0]
_lock = threading.Lock()


def is_rate_limited(exc):
    """True when an exception looks like a yfinance/Yahoo rate-limit (HTTP 429)."""
    text = str(exc).lower()
    return ("429" in text or "too many requests" in text
            or "rate limit" in text or "rate-limit" in text)


def pace():
    """Sleep so at least ``config.YF_SLEEP`` seconds separate network calls."""
    with _lock:
        gap = config.YF_SLEEP - (time.monotonic() - _last_call[0])
        if gap > 0:
            time.sleep(gap)
        _last_call[0] = time.monotonic()


def backoff(context=""):
    """Hard sleep ``config.YF_BACKOFF`` seconds after a rate-limit signal.

    The shared backoff: the yf_download wrapper calls it on a 429, and
    data_utils.run_batches calls it when a whole batch fails — one uniform log
    line and one canonical duration for every collector.
    """
    where = f" ({context})" if context else ""
    print(f"    ⚠️  yfinance rate limit{where} — backing off {config.YF_BACKOFF}s")
    time.sleep(config.YF_BACKOFF)


def _call(fn, label):
    """Run a yfinance call with shared pacing and rate-limit backoff/retry.

    Paces before each attempt; on a rate-limit signal it backs off and retries
    up to ``config.YF_RETRIES`` times, then re-raises. Any non-rate-limit error
    propagates immediately (the caller's own try/except still owns it)."""
    attempts = config.YF_RETRIES + 1
    for attempt in range(attempts):
        pace()
        try:
            return fn()
        except Exception as e:                       # noqa: BLE001 — re-raised below
            if is_rate_limited(e) and attempt < attempts - 1:
                backoff(label)
                continue
            raise


def yf_download(*args, **kwargs):
    """yf.download with shared pacing + rate-limit backoff/retry."""
    return _call(lambda: yf.download(*args, **kwargs), "download")


def yf_ticker(symbol):
    """yf.Ticker(symbol). Construction is paced; the object's own method calls
    (``.info``, ``.history`` …) hit the network under the caller's try/except."""
    pace()
    return yf.Ticker(symbol)


def yf_tickers(symbols):
    """yf.Tickers for a batch. ``symbols`` may be a list/tuple or an already
    space-joined string. Construction is paced."""
    if not isinstance(symbols, str):
        symbols = " ".join(symbols)
    pace()
    return yf.Tickers(symbols)
