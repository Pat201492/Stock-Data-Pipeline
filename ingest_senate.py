"""
ingest_senate.py — DEPRECATED. The senate-stock-watcher mirror has been dead
since 2020 (newest trade 2020-12-02). Senate coverage will move to an official
efdsearch.senate.gov ingest. Retained only for `_normalize_name` /
`_build_bioguide_lookup` (reused by ingest_house.py) and as an explicit
`--senate` escape hatch; no longer runs by default.

ingest_senate.py — Pull Senate Stock Watcher JSON into congressional_trades
Source: https://github.com/timothycarambat/senate-stock-watcher-data
"""
import hashlib, json, urllib.request, urllib.error, time, os, sys
from datetime import datetime, date

from politicians_database import init_pol_db, SessionLocal, CongressionalTrade, Politician

SENATE_DATA_URL = (
    "https://raw.githubusercontent.com/timothycarambat/"
    "senate-stock-watcher-data/master/aggregate/all_transactions.json"
)

AMOUNT_MAP = {
    "$1,001 - $15,000":     (1001,   15000),
    "$15,001 - $50,000":    (15001,  50000),
    "$50,001 - $100,000":   (50001,  100000),
    "$100,001 - $250,000":  (100001, 250000),
    "$250,001 - $500,000":  (250001, 500000),
    "$500,001 - $1,000,000":(500001, 1000000),
    "$1,000,001 - $5,000,000":(1000001,5000000),
    "Over $5,000,000":      (5000001, 9999999),
    "$1,001 -":             (1001,   15000),
}

TYPE_MAP = {
    "Purchase":       "purchase",
    "Sale (Full)":    "sale_full",
    "Sale (Partial)": "sale_partial",
    "Exchange":       "exchange",
    "Sale":           "sale_full",
}


def _parse_date(s):
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _trade_id(bioguide, ticker, txn_date, amount_min, txn_type):
    raw = f"{bioguide}|{ticker}|{txn_date}|{amount_min}|{txn_type}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _normalize_name(first, last):
    """Strip suffixes/middle initials, lowercase. 'David A Perdue, Jr' -> 'david perdue'."""
    import re as _re
    full = f"{first} {last}".strip().lower()
    full = _re.sub(r"[,.]", " ", full)
    full = _re.sub(r"\b(jr|sr|ii|iii|iv|md|phd)\b", "", full)
    tokens = [t for t in full.split() if len(t) > 1]   # drop single-letter middle initials
    if len(tokens) >= 2:
        return f"{tokens[0]} {tokens[-1]}"   # first + last only
    return " ".join(tokens)


def _build_bioguide_lookup(db):
    """Map normalized 'firstname lastname' -> bioguide_id for senate-affiliated members."""
    lookup = {}
    for p in db.query(Politician).filter(Politician.chamber.in_(("sen", "senate"))).all():
        key = _normalize_name(p.first_name or "", p.last_name or "")
        if key:
            lookup[key] = p.bioguide_id
    return lookup


def _fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.urlopen(url, timeout=30)
            return json.loads(req.read().decode("utf-8"))
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def ingest(full_refresh=False):
    init_pol_db()
    db = SessionLocal()
    try:
        print(f"[senate] Fetching {SENATE_DATA_URL}")
        raw = _fetch_json(SENATE_DATA_URL)
        print(f"[senate] {len(raw)} raw records")

        existing_ids = set()
        if not full_refresh:
            existing_ids = {r[0] for r in db.execute(
                __import__("sqlalchemy").text(
                    "SELECT trade_id FROM congressional_trades WHERE source='senate_stock_watcher'"
                )
            ).fetchall()}
            print(f"[senate] {len(existing_ids)} existing trades — incremental mode")

        inserted = 0
        skipped  = 0
        bad      = 0
        seen_pols = set()
        bio_lookup = _build_bioguide_lookup(db)
        print(f"[senate] {len(bio_lookup)} senate bioguide lookups loaded")
        matched   = 0
        unmatched = 0

        for row in raw:
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker or ticker in ("--", "N/A", ""):
                bad += 1
                continue

            txn_type_raw = row.get("type", "")
            txn_type     = TYPE_MAP.get(txn_type_raw, txn_type_raw.lower().replace(" ", "_"))

            amount_raw = row.get("amount", "")
            amount_min, amount_max = AMOUNT_MAP.get(amount_raw, (None, None))

            txn_date  = _parse_date(row.get("transaction_date"))
            disc_date = _parse_date(row.get("disclosure_date"))

            first = (row.get("first_name") or "").strip()
            last  = (row.get("last_name")  or row.get("senator") or "").strip()
            # Senate Stock Watcher has no bioguide; look up real bioguide by name
            real_bio = bio_lookup.get(_normalize_name(first, last))
            if real_bio:
                bioguide = real_bio
                matched += 1
            else:
                bioguide = row.get("bioguide_id") or f"SEN_{last.upper().replace(' ','_')}"
                unmatched += 1

            trade_id = _trade_id(bioguide, ticker, txn_date, amount_min, txn_type)

            if trade_id in existing_ids:
                skipped += 1
                continue
            existing_ids.add(trade_id)   # dedupe within this run too

            # Only create stub if we didn't find a real bioguide match
            if bioguide not in seen_pols and not real_bio:
                db.merge(Politician(
                    bioguide_id=bioguide,
                    first_name=first,
                    last_name=last,
                    chamber="senate",
                    party=row.get("party", ""),
                    state=row.get("state", ""),
                    active=True,
                ))
                seen_pols.add(bioguide)

            db.merge(CongressionalTrade(
                trade_id         = trade_id,
                bioguide_id      = bioguide,
                ticker           = ticker,
                asset_description= row.get("asset_description", ""),
                transaction_date = txn_date,
                disclosure_date  = disc_date,
                transaction_type = txn_type,
                amount_min       = amount_min,
                amount_max       = amount_max,
                owner            = row.get("owner", "self").lower(),
                source           = "senate_stock_watcher",
                ingested_at      = datetime.utcnow(),
            ))
            inserted += 1

            if inserted % 500 == 0:
                db.commit()
                print(f"[senate]   committed {inserted} so far…")

        db.commit()
        print(f"[senate] Done — inserted={inserted} skipped={skipped} non-equity={bad}")
        print(f"[senate] Bioguide match: matched={matched} unmatched={unmatched}")
    finally:
        db.close()


if __name__ == "__main__":
    full = "--full" in sys.argv
    ingest(full_refresh=full)
