"""
ingest_house.py — Pull U.S. House stock trades from the OFFICIAL source:
the Clerk of the House Financial Disclosure system.

  Index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{YEAR}FD.zip
         -> {YEAR}FD.txt  (tab-delimited; FilingType 'P' == Periodic Transaction Report)
  PTR:   https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{YEAR}/{DocID}.pdf

Replaces the stale third-party feeds (Peez49/Informed-Trading froze 2026-02;
senate-stock-watcher dead since 2020). Official PTRs are current to within the
STOCK Act's 45-day disclosure window.

Transaction detail lives in per-filing PDFs. E-filed PTRs are text PDFs we parse
with pypdf (portable) or `pdftotext -layout` when available. Scanned/handwritten
PTRs yield no text and are skipped + logged (no OCR in scope).
"""
import csv, hashlib, io, os, re, sys, time, urllib.request, zipfile
from datetime import datetime, date

from politicians_database import init_pol_db, SessionLocal, CongressionalTrade, Politician

YEARS = [2025, 2026]
SOURCE = "house_clerk"
UA = "Mozilla/5.0 (compatible; StockApp/1.0; +https://github.com/Pat201492/Stock-App)"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "house_ptrs")

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"

# House e-filed PTRs only mark Purchase / Sale (no full/partial split).
TYPE_MAP = {"P": "purchase", "S": "sale_full", "E": "exchange"}
OWNER_MAP = {"SP": "spouse", "JT": "joint", "DC": "dependent"}

# --- regexes over pypdf text ------------------------------------------------
# data line, e.g.  "S 04/16/202605/04/2026$15,001 -"  or  "S (partial) 03/16/2026..."
# groups: type, partial|full?, txn_date, notif_date, amount_min, amount_max?
_DATA_RE = re.compile(
    r"\b([SPE])\s*(?:\((partial|full)\))?\s+"
    r"(\d{2}/\d{2}/\d{4})\s*(\d{2}/\d{2}/\d{4})\s*\$([\d,]+)"
    r"\s*(?:-\s*\$?([\d,]+))?"
)
# ticker like "(ABT) [ST]" — first char alpha so CUSIPs/bonds (start w/ digit) drop
_TICKER_RE = re.compile(r"\(([A-Z][A-Z.]{0,5})\)\s*\[([A-Z]{2})\]")
_OWNER_RE = re.compile(r"^\s*(SP|JT|DC)\b")
_AMT_ONLY_RE = re.compile(r"^\s*\$?([\d,]+)\s*$")


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


def _norm_name(first, last):
    """'David A Perdue, Jr' -> 'david perdue'. Mirrors ingest_senate._normalize_name."""
    full = f"{first} {last}".strip().lower()
    full = re.sub(r"[,.]", " ", full)
    full = re.sub(r"\b(jr|sr|ii|iii|iv|md|phd)\b", "", full)
    tokens = [t for t in full.split() if len(t) > 1]
    if len(tokens) >= 2:
        return f"{tokens[0]} {tokens[-1]}"
    return " ".join(tokens)


def _int(s):
    return int(s.replace(",", "")) if s else None


# --- pure parsers (unit-tested, no network) ---------------------------------
def parse_header(text):
    """Return (name, statedst) from PTR text. statedst like 'GA12'."""
    name = None
    statedst = None
    m = re.search(r"Name:\s*(?:Hon\.\s*)?(.+)", text)
    if m:
        name = m.group(1).strip()
    m = re.search(r"State/District:\s*([A-Z]{2}\d{1,2})", text)
    if m:
        statedst = m.group(1).strip()
    return name, statedst


def is_scanned(text):
    """True if the PDF yielded no usable text (scanned/handwritten filing)."""
    if not text:
        return True
    if len(re.sub(r"\s", "", text)) < 200:
        return True
    return not _DATA_RE.search(text)


def parse_transactions(text):
    """Parse PTR text -> list of dicts {ticker, type, txn_date, disc_date,
    amount_min, amount_max, owner}. Pure function; no I/O."""
    # rejoin tickers whose "(TICKER)" and "[ST]" wrapped onto separate lines
    text = re.sub(r"\)\s*\n\s*\[", ") [", text)
    lines = text.split("\n")
    out = []
    pending_ticker = None
    pending_owner = None
    for i, line in enumerate(lines):
        om = _OWNER_RE.match(line)
        if om:
            pending_owner = OWNER_MAP.get(om.group(1), "self")
        tm = _TICKER_RE.search(line)
        if tm:
            pending_ticker = tm.group(1)
        dm = _DATA_RE.search(line)
        if not dm:
            continue
        type_letter, partfull, d1, d2, amin, amax = dm.groups()
        # ticker on the same line before the data wins over a stale pending one
        same = _TICKER_RE.search(line[: dm.start()])
        ticker = same.group(1) if same else pending_ticker
        if not ticker:
            pending_ticker = None
            pending_owner = None
            continue
        if amax is None and i + 1 < len(lines):
            nm = _AMT_ONLY_RE.match(lines[i + 1])
            if nm:
                amax = nm.group(1)
        if type_letter == "S":
            ttype = "sale_partial" if partfull == "partial" else "sale_full"
        else:
            ttype = TYPE_MAP.get(type_letter, type_letter.lower())
        out.append({
            "ticker": ticker.upper().replace(".", "-"),
            "type": ttype,
            "txn_date": _parse_date(d1),
            "disc_date": _parse_date(d2),
            "amount_min": _int(amin),
            "amount_max": _int(amax),
            "owner": pending_owner or "self",
        })
        pending_ticker = None
        pending_owner = None
    return out


