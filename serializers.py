"""serializers.py — the one place a pipeline record becomes a DB row.

`stocks.db` is the AUTHORITATIVE store for pipeline stock/valuation state and the
inter-stage handoff medium; the `*.json` files the API serves are DERIVED
exports. See the "Persistence — source of truth" section of README.md.

Each stage builds its per-ticker record ONCE (a plain dict). It writes that dict
to its JSON export AND passes the same dict to `db_record` here to build the DB
row, so the two serializations are projections of one object and cannot silently
drift. The field list for a DB row is read straight off the SQLAlchemy model in
`database.py`, so adding a field is a one-place edit — there is no parallel
field list to keep in sync.
"""
from database import Stock, Fundamentals, Valuation  # noqa: F401  (re-exported)


def db_columns(model) -> list:
    """Column names for `model`. JSON exports that mirror a table 1:1 and the DB
    upsert both derive their field list from this — one source of truth."""
    return [c.name for c in model.__table__.columns]


def db_record(model, rec: dict, rename: dict | None = None) -> dict:
    """Project a stage's in-memory record onto `model`'s columns — the single
    code path that constructs a DB row for this entity.

    `rename` maps a record key to a differently-named column (model.py's result
    keys differ from the `Valuation` column names). Keys that do not name a
    column are dropped; columns absent from `rec` are omitted so a partial
    record (e.g. a momentum-only update) does not null out untouched columns.
    """
    src = dict(rec)
    if rename:
        for key, col in rename.items():
            if key in src:
                src[col] = src[key]
    cols = set(db_columns(model))
    return {k: v for k, v in src.items() if k in cols}
