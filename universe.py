"""
universe.py — Build the stock universe
=======================================
Three phases:
  1. DISCOVER  ~8000 raw tickers from NASDAQ Trader FTP (no yfinance needed)
  2. ENRICH    batch yf.Tickers calls → market cap, sector, price
  3. RANK      sort by market cap descending, save top TARGET stocks

Primary source: NASDAQ Trader FTP plain-text files (updated daily since ~2005)
  nasdaqlisted.txt — every stock on NASDAQ
  otherlisted.txt  — every stock on NYSE, AMEX, ARCA, BATS

Fallbacks if FTP returns < 1000 tickers:
  Wikipedia S&P 500 / 400 / 600 index pages
  iShares ETF public CSV holdings

Persistent cache: universe_cache.json (safe to interrupt and resume)
Output:           universe.json

Run:      python3 universe.py
Schedule: 0 1 * * 1   (Mondays 1am)
"""

import json, os, time, warnings
from datetime import datetime
from collections import Counter
from urllib.request import urlopen, Request
from urllib.error import URLError
warnings.filterwarnings("ignore")

import yfinance as yf

DATA_DIR     = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
OUTPUT_FILE  = os.path.join(DATA_DIR, "universe.json")
CACHE_FILE   = os.path.join(DATA_DIR, "universe_cache.json")
TARGET       = 2500
# Env-tunable pacing — slow down to dodge yfinance rate limits on big runs.
ENRICH_BATCH = int(os.environ.get("YF_BATCH", 50))
SLEEP_SEC    = float(os.environ.get("YF_SLEEP", 2))
YF_BACKOFF   = float(os.environ.get("YF_BACKOFF", 45))  # hard sleep on rate-limit signal
MAX_RETRIES  = 3
STALE_DAYS   = 7   # re-enrich if DB record older than this

NASDAQ_API_URL  = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true"
NASDAQ_FTP_URLS = [
    "https://ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://ftp.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
]

WIKI_SOURCES = [
    ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", ["Symbol"]),
    ("https://en.wikipedia.org/wiki/List_of_S%26P_400_companies", ["Symbol", "Ticker symbol", "Ticker"]),
    ("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies", ["Symbol", "Ticker symbol", "Ticker"]),
]

ISHARES_URLS = [
    "https://www.ishares.com/us/products/239707/ISHARES-RUSSELL-1000-ETF/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund",
    "https://www.ishares.com/us/products/239710/ISHARES-RUSSELL-2000-ETF/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
    "https://www.ishares.com/us/products/239726/ISHARES-CORE-SP-500-ETF/1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund",
]

