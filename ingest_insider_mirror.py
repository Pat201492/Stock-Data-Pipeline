"""
ingest_insider_mirror.py — Pull recent insider trades from pre-parsed mirror
Source: https://github.com/nickhuangcyh/sec-insider-tracker
Format: one JSON file per trading day under /data/YYYY-MM-DD.json
Covers: last ~12-30 days, updated daily

Complements ingest_edgar.py (full historical backfill) by giving us
recent insider activity in minutes instead of days.
"""
import hashlib, json, sys, time, urllib.request, urllib.error
from datetime import datetime, date

from politicians_database import init_pol_db, SessionLocal, InsiderTrade

REPO       = "nickhuangcyh/sec-insider-tracker"
API_BASE   = f"https://api.github.com/repos/{REPO}/contents/data"
RAW_BASE   = f"https://raw.githubusercontent.com/{REPO}/main/data"


def _get(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "StockTracker pat@example.com"}
            )
            return urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
            elif attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def _list_days():
    """Get all available YYYY-MM-DD.json filenames from the repo."""
    body = _get(API_BASE + "?per_page=100")
    files = json.loads(body)
    return sorted(
        f["name"][:-5]
        for f in files
        if f.get("name", "").endswith(".json") and f["name"] != "index.json" and f["name"] != "failed.json"
    )


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _trade_hash(idx_path, ticker, txn_date, txn_type, shares):
    raw = f"{idx_path}|{ticker}|{txn_date}|{txn_type}|{shares}"
    return hashlib.sha1(raw.encode()).hexdigest()


def ingest(full_refresh=False):
    init_pol_db()
    db = SessionLocal()
    try:
        days = _list_days()
        print(f"[mirror] {len(days)} day-files available ({days[0]} → {days[-1]})")

        from sqlalchemy import text
        existing_ids = set()
        if not full_refresh:
            existing_ids = {r[0] for r in db.execute(text(
                "SELECT filing_id FROM insider_trades WHERE source_url LIKE 'mirror:%'"
            )).fetchall()}
            print(f"[mirror] {len(existing_ids)} existing mirror trades — incremental mode")
        # Logical keys already present from ANY source (incl. EDGAR) so the two
        # sources don't double-insert the same trade.
        existing_logical = {
            "|".join("" if c is None else str(c) for c in r)
            for r in db.execute(text(
                "SELECT ticker, transaction_date, insider_name, transaction_type, shares FROM insider_trades"
            )).fetchall()
        }

        inserted = 0
        skipped  = 0
        for d in days:
            try:
                body = _get(f"{RAW_BASE}/{d}.json")
                doc  = json.loads(body)
                txns = doc.get("transactions", [])
            except Exception as e:
                print(f"  [warn] {d}: {e}")
                continue

            day_inserts = 0
            for t in txns:
                ticker  = (t.get("ticker") or "").strip().upper()
                if not ticker or ticker in ("--", "N/A"):
                    continue
                txn_date = _parse_date(t.get("date"))
                txn_type = (t.get("type") or "").strip()
                shares   = t.get("shares")
                url      = t.get("url") or ""
                trade_id = _trade_hash(url, ticker, txn_date, txn_type, shares)
                if trade_id in existing_ids:
                    skipped += 1
                    continue
                logical = "|".join("" if c is None else str(c)
                                   for c in (ticker, txn_date, t.get("insider", ""), txn_type, shares))
                if logical in existing_logical:   # already have it from EDGAR or earlier
                    skipped += 1
                    continue
                existing_ids.add(trade_id)
                existing_logical.add(logical)

                db.merge(InsiderTrade(
                    filing_id          = trade_id,
                    ticker             = ticker,
                    company_name       = t.get("company", ""),
                    insider_name       = t.get("insider", ""),
                    insider_title      = t.get("title") or t.get("relationship") or "",
                    transaction_date   = txn_date,
                    transaction_type   = txn_type,
                    shares             = shares,
                    price_per_share    = t.get("price"),
                    total_value        = t.get("value"),
                    shares_owned_after = t.get("shares_total"),
                    form_type          = "4",
                    filed_date         = _parse_date(d),
                    source_url         = f"mirror:{url}",
                    ingested_at        = datetime.utcnow(),
                ))
                day_inserts += 1
                inserted += 1

            db.commit()
            if day_inserts:
                print(f"  [mirror] {d}: +{day_inserts} trades")

        print(f"[mirror] Done — inserted={inserted} skipped={skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    full = "--full" in sys.argv
    ingest(full_refresh=full)
