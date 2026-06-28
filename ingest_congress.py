"""
ingest_congress.py — DEPRECATED. The upstream Peez49/Informed-Trading CSV froze
in Feb 2026 (newest trade 2026-01-30). Superseded by ingest_house.py (official
House Clerk PTRs). Kept for historical rows already in the DB and as an explicit
`--congress` escape hatch; no longer runs by default. Will be removed once the
official Senate eFD ingest lands.

ingest_congress.py — Pull House + Senate trades from Peez49/Informed-Trading
Source: https://github.com/Peez49/Informed-Trading
File:   raw:/Trading_Data.csv (~21MB, ~109k trades, both chambers, real BioGuideIDs)
Covers: 2012 -> recent (last updated daily)

Replaces the stale Senate Stock Watcher (timothycarambat) repo which froze in 2021.
"""
import csv, hashlib, io, sys, time, urllib.request
from datetime import datetime, date

from politicians_database import init_pol_db, SessionLocal, CongressionalTrade, Politician

DATA_URL = (
    "https://raw.githubusercontent.com/Peez49/Informed-Trading/"
    "main/raw%3A/Trading_Data.csv"
)

# Map Trade_Size_USD label ranges to (min, max) integers
AMOUNT_MAP = {
    "$1,001 - $15,000":       (1001,   15000),
    "$15,001 - $50,000":      (15001,  50000),
    "$50,001 - $100,000":     (50001,  100000),
    "$100,001 - $250,000":    (100001, 250000),
    "$250,001 - $500,000":    (250001, 500000),
    "$500,001 - $1,000,000":  (500001, 1000000),
    "$1,000,001 - $5,000,000":(1000001,5000000),
    "$5,000,001 - $25,000,000":(5000001,25000000),
    "$25,000,001 - $50,000,000":(25000001,50000000),
    "Over $50,000,000":       (50000001, 99999999),
    "Over $5,000,000":        (5000001, 9999999),
}

TYPE_MAP = {
    "Purchase":        "purchase",
    "Sale":            "sale_full",
    "Sale (Full)":     "sale_full",
    "Sale (Partial)":  "sale_partial",
    "Exchange":        "exchange",
}

CHAMBER_MAP = {"House": "rep", "Senate": "sen"}

PARTY_MAP = {
    "D": "Democrat", "R": "Republican", "I": "Independent",
    "Democrat": "Democrat", "Republican": "Republican", "Independent": "Independent",
}


def _parse_date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _trade_id(bioguide, ticker, txn_date, amount_min, txn_type):
    raw = f"{bioguide}|{ticker}|{txn_date}|{amount_min}|{txn_type}"
    return hashlib.sha1(raw.encode()).hexdigest()


def _fetch_csv(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.urlopen(url, timeout=60)
            return req.read().decode("utf-8-sig", errors="replace")
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def ingest(full_refresh=False):
    init_pol_db()
    db = SessionLocal()
    try:
        print(f"[congress] Fetching {DATA_URL}")
        text = _fetch_csv(DATA_URL)
        rows = list(csv.DictReader(io.StringIO(text)))
        print(f"[congress] {len(rows)} raw records")

        existing_ids = set()
        if not full_refresh:
            from sqlalchemy import text as _t
            existing_ids = {r[0] for r in db.execute(_t(
                "SELECT trade_id FROM congressional_trades WHERE source LIKE 'peez49%'"
            )).fetchall()}
            print(f"[congress] {len(existing_ids)} existing trades — incremental mode")

        inserted  = 0
        skipped   = 0
        bad       = 0
        seen_pols = set()

        for row in rows:
            ticker = (row.get("Ticker") or "").strip().upper()
            if not ticker or ticker in ("--", "N/A", ""):
                bad += 1
                continue

            bioguide = (row.get("BioGuideID") or "").strip()
            if not bioguide:
                bad += 1
                continue

            txn_type_raw = row.get("Transaction", "")
            txn_type     = TYPE_MAP.get(txn_type_raw, txn_type_raw.lower().replace(" ", "_"))

            amount_raw = (row.get("Trade_Size_USD") or "").strip()
            amount_min, amount_max = AMOUNT_MAP.get(amount_raw, (None, None))

            txn_date  = _parse_date(row.get("Traded"))
            disc_date = _parse_date(row.get("Filed"))

            trade_id = _trade_id(bioguide, ticker, txn_date, amount_min, txn_type)

            if trade_id in existing_ids:
                skipped += 1
                continue
            existing_ids.add(trade_id)

            # Upsert politician stub (real bioguide; committees.py fills committee
            # data; this preserves party/state/chamber regardless of committee ingest)
            if bioguide not in seen_pols:
                name_full = (row.get("Name") or "").strip()
                parts     = name_full.split()
                first     = parts[0] if parts else ""
                last      = parts[-1] if len(parts) > 1 else (parts[0] if parts else "")
                db.merge(Politician(
                    bioguide_id=bioguide,
                    first_name=first,
                    last_name=last,
                    chamber=CHAMBER_MAP.get(row.get("Chamber", ""), row.get("Chamber", "").lower()),
                    party=PARTY_MAP.get((row.get("Party") or "").strip(), row.get("Party") or ""),
                    state=(row.get("State") or "").strip(),
                    district=(row.get("District") or "").strip() or None,
                    active=True,
                ))
                seen_pols.add(bioguide)

            db.merge(CongressionalTrade(
                trade_id         = trade_id,
                bioguide_id      = bioguide,
                ticker           = ticker,
                asset_description= row.get("Company", ""),
                transaction_date = txn_date,
                disclosure_date  = disc_date,
                transaction_type = txn_type,
                amount_min       = amount_min,
                amount_max       = amount_max,
                owner            = (row.get("Subholding") or "self").strip().lower()[:20],
                source           = "peez49_informed_trading",
                ingested_at      = datetime.utcnow(),
            ))
            inserted += 1

            if inserted % 1000 == 0:
                db.commit()
                print(f"[congress]   committed {inserted} so far…")

        db.commit()
        print(f"[congress] Done — inserted={inserted} skipped={skipped} bad={bad}")
        print(f"[congress] {len(seen_pols)} politicians upserted")
    finally:
        db.close()


if __name__ == "__main__":
    full = "--full" in sys.argv
    ingest(full_refresh=full)
