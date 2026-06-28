"""
ingest_senate_efd.py — Pull U.S. Senate stock trades from the OFFICIAL source:
the Senate Electronic Financial Disclosure system (efdsearch.senate.gov).

Flow:
  1. GET /search/home/            -> scrape csrfmiddlewaretoken
  2. POST /search/home/           -> accept prohibition agreement (sets session)
  3. POST /search/report/data/    -> paginated list of Periodic Transaction Reports
  4. GET  /search/view/ptr/{uuid} -> e-filed HTML table (parsed); /paper/ = scanned PDF (skipped)

Companion to ingest_house.py. source='senate_efd'. Senate e-filed PTRs are clean
HTML tables, so parsing is far simpler than the House PDF path. Paper filings are
skipped + logged (no OCR/PDF parse in v1).
"""
import re, sys, time
from datetime import datetime, date, timedelta

import httpx
from lxml import html as LH

from politicians_database import init_pol_db, SessionLocal, CongressionalTrade, Politician
from ingest_house import _trade_id, _parse_date, _norm_name

# Senate PTR amount labels -> (min, max) integers
AMOUNT_RANGES = {
    "$1,001 - $15,000":        (1001,    15000),
    "$15,001 - $50,000":       (15001,   50000),
    "$50,001 - $100,000":      (50001,   100000),
    "$100,001 - $250,000":     (100001,  250000),
    "$250,001 - $500,000":     (250001,  500000),
    "$500,001 - $1,000,000":   (500001,  1000000),
    "$1,000,001 - $5,000,000": (1000001, 5000000),
    "$5,000,001 - $25,000,000": (5000001, 25000000),
    "$25,000,001 - $50,000,000": (25000001, 50000000),
    "Over $50,000,000":        (50000001, 99999999),
}

BASE = "https://efdsearch.senate.gov"
HOME = BASE + "/search/home/"
DATA = BASE + "/search/report/data/"
SOURCE = "senate_efd"
UA = "Mozilla/5.0 (compatible; StockApp/1.0; +https://github.com/Pat201492/Stock-App)"
REPORT_TYPE_PTR = "[11]"   # efdsearch report_types code for Periodic Transaction Report

TYPE_MAP = {
    "Purchase": "purchase",
    "Sale (Full)": "sale_full",
    "Sale (Partial)": "sale_partial",
    "Sale": "sale_full",
    "Exchange": "exchange",
}


def _amount(label):
    return AMOUNT_RANGES.get((label or "").strip(), (None, None))


def parse_ptr_html(text):
    """Parse an e-filed Senate PTR page -> list of dicts. Pure function."""
    doc = LH.fromstring(text)
    out = []
    for tr in doc.xpath("//table//tbody/tr"):
        cells = [(td.text_content() or "").strip() for td in tr.xpath("./td")]
        if len(cells) < 8:
            continue
        # 0:# 1:date 2:owner 3:ticker 4:asset 5:asset_type 6:txn_type 7:amount 8:comment
        ticker = cells[3].strip().upper()
        if not ticker or ticker in ("--", "N/A") or not re.fullmatch(r"[A-Z][A-Z.\-]{0,5}", ticker):
            continue
        amin, amax = _amount(cells[7])
        out.append({
            "ticker": ticker.replace(".", "-"),
            "owner": (cells[2] or "self").strip().lower()[:20],
            "txn_date": _parse_date(cells[1]),
            "type": TYPE_MAP.get(cells[6], (cells[6] or "").lower().replace(" ", "_")),
            "amount_min": amin,
            "amount_max": amax,
        })
    return out


def _session():
    c = httpx.Client(timeout=30, headers={"User-Agent": UA, "Referer": HOME},
                     follow_redirects=True)
    r = c.get(HOME)
    m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("efdsearch: csrf token not found")
    c.post(HOME, data={"csrfmiddlewaretoken": m.group(1),
                       "prohibition_agreement": "1"}, headers={"Referer": HOME})
    return c


