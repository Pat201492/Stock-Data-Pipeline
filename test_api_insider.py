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


# -- #60: a cluster must put real money in ----------------------------------
def _add(rows):
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()
    api._db_cache.clear()


def test_cluster_under_min_value_is_not_a_cluster():
    # TSM-shaped: three insiders, a few thousand dollars each.
    _add([_t("TINY", n, date(2026, 9, 7), shares=40, price=79.39)
          for n in ("T1", "T2", "T3")])
    clusters = _get("/api/insider-trades/rollups", **RANGE)["clusters"]
    assert "TINY" not in {c["ticker"] for c in clusters}
    assert "CLST" in {c["ticker"] for c in clusters}           # $180k: kept
    every = _get("/api/insider-trades/rollups", cluster_min_value=0, cluster_min_avg=0, **RANGE)["clusters"]
    assert "TINY" in {c["ticker"] for c in every}


def test_cluster_at_exactly_min_value_is_kept():
    _add([_t("EXCT", n, date(2026, 9, 7), shares=1000, price=33.0, value=v)
          for n, v in (("E1", 30_000), ("E2", 30_000), ("E3", 40_000))])
    clusters = _get("/api/insider-trades/rollups", **RANGE)["clusters"]
    assert "EXCT" in {c["ticker"] for c in clusters}


def test_negative_cluster_min_value_is_400():
    r = client.get("/api/insider-trades/rollups", params={"cluster_min_value": -1})
    assert r.status_code == 400


def test_cluster_of_tiny_plan_buys_is_not_a_cluster():
    # TSM on 2026-10-07: 30 insiders, $121k in total -- over the $100k floor,
    # but ~$4k each. The per-insider average floor keeps it out.
    _add([_t("PLAN", f"Emp {i}", date(2026, 9, 7), shares=51, price=79.39)
          for i in range(30)])
    clusters = _get("/api/insider-trades/rollups", **RANGE)["clusters"]
    assert "PLAN" not in {c["ticker"] for c in clusters}
    loose = _get("/api/insider-trades/rollups", cluster_min_avg=0, **RANGE)["clusters"]
    assert "PLAN" in {c["ticker"] for c in loose}
    r = client.get("/api/insider-trades/rollups", params={"cluster_min_avg": -1})
    assert r.status_code == 400


# -- #61: a stake no insider could hold --------------------------------------
def test_impossible_holding_is_suspect():
    # IHT-shaped: 7.94M shares owned after, at $50,410.80, would be a $400bn
    # stake -- the price is wrong.
    row = _t("IHTX", "James Wirth", date(2026, 9, 21), ttype="S", shares=43_111,
             price=50_410.8, title="President & CEO")
    row.shares_owned_after = 7_940_275
    big_buy = _t("IHTB", "Odd Buyer", date(2026, 9, 21), ttype="P", shares=43_111, price=50_410.8)
    big_buy.shares_owned_after = 7_940_275
    _add([row, big_buy])
    default = _get("/api/insider-trades", **RANGE)
    assert not {"IHTX", "IHTB"} & {r["ticker"] for r in default["rows"]}
    roll = _get("/api/insider-trades/rollups", **RANGE)
    assert "IHTX" not in {s["ticker"] for s in roll["most_sold"]}
    assert "IHTB" not in {r["ticker"] for r in roll["top_buys"]}
    shown = _get("/api/insider-trades", include_suspect=1, ticker="IHTX", **RANGE)["rows"]
    assert len(shown) == 1 and shown[0]["suspect"] is True


def test_ordinary_large_holding_is_not_suspect():
    # A founder-sized stake ($50bn) at a real price stays visible.
    row = _t("FNDR", "Founder", date(2026, 9, 22), ttype="S", shares=1000, price=400.0)
    row.shares_owned_after = 125_000_000
    _add([row])
    rows = _get("/api/insider-trades", ticker="FNDR", **RANGE)["rows"]
    assert len(rows) == 1 and rows[0]["suspect"] is False
