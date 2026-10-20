"""Offline checks for the insider-trades routes (issues #57, #58).

`politicians_database` builds its engine at import from config.POL_DB_PATH, so
DATA_DIR / POL_DB_PATH point at a temp dir BEFORE `api` is imported.

Run:  python -m pytest test_api_insider.py
"""
import os
import tempfile
from datetime import date

import pytest

_TMP = tempfile.mkdtemp(prefix="insider_")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = os.path.join(_TMP, "stocks.db")
os.environ["POL_DB_PATH"] = os.path.join(_TMP, "politicians.db")

from fastapi.testclient import TestClient
import database
import politicians_database as pdb
from politicians_database import InsiderTrade, SessionLocal
import api

database.init_db()
pdb.init_pol_db()
client = TestClient(api.app)

_n = [0]


def _t(ticker, name, day, ttype="P", shares=100, price=10.0, value=None,
       title="Director", url=None):
    _n[0] += 1
    return InsiderTrade(
        filing_id=f"0000000000-26-{_n[0]:06d}", ticker=ticker,
        company_name=f"{ticker} Corp", insider_name=name, insider_title=title,
        transaction_date=day, transaction_type=ttype, shares=shares,
        price_per_share=price, total_value=shares * price if value is None else value,
        form_type="4", filed_date=day,
        source_url=url or f"https://www.sec.gov/Archives/edgar/data/1/{_n[0]}.txt")


@pytest.fixture(autouse=True)
def seeded():
    db = SessionLocal()
    try:
        db.query(InsiderTrade).delete()
        d = lambda n: date(2026, 9, n)
        db.add_all([
            # Cluster: 3 different insiders buy CLST within 14 days.
            _t("CLST", "Ann A", d(1), value=50_000, shares=5000, price=10.0),
            _t("CLST", "Bob B", d(5), value=60_000, shares=6000, price=10.0, title="Chief Executive Officer"),
            _t("CLST", "Cy C", d(10), value=70_000, shares=7000, price=10.0),
            # Same insider three times: not a cluster.
            _t("SOLO", "Dee D", d(2)), _t("SOLO", "Dee D", d(3)), _t("SOLO", "Dee D", d(4)),
            # Three insiders, spread over 30 days: not a cluster.
            _t("SPRD", "Ed E", date(2026, 8, 20)), _t("SPRD", "Fay F", d(5)),
            _t("SPRD", "Gus G", d(19)),
            # Sales.
            _t("SELL", "Hal H", d(8), ttype="S", shares=1000, price=500.0, title="10% Owner"),
            _t("SELL", "Ivy I", d(9), ttype="S", shares=1000, price=100.0),
            _t("SMAL", "Jo J", d(9), ttype="S", shares=10, price=10.0),
            # Award, and a mirror: URL.
            _t("AWRD", "Kim K", d(12), ttype="A", shares=500, price=0.0, value=0.0,
               url="mirror:https://www.sec.gov/Archives/edgar/data/2/x.txt"),
            # The live SLBT row: a $2,272,653/share "purchase" worth $10.3 trillion.
            _t("SLBT", "Lou L", date(2026, 9, 29), shares=4_532_000,
               price=2_272_653.0, value=1.03e13, title="Chief Executive Officer"),
            # Out of range.
            _t("OLD", "Max M", date(2025, 1, 1)),
        ])
        db.commit()
    finally:
        db.close()
    api._db_cache.clear()
    yield


def _get(path, **params):
    r = client.get(path, params=params)
    assert r.status_code == 200, r.text
    return r.json()


RANGE = {"from": "2026-08-01", "to": "2026-09-30"}


def test_date_range_newest_first_with_count_and_total():
    out = _get("/api/insider-trades", **RANGE)
    dates = [r["transaction_date"] for r in out["rows"]]
    assert all("2026-08-01" <= x <= "2026-09-30" for x in dates)
    assert dates == sorted(dates, reverse=True)
    assert out["count"] == len(out["rows"]) == out["total"] == 13   # 15 seeded - OLD - SLBT
    narrow = _get("/api/insider-trades", **{"from": "2026-09-05", "to": "2026-09-05"})
    assert {r["ticker"] for r in narrow["rows"]} == {"CLST", "SPRD"}


