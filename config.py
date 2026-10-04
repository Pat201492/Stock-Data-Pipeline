"""
config.py — Single source of environment-driven configuration & path policy
============================================================================
Every collector used to re-derive DATA_DIR and its env tunables independently
(9+ modules, two subtly different idioms). This module owns that resolution ONCE
so the pieces cannot drift.

The env-var contract lives here — DEPLOY.md points at this file rather than
hand-maintaining a parallel list.

Path policy
-----------
DATA_DIR is the one writable root. On Fly it is the /data volume (see fly.toml);
locally it defaults to the repo directory. EVERY JSON output, cache, DB and the
run log resolve under it, so nothing escapes onto the ephemeral container FS.

Resolution idiom: ``os.environ.get("DATA_DIR") or SCRIPT_DIR`` — the ``or`` form
so an empty string (``DATA_DIR=""``) falls back to SCRIPT_DIR instead of writing
to the CWD root (the old ``get("DATA_DIR", SCRIPT_DIR)`` idiom did the latter).
"""

import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# The one canonical DATA_DIR resolution. Empty string falls back to SCRIPT_DIR.
DATA_DIR = os.environ.get("DATA_DIR") or SCRIPT_DIR


def data_path(name):
    """Resolve a filename under DATA_DIR."""
    return os.path.join(DATA_DIR, name)


# ── Derived output / cache paths ────────────────────────────────────────────
# Pipeline JSON outputs
UNIVERSE_JSON            = data_path("universe.json")
FUNDAMENTALS_JSON        = data_path("fundamentals.json")
MODEL_JSON               = data_path("model.json")
COMMODITIES_JSON         = data_path("commodities.json")
COMMODITIES_HISTORY_JSON = data_path("commodities_history.json")
OPTIONS_JSON             = data_path("options.json")
EXPOSURE_JSON            = data_path("exposure.json")

# Persistent per-collector caches (safe to interrupt/resume)
UNIVERSE_CACHE_JSON      = data_path("universe_cache.json")
FUNDAMENTALS_CACHE_JSON  = data_path("fundamentals_cache.json")
MODEL_CACHE_JSON         = data_path("model_cache.json")
ETF_CACHE_JSON           = data_path("etf_cache.json")

# Pipeline runner log — under DATA_DIR so it survives on the Fly volume.
RUN_LOG                  = data_path("run.log")

# ── yfinance pacing knobs (single canonical defaults) ───────────────────────
# Slow these down to dodge yfinance rate limits on big runs, e.g.
# YF_SLEEP=4 YF_BATCH=30 python run.py
YF_SLEEP   = float(os.environ.get("YF_SLEEP", 2))
YF_BATCH   = int(os.environ.get("YF_BATCH", 50))
YF_RETRIES = int(os.environ.get("YF_RETRIES", 2))
YF_BACKOFF = float(os.environ.get("YF_BACKOFF", 45))  # hard sleep on rate-limit signal
# fundamentals pulls .info + full statements per ticker -- heavier than a price
# download -- so it runs smaller, slower batches by default. Still env-tunable.
YF_BATCH_FUNDAMENTALS = int(os.environ.get("YF_BATCH_FUNDAMENTALS", 20))
YF_SLEEP_FUNDAMENTALS = float(os.environ.get("YF_SLEEP_FUNDAMENTALS", 3))

# ── DB paths ────────────────────────────────────────────────────────────────
DB_PATH     = os.environ.get("DB_PATH")     or data_path("stocks.db")
POL_DB_PATH = os.environ.get("POL_DB_PATH") or data_path("politicians.db")

# ── Secrets ─────────────────────────────────────────────────────────────────
FRED_API_KEY = os.environ.get("FRED_API_KEY")

# ── Other collector knobs ───────────────────────────────────────────────────
OPTIONS_TOP_N   = int(os.environ.get("OPTIONS_TOP_N", "25"))
OPTIONS_MAX_EXP = int(os.environ.get("OPTIONS_MAX_EXP", "6"))
EXPOSURE_COMPUTE = os.environ.get("EXPOSURE_COMPUTE") == "1"
RUN_SCHEDULER    = os.environ.get("RUN_SCHEDULER") == "1"

# ── yfinance lock (prevent multiple collectors running simultaneously) ──────
YFINANCE_LOCK_PATH = data_path(".yfinance.lock")


import contextlib, sys, time


LOCK_HELD_ENV = "YF_LOCK_HELD_BY_PARENT"


@contextlib.contextmanager
def yfinance_lock(wait_timeout=30, poll_interval=1):
    """Context manager for cross-process yfinance lock.
    Acquired by any script hitting yfinance to prevent concurrent collectors.
    If lock cannot be acquired within wait_timeout, exits with clear message.

    Usage:
        with yfinance_lock():
            # yfinance operations here
    """
    # A child started by a collector that already holds the lock (run.py runs
    # commodities.py / options.py as subprocesses inside its own lock) must not
    # wait on its parent: it timed out after 30s and exited 1, so commodities and
    # options failed on EVERY nightly run. The parent marks its children with
    # YF_LOCK_HELD_BY_PARENT=1; a standalone run still locks normally.
    if os.environ.get(LOCK_HELD_ENV) == "1":
        yield
        return
    lock_file = None
    try:
        lock_file = open(YFINANCE_LOCK_PATH, "w")
        start = time.time()
        acquired = False

        while time.time() - start < wait_timeout:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, IOError, BlockingIOError):
                time.sleep(poll_interval)

        if not acquired:
            print("Another collector is already running - try again in a moment.")
            sys.stdout.flush()
            sys.exit(1)

        yield
    finally:
        if lock_file:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    except (OSError, IOError):
                        pass
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
            lock_file.close()
