"""Offline checks for the single-source-of-truth persistence model (issue #12).

stocks.db is authoritative; the *.json exports are projections of the SAME
per-ticker dict via serializers.db_record. These tests prove the DB row and the
JSON record share one field list (read off the SQLAlchemy model) and therefore
cannot silently drift, and that the schema is declared in exactly one place.

Run:  python test_persistence.py      (or: pytest test_persistence.py)
"""
import os, tempfile

# Point the DB at a temp path before importing database (import creates the
# engine). No file is actually written — these tests never open a connection.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.gettempdir(), "issue12_test.db"))

from database import Stock, Fundamentals, Valuation
from serializers import db_record, db_columns


def _sample(model):
    """A record carrying a distinct value for every column of `model`."""
    return {c: i for i, c in enumerate(db_columns(model))}


def test_db_record_drops_json_only_fields():
    rec = {**_sample(Stock), "some_export_only_field": "x"}
    row = db_record(Stock, rec)
    assert set(row) == set(db_columns(Stock))     # projected onto columns only
    assert "some_export_only_field" not in row


def test_row_is_a_faithful_projection_of_the_record():
    # For each entity the DB row is exactly (record keys ∩ columns) with the same
    # values — one dict feeds both the JSON export and the DB row, no re-derivation.
    for model in (Stock, Fundamentals, Valuation):
        rec = _sample(model)
        row = db_record(model, rec)
        assert set(row) == set(db_columns(model))
        for k in row:
            assert row[k] == rec[k]


def test_schema_lives_in_one_place():
    # db_record reads the field list straight off the model, so a new column
    # flows into the DB row automatically — there is no parallel list to edit.
    for model in (Stock, Fundamentals, Valuation):
        assert db_columns(model) == [c.name for c in model.__table__.columns]


def test_partial_record_only_touches_present_columns():
    # A momentum-only update (model.py) must not null out untouched columns.
    row = db_record(Fundamentals, {"ticker": "AAA", "rsi": 55.0})
    assert row == {"ticker": "AAA", "rsi": 55.0}


def test_rename_targets_real_columns_only():
    # model.py's rename map translates result keys to Valuation columns; any key
    # that is not a real column is dropped by db_record.
    mapped = {"dcf_intrinsic": 12.0, "ticker": "AAA"}
    rename = {"dcf_intrinsic": "dcf_fair_value"}
    row = db_record(Valuation, mapped, rename=rename)
    assert row["dcf_fair_value"] == 12.0
    assert "dcf_intrinsic" not in row          # source key is not a column


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  [ok] {t.__name__}")
    print("persistence: PASS")
