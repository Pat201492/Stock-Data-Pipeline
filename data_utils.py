"""
data_utils.py — Shared utilities
==================================
Imported by universe.py, fundamentals.py, and model.py.

Provides:
  - Safe numeric helpers      sf, fmt, pct, ratio, cagr
  - DataFrame extractors      extract_field, first_valid, second_valid,
                              series_values, series_cagr
  - Cache I/O                 load_cache, save_cache
  - Cap size labels           cap_label
  - Batch retry runner        run_batches
  - Data quality scoring      data_quality_score
"""

import json, math, time, os
import config


# ── Numeric safety ────────────────────────────────────────────────────────────

def sf(val):
    """Convert val to float; return None on failure, NaN, or Inf."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None

def fmt(val, decimals=2):
    """Round to decimals; return None if not a valid number."""
    f = sf(val)
    return round(f, decimals) if f is not None else None

def pct(numerator, denominator, decimals=2):
    """(n / d) * 100, or None if either is missing or d == 0."""
    n, d = sf(numerator), sf(denominator)
    if n is None or d is None or d == 0:
        return None
    return fmt(n / d * 100, decimals)

def ratio(numerator, denominator, decimals=2):
    """n / d, or None if either is missing or d == 0."""
    n, d = sf(numerator), sf(denominator)
    if n is None or d is None or d == 0:
        return None
    return fmt(n / d, decimals)

def cagr(start, end, years, decimals=2):
    """
    Compound annual growth rate from start → end over `years`.
    Returns a percentage (e.g. 12.5 means 12.5%), or None.
    Requires start > 0.
    """
    s, e, y = sf(start), sf(end), sf(years)
    if s is None or e is None or y is None or y <= 0 or s <= 0:
        return None
    try:
        return fmt(((e / s) ** (1 / y) - 1) * 100, decimals)
    except (ZeroDivisionError, ValueError):
        return None


# ── Cap size labels ───────────────────────────────────────────────────────────

CAP_TIERS = [
    (200e9, float("inf"), "Mega Cap"),
    (10e9,  200e9,        "Large Cap"),
    (2e9,   10e9,         "Mid Cap"),
    (300e6, 2e9,          "Small Cap"),
    (0,     300e6,        "Micro Cap"),
]

def cap_label(mc):
    """Return a cap-size string for a raw market-cap value in dollars."""
    mc = sf(mc)
    if not mc or mc <= 0:
        return "Micro Cap"
    for lo, hi, label in CAP_TIERS:
        if lo <= mc < hi:
            return label
    return "Micro Cap"


# ── Cache I/O ─────────────────────────────────────────────────────────────────

def load_cache(path):
    """Load a JSON cache file; return empty dict if missing or corrupt."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_cache(cache, path):
    """Write cache dict to a JSON file atomically."""
    with open(path, "w") as f:
        json.dump(cache, f, indent=2)


# ── Batch runner with retry ───────────────────────────────────────────────────