def test_filters_narrow_and_sort_by_value():
    q = dict(RANGE)
    assert {r["transaction_type"] for r in _get("/api/insider-trades", type="S", **q)["rows"]} == {"S"}
    assert len(_get("/api/insider-trades", type="S,A", **q)["rows"]) == 4
    assert {r["ticker"] for r in _get("/api/insider-trades", ticker="clst", **q)["rows"]} == {"CLST"}
    assert [r["insider_name"] for r in _get("/api/insider-trades", insider="bob", **q)["rows"]] == ["Bob B"]
    assert [r["insider_name"] for r in _get("/api/insider-trades", role="ten_pct", **q)["rows"]] == ["Hal H"]
    assert [r["insider_name"] for r in _get("/api/insider-trades", role="officer", **q)["rows"]] == ["Bob B"]
    big = _get("/api/insider-trades", min_value=60_000, **q)["rows"]
    assert {r["insider_name"] for r in big} == {"Bob B", "Cy C", "Hal H", "Ivy I"}
    vals = [r["total_value"] for r in _get("/api/insider-trades", sort="value", **q)["rows"]]
    assert vals == sorted(vals, reverse=True) and vals[0] == 500_000


def test_limit_cap_offset_and_total():
    page1 = _get("/api/insider-trades", limit=5, **RANGE)
    page2 = _get("/api/insider-trades", limit=5, offset=5, **RANGE)
    assert page1["count"] == 5 and page1["total"] == 13 and page2["total"] == 13
    assert not {r["filing_id"] for r in page1["rows"]} & {r["filing_id"] for r in page2["rows"]}


def test_limit_is_capped_at_2000():
    db = SessionLocal()
    try:
        db.add_all([_t("BULK", f"P{i}", date(2026, 9, 15)) for i in range(2100)])
        db.commit()
    finally:
        db.close()
    api._db_cache.clear()
    out = _get("/api/insider-trades", limit=5000, **RANGE)
    assert out["count"] == 2000 and out["total"] == 13 + 2100


def test_suspect_row_hidden_by_default():
    default = _get("/api/insider-trades", **RANGE)
    assert "SLBT" not in {r["ticker"] for r in default["rows"]}
    shown = _get("/api/insider-trades", include_suspect=1, **RANGE)
    slbt = [r for r in shown["rows"] if r["ticker"] == "SLBT"]
    assert len(slbt) == 1 and slbt[0]["suspect"] is True
    assert shown["total"] == default["total"] + 1
    assert all(r["suspect"] is False for r in default["rows"])


def test_sec_url_drops_mirror_prefix():
    rows = _get("/api/insider-trades", **RANGE)["rows"]
    award = next(r for r in rows if r["ticker"] == "AWRD")
    assert award["sec_url"] == "https://www.sec.gov/Archives/edgar/data/2/x.txt"
    assert all(not r["sec_url"].startswith("mirror:") for r in rows)


def test_bad_params_are_400():
    assert client.get("/api/insider-trades", params={"from": "nope"}).status_code == 400
    assert client.get("/api/insider-trades", params={"sort": "size"}).status_code == 400
    assert client.get("/api/insider-trades", params={"role": "janitor"}).status_code == 400


def test_rollups_top_buys():
    out = _get("/api/insider-trades/rollups", **RANGE)
    top = out["top_buys"]
    assert 0 < len(top) <= 25 and all(r["transaction_type"] == "P" for r in top)
    vals = [r["total_value"] for r in top]
    assert vals == sorted(vals, reverse=True)
    assert "SLBT" not in {r["ticker"] for r in top}
    with_sus = _get("/api/insider-trades/rollups", include_suspect=1, **RANGE)
    assert with_sus["top_buys"][0]["ticker"] == "SLBT"


def test_rollups_most_sold():
    sold = _get("/api/insider-trades/rollups", **RANGE)["most_sold"]
    assert [s["ticker"] for s in sold] == ["SELL", "SMAL"]
    assert sold[0] == {"ticker": "SELL", "company_name": "SELL Corp", "sell_value": 600_000.0,
                       "sell_count": 2, "insiders": 2}


def test_rollups_clusters():
    db = SessionLocal()
    try:   # three buyers under the source's placeholder ticker: not a company
        db.add_all([_t("NONE", n, date(2026, 9, 3)) for n in ("N1", "N2", "N3")])
        db.commit()
    finally:
        db.close()
    clusters = _get("/api/insider-trades/rollups", **RANGE)["clusters"]
    assert [c["ticker"] for c in clusters] == ["CLST"]
    c = clusters[0]
    assert c["insiders"] == 3 and c["buy_value"] == 180_000
    assert c["first_date"] == "2026-09-01" and c["last_date"] == "2026-09-10"
