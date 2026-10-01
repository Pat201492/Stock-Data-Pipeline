"""Tests for the price_history WITHOUT ROWID migration (issue #36).

Proves the new schema, the rebuild's dedup/idempotence, and that the
`/api/stocks/{ticker}/history` endpoint serves identical rows before and after
the migration. The DB path is pointed at a temp file BEFORE importing `api`
(which imports `database`, which builds its engine at import time).

Run:  pytest test_price_history_migration.py
"""
import os
import sqlite3
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="issue36_")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = os.path.join(_TMP, "stocks.db")

from fastapi.testclient import TestClient
import database
from database import PriceHistory
import api
import migrate_price_history

# The real path the global engine bound to at import time. Equals our env var
# when this module imports `database` first, but if another test module got
# there first the engine points at *its* temp DB — read the truth from database
# so the endpoint test seeds the same file the app queries.
DB_FILE = database._DB_PATH
client = TestClient(api.app)


def _write_old_schema(path, rows):
    """Create the pre-migration rowid table (id PK + ticker/date columns) and
    seed it in order, so each row's autoincrement id ascends with insertion.
    No UNIQUE(ticker, date) here so we can seed duplicate pairs and prove the
    migration collapses them to the highest-id row."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE IF EXISTS price_history")
        conn.execute(
            """
            CREATE TABLE price_history (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker VARCHAR,
                date   VARCHAR,
                close  FLOAT,
                volume FLOAT
            )
            """
        )
        conn.executemany(
            "INSERT INTO price_history (ticker, date, close, volume) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _table_info(path):
    conn = sqlite3.connect(path)
    try:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='price_history'"
        ).fetchone()[0]
        info = conn.execute("PRAGMA table_info(price_history)").fetchall()
        return sql, info
    finally:
        conn.close()


def test_fresh_init_db_is_without_rowid(tmp_path):
    """A fresh init creates price_history as WITHOUT ROWID with PK (ticker, date)
    and no id column."""
    from sqlalchemy import create_engine

    p = str(tmp_path / "fresh.db")
    eng = create_engine(f"sqlite:///{p}")
    database.Base.metadata.create_all(eng)

    sql, info = _table_info(p)
    assert "WITHOUT ROWID" in sql.upper()
    names = [r[1] for r in info]
    assert "id" not in names
    pk_cols = [r[1] for r in info if r[5] > 0]  # pk flag in column 5, in key order
    assert pk_cols == ["ticker", "date"]


def test_migration_keeps_distinct_rows_and_collapses_dupes(tmp_path):
    p = str(tmp_path / "old.db")
    _write_old_schema(p, [
        ("AAPL", "2026-01-01", 100.0, 10),
        ("AAPL", "2026-01-02", 101.0, 11),
        ("MSFT", "2026-01-01", 200.0, 20),
        ("AAPL", "2026-01-01", 999.0, 99),  # dup of the first pair, higher id wins
    ])

    did_work = migrate_price_history.migrate(p)
    assert did_work is True

    conn = sqlite3.connect(p)
    try:
        rows = conn.execute(
            "SELECT ticker, date, close, volume FROM price_history ORDER BY ticker, date"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("AAPL", "2026-01-01", 999.0, 99),  # collapsed to highest-id version
        ("AAPL", "2026-01-02", 101.0, 11),
        ("MSFT", "2026-01-01", 200.0, 20),
    ]

    sql, info = _table_info(p)
    assert "WITHOUT ROWID" in sql.upper()
    assert [r[1] for r in info if r[5] > 0] == ["ticker", "date"]


def test_migration_is_idempotent(tmp_path):
    p = str(tmp_path / "old.db")
    _write_old_schema(p, [("AAPL", "2026-01-01", 100.0, 10)])

    assert migrate_price_history.migrate(p) is True      # first: rebuilds
    assert migrate_price_history.migrate(p) is False     # second: no-op
    assert migrate_price_history.main([p]) == 0          # CLI exits 0


def test_api_history_identical_before_and_after(tmp_path):
    """The endpoint returns the same rows, in the same order, pre and post."""
    # Ticker PHMIG is unique to this test so it never collides with another
    # module seeding the shared engine DB; the finally clause drops it again so
    # we leave price_history exactly as we found it.
    try:
        _write_old_schema(DB_FILE, [
            ("PHMIG", "2026-01-03", 102.0, 12),
            ("PHMIG", "2026-01-01", 100.0, 10),
            ("PHMIG", "2026-01-02", 101.0, 11),
        ])

        api._db_cache.clear()
        before = client.get("/api/stocks/PHMIG/history")
        assert before.status_code == 200

        assert migrate_price_history.migrate(DB_FILE) is True

        api._db_cache.clear()
        after = client.get("/api/stocks/PHMIG/history")
        assert after.status_code == 200

        assert before.json() == after.json()
        hist = after.json()["history"]
        assert [h["date"] for h in hist] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    finally:
        # Leave an empty WITHOUT ROWID table so other modules seeding this shared
        # DB (whichever imported `database` first) start from a clean slate.
        ph = database.Base.metadata.tables["price_history"]
        ph.drop(database.engine, checkfirst=True)
        ph.create(database.engine)
        api._db_cache.clear()


def test_model_has_no_id_attribute():
    assert not hasattr(PriceHistory, "id")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