def run_batches(items, fetch_fn, cache, cache_path,
                batch_size=None, sleep_sec=None, max_retries=None,
                progress_every=10):
    """
    Generic batch processor used by all three fetch scripts.

    Parameters
    ----------
    items       : list of keys (tickers) to process
    fetch_fn    : callable(batch) → (results_dict, failed_list)
    cache       : dict to update in-place with results
    cache_path  : file path to save after each batch
    batch_size  : tickers per API call
    sleep_sec   : seconds to wait between batches
    max_retries : how many times to retry failed tickers
    progress_every : print progress line every N batches

    Returns
    -------
    cache (mutated in-place, also returned for convenience)
    """
    # Env-tunable pacing — single source of defaults is config.py (which reads the
    # YF_* env vars once). Env still wins over a caller-supplied arg; when a caller
    # passes nothing the canonical config default applies. No defaults duplicated here.
    sleep_sec   = float(os.environ.get("YF_SLEEP",   config.YF_SLEEP   if sleep_sec   is None else sleep_sec))
    batch_size  = int(os.environ.get("YF_BATCH",     config.YF_BATCH   if batch_size  is None else batch_size))
    max_retries = int(os.environ.get("YF_RETRIES",   config.YF_RETRIES if max_retries is None else max_retries))
    backoff     = config.YF_BACKOFF  # hard sleep on a rate-limit signal

    need  = [t for t in items if t not in cache]
    if not need:
        print(f"  All {len(items)} already in cache — skipping fetch")
        return cache

    total  = len(need)
    pages  = (total + batch_size - 1) // batch_size
    failed = []
    print(f"  {total} to fetch ({pages} batches of {batch_size}, sleep {sleep_sec}s)")

    for i in range(0, total, batch_size):
        batch  = need[i:i + batch_size]
        page   = i // batch_size + 1
        if page == 1 or page % progress_every == 0 or page == pages:
            pct_done = round(i / total * 100)
            print(f"    Batch {page:>4}/{pages}  "
                  f"({i+1}–{min(i+batch_size, total)})  {pct_done}%")
        results, errs = fetch_fn(batch)
        cache.update(results)
        failed.extend(errs)
        save_cache(cache, cache_path)
        # A whole batch failing is the rate-limit signal — back off hard so the
        # rest of the run isn't lost (this is what wiped commodities/options).
        if errs and len(errs) == len(batch):
            print(f"    ⚠️  batch {page} fully failed — backing off {backoff}s (rate limit?)")
            time.sleep(backoff)
        else:
            time.sleep(sleep_sec)

    # Retry pass — longer pause first, the failures are usually rate-limit driven
    for attempt in range(max_retries):
        if not failed:
            break
        failed = list(set(failed))
        print(f"  Retry pass {attempt+1}: {len(failed)} tickers (waiting {backoff}s first)")
        time.sleep(backoff)
        next_failed = []
        for i in range(0, len(failed), batch_size):
            results, errs = fetch_fn(failed[i:i + batch_size])
            cache.update(results)
            next_failed.extend(errs)
            save_cache(cache, cache_path)
            time.sleep(sleep_sec)
        failed = next_failed

    if failed:
        print(f"  ⚠️  Gave up on {len(failed)} tickers: {failed[:8]}")

    return cache


# ── DataFrame helpers ─────────────────────────────────────────────────────────

def get_row(df, *keys):
    """
    Return the first matching row from a DataFrame index.
    Tries each key in order; returns a pandas Series or None.
    """
    if df is None or df.empty:
        return None
    for key in keys:
        if key in df.index:
            return df.loc[key]
    return None

def first_valid(series):
    """First non-NaN float in a pandas Series (most-recent year first)."""
    if series is None:
        return None
    for v in series.values:
        f = sf(v)
        if f is not None:
            return f
    return None

def second_valid(series):
    """Second non-NaN float in a pandas Series (prior year)."""
    if series is None:
        return None
    count = 0
    for v in series.values:
        f = sf(v)
        if f is not None:
            count += 1
            if count == 2:
                return f
    return None

def series_values(series, max_n=6):
    """Return up to max_n valid floats from a pandas Series, newest first."""
    if series is None:
        return []
    vals = []
    for v in series.values:
        f = sf(v)
        if f is not None:
            vals.append(f)
        if len(vals) >= max_n:
            break
    return vals

def series_cagr(series, years=5, decimals=2):
    """CAGR computed from a pandas Series (newest-first, up to `years` back)."""
    vals = series_values(series, years + 1)
    if len(vals) < 2:
        return None
    n = min(len(vals) - 1, years)
    return cagr(vals[n], vals[0], n, decimals)


# ── Financial field name map ──────────────────────────────────────────────────
# yfinance uses inconsistent field names across versions and company types.
# Each tuple is tried in order until one matches the DataFrame index.

