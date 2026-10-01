"""Offline checks for GET /api/macro (issue #30).

The route exposes FRED's VIX term structure + 10y-3m spread in the shape the
Trader-Screener dashboard reads (its #39). `fred.fetch_series` is patched with
in-memory observations, so no network and no FRED_API_KEY are needed.

A temp DATA_DIR / DB_PATH is set BEFORE importing `api` (which imports
`database`, which builds its engine at import time from config.DB_PATH).

Run:  python test_macro_endpoint.py      (or: pytest test_macro_endpoint.py)
"""
import os, tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="issue30_")
os.environ.setdefault("DATA_DIR", _TMP)
os.environ.setdefault("DB_PATH", os.path.join(_TMP, "stocks.db"))

from fastapi.testclient import TestClient
import fred
import api

client = TestClient(api.app)

# series_id -> ascending observations (date, value). latest_with_change uses the
# last entry as the latest value and its date.
_SERIES = {
    "VIXCLS":  [{"date": "2026-09-29", "value": 17.0}, {"date": "2026-09-30", "value": 18.0}],
    "VXVCLS":  [{"date": "2026-09-28", "value": 19.0}, {"date": "2026-09-29", "value": 20.0}],
    "T10Y3M":  [{"date": "2026-09-30", "value": 0.4},  {"date": "2026-10-01", "value": 0.5}],
    "T10Y3MM": [{"date": "2026-08-31", "value": 0.3},  {"date": "2026-09-30", "value": 0.4}],
}


@pytest.fixture
def patched_fred(monkeypatch):
    monkeypatch.setattr(fred, "FRED_API_KEY", "test-key")

    def fake_fetch(series_id, limit=24):
        return list(_SERIES[series_id])

    monkeypatch.setattr(fred, "fetch_series", fake_fetch)
    return monkeypatch


def test_macro_returns_seven_keys_with_latest_values(patched_fred):
    r = client.get("/api/macro")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"vix", "vixcls", "vix9d", "vix3m",
                         "t10y3m", "t10y3m_monthly", "asof"}
    assert body["vix"] == 18.0
    assert body["vixcls"] == 18.0
    assert body["vix3m"] == 20.0
    assert body["t10y3m"] == 0.5
    assert body["t10y3m_monthly"] == 0.4
    assert body["vix9d"] is None            # no FRED source
    assert body["asof"] == "2026-10-01"     # newest date among the series used


def test_vix9d_present_as_null(patched_fred):
    body = client.get("/api/macro").json()
    assert "vix9d" in body and body["vix9d"] is None


def test_one_series_raising_leaves_key_null_and_200(patched_fred):
    def fake_fetch(series_id, limit=24):
        if series_id == "VXVCLS":
            raise RuntimeError("FRED 500")
        return list(_SERIES[series_id])

    patched_fred.setattr(fred, "fetch_series", fake_fetch)
    r = client.get("/api/macro")
    assert r.status_code == 200
    body = r.json()
    assert body["vix3m"] is None            # the failing series
    assert body["vixcls"] == 18.0           # others still populated
    assert body["t10y3m"] == 0.5
    assert body["asof"] == "2026-10-01"     # unaffected by the dropped series


def test_missing_api_key_gives_503(monkeypatch):
    monkeypatch.setattr(fred, "FRED_API_KEY", None)
    r = client.get("/api/macro")
    assert r.status_code == 503
    assert r.json() == {"error": "FRED_API_KEY not set"}


if __name__ == "__main__":
    import contextlib

    class _MP:
        """Minimal monkeypatch stand-in for the __main__ runner."""
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)

    def run(fn, needs_patch):
        mp = _MP()
        try:
            if needs_patch:
                mp.setattr(fred, "FRED_API_KEY", "test-key")
                mp.setattr(fred, "fetch_series", lambda sid, limit=24: list(_SERIES[sid]))
            fn(mp)
        finally:
            mp.undo()
        print(f"  [ok] {fn.__name__}")

    run(test_macro_returns_seven_keys_with_latest_values, True)
    run(test_vix9d_present_as_null, True)
    run(test_one_series_raising_leaves_key_null_and_200, True)
    run(test_missing_api_key_gives_503, False)
    print("macro endpoint: PASS")
