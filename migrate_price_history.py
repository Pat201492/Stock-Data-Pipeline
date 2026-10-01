"""Rebuild `price_history` as `PRIMARY KEY (ticker, date) WITHOUT ROWID`.

The old schema was a rowid table on an unused `id` plus a UNIQUE(ticker, date)
index — the key was stored twice. The new schema stores it once in the b-tree.
Measured on 1,665,213 live rows: 106.2 MB -> 62.2 MB (-44 MB, -41%). VACUUM
reclaims nothing on top, so this rebuild is the whole win.

Idempotent: on a table that is already WITHOUT ROWID this does nothing and
exits 0. On the old schema it rebuilds in a single transaction — create the
new table, copy every distinct (ticker, date) keeping the highest-`id` row on
duplicates, drop the old, rename. Prints row counts before and after.

Run:  python migrate_price_history.py            (uses config.DB_PATH)
      python migrate_price_history.py path/to.db
"""
import sqlite3
import sys


def _table_sql(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def _is_without_rowid(create_sql: str) -> bool:
    return "WITHOUT ROWID" in create_sql.upper()


def migrate(db_path: str) -> bool:
    """Rebuild price_history WITHOUT ROWID. Returns True if it did work,
    False if the table was already migrated (or absent)."""
    conn = sqlite3.connect(db_path)
    try:
        create_sql = _table_sql(conn, "price_history")
        if create_sql is None:
            print("price_history: table not present, nothing to migrate")
            return False
        if _is_without_rowid(create_sql):
            n = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
            print(f"price_history: already WITHOUT ROWID, {n} rows, nothing to do")
            return False

        before = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        print(f"price_history: {before} rows before migration")

        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS price_history_new")
        conn.execute(
            """
            CREATE TABLE price_history_new (
                ticker VARCHAR NOT NULL,
                date   VARCHAR NOT NULL,
                close  FLOAT,
                volume FLOAT,
                PRIMARY KEY (ticker, date)
            ) WITHOUT ROWID
            """
        )
        # Copy each distinct (ticker, date), keeping the highest-id row on dups.
        conn.execute(
            """
            INSERT INTO price_history_new (ticker, date, close, volume)
            SELECT ticker, date, close, volume
            FROM price_history p
            WHERE id = (
                SELECT MAX(id) FROM price_history p2
                WHERE p2.ticker = p.ticker AND p2.date = p.date
            )
            """
        )
        conn.execute("DROP TABLE price_history")
        conn.execute("ALTER TABLE price_history_new RENAME TO price_history")
        conn.commit()

        after = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
        print(f"price_history: {after} rows after migration "
              f"({before - after} duplicate (ticker, date) pairs collapsed)")
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        db_path = argv[0]
    else:
        import config
        db_path = config.DB_PATH
    migrate(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