FIELD_MAPS = {
    # Income statement
    "revenue":          ("Total Revenue", "Revenue", "totalRevenue",
                         "Net Revenue", "Sales", "Total Net Revenue"),
    "gross_profit":     ("Gross Profit", "GrossProfit", "grossProfit"),
    "operating_income": ("Operating Income", "EBIT", "Operating Profit",
                         "Total Operating Profit Loss", "Pretax Income"),
    "net_income":       ("Net Income", "Net Income Common Stockholders",
                         "Net Income From Continuing Operations",
                         "netIncome", "Net Profit"),
    "ebitda":           ("EBITDA", "Normalized EBITDA", "ebitda",
                         "Adjusted EBITDA"),
    "interest_expense": ("Interest Expense", "Interest Expense Non Operating",
                         "interestExpense", "Net Interest Income"),
    "tax_provision":    ("Tax Provision", "Income Tax Expense", "taxProvision"),
    "eps_diluted":      ("Diluted EPS", "Basic EPS", "EPS",
                         "dilutedEPS", "Diluted Earnings Per Share"),
    # Cash flow
    "operating_cf":     ("Operating Cash Flow", "Cash Flow From Operations",
                         "Cash From Operations",
                         "Total Cash From Operating Activities",
                         "Net Cash Provided By Operating Activities",
                         "Cash Generated From Operations"),
    "capex":            ("Capital Expenditure", "Purchase Of PPE",
                         "Capital Expenditures",
                         "Purchases Of Property Plant And Equipment",
                         "Net PPE Purchase And Sale",
                         "Capital Expenditure Reported"),
    "free_cash_flow":   ("Free Cash Flow", "freeCashFlow"),
    # Balance sheet
    "total_debt":       ("Total Debt",
                         "Long Term Debt And Capital Lease Obligation",
                         "Long Term Debt", "Total Long Term Debt",
                         "Short Long Term Debt Total"),
    "cash":             ("Cash And Cash Equivalents",
                         "Cash Cash Equivalents And Short Term Investments",
                         "Cash And Short Term Investments", "Cash"),
    "equity":           ("Stockholders Equity", "Total Stockholders Equity",
                         "Common Stock Equity",
                         "Total Equity Gross Minority Interest",
                         "Stockholders Equity Including Minority Interest"),
    "total_assets":     ("Total Assets", "totalAssets"),
    "current_assets":   ("Current Assets", "Total Current Assets",
                         "currentAssets"),
    "current_liabilities": ("Current Liabilities", "Total Current Liabilities",
                            "currentLiabilities"),
    "invested_capital": ("Invested Capital", "Total Capital"),
    "ppe_net":          ("Net PPE", "Net Property Plant And Equipment",
                         "Property Plant Equipment Net"),
}


# ── Magic Formula (Greenblatt) ────────────────────────────────────────────────
# Both return a percent, or None when any input is missing or the denominator is
# zero -- never a 0 substituted for an unreported number. Definitions match
# Trader-Screener's tools/edgar_scrubber/magic_inputs.py so its XBRL compare is
# like-for-like (Trader-Screener #248). Distinct from `roic` (NOPAT / invested
# capital), which is unchanged.

def roc_greenblatt(ebit, current_assets, current_liabilities, ppe_net):
    """Greenblatt ROC = EBIT / ((current assets - current liabilities) + net PP&E)."""
    vals = [sf(v) for v in (ebit, current_assets, current_liabilities, ppe_net)]
    if any(v is None for v in vals):
        return None
    e, ca, cl, ppe = vals
    return pct(e, (ca - cl) + ppe)

def ebit_ev_yield(ebit, mkt_cap, total_debt, cash):
    """Earnings yield = EBIT / EV, with EV = market cap + total debt - cash."""
    vals = [sf(v) for v in (ebit, mkt_cap, total_debt, cash)]
    if any(v is None for v in vals):
        return None
    e, mc, debt, c = vals
    return pct(e, mc + debt - c)

def extract_field(df, field_name):
    """Look up a field in a DataFrame using FIELD_MAPS fallback names."""
    return get_row(df, *FIELD_MAPS.get(field_name, ()))


# ── Data quality scoring ──────────────────────────────────────────────────────

# Fields and their weights (total = 100 when all present)
# Core income/cash flow fields = highest weight (they drive both models)
# Growth CAGRs = medium weight (needed for DCF)
# Ratios = lower weight (useful but not blocking)
_QUALITY_WEIGHTS = {
    # Core financials — 60 pts total
    "rev_now":      8,
    "net_income":   8,
    "fcf":          8,
    "ebitda":       6,
    "gross_margin": 6,
    "op_margin":    6,
    "eps_ttm":      6,
    "eps_fwd":      6,   # needed for comps P/E
    # Growth — 20 pts total
    "rev_cagr_5y":  7,
    "eps_cagr_5y":  7,
    "fcf_cagr_5y":  6,
    # Valuation multiples — 12 pts total
    "pe":           6,
    "ev_ebitda":    6,
    # Balance sheet — 8 pts total
    "d_to_e":       4,
    "roic":         4,
}
_QUALITY_TOTAL = sum(_QUALITY_WEIGHTS.values())  # = 100

def data_quality_score(record):
    """
    0–100 weighted score representing how complete a stock's data is.
    Missing sector/industry caps the score at 85 (can't do comps properly).
    Missing price caps at 50 (nothing useful to compute).
    """
    score = sum(
        w for f, w in _QUALITY_WEIGHTS.items()
        if record.get(f) is not None
    )
    score = round(score / _QUALITY_TOTAL * 100)

    # Penalise missing sector/industry (needed for peer groups)
    if not record.get("sector") or record.get("sector") == "Unknown":
        score = min(score, 50)
    elif not record.get("industry") or record.get("industry") == "Unknown":
        score = min(score, 85)

    # Penalise missing price (nothing to compare)
    if not record.get("price"):
        score = min(score, 30)

    return score