# ── Fallback ticker list ───────────────────────────────────────────
# Used when all network discovery sources fail (firewall, blocked domains, etc.)
# Covers S&P 500 + major non-S&P large caps — ~530 tickers
FALLBACK_TICKERS = [
    # Mega Cap / FAANG+
    "AAPL","MSFT","NVDA","AMZN","GOOGL","GOOG","META","TSLA","BRK-B","AVGO",
    "JPM","LLY","V","UNH","XOM","MA","COST","HD","PG","JNJ",
    "ORCL","ABBV","BAC","KO","MRK","CVX","NFLX","CRM","AMD","PEP",
    "TMO","ACN","LIN","MCD","CSCO","ABT","DHR","IBM","TXN","GE",
    "PM","CAT","ISRG","GS","INTU","AMGN","SPGI","RTX","VZ","BKNG",
    "NOW","T","NEE","BLK","PFE","UBER","SCHW","HON","MS","LOW",
    "ETN","TJX","SYK","AMAT","PLD","BSX","VRTX","PANW","AXP","ADI",
    "GILD","MDT","C","DE","REGN","MMC","CB","SO","MO","ZTS",
    "CI","CME","LRCX","DUK","USB","ICE","AON","SHW","WM","PH",
    "MCO","GD","FI","INTC","EMR","ITW","APD","FCX","NSC","ELV",
    # S&P 500 mid-large
    "PSA","CL","HCA","NOC","MET","AFL","AIG","PRU","ALL","TRV",
    "HUM","CVS","MCK","WBA","ABC","CAH","BMY","BIIB","ILMN","MRNA",
    "DXCM","IQV","CNC","MOH","HIG","LHX","GWW","ROK","PTC","KEYS",
    "ANSS","CDNS","SNPS","FTNT","CRWD","OKTA","ZS","NET","DDOG","MDB",
    "SNOW","TEAM","WDAY","VEEV","HUBS","TTD","RBLX","COIN","HOOD","PLTR",
    "SQ","PYPL","AFRM","SOFI","NU","WFC","RF","CFG","HBAN","KEY",
    "FITB","MTB","ZION","CMA","SIVB","WAL","EWBC","PACW","FHN","BOK",
    "PNC","TFC","NTRS","STT","BK","TROW","IVZ","BEN","AMG","WRB",
    "RE","RNR","AJG","MMB","ERIE","HIG","CINF","GL","FG","KMPR",
    "UPS","FDX","XPO","SAIA","ODFL","JBHT","KNX","CHRW","EXPD","LSTR",
    "DAL","UAL","LUV","AAL","ALK","HA","JBLU","SAVE","ULCC","CEA",
    "CCL","RCL","NCLH","MGM","LVS","WYNN","CZR","PENN","DKNG","RSI",
    "MAR","HLT","H","IHG","CHH","WH","RHP","HST","PK","AHT",
    "DIS","CMCSA","WBD","PARA","FOX","FOXA","NYT","NWSA","NWS","IPG",
    "OMC","PUB","WPP","MTCH","IAC","ANGI","Z","RDFN","OPEN","EXPI",
    "AMGN","REGN","VRTX","BIIB","MRNA","BNTX","GILD","ALNY","SGEN","BMRN",
    "INCY","RARE","ACAD","FOLD","HALO","ARWR","IONS","AGEN","FATE","NTLA",
    "PFE","MRK","BMY","LLY","ABBV","AZN","GSK","NVO","ROG","SNY",
    "JNJ","ABT","MDT","SYK","BSX","ZBH","EW","HOLX","DXCM","PODD",
    "TMO","DHR","A","BIO","ILMN","MTD","PKI","WAT","TECH","NEOG",
    "BA","LMT","RTX","NOC","GD","L3H","HII","TXT","SPR","HXL",
    "CAT","DE","CMI","PCAR","NAV","OSK","TEX","AGCO","CNH","CNHI",
    "MMM","HON","GE","ITW","EMR","ROK","PH","IR","AME","ROP",
    "XOM","CVX","COP","EOG","PXD","DVN","MRO","APA","HAL","SLB",
    "BKR","FTI","NOV","HP","WTTR","NE","VAL","PBF","VLO","PSX",
    "MPC","HES","OXY","FANG","CLR","WPX","MEG","SU","CNQ","CVE",
    "NEE","DUK","SO","D","AEP","EXC","SRE","PCG","ED","XEL",
    "WEC","ES","ETR","FE","PPL","CMS","LNT","EVRG","NI","OGE",
    "AMT","CCI","SBAC","DLR","EQIX","PLD","PSA","EXR","CUBE","LSI",
    "WY","PCH","PotlatchDeltic","RYN","CTT","NTR","MOS","CF","IPI","SMG",
    "APD","LIN","PPG","SHW","ECL","IFF","EMN","CE","HUN","RPM",
    "COST","WMT","TGT","KR","ACI","SFM","GO","CASY","MUSA","DENN",
    "MCD","SBUX","CMG","YUM","QSR","DPZ","WING","JACK","FAT","EAT",
    "NKE","LULU","PVH","HBI","VFC","RL","TPR","CPRI","UAA","UA",
    "AMZN","EBAY","ETSY","W","CHWY","OSTK","PRTS","RCII","RENT","AAN",
    "NFLX","DIS","WBD","PARA","AMCX","LGF-A","CURI","FWONA","FWONK","LSXMA",
    "GOOGL","META","SNAP","PINS","TWTR","BMBL","MTCH","YELP","ANGI","DHX",
    "MSFT","ORCL","SAP","CRM","NOW","WDAY","VEEV","HUBS","PCTY","PAYC",
    "ADBE","INTU","ANSS","CDNS","SNPS","MANH","GWRE","PEGA","NICE","SMAR",
    "NVDA","AMD","INTC","AVGO","QCOM","TXN","MRVL","MCHP","ADI","LSCC",
    "AMAT","LRCX","KLAC","ASML","TER","COHU","FORM","ACLS","ONTO","CAMT",
    "AAPL","HPQ","HPE","DELL","NTAP","STX","WDC","PSTG","NTNX","PYCR",
    "CSCO","JNPR","ANET","CIEN","VIAV","LITE","IIVI","COHR","OCLR","FNSR",
    "V","MA","PYPL","SQ","FIS","FISV","GPN","WEX","EVTC","RPAY",
    "JPM","BAC","WFC","C","GS","MS","USB","PNC","TFC","COF",
    "AXP","DFS","SYF","COF","BFH","OMF","CACC","SC","ALLY","NMFC",
    "BLK","SCHW","TROW","IVZ","BEN","AMG","WRB","RJF","SF","VRTS",
    "SPGI","MCO","ICE","CME","CBOE","NDAQ","MKTX","TW","BGCP","GFI",
]

