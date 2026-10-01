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

import json
import os
import threading
import time

import yfinance as yf

import config

__all__ = [
    "yf_download", "yf_ticker", "yf_tickers",
    "pace", "backoff", "is_rate_limited",
    "EmptyUpstreamResponse", "is_empty", "yf_info_with_retry",
    "tolerate_empties", "empty_rate_report", "reset_empty_rates",
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


def backoff(context="", attempt=0):
    """Hard sleep after a rate-limit signal, growing with ``attempt``.

    The shared backoff: the yf_download wrapper calls it on a 429, and
    data_utils.run_batches calls it when a whole batch fails — one uniform log
    line and one canonical duration for every collector. The delay is
    ``config.YF_BACKOFF * 2**attempt`` (exponential), so a persistent throttle is
    not hammered with the same 1.5s gap every time; ``attempt=0`` (the default)
    keeps the historical flat duration for existing callers.
    """
    delay = config.YF_BACKOFF * (2 ** max(attempt, 0))
    where = f" ({context})" if context else ""
    print(f"    ⚠️  yfinance rate limit{where} — backing off {delay}s")
    time.sleep(delay)


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
                backoff(label, attempt)
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


# ── Empty-upstream response signal (issue #38) ──────────────────────────────────
# An empty dict / empty DataFrame / empty list from Yahoo is NOT success. The old
# ``yft.info or {}`` idiom swallowed a throttled/degraded response and made it
# look like a legitimately empty one, so a stage completed "clean" while silently
# collecting nothing. The core fetch path now RAISES EmptyUpstreamResponse on an
# empty payload; a single call cannot tell throttle from a truly empty symbol, so
# that verdict belongs to the CALLER, which alone knows the expected hit rate over
# a whole batch (see ``tolerate_empties``).

class EmptyUpstreamResponse(Exception):
    """Raised when an upstream (yfinance/Yahoo) call returns an empty payload
    where real data was expected. Carries ``symbol`` and ``label`` for logging."""

    def __init__(self, message, symbol=None, label=None):
        super().__init__(message)
        self.symbol = symbol
        self.label = label


def is_empty(resp):
    """True when a yfinance response carries no data.

    Handles the shapes yfinance returns: ``None``; an empty ``dict`` (``.info``);
    an empty ``list``/``tuple`` (``.news``); an empty ``str``; and a pandas
    ``DataFrame``/``Series`` (``.history``) whose ``.empty`` is True. Non-empty
    values — including a scalar like ``0`` or ``False`` that is still a real
    answer — return False."""
    if resp is None:
        return True
    empty_attr = getattr(resp, "empty", None)   # pandas DataFrame / Series
    if isinstance(empty_attr, bool):
        return empty_attr
    if isinstance(resp, (dict, list, tuple, str)):
        return len(resp) == 0
    return False


def yf_info_with_retry(symbol, label="info"):
    """Fetch ``yf.Ticker(symbol).info`` through the shared pacing/backoff, and
    RAISE ``EmptyUpstreamResponse`` on an empty payload instead of returning
    ``{}``. Rate-limit signals are retried with exponential backoff up to
    ``config.YF_RETRIES`` times; any other error propagates to the caller."""
    def _fetch():
        return yf.Ticker(symbol).info
    resp = _call(_fetch, f"{label}:{symbol}")
    if is_empty(resp):
        raise EmptyUpstreamResponse(
            f"empty {label} response for {symbol}", symbol=symbol, label=label)
    return resp


# ── Aggregate empty-rate tolerance + reporting surface (issue #38) ───────────────
# The caller runs a whole step over many symbols, counts how many came back empty,
# and decides: a few empties out of thousands is normal (delisted names, symbols
# with no news); most empties means the source is down/throttled and the step must
# FAIL rather than overwrite good data with nothing. Every verdict is recorded to
# ``<DATA_DIR>/empty_rates.json`` so a human — or Stock-App #109 — can read the
# per-step empty-response rate (also surfaced via the API's /api/integrity).

_EMPTY_RATES_FILE = "empty_rates.json"
_empty_rates = {}
_empty_lock = threading.Lock()


def _empty_rates_path():
    return os.path.join(getattr(config, "DATA_DIR", "."), _EMPTY_RATES_FILE)


def _persist_empty_rates():
    try:
        with open(_empty_rates_path(), "w", encoding="utf-8") as f:
            json.dump(_empty_rates, f, indent=2)
    except OSError as e:                         # reporting must never break a run
        print(f"    ⚠️  could not write empty-rate report: {e}")


def tolerate_empties(step, total, empty, max_empty_rate):
    """Aggregate the empty-response count for ``step`` and decide tolerance.

    ``total`` symbols were attempted, ``empty`` of them came back empty. The rate
    is recorded to the empty-rate report. If it exceeds ``max_empty_rate`` the
    source is treated as down/throttled and ``EmptyUpstreamResponse`` is raised so
    the step fails loudly; otherwise the rate is returned and the step proceeds."""
    rate = (empty / total) if total else 0.0
    with _empty_lock:
        _empty_rates[step] = {
            "total": total, "empty": empty, "rate": round(rate, 4),
            "max_empty_rate": max_empty_rate,
            "ok": rate <= max_empty_rate,
            "asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _persist_empty_rates()
    print(f"    empty-rate[{step}]: {empty}/{total} = {rate:.1%} "
          f"(tolerance {max_empty_rate:.0%})")
    if rate > max_empty_rate:
        raise EmptyUpstreamResponse(
            f"{step}: empty-response rate {rate:.0%} exceeds tolerance "
            f"{max_empty_rate:.0%} ({empty}/{total}) — source likely throttled/down",
            label=step)
    return rate


def empty_rate_report():
    """Return the recorded per-step empty-response rates (in-memory; the same data
    is persisted to ``<DATA_DIR>/empty_rates.json``)."""
    with _empty_lock:
        return {k: dict(v) for k, v in _empty_rates.items()}


def reset_empty_rates():
    """Clear the in-memory empty-rate registry (used by tests)."""
    with _empty_lock:
        _empty_rates.clear()