# ── Investment scorecard (shared between deepdive.py and sheets.py) ───────────

def score_stock(stock, dcf=None, comps=None, m3=None):
    """
    Seven-category weighted scorecard (0–100 composite).

    dcf / comps / m3 are optional dicts from the model layer.
    When called from sheets.py they are passed directly from model.json fields
    on the merged stock record (dcf_upside_pct, comps_upside_pct, etc.).
    When called from deepdive.py the full model dicts are passed.

    Categories & weights:
      Business Quality       25 %
      Growth Track Record    20 %
      Financial Health       15 %
      Valuation              20 %
      Earnings Quality       10 %
      Momentum & Technicals   5 %
      Analyst Consensus       5 %

    Returns dict with:
      composite, rec_label, rec_stars, rec_bg, rec_fg,
      categories {name: {weight, score, items}}, avg_upside
    """
    dcf   = dcf   or {}
    comps = comps or {}
    m3    = m3    or {}

    def _f(x):
        try:
            v = float(x)
            return None if v != v else v
        except Exception:
            return None

    def _score(val, thresholds):
        if val is None:
            return 0
        for thresh, pts in thresholds:
            if val >= thresh:
                return pts
        return 0

    details = {}

    # ── 1. Business Quality (25 %) ────────────────────────────────────────────
    bq = []
    roic = _f(stock.get("roic"))
    bq.append(("ROIC",            _score(roic, [(25,10),(20,9),(15,8),(10,7),(5,4),(0,2)]), 10,
               f"{roic:.1f}%" if roic is not None else "N/A"))
    gm = _f(stock.get("gross_margin"))
    bq.append(("Gross Margin",    _score(gm,   [(60,10),(40,8),(25,6),(15,4),(0,2)]),       10,
               f"{gm:.1f}%" if gm is not None else "N/A"))
    om = _f(stock.get("op_margin"))
    bq.append(("Operating Margin",_score(om,   [(25,10),(15,8),(10,6),(5,4),(0,2)]),        10,
               f"{om:.1f}%" if om is not None else "N/A"))
    fcfm = _f(stock.get("fcf_margin"))
    bq.append(("FCF Margin",      _score(fcfm, [(20,10),(12,8),(7,6),(3,4),(0,2)]),         10,
               f"{fcfm:.1f}%" if fcfm is not None else "N/A"))
    lng = _f(stock.get("longevity_score")) or 0
    bq.append(("Data Longevity",  _score(lng,  [(5,10),(4,8),(3,6),(2,4),(1,2)]),           10,
               f"{int(lng)}/5"))
    bq_raw = sum(x[1] for x in bq) / len(bq) * 10
    details["Business Quality"] = {"weight": 25, "score": round(bq_raw), "items": bq}

    # ── 2. Growth Track Record (20 %) ─────────────────────────────────────────
    gr = []
    for label, key in [("EPS CAGR 5Y","eps_cagr_5y"),("Rev CAGR 5Y","rev_cagr_5y"),
                        ("BVPS CAGR 5Y","bvps_cagr_5y"),("FCF CAGR 5Y","fcf_cagr_5y")]:
        val = _f(stock.get(key))
        gr.append((label, _score(val, [(20,10),(15,9),(10,8),(7,6),(4,4),(0,2)]), 10,
                   f"{val:.1f}%" if val is not None else "N/A"))
    consistent = sum(
        1 for k3, k5 in [("eps_cagr_3y","eps_cagr_5y"),("rev_cagr_3y","rev_cagr_5y"),
                          ("bvps_cagr_3y","bvps_cagr_5y"),("fcf_cagr_3y","fcf_cagr_5y")]
        if (_f(stock.get(k3)) or -1) >= 10 and (_f(stock.get(k5)) or -1) >= 10
    )
    gr.append(("Consistency 3Y+5Y", _score(consistent, [(4,10),(3,8),(2,6),(1,4),(0,0)]), 10,
               f"{consistent}/4"))
    r1p = stock.get("rule1_passes") or 0
    gr.append(("Rule #1 Passes",  _score(r1p, [(5,10),(4,9),(3,7),(2,5),(1,3),(0,0)]), 10,
               f"{r1p}/5"))
    gr_raw = sum(x[1] for x in gr) / len(gr) * 10
    details["Growth Track Record"] = {"weight": 20, "score": round(gr_raw), "items": gr}

    # ── 3. Financial Health (15 %) ────────────────────────────────────────────
    fh = []
    de = _f(stock.get("d_to_e"))
    de_s = (10 if de is not None and de < 0.3 else 8 if de is not None and de < 0.7
            else 6 if de is not None and de < 1.2 else 4 if de is not None and de < 2.0
            else 1 if de is not None else 0)
    fh.append(("Debt / Equity",      de_s, 10, f"{de:.2f}x" if de is not None else "N/A"))
    ic = _f(stock.get("int_cov"))
    fh.append(("Interest Coverage",  _score(ic, [(15,10),(8,8),(5,6),(3,4),(1,2)]), 10,
               f"{ic:.1f}x" if ic is not None else "N/A"))
    cr = _f(stock.get("curr_ratio"))
    fh.append(("Current Ratio",      _score(cr, [(3,10),(2,9),(1.5,7),(1.0,5),(0.8,3)]), 10,
               f"{cr:.2f}" if cr is not None else "N/A"))
    deb = _f(stock.get("d_to_ebitda"))
    deb_s = (10 if deb is not None and deb < 1 else 8 if deb is not None and deb < 2
             else 6 if deb is not None and deb < 3 else 3 if deb is not None and deb < 5
             else 1 if deb is not None else 5)
    fh.append(("Debt / EBITDA",      deb_s, 10, f"{deb:.1f}x" if deb is not None else "N/A"))
    fh_raw = sum(x[1] for x in fh) / len(fh) * 10
    details["Financial Health"] = {"weight": 15, "score": round(fh_raw), "items": fh}

    # ── 4. Valuation (20 %) ───────────────────────────────────────────────────
    val_items = []
    # Pull upsides from either the merged stock record or the model dicts
    dcf_up   = _f(dcf.get("dcf_upside_pct")   or stock.get("dcf_upside_pct"))
    comps_up = _f(comps.get("comps_upside_pct") or stock.get("comps_upside_pct"))
    m3_up    = _f(m3.get("m3_upside_pct")      or stock.get("m3_upside_pct"))
    valid_ups = [u for u in [dcf_up, comps_up, m3_up] if u is not None]
    avg_up    = sum(valid_ups) / len(valid_ups) if valid_ups else None
    for label, val in [("DCF Upside", dcf_up),
                        ("Comps Upside", comps_up),
                        ("EPV/Graham Upside", m3_up)]:
        val_items.append((label, _score(val, [(50,10),(30,9),(15,7),(0,5),(-15,3),(-30,1)]), 10,
                          f"{val:.1f}%" if val is not None else "N/A"))
    mos_price = _f(dcf.get("dcf_mos_price") or stock.get("dcf_mos_price"))
    price_now = _f(stock.get("price"))
    below_mos = bool(mos_price and price_now and price_now <= mos_price)
    val_items.append(("Below MOS", 10 if below_mos else 0, 10,
                      "✅ Yes" if below_mos else "❌ No"))
    green_models = sum(1 for u in valid_ups if u >= 0)
    val_items.append(("Models Agree", _score(green_models, [(3,10),(2,7),(1,4),(0,0)]), 10,
                      f"{green_models}/3"))
    val_raw = sum(x[1] for x in val_items) / len(val_items) * 10
    details["Valuation"] = {"weight": 20, "score": round(val_raw), "items": val_items}

    # ── 5. Earnings Quality (10 %) ────────────────────────────────────────────
    eq = []
    eps_ttm = _f(stock.get("eps_ttm"))
    eq.append(("EPS Positive", 10 if (eps_ttm and eps_ttm > 0) else 0, 10,
               f"${eps_ttm:.2f}" if eps_ttm is not None else "N/A"))
    fcf_b = _f(stock.get("fcf"))
    ni_b  = _f(stock.get("net_income"))
    if fcf_b is not None and ni_b and ni_b != 0:
        r = fcf_b / ni_b
        eq.append(("FCF / Net Income", _score(r, [(1.2,10),(0.9,8),(0.7,6),(0.5,4),(0,2)]), 10,
                   f"{r:.2f}x"))
    else:
        eq.append(("FCF / Net Income", 0, 10, "N/A"))
    peg = _f(stock.get("peg"))
    peg_s = (10 if peg and peg > 0 and peg < 0.8 else 8 if peg and peg < 1.2
             else 5 if peg and peg < 2.0 else 2 if peg and peg > 0 else 0)
    eq.append(("PEG Ratio", peg_s, 10, f"{peg:.2f}" if (peg and peg > 0) else "N/A"))
    roe = _f(stock.get("roe"))
    eq.append(("ROE", _score(roe, [(30,10),(20,8),(15,7),(10,5),(0,2)]), 10,
               f"{roe:.1f}%" if roe is not None else "N/A"))
    eq_raw = sum(x[1] for x in eq) / len(eq) * 10
    details["Earnings Quality"] = {"weight": 10, "score": round(eq_raw), "items": eq}

    # ── 6. Momentum & Technicals (5 %) ───────────────────────────────────────
    mo = []
    rsi = _f(stock.get("rsi"))
    rsi_s = (8 if rsi and 40 <= rsi <= 60 else 10 if rsi and 30 <= rsi < 40
             else 6 if rsi and 60 < rsi <= 70 else 3 if rsi and rsi > 70
             else 4 if rsi else 0)
    mo.append(("RSI", rsi_s, 10, f"{rsi:.1f}" if rsi else "N/A"))
    mar = _f(stock.get("ma_ratio"))
    ma_s = (10 if mar and mar > 1.05 else 7 if mar and mar > 1.0
            else 4 if mar and mar > 0.95 else 2 if mar else 0)
    mo.append(("MA50/MA200", ma_s, 10, f"{mar:.3f}" if mar else "N/A"))
    r1y = _f(stock.get("ret_1y"))
    mo.append(("1Y Return", _score(r1y, [(30,10),(15,8),(0,6),(-10,4),(-25,2)]), 10,
               f"{r1y:.1f}%" if r1y is not None else "N/A"))
    mo_raw = sum(x[1] for x in mo) / len(mo) * 10
    details["Momentum & Technicals"] = {"weight": 5, "score": round(mo_raw), "items": mo}

    # ── 7. Analyst Consensus (5 %) ────────────────────────────────────────────
    an = []
    am = _f(stock.get("analyst_mean"))
    pn = _f(stock.get("price"))
    if am and pn and pn > 0:
        an_up = (am - pn) / pn * 100
        an.append(("Analyst Upside", _score(an_up, [(30,10),(15,8),(5,6),(0,4),(-10,2)]), 10,
                   f"{an_up:.1f}%"))
    else:
        an.append(("Analyst Upside", 0, 10, "N/A"))
    rec_key = (stock.get("analyst_rec") or "").upper()
    rec_map = {"STRONG_BUY":10,"STRONGBUY":10,"BUY":8,"OUTPERFORM":8,
               "OVERWEIGHT":8,"HOLD":5,"NEUTRAL":5,"UNDERPERFORM":2,"SELL":1,"STRONG_SELL":0}
    an.append(("Analyst Rec", rec_map.get(rec_key.replace(" ","_"), 0) if rec_key else 0, 10,
               rec_key or "N/A"))
    n_an = _f(stock.get("num_analysts")) or 0
    an.append(("# Analysts", _score(n_an, [(20,10),(10,8),(5,6),(2,4),(1,2)]), 10,
               str(int(n_an)) if n_an else "N/A"))
    an_raw = sum(x[1] for x in an) / len(an) * 10
    details["Analyst Consensus"] = {"weight": 5, "score": round(an_raw), "items": an}

    # ── Composite ─────────────────────────────────────────────────────────────
    composite = round(sum(
        details[c]["score"] * details[c]["weight"] / 100 for c in details
    ))
    if   composite >= 85: rec = ("⭐⭐⭐", "STRONG BUY",  "C6EFCE", "375623")
    elif composite >= 70: rec = ("⭐⭐",   "BUY",         "DDEBF7", "1F497D")
    elif composite >= 55: rec = ("⭐",     "WATCHLIST",   "FFEB9C", "7F6000")
    elif composite >= 40: rec = ("⚠️",     "HOLD",        "FCE4D6", "843C0C")
    elif composite >= 25: rec = ("⛔",     "CAUTION",     "FFB3B3", "9C0006")
    else:                 rec = ("🚫",     "AVOID",       "FF7070", "9C0006")

    return {
        "composite":  composite,
        "rec_stars":  rec[0],
        "rec_label":  rec[1],
        "rec_bg":     rec[2],
        "rec_fg":     rec[3],
        "categories": details,
        "avg_upside": round(avg_up, 1) if avg_up is not None else None,
    }
