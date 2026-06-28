"""
fred.py — minimal FRED (St. Louis Fed) API client for the Fed / economic-health page.
Needs a free API key in the FRED_API_KEY env var (https://fred.stlouisfed.org/docs/api/api_key.html).
No extra deps — uses urllib.
"""
import os, json, time
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from urllib.parse import urlencode

FRED_API_KEY = os.environ.get("FRED_API_KEY")
BASE = "https://api.stlouisfed.org/fred/series/observations"

# In-memory cache: macro data updates daily at most, so cache responses to avoid
# hammering FRED (which 429s on bursts) and to make the page fast.
_CACHE = {}            # (series_id, limit) -> (expires_ts, obs)
_CACHE_TTL = 3600      # 1 hour


def configured() -> bool:
    return bool(FRED_API_KEY)


def fetch_series(series_id: str, limit: int = 24):
    """Most-recent `limit` observations (ascending). Returns list of {date, value}.
    Cached for _CACHE_TTL; retries on HTTP 429 with backoff."""
    if not FRED_API_KEY:
        raise RuntimeError("FRED_API_KEY not set")

    key = (series_id, limit)
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    qs = urlencode({
        "series_id": series_id, "api_key": FRED_API_KEY, "file_type": "json",
        "sort_order": "desc", "limit": limit,
    })
    url = f"{BASE}?{qs}"
    data = None
    for attempt in range(4):
        try:
            req = Request(url, headers={"User-Agent": "StockTracker"})
            data = json.loads(urlopen(req, timeout=20).read().decode("utf-8"))
            break
        except HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(0.6 * (attempt + 1))  # back off and retry
                continue
            raise
    obs = []
    for o in data.get("observations", []):
        v = o.get("value")
        if v in (".", "", None):
            continue
        try:
            obs.append({"date": o["date"], "value": float(v)})
        except ValueError:
            continue
    obs.reverse()  # ascending by date
    _CACHE[key] = (time.time() + _CACHE_TTL, obs)
    return obs


def latest_with_change(series_id: str, limit: int = 24, yoy: bool = False):
    """Latest value + change vs prior obs (or YoY % when yoy=True) + history for a sparkline."""
    obs = fetch_series(series_id, limit=max(limit, 14 if yoy else 2))
    if not obs:
        return None
    latest = obs[-1]
    out = {"value": latest["value"], "asof": latest["date"],
           "history": [o["value"] for o in obs[-limit:]]}
    if yoy and len(obs) >= 13:
        year_ago = obs[-13]["value"]
        out["value"] = round((latest["value"] / year_ago - 1) * 100, 2) if year_ago else None
        out["history"] = [
            round((obs[i]["value"] / obs[i - 12]["value"] - 1) * 100, 2)
            for i in range(12, len(obs)) if obs[i - 12]["value"]
        ]
        out["change"] = round(out["value"] - (
            (obs[-2]["value"] / obs[-14]["value"] - 1) * 100), 2) if len(obs) >= 14 and obs[-14]["value"] else None
    else:
        out["change"] = round(latest["value"] - obs[-2]["value"], 2) if len(obs) >= 2 else None
    return out
