"""
ingest_edgar.py — Pull SEC EDGAR Form 4 insider trades
Source: https://data.sec.gov/submissions/ and full-index feeds
Strategy: fetch daily Form 4 index from EDGAR full-index, parse XML filings.
Full backfill: fetch quarterly indexes from 2014 onward.
"""
import hashlib, json, re, time, urllib.request, urllib.error, gzip, sys, os
from datetime import datetime, date, timedelta
from xml.etree import ElementTree as ET

from politicians_database import init_pol_db, SessionLocal, InsiderTrade

EDGAR_BASE   = "https://www.sec.gov"
FULL_IDX_URL = EDGAR_BASE + "/Archives/edgar/full-index/{year}/QTR{qtr}/form.idx"
USER_AGENT   = "StockTracker pat@example.com"   # SEC requires User-Agent


def _get(url, retries=3, binary=False):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            resp = urllib.request.urlopen(req, timeout=20)
            return resp.read() if binary else resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(10)
            elif attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def _quarters_since(start_year=2014):
    quarters = []
    today = date.today()
    for year in range(start_year, today.year + 1):
        for qtr in range(1, 5):
            qtr_end_month = qtr * 3
            qtr_end = date(year, qtr_end_month, 1)
            if qtr_end <= today:
                quarters.append((year, qtr))
    return quarters


def _parse_form4_xml(xml_text, filing_id, filed_date):
    """Extract trades from Form 4 XML. Returns list of InsiderTrade kwargs."""
    trades = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return trades

    ns = ""

    def find(el, tag):
        r = el.find(tag)
        return r.text.strip() if r is not None and r.text else ""

    issuer      = root.find(".//issuer")
    ticker      = find(issuer, "issuerTradingSymbol").upper() if issuer is not None else ""
    company     = find(issuer, "issuerName")         if issuer is not None else ""

    owner_el    = root.find(".//reportingOwner")
    insider_name  = ""
    insider_title = ""
    if owner_el is not None:
        insider_name  = find(owner_el, ".//rptOwnerName")
        insider_title = find(owner_el, ".//officerTitle") or find(owner_el, ".//relationship")

    if not ticker or ticker in ("--", "N/A"):
        return trades

    for txn in root.findall(".//nonDerivativeTransaction"):
        try:
            txn_date_str = find(txn, ".//transactionDate/value")
            txn_date     = date.fromisoformat(txn_date_str) if txn_date_str else filed_date
            txn_code     = find(txn, ".//transactionCode")
            shares_str   = find(txn, ".//transactionShares/value")
            price_str    = find(txn, ".//transactionPricePerShare/value")
            owned_str    = find(txn, ".//sharesOwnedFollowingTransaction/value")

            shares = float(shares_str) if shares_str else None
            price  = float(price_str)  if price_str  else None
            owned  = float(owned_str)  if owned_str  else None
            total  = round(shares * price, 2) if shares and price else None

            trade_hash = hashlib.sha1(
                f"{filing_id}|{ticker}|{txn_date}|{txn_code}|{shares}".encode()
            ).hexdigest()

            trades.append(dict(
                filing_id         = trade_hash,   # unique per transaction row within filing
                ticker            = ticker,
                company_name      = company,
                insider_name      = insider_name,
                insider_title     = insider_title,
                transaction_date  = txn_date,
                transaction_type  = txn_code,
                shares            = shares,
                price_per_share   = price,
                total_value       = total,
                shares_owned_after= owned,
                form_type         = "4",
                filed_date        = filed_date,
                source_url        = f"{EDGAR_BASE}/Archives/{filing_id}",
            ))
        except Exception:
            continue

    return trades