def _iter_ptr_rows(c, start_date):
    """Yield (first, last, href, report_date) for PTR filings since start_date."""
    csrf = c.cookies.get("csrftoken")
    start = 0
    while True:
        payload = {
            "start": str(start), "length": "100",
            "report_types": REPORT_TYPE_PTR, "filer_types": "[]",
            "submitted_start_date": start_date + " 00:00:00",
            "submitted_end_date": "",
            "candidate_state": "", "senator_state": "", "office_id": "",
            "first_name": "", "last_name": "",
            "csrfmiddlewaretoken": csrf,
        }
        r = c.post(DATA, data=payload, headers={
            "Referer": HOME, "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrf})
        data = r.json()
        rows = data.get("data", [])
        if not rows:
            break
        for row in rows:
            first = re.sub(r"\s+", " ", row[0]).strip()
            last = re.sub(r"\s+", " ", row[1]).strip()
            href = ""
            m = re.search(r'href="([^"]+)"', row[3])
            if m:
                href = m.group(1)
            yield first, last, href, row[4]
        start += len(rows)
        if start >= data.get("recordsTotal", 0):
            break
        time.sleep(0.3)


def ingest(full_refresh=False):
    init_pol_db()
    db = SessionLocal()
    try:
        # senate name -> bioguide lookup (committees-seeded). Full-name match first;
        # fall back to last name when it's unique among ACTIVE senators (handles
        # nickname mismatches like Jim/James, Mike/Michael, Dave/David).
        name_map, last_count, last_map = {}, {}, {}
        for p in db.query(Politician).filter(Politician.chamber.in_(("sen", "senate"))).all():
            key = _norm_name(p.first_name or "", p.last_name or "")
            if key:
                name_map[key] = p.bioguide_id
            ln = (p.last_name or "").strip().lower()
            if ln and getattr(p, "active", False):
                last_count[ln] = last_count.get(ln, 0) + 1
                last_map[ln] = p.bioguide_id
        last_unique = {ln: b for ln, b in last_map.items() if last_count[ln] == 1}

        def _match(first, last):
            return name_map.get(_norm_name(first, last)) \
                or last_unique.get(last.strip().lower())

        existing_ids = set()
        if not full_refresh:
            from sqlalchemy import text as _t
            existing_ids = {r[0] for r in db.execute(_t(
                "SELECT trade_id FROM congressional_trades WHERE source = 'senate_efd'"
            )).fetchall()}
            print(f"[senate_efd] {len(existing_ids)} existing — incremental")

        start_date = "01/01/2012" if full_refresh else \
            (date.today() - timedelta(days=90)).strftime("%m/%d/%Y")

        c = _session()
        inserted = skipped = paper = unmatched = no_txn = 0
        for first, last, href, report_date in _iter_ptr_rows(c, start_date):
            if "/view/paper/" in href or "/view/ptr/" not in href:
                paper += 1
                continue
            bioguide = _match(first, last)
            if not bioguide:
                unmatched += 1
                continue
            try:
                rep = c.get(BASE + href, headers={"Referer": HOME})
                txns = parse_ptr_html(rep.text)
                time.sleep(0.3)
            except Exception as e:
                print(f"[senate_efd]   fetch {href} failed: {e}")
                continue
            if not txns:
                no_txn += 1
                continue
            disc_date = _parse_date(report_date)
            for t in txns:
                if t["txn_date"] is None:
                    continue
                tid = _trade_id(bioguide, t["ticker"], t["txn_date"],
                                t["amount_min"], t["type"])
                if tid in existing_ids:
                    skipped += 1
                    continue
                existing_ids.add(tid)
                db.merge(CongressionalTrade(
                    trade_id=tid, bioguide_id=bioguide, ticker=t["ticker"],
                    asset_description="", transaction_date=t["txn_date"],
                    disclosure_date=disc_date, transaction_type=t["type"],
                    amount_min=t["amount_min"], amount_max=t["amount_max"],
                    owner=t["owner"], source=SOURCE, ingested_at=datetime.utcnow(),
                ))
                inserted += 1
        db.commit()
        print(f"[senate_efd] Done — inserted={inserted} skipped={skipped} "
              f"paper={paper} unmatched={unmatched} no_txn={no_txn}")
    finally:
        db.close()


if __name__ == "__main__":
    ingest(full_refresh="--full" in sys.argv)
