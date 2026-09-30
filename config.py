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