CAP_TIERS = [
    (200e9, float("inf"), "Mega Cap"),
    (10e9,  200e9,        "Large Cap"),
    (2e9,   10e9,         "Mid Cap"),
    (300e6, 2e9,          "Small Cap"),
    (0,     300e6,        "Micro Cap"),
]


# ── Helpers ───────────────────────────────────────────────────────

def cap_label(mc):
    for lo, hi, label in CAP_TIERS:
        if lo <= (mc or 0) < hi:
            return label
    return "Micro Cap"

def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_cache(cache):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)

def http_get(url, timeout=30):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def clean_ticker(raw):
    """
    Normalise a raw ticker string for Yahoo Finance.
    Returns cleaned ticker or None if it looks like a non-equity instrument.
    """
    t = str(raw).strip().upper()
    if not t or t in ("SYMBOL", "TEST", "N/A", "-"):
        return None
    t = t.replace(".", "-")  # BRK.B → BRK-B
    # Reject warrants, rights, units, preferred
    if any(t.endswith(s) for s in ("W", "R", "U", "WS", "WT", "RT", "+", "~", "^")):
        return None
    if not all(c.isalpha() or c == "-" for c in t):
        return None
    if len(t) > 6:
        return None
    return t

def _retry(fn, label):
    """Run fn() with exponential backoff. Returns result or None on failure."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                wait = SLEEP_SEC * (attempt + 1)
                print(f"    Attempt {attempt+1} failed ({label}): {e} — retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"    ❌ Failed after {MAX_RETRIES} attempts ({label}): {e}")
    return None


# ── Phase 1: Discovery ────────────────────────────────────────────

def fetch_nasdaq_api():
    """
    Primary source: NASDAQ Screener API (JSON).
    Returns all US-listed stocks in one HTTPS request.
    """
    import json as _json

    def fetch():
        text = http_get(NASDAQ_API_URL, timeout=45)
        data = _json.loads(text).get("data", {})
        # The &download=true endpoint returns rows at data.rows (~7000 tickers);
        # the non-download form nests them under data.table.rows. Support both.
        rows = data.get("rows") or data.get("table", {}).get("rows", [])
        found = []
        for row in rows:
            t = clean_ticker(row.get("symbol", ""))
            if t:
                found.append(t)
        return found

    result = _retry(fetch, "nasdaq-api")
    if result:
        print(f"    NASDAQ API: {len(result)} tickers")
    return result or []


def fetch_nasdaq_ftp():
    """
    Reads nasdaqlisted.txt and otherlisted.txt — first checks for local files,
    then falls back to HTTPS download.
    """
    LOCAL_FILES = {
        "nasdaqlisted.txt": False,
        "otherlisted.txt":  True,
    }
    all_tickers = []

    for filename, is_other in LOCAL_FILES.items():
        # Try local file first
        text = None
        if os.path.exists(filename):
            try:
                with open(filename, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                print(f"    Reading local {filename} …")
            except Exception:
                text = None

        # Fall back to HTTPS
        if not text:
            url = f"https://ftp.nasdaqtrader.com/dynamic/SymDir/{filename}"
            def fetch(url=url):
                return http_get(url, timeout=45)
            text = _retry(fetch, filename.replace(".txt", ""))

        if not text:
            continue

        lines = text.strip().splitlines()
        data  = lines[1:]
        if data and data[-1].startswith("File Creation"):
            data = data[:-1]

        found = []
        for line in data:
            parts = line.split("|")
            if not parts:
                continue
            if is_other:
                is_etf     = parts[4].strip() if len(parts) > 4 else "N"
                test_issue = parts[6].strip() if len(parts) > 6 else "N"
            else:
                is_etf     = parts[6].strip() if len(parts) > 6 else "N"
                test_issue = parts[3].strip() if len(parts) > 3 else "N"
            if is_etf == "Y" or test_issue == "Y":
                continue
            t = clean_ticker(parts[0])
            if t:
                found.append(t)

        if found:
            all_tickers.extend(found)
            print(f"    {filename}: {len(found)} tickers")

    return all_tickers


def fetch_wikipedia():
    try:
        import pandas as pd
    except ImportError:
        print("  pandas not available — skipping Wikipedia")
        return []

    tickers = []
    for url, cols in WIKI_SOURCES:
        def fetch(url=url, cols=cols):
            from io import StringIO
            import requests
            # pd.read_html has no session/UA param; default urllib UA gets 403 from
            # Wikipedia. Fetch HTML ourselves with a browser UA, then parse the text.
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            tables = pd.read_html(StringIO(resp.text))
            for df in tables:
                for col in cols:
                    if col in df.columns:
                        found = [clean_ticker(t) for t in df[col].dropna()]
                        return [t for t in found if t]
            return []

        result = _retry(fetch, url.split("/")[-1])
        if result:
            tickers.extend(result)
            print(f"    Wikipedia: {len(result)} tickers")
        time.sleep(2)

    return tickers


def fetch_ishares():
    try:
        import pandas as pd
    except ImportError:
        return []

    tickers = []
    for url in ISHARES_URLS:
        def fetch(url=url):
            # iShares CSVs have ~2 metadata rows then a header row
            # Try skiprows 2, 1, 0 until we find a "Ticker" column
            for skip in (2, 1, 0, 3):
                try:
                    df = pd.read_csv(url, skiprows=skip, on_bad_lines="skip")
                    # Find whichever column looks like the ticker column
                    ticker_col = None
                    for col in df.columns:
                        if str(col).strip().lower() in ("ticker", "symbol", "cusip"):
                            if str(col).strip().lower() in ("ticker", "symbol"):
                                ticker_col = col
                                break
                    if ticker_col is None:
                        # Use first column as fallback
                        ticker_col = df.columns[0]
                    found = [clean_ticker(str(t)) for t in df[ticker_col].dropna()]
                    result = [t for t in found if t and t.isalpha() and len(t) <= 5]
                    if len(result) > 10:   # sanity check — at least 10 real tickers
                        return result
                except Exception:
                    continue
            return []

        label  = url.split("fileName=")[1].split("&")[0]
        result = _retry(fetch, label)
        if result:
            tickers.extend(result)
            print(f"    iShares ({label}): {len(result)} tickers")
        time.sleep(1)

    return tickers


# ── Phase 2: Enrichment ───────────────────────────────────────────

def _enrich_batch(batch, cache):
    """Fetch metadata for one batch of tickers. Returns list of tickers that failed."""
    failed = []
    try:
        group = yf.Tickers(" ".join(batch))
        for ticker in batch:
            try:
                info = group.tickers[ticker].info or {}
                mc   = info.get("marketCap") or 0
                if mc > 0 or info.get("longName") or info.get("shortName"):
                    cache[ticker] = {
                        "ticker":   ticker,
                        "name":     info.get("longName") or info.get("shortName", ""),
                        "sector":   info.get("sector", "Unknown"),
                        "industry": info.get("industry", "Unknown"),
                        "country":  info.get("country", "Unknown"),
                        "mkt_cap":  mc,
                        "cap_size": cap_label(mc),
                        "price":    info.get("currentPrice") or info.get("regularMarketPrice") or 0,
                        "exchange": info.get("exchange", ""),
                    }
                # If no data at all, don't cache — let future runs retry naturally
            except Exception:
                failed.append(ticker)
    except Exception:
        failed.extend(batch)
    return failed


def _fresh_in_db(tickers):
    """Return set of tickers already in the stocks DB table and recently updated."""
    try:
        from database import SessionLocal, Stock
        from datetime import datetime as _dt
        db = SessionLocal()
        rows = db.query(Stock.ticker, Stock.last_updated).filter(Stock.ticker.in_(tickers)).all()
        db.close()
        return {t for t, upd in rows if upd and (_dt.utcnow() - upd).days < STALE_DAYS}
    except Exception:
        return set()


def enrich(tickers, cache):
    fresh = _fresh_in_db(tickers)
    need = [t for t in tickers if t not in cache and t not in fresh]
    skipped = len(tickers) - len(need)
    if skipped:
        print(f"  Skipping {skipped} tickers (fresh in DB or cache)")
    if not need:
        print(f"  All {len(tickers)} tickers are fresh — nothing to enrich")
        return cache

    total = len(need)
    pages = (total + ENRICH_BATCH - 1) // ENRICH_BATCH
    print(f"  Enriching {total} tickers ({pages} batches of {ENRICH_BATCH}) …")

    failed = []
    for i in range(0, total, ENRICH_BATCH):
        batch = need[i:i + ENRICH_BATCH]
        page  = i // ENRICH_BATCH + 1
        if page == 1 or page % 10 == 0 or page == pages:
            print(f"    Batch {page:>4}/{pages}  ({i+1}–{min(i+ENRICH_BATCH, total)})  {round(i/total*100)}%")
        batch_failed = _enrich_batch(batch, cache)
        failed.extend(batch_failed)
        save_cache(cache)
        # Whole batch failing is the rate-limit signal — back off hard so the
        # rest of the universe isn't lost (this is why we only got 539/2500).
        if batch_failed and len(batch_failed) == len(batch):
            print(f"    ⚠️  batch {page} fully failed — backing off {YF_BACKOFF}s (rate limit?)")
            time.sleep(YF_BACKOFF)
        else:
            time.sleep(SLEEP_SEC)

    # One retry pass for anything that failed
    failed = list(set(failed))
    if failed:
        print(f"  Retrying {len(failed)} failed tickers (waiting {YF_BACKOFF}s first) …")
        time.sleep(YF_BACKOFF)
        still_failed = []
        for i in range(0, len(failed), ENRICH_BATCH):
            still_failed.extend(_enrich_batch(failed[i:i + ENRICH_BATCH], cache))
            save_cache(cache)
            time.sleep(SLEEP_SEC)
        if still_failed:
            print(f"  ⚠️  No data for {len(still_failed)} tickers — excluded from universe")

    return cache


# ── Phase 3: Ranking + output ─────────────────────────────────────

def main():
    print("=" * 58)
    print(f"  universe.py — Dynamic Top {TARGET} by Market Cap")
    print(f"  yfinance {yf.__version__}")
    print("=" * 58)

    cache = load_cache()
    cached_valid = sum(1 for s in cache.values() if s.get("mkt_cap", 0) > 0)
    print(f"Cache: {len(cache)} entries  ({cached_valid} with valid market cap)\n")

    # ── Discover ──────────────────────────────────────────────────
    print("─── Source 1: NASDAQ API ───")
    raw = fetch_nasdaq_api()
    print(f"  Total from NASDAQ API: {len(raw)} tickers")

    if len(raw) < 1000:
        print("\n─── Source 1b: NASDAQ Trader FTP (fallback) ───")
        ftp = fetch_nasdaq_ftp()
        raw.extend(ftp)
        print(f"  Total from FTP: {len(ftp)} tickers")

    print("\n─── Source 2: Wikipedia index pages ───")
    wiki = fetch_wikipedia()
    raw.extend(wiki)
    print(f"  Total from Wikipedia: {len(wiki)} tickers")

    if len(set(raw)) < 1000:
        print("\n─── Source 3: iShares ETF holdings ───")
        etf = fetch_ishares()
        raw.extend(etf)
        print(f"  Total from iShares: {len(etf)} tickers")

    unique = list(dict.fromkeys(t for t in raw if t))  # deduplicate, preserve order
    print(f"\nTotal unique tickers: {len(unique)}")

    if len(unique) < 100:
        print("⚠️  All network sources failed — using built-in fallback ticker list (~530 tickers)")
        print("   This covers S&P 500 + major large caps. Re-run later for full 2500-stock universe.")
        unique = list(dict.fromkeys(t for t in FALLBACK_TICKERS if t))

    # ── Enrich ────────────────────────────────────────────────────
    print("\n─── Enriching via yf.Tickers batches ───")
    cache = enrich(unique, cache)

    # ── Rank ──────────────────────────────────────────────────────
    print("\n─── Building ranked universe ───")
    valid = sorted(
        [s for s in cache.values() if s.get("mkt_cap", 0) > 0],
        key=lambda x: x["mkt_cap"],
        reverse=True,
    )
    universe = valid[:TARGET]
    for i, s in enumerate(universe):
        s["rank"] = i + 1

    with open(OUTPUT_FILE, "w") as f:
        json.dump({"generated": datetime.now().isoformat(), "total": len(universe), "stocks": universe}, f, indent=2)

    # ── Save to database ──────────────────────────────────────────
    _save_to_db(universe)

    # ── Summary ───────────────────────────────────────────────────
    caps = Counter(s["cap_size"] for s in universe)
    secs = Counter(s.get("sector", "Unknown") for s in universe)

    print(f"\n✅ Saved {len(universe)} stocks → {OUTPUT_FILE}")
    print("\nCap Breakdown:")
    for label in ["Mega Cap", "Large Cap", "Mid Cap", "Small Cap", "Micro Cap"]:
        print(f"  {label:12}: {caps.get(label, 0)}")
    print("\nTop Sectors:")
    for k, v in secs.most_common(8):
        print(f"  {k}: {v}")
    print("\nTop 10 by Market Cap:")
    for s in universe[:10]:
        print(f"  #{s['rank']:>4}  {s['ticker']:<6}  {s.get('name','')[:32]:<32}  ${s['mkt_cap']/1e9:>8.1f}B  {s['cap_size']}")

    if len(universe) < TARGET:
        print(f"\n⚠️  Got {len(universe)}/{TARGET}. Re-run to continue — cache persists.")


def _save_to_db(universe):
    """Upsert the ranked universe into stocks.db (the authoritative store).

    Each entry `s` is the SAME dict already written to universe.json above;
    serializers.db_record projects it onto the Stock columns so the JSON row and
    the DB row can't drift and the schema lives in one place (issue #12)."""
    try:
        from database import SessionLocal, Stock, init_db, upsert
        from serializers import db_record
        from datetime import datetime as _dt
        init_db()
        db = SessionLocal()
        for s in universe:
            row = db_record(Stock, s)
            row["last_updated"] = _dt.utcnow()
            upsert(db, Stock, row)
        db.commit()
        db.close()
        print(f"  ✅ Saved {len(universe)} stocks to database")
    except Exception as e:
        print(f"  ⚠️  DB save failed: {e}")


if __name__ == "__main__":
    main()