def _ingest_quarter(year, qtr, db, existing_ids, dry_run=False, existing_logical=None):
    if existing_logical is None:
        existing_logical = set()
    url = FULL_IDX_URL.format(year=year, qtr=qtr)
    try:
        idx_text = _get(url)
    except Exception as e:
        print(f"  [edgar] Skipping {year}/Q{qtr}: {e}")
        return 0, 0

    inserted = 0
    skipped  = 0
    filings  = []

    # form.idx is fixed-width. Use regex: form_type, company, cik, date, path
    row_re = re.compile(r"^(\S+)\s+.+?\s+(\d+)\s+(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$")
    for line in idx_text.splitlines():
        if not (line.startswith("4 ") or line.startswith("4/A ")):
            continue
        m = row_re.match(line)
        if not m:
            continue
        form_type, cik, filed_str, idx_path = m.group(1), m.group(2), m.group(3), m.group(4)
        if form_type not in ("4", "4/A"):
            continue
        try:
            filed_date = date.fromisoformat(filed_str)
        except ValueError:
            continue
        filing_id = idx_path.replace("/", "_").replace(".txt", "")
        filings.append((filing_id, idx_path, filed_date))

    # Dedupe — multiple reporting owners share the same filing .txt
    seen = set()
    deduped = []
    for f in filings:
        if f[1] in seen:
            continue
        seen.add(f[1])
        deduped.append(f)
    filings = deduped

    print(f"  [edgar] {year}/Q{qtr}: {len(filings)} Form 4 filings", flush=True)

    errors      = 0
    no_xml      = 0
    last_commit = 0

    for i, (filing_id, idx_path, filed_date) in enumerate(filings):
        if filing_id in existing_ids:
            skipped += 1
            continue

        # Rate limit: SEC asks for max 10 req/sec
        if i % 10 == 0 and i > 0:
            time.sleep(1.1)

        try:
            full_url  = EDGAR_BASE + "/Archives/" + idx_path
            full_body = _get(full_url)
            m = re.search(r"<XML>\s*(.*?)\s*</XML>", full_body, re.DOTALL)
            if not m:
                no_xml += 1
                continue
            xml_text = m.group(1).strip()

            trades = _parse_form4_xml(xml_text, filing_id, filed_date)
            for t in trades:
                logical = "|".join("" if c is None else str(c) for c in (
                    t.get("ticker"), t.get("transaction_date"), t.get("insider_name"),
                    t.get("transaction_type"), t.get("shares")))
                if logical in existing_logical:   # already captured (e.g. by the mirror)
                    skipped += 1
                    continue
                existing_logical.add(logical)
                if not dry_run:
                    db.merge(InsiderTrade(**t, ingested_at=datetime.utcnow()))
                inserted += 1

            # Commit every ~200 new inserts
            if inserted - last_commit >= 200:
                if not dry_run:
                    db.commit()
                print(f"    [{i}/{len(filings)}] +{inserted} trades, skip={skipped}, noxml={no_xml}, err={errors}", flush=True)
                last_commit = inserted

        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"    [err] {type(e).__name__}: {str(e)[:120]}", flush=True)
            db.rollback()
            continue

    if not dry_run:
        db.commit()
    print(f"  [edgar] {year}/Q{qtr} totals: inserted={inserted} noxml={no_xml} err={errors}", flush=True)
    return inserted, skipped


def ingest(start_year=2014, full_refresh=False):
    init_pol_db()
    db = SessionLocal()
    try:
        from sqlalchemy import text as _text
        existing_ids = set()
        if not full_refresh:
            existing_ids = {r[0] for r in db.execute(
                _text("SELECT filing_id FROM insider_trades")
            ).fetchall()}
            print(f"[edgar] {len(existing_ids)} existing filings — incremental mode")
        # Logical keys present from ANY source (incl. the daily mirror) so EDGAR
        # doesn't re-insert a trade the mirror already captured.
        existing_logical = {
            "|".join("" if c is None else str(c) for c in r)
            for r in db.execute(_text(
                "SELECT ticker, transaction_date, insider_name, transaction_type, shares FROM insider_trades"
            )).fetchall()
        }

        quarters = _quarters_since(start_year)
        total_inserted = 0
        for year, qtr in quarters:
            ins, skip = _ingest_quarter(year, qtr, db, existing_ids, existing_logical=existing_logical)
            total_inserted += ins
            print(f"  [edgar] {year}/Q{qtr} done — +{ins} inserted, {skip} skipped")

        print(f"[edgar] Complete — {total_inserted} insider trades inserted")
    finally:
        db.close()


if __name__ == "__main__":
    full      = "--full" in sys.argv
    year_arg  = next((int(a) for a in sys.argv[1:] if a.isdigit()), 2014)
    ingest(start_year=year_arg, full_refresh=full)
