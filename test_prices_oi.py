"""Index-proxy prices + backfill (#65) and the open-interest history (#66).

DB_PATH points at a temp file BEFORE `database` is imported (its engine is
built at import). No network: the yfinance ticker is stubbed.

Run:  python -m pytest test_prices_oi.py
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="prices_oi_")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = os.path.join(_TMP, "stocks.db")
os.environ["POL_DB_PATH"] = os.path.join(_TMP, "politicians.db")

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import api
import backfill_prices
import config
import database
import news
import options
from database import OptionsOIHistory, PriceHistory, SessionLocal, Stock

database.init_db()
client = TestClient(api.app)


_news_calls = []


class _FakeTicker:
    calls = []

    def __init__(self, sym):
        self.sym = sym

    def history(self, period="1y"):
        _FakeTicker.calls.append((self.sym, period))
        days = {"1y": 5, "3y": 15}.get(period, 5)
        idx = pd.date_range(end="2026-10-16", periods=days, freq="B")
        return pd.DataFrame({"Close": [100.0 + i for i in range(days)],
                             "Volume": [1e6] * days}, index=idx)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    db = SessionLocal()
    db.query(PriceHistory).delete()
    db.query(OptionsOIHistory).delete()
    db.query(Stock).delete()
    db.commit()
    db.close()
    _FakeTicker.calls.clear()
    monkeypatch.setattr(news, "yf_ticker", _FakeTicker)
    _news_calls.clear()
    monkeypatch.setattr(news, "fetch_and_store_news", lambda t, *a, **k: _news_calls.append(t) or 0)
    monkeypatch.setattr(news.time, "sleep", lambda s: None)
    api._db_cache.clear()
    yield


def _count(ticker):
    db = SessionLocal()
    try:
        return db.query(PriceHistory).filter(PriceHistory.ticker == ticker).count()
    finally:
        db.close()


# -- #65 ---------------------------------------------------------------------
def test_default_proxies():
    assert config.INDEX_PROXIES == ["SPY", "QQQ", "IWM", "DIA"]


def test_nightly_step_keeps_the_proxies(monkeypatch):
    db = SessionLocal()
    db.add(Stock(ticker="QZXA", rank=1))
    db.commit()
    db.close()
    news.main()
    for t in ("QZXA", "SPY", "QQQ", "IWM", "DIA"):
        assert _count(t) > 0, t
    assert {s for s, _ in _FakeTicker.calls} == {"QZXA", "SPY", "QQQ", "IWM", "DIA"}
    # The proxies are price-only: news was fetched for the universe ticker alone.
    assert _news_calls == ["QZXA"]


def test_explicit_ticker_list_skips_proxies():
    news.main(tickers=["QZXA"])
    assert _count("QZXA") > 0 and _count("SPY") == 0


def test_backfill_loads_years_and_is_idempotent():
    first = backfill_prices.backfill(["SPY", "IWM"], years=3)
    assert first == {"SPY": 15, "IWM": 15}
    assert ("SPY", "3y") in _FakeTicker.calls
    assert backfill_prices.backfill(["SPY", "IWM"], years=3) == {"SPY": 0, "IWM": 0}
    assert _count("SPY") == 15


def test_backfill_reports_an_empty_source(monkeypatch):
    def empty(t, db, period="1y"):
        raise news.EmptyUpstreamResponse("empty", symbol=t, label="history")
    assert backfill_prices.backfill(["ZZZZ"], fetch=empty) == {"ZZZZ": None}


# -- #66 ---------------------------------------------------------------------
def _snap(asof, oi):
    return {t: {"asof": asof, "summary": {"spot": 10.0, "atm_iv": 0.3, "total_oi": c + p,
                                          "call_oi": c, "put_oi": p}}
            for t, (c, p) in oi.items()}


def test_oi_history_one_row_per_ticker_per_day():
    assert options.record_oi_history(_snap("2026-10-19", {"NVDA": (100, 50), "AAPL": (70, 30)})) == 2
    # A second run the same day replaces, never duplicates.
    options.record_oi_history(_snap("2026-10-19", {"NVDA": (110, 55), "AAPL": (70, 30)}))
    options.record_oi_history(_snap("2026-10-20", {"NVDA": (120, 60)}))
    db = SessionLocal()
    try:
        rows = db.query(OptionsOIHistory).order_by(OptionsOIHistory.date, OptionsOIHistory.ticker).all()
        assert [(r.ticker, r.date, r.total_oi) for r in rows] == [
            ("AAPL", "2026-10-19", 100), ("NVDA", "2026-10-19", 165), ("NVDA", "2026-10-20", 180)]
    finally:
        db.close()


def test_oi_history_skips_records_without_oi():
    out = {"X": {"asof": "2026-10-19", "summary": {"spot": 1.0}}, "Y": {"summary": {"total_oi": 5}}}
    assert options.record_oi_history(out) == 0


def test_oi_history_route_filters():
    options.record_oi_history(_snap("2026-10-19", {"NVDA": (100, 50), "AAPL": (70, 30)}))
    options.record_oi_history(_snap("2026-10-20", {"NVDA": (120, 60)}))
    r = client.get("/api/options/oi-history", params={"ticker": "nvda"}).json()
    assert [x["date"] for x in r["rows"]] == ["2026-10-19", "2026-10-20"] and r["count"] == 2
    r = client.get("/api/options/oi-history", params={"from": "2026-10-20", "to": "2026-10-20"}).json()
    assert [(x["ticker"], x["call_oi"]) for x in r["rows"]] == [("NVDA", 120)]
    # The literal route is not swallowed by /api/options/{ticker}.
    assert "rows" in client.get("/api/options/oi-history").json()
