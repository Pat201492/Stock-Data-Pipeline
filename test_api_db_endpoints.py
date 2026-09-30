"""Offline checks for the DB-only API routes (issue #13).

News, price history, and ETFs live only in `stocks.db` (no JSON export). These
tests seed a throwaway DB and prove the new `/api/...` routes serve those
datasets, so no consumer has to open the SQLite file directly.

The DB path is pointed at a temp file BEFORE importing `api` (which imports
`database`, and `database` builds its engine at import time from config.DB_PATH).

Run:  python test_api_db_endpoints.py      (or: pytest test_api_db_endpoints.py)
"""
import os, tempfile
from datetime import datetime

import pytest

_TMP = tempfile.mkdtemp(prefix="issue13_")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = os.path.join(_TMP, "stocks.db")

from fastapi.testclient import TestClient
import database
from database import SessionLocal, News, PriceHistory, ETF, ETFHolding
import api

database.init_db()
client = TestClient(api.app)


def _seed():
    db = SessionLocal()
    try:
        db.add_all([
            News(ticker="AAPL", title="Apple soars", url="http://x/1",
                 publisher="Reuters", published_at=datetime(2026, 1, 2),
                 sentiment=0.8, summary="up"),
            News(ticker="AAPL", title="Apple dips", url="http://x/2",
                 publisher="Bloomberg", published_at=datetime(2026, 1, 3),
                 sentiment=-0.2, summary="down"),
            News(ticker="MSFT", title="MSFT news", url="http://x/3",
                 publisher="WSJ", published_at=datetime(2026, 1, 1),
                 sentiment=0.1, summary="flat"),
        ])
        db.add_all([
            PriceHistory(ticker="AAPL", date="2026-01-01", close=100.0, volume=10),
            PriceHistory(ticker="AAPL", date="2026-01-02", close=101.0, volume=11),
            PriceHistory(ticker="AAPL", date="2026-01-03", close=102.0, volume=12),
        ])
        db.add(ETF(ticker="SPY", name="SPDR S&P 500", category="Large Blend",
                   asset_class="equity", aum=5e11, expense_ratio=0.09,
                   holdings_count=2))
        db.add(ETF(ticker="AGG", name="Core US Bond", category="Interm Bond",
                   asset_class="bond", aum=1e11, holdings_count=0))
        db.add_all([
            ETFHolding(etf_ticker="SPY", holding_ticker="AAPL",
                       holding_name="Apple", weight=0.07),
            ETFHolding(etf_ticker="SPY", holding_ticker="MSFT",
                       holding_name="Microsoft", weight=0.06),
        ])
        db.commit()
    finally:
        db.close()


def _clear_cache():
    api._db_cache.clear()


@pytest.fixture(scope="session", autouse=True)
def _seeded():
    _seed()


@pytest.fixture(autouse=True)
def _fresh_cache():
    # TTL cache would otherwise mask per-test filter/limit differences.
    _clear_cache()
    yield


def test_news_by_ticker_route():
    r = client.get("/api/stocks/AAPL/news")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "AAPL"
    assert body["count"] == 2
    # newest first
    assert body["news"][0]["title"] == "Apple dips"
    assert set(body["news"][0]) >= {"title", "url", "publisher",
                                    "published_at", "sentiment", "summary"}
    assert "last_updated" not in body["news"][0]


def test_news_query_filter_and_limit():
    r = client.get("/api/news", params={"ticker": "aapl", "limit": 1})
    assert r.status_code == 200
    assert r.json()["count"] == 1
    r_all = client.get("/api/news")
    assert r_all.json()["count"] == 3     # unfiltered sees every ticker


def test_history_series_ascending():
    r = client.get("/api/stocks/AAPL/history")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "AAPL"
    dates = [p["date"] for p in body["history"]]
    assert dates == sorted(dates)          # oldest -> newest
    assert body["history"][-1]["close"] == 102.0


def test_history_limit_keeps_most_recent():
    r = client.get("/api/stocks/AAPL/history", params={"limit": 2})
    body = r.json()
    assert [p["date"] for p in body["history"]] == ["2026-01-02", "2026-01-03"]


def test_history_unknown_ticker_404():
    assert client.get("/api/stocks/NOPE/history").status_code == 404


def test_etfs_list_and_filter():
    r = client.get("/api/etfs")
    assert r.status_code == 200
    assert r.json()["total"] == 2
    r_eq = client.get("/api/etfs", params={"asset_class": "equity"})
    tickers = [e["ticker"] for e in r_eq.json()["etfs"]]
    assert tickers == ["SPY"]


def test_etf_detail_with_holdings():
    r = client.get("/api/etfs/spy")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "SPY"
    assert [h["ticker"] for h in body["holdings"]] == ["AAPL", "MSFT"]  # weight desc
    assert "last_updated" not in body


def test_etf_detail_404():
    assert client.get("/api/etfs/NOPE").status_code == 404


if __name__ == "__main__":
    _seed()
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        _clear_cache()
        t()
        print(f"  [ok] {t.__name__}")
    print("api db endpoints: PASS")