# --- I/O --------------------------------------------------------------------
def _http_get(url, retries=3, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def download_index(year):
    """Return list of PTR rows (FilingType 'P') for a year."""
    raw = _http_get(INDEX_URL.format(year=year))
    zf = zipfile.ZipFile(io.BytesIO(raw))
    txt = zf.read(f"{year}FD.txt").decode("utf-8", errors="replace")
    rows = list(csv.DictReader(io.StringIO(txt), delimiter="\t"))
    return [r for r in rows if (r.get("FilingType") or "").strip() == "P"]


def fetch_pdf(year, doc_id):
    """Download a PTR PDF to the on-disk cache; return path (skip if cached)."""
    ydir = os.path.join(CACHE_DIR, str(year))
    os.makedirs(ydir, exist_ok=True)
    path = os.path.join(ydir, f"{doc_id}.pdf")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    data = _http_get(PTR_URL.format(year=year, doc=doc_id))
    with open(path, "wb") as f:
        f.write(data)
    time.sleep(0.5)
    return path


def extract_text(path):
    """Extract PTR text with pypdf (pure-Python, portable, deterministic).

    NB: we standardize on pypdf rather than `pdftotext -layout` — the two emit
    the transaction table in different orders (pypdf puts the (TICKER) line
    before the amount/date line; pdftotext after), and parse_transactions is
    written for pypdf's ordering.
    """
    try:
        import pypdf
        r = pypdf.PdfReader(path)
        return "\n".join((p.extract_text() or "") for p in r.pages)
    except Exception as e:
        print(f"[house]   extract failed {path}: {e}")
        return ""


def _build_house_lookup(db):
    """(normalized_name -> bioguide) and ((name, statedst) -> bioguide) for House."""
    by_name = {}
    by_name_dist = {}
    for p in db.query(Politician).filter(Politician.chamber.in_(("rep", "house"))).all():
        key = _norm_name(p.first_name or "", p.last_name or "")
        if not key:
            continue
        by_name[key] = p.bioguide_id
        sd = f"{(p.state or '').strip()}{(p.district or '').strip()}"
        if sd:
            by_name_dist[(key, sd)] = p.bioguide_id
    return by_name, by_name_dist


def ingest(full_refresh=False):
    init_pol_db()
    db = SessionLocal()
    try:
        by_name, by_name_dist = _build_house_lookup(db)

        existing_ids = set()
        if not full_refresh:
            from sqlalchemy import text as _t
            existing_ids = {r[0] for r in db.execute(_t(
                "SELECT trade_id FROM congressional_trades WHERE source = 'house_clerk'"
            )).fetchall()}
            print(f"[house] {len(existing_ids)} existing house trades — incremental")

        inserted = skipped = scanned = unmatched = no_txn = 0
        for year in YEARS:
            try:
                ptrs = download_index(year)
            except Exception as e:
                print(f"[house] index {year} failed: {e}")
                continue
            print(f"[house] {year}: {len(ptrs)} PTR filings")
            for r in ptrs:
                doc = (r.get("DocID") or "").strip()
                first = (r.get("First") or "").strip()
                last = (r.get("Last") or "").strip()
                statedst = (r.get("StateDst") or "").strip()
                if not doc:
                    continue
                key = _norm_name(first, last)
                bioguide = by_name_dist.get((key, statedst)) or by_name.get(key)
                if not bioguide:
                    unmatched += 1
                    continue
                try:
                    path = fetch_pdf(year, doc)
                    text = extract_text(path)
                except Exception as e:
                    print(f"[house]   fetch/extract {doc} failed: {e}")
                    continue
                if is_scanned(text):
                    scanned += 1
                    continue
                txns = parse_transactions(text)
                if not txns:
                    no_txn += 1
                    continue
                for t in txns:
                    if not t["ticker"] or t["txn_date"] is None:
                        continue
                    if t["txn_date"] > date.today():
                        continue  # filer-typo'd future date — can't report a future trade
                    tid = _trade_id(bioguide, t["ticker"], t["txn_date"],
                                    t["amount_min"], t["type"])
                    if tid in existing_ids:
                        skipped += 1
                        continue
                    existing_ids.add(tid)
                    db.merge(CongressionalTrade(
                        trade_id=tid,
                        bioguide_id=bioguide,
                        ticker=t["ticker"],
                        asset_description="",
                        transaction_date=t["txn_date"],
                        disclosure_date=t["disc_date"],
                        transaction_type=t["type"],
                        amount_min=t["amount_min"],
                        amount_max=t["amount_max"],
                        owner=t["owner"],
                        source=SOURCE,
                        ingested_at=datetime.utcnow(),
                    ))
                    inserted += 1
                    if inserted % 1000 == 0:
                        db.commit()
                        print(f"[house]   committed {inserted}…")
        db.commit()
        print(f"[house] Done — inserted={inserted} skipped={skipped} "
              f"scanned={scanned} unmatched_filer={unmatched} no_txn={no_txn}")
    finally:
        db.close()


if __name__ == "__main__":
    ingest(full_refresh="--full" in sys.argv)
