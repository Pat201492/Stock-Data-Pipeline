"""
test_insider_mirror.py — the mirror ingest must not re-insert a trade the DB
already holds. The nightly runs of 2026-10-13 and 2026-10-16 failed on the
uq_insider_logical index: the stored `shares` is a FLOAT (1322.0) and the
mirror's JSON is an int (1322), so the text-joined dedupe key never matched.
No network: the mirror is stubbed.

Run:  python -m pytest test_insider_mirror.py
"""
import json
from datetime import date

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import ingest_insider_mirror as iim
import politicians_database as pdb


@pytest.fixture
def pol_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path / 'politicians.db'}")
    monkeypatch.setattr(pdb, "engine", eng)
    monkeypatch.setattr(iim, "SessionLocal", sessionmaker(bind=eng))
    pdb.init_pol_db()              # creates the table and the unique index
    return eng


def _stub_mirror(monkeypatch, txns):
    monkeypatch.setattr(iim, "_list_days", lambda: ["2026-10-16"])
    monkeypatch.setattr(iim, "_get", lambda url, retries=3: json.dumps({"transactions": txns}))


def test_logical_key_matches_float_and_int_shares():
    stored = iim._logical_key("CBNK", "2026-09-25", "Deborah Ratnersalzberg", "F", 1322.0)
    mirror = iim._logical_key("CBNK", date(2026, 9, 25), "Deborah Ratnersalzberg", "F", 1322)
    assert stored == mirror


def test_existing_trade_from_another_source_is_skipped(pol_db, monkeypatch):
    with pol_db.begin() as c:
        c.execute(text(
            "INSERT INTO insider_trades (filing_id, ticker, transaction_date, insider_name, "
            "transaction_type, shares, source_url) VALUES ('edgar-1', 'CBNK', '2026-09-25', "
            "'Deborah Ratnersalzberg', 'F', 1322.0, 'https://www.sec.gov/x')"))
    _stub_mirror(monkeypatch, [
        {"ticker": "CBNK", "date": "2026-09-25", "insider": "Deborah Ratnersalzberg",
         "type": "F", "shares": 1322, "url": "https://www.sec.gov/y"},
        {"ticker": "INCY", "date": "2026-10-15", "insider": "BAKER BROS. ADVISORS LP",
         "type": "M", "shares": 15000, "url": "https://www.sec.gov/z"},
    ])
    iim.ingest()                   # raised IntegrityError before the fix
    with pol_db.connect() as c:
        rows = c.execute(text("SELECT ticker, shares FROM insider_trades ORDER BY ticker")).fetchall()
    assert rows == [("CBNK", 1322.0), ("INCY", 15000.0)]


def test_rerun_is_a_no_op(pol_db, monkeypatch):
    _stub_mirror(monkeypatch, [{"ticker": "INCY", "date": "2026-10-15", "insider": "A",
                                "type": "M", "shares": 15000, "url": "u"}])
    iim.ingest()
    iim.ingest()
    with pol_db.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM insider_trades")).scalar() == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
