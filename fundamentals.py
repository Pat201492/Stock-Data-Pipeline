"""
fundamentals.py — Fetch and compute all fundamental data
=========================================================
Reads universe.json for ticker order.
Batch-fetches via yf.Tickers (one API call per batch of 20).
Uses data_utils.py for robust field extraction with multiple
fallback names — handles yfinance inconsistencies across versions.
Assigns a data_quality score (0-100) to every stock.
Saves after every batch — safe to interrupt and resume.

Persistent cache: fundamentals_cache.json
Output:           fundamentals.json

Run:      python3 fundamentals.py
Schedule: 0 2 * * 1-5   (weeknights 2am)
"""

import json, os, time, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

import yfinance as yf
from data_utils import (
    sf, fmt, pct, ratio, cagr,
    get_row, first_valid, second_valid, series_values, series_cagr,
    extract_field, data_quality_score,
    load_cache, save_cache, run_batches,
)

DATA_DIR      = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
UNIVERSE_FILE = os.path.join(DATA_DIR, "universe.json")
CACHE_FILE    = os.path.join(DATA_DIR, "fundamentals_cache.json")
OUTPUT_FILE   = os.path.join(DATA_DIR, "fundamentals.json")
BATCH_SIZE    = 20
SLEEP_SEC     = 3
MAX_RETRIES   = 2
STALE_DAYS    = 7   # re-fetch if DB record older than this


# ── Shared series builder ─────────────────────────────────────────────────────
# FCF, BVPS, and ROIC series all iterate statement columns in the same way.
# This helper centralises that loop to avoid three near-identical blocks.

def _build_col_series(stmts, extract_fn, max_cols=6):
    """
    Iterate statement columns (newest first), call extract_fn(stmt, col) for
    each, collect non-None results.  Tries annual first, then quarterly.
    Returns the first non-empty series found, or [].
    """
    for stmt in stmts:
        if stmt is None or stmt.empty:
            continue
        vals = []
        for col in list(stmt.columns)[:max_cols]:
            try:
                v = extract_fn(stmt, col)
                if v is not None:
                    vals.append(v)
            except Exception:
                continue
        if vals:
            return vals
    return []


# ── Earnings helpers ──────────────────────────────────────────────────────────

def _next_earnings_date(info):
    """Return next earnings date as YYYY-MM-DD string, or empty string."""
    try:
        # yfinance returns a list of timestamps [start, end] or just a timestamp
        ed = info.get("earningsDate") or info.get("earningsTimestampStart")
        if ed is None:
            return ""
        # Could be a list
        if isinstance(ed, (list, tuple)):
            ed = ed[0]
        # Could be a unix timestamp int
        if isinstance(ed, (int, float)):
            from datetime import timezone
            return datetime.fromtimestamp(ed, tz=timezone.utc).strftime("%Y-%m-%d")
        # Could already be a string
        return str(ed)[:10]
    except Exception:
        return ""

def _eps_surprise(edf):
    """
    Return the most-recent reported EPS surprise % as a float, or None.

    yfinance's `.info` dict has no surprise field (the old "earningsSurprisePct"
    key never existed, so this always returned None). The real data lives in
    `Ticker.get_earnings_dates()`, a DataFrame indexed by earnings date (newest
    first) with an "EPS Estimate" / "Reported EPS" / "Surprise(%)" column set.
    We take the latest row that has actually been reported.
    """
    if edf is None:
        return None
    try:
        col = None
        for c in edf.columns:
            if str(c).lower().startswith("surprise"):
                col = c
                break
        if col is None:
            return None
        # Rows are newest-first; first non-NaN surprise is the last reported quarter.
        for val in edf[col].tolist():
            v = sf(val)
            if v is not None:
                return fmt(v)
        return None
    except Exception:
        return None


def _next_earnings_from_df(edf):
    """Next (unreported) earnings date as YYYY-MM-DD, or '' — fallback when
    info has no earningsDate. Earliest row whose Reported EPS is still NaN."""
    if edf is None:
        return ""
    try:
        rep_col = None
        for c in edf.columns:
            if "reported" in str(c).lower():
                rep_col = c
                break
        # Index sorted newest-first; iterate oldest-first to find the next unreported.
        future = []
        for idx, row in edf.iterrows():
            reported = sf(row[rep_col]) if rep_col is not None else None
            if reported is None:
                future.append(idx)
        if future:
            return str(min(future))[:10]
        return ""
    except Exception:
        return ""


# ── Core extraction ───────────────────────────────────────────────────────────

def extract_fundamentals(ticker, yft):
    """
    Extract all financial data from a yfinance Ticker object.
    Returns a dict — every field either has a value or is explicitly None.
    Missing = None, never a missing key.
    """

    # Fetch all statements (each wrapped separately so one failure
    # doesn't poison the others)
    info = {}
    try: info = yft.info or {}
    except Exception: pass

    fin, cf, bs = None, None, None
    try: fin = yft.financials
    except Exception: pass
    try: cf  = yft.cashflow
    except Exception: pass
    try: bs  = yft.balance_sheet
    except Exception: pass

    # Quarterly as fallback for missing annual data
    fin_q, cf_q, bs_q = None, None, None
    try: fin_q = yft.quarterly_financials
    except Exception: pass
    try: cf_q  = yft.quarterly_cashflow
    except Exception: pass
    try: bs_q  = yft.quarterly_balance_sheet
    except Exception: pass

    # Earnings dates DataFrame — source for EPS surprise %, and a fallback for
    # the next earnings date when info lacks one.
    edf = None
    try:
        edf = yft.get_earnings_dates(limit=12)
    except Exception:
        try: edf = yft.earnings_dates
        except Exception: edf = None

    def annual_or_quarterly(field, annual_df, quarterly_df):
        """Try annual first; fall back to quarterly."""
        s = extract_field(annual_df, field)
        if first_valid(s) is not None:
            return s
        return extract_field(quarterly_df, field)

    # ── Income statement ──────────────────────────────────────────────────────
    rev_s    = annual_or_quarterly("revenue",          fin, fin_q)
    gp_s     = annual_or_quarterly("gross_profit",     fin, fin_q)
    op_s     = annual_or_quarterly("operating_income", fin, fin_q)
    ni_s     = annual_or_quarterly("net_income",       fin, fin_q)
    ebitda_s = annual_or_quarterly("ebitda",           fin, fin_q)
    int_s    = annual_or_quarterly("interest_expense", fin, fin_q)
    tax_s    = annual_or_quarterly("tax_provision",    fin, fin_q)
    eps_s    = annual_or_quarterly("eps_diluted",      fin, fin_q)

    # ── Cash flow ─────────────────────────────────────────────────────────────
    ocf_s   = annual_or_quarterly("operating_cf",   cf, cf_q)
    capex_s = annual_or_quarterly("capex",          cf, cf_q)
    fcf_s   = annual_or_quarterly("free_cash_flow", cf, cf_q)

    # ── Balance sheet ─────────────────────────────────────────────────────────
    debt_s   = annual_or_quarterly("total_debt",          bs, bs_q)
    stdebt_s = annual_or_quarterly("short_term_debt",     bs, bs_q)
    cash_s   = annual_or_quarterly("cash",                bs, bs_q)
    eq_s     = annual_or_quarterly("equity",              bs, bs_q)
    ta_s     = annual_or_quarterly("total_assets",        bs, bs_q)
    ca_s     = annual_or_quarterly("current_assets",      bs, bs_q)
    cl_s     = annual_or_quarterly("current_liabilities", bs, bs_q)
    ic_s     = annual_or_quarterly("invested_capital",    bs, bs_q)

    # ── Raw scalar values ─────────────────────────────────────────────────────
    rev      = first_valid(rev_s)    or 0
    rev_prev = second_valid(rev_s)   or 0
    gp       = first_valid(gp_s)     or 0
    op       = first_valid(op_s)     or 0
    ni       = first_valid(ni_s)     or 0
    ebitda   = first_valid(ebitda_s) or 0
    int_exp  = abs(first_valid(int_s) or 0)
    ocf      = first_valid(ocf_s)    or 0
    capex    = abs(first_valid(capex_s) or 0)
    debt     = first_valid(debt_s)   or 0
    stdebt   = first_valid(stdebt_s) or 0
    cash     = first_valid(cash_s)   or 0
    equity   = first_valid(eq_s)     or 0
    ta       = first_valid(ta_s)     or 0
    ca       = first_valid(ca_s)     or 0
    cl       = first_valid(cl_s)     or 0
    ic       = first_valid(ic_s)     or 0

    shares = sf(info.get("sharesOutstanding") or info.get("impliedSharesOutstanding")) or 1

    # FCF: use reported if available, else OCF - CapEx
    reported_fcf = first_valid(fcf_s)
    fcf = reported_fcf if reported_fcf is not None else (ocf - capex)

    # Invested capital: use reported, else equity + debt - cash
    if not ic:
        ic = equity + debt - cash
    if not ic or ic <= 0:
        ic = equity + debt  # don't subtract cash if that makes it negative

    # ── Computed ratios ───────────────────────────────────────────────────────
    gross_margin = pct(gp, rev)
    op_margin    = pct(op, rev)
    net_margin   = pct(ni, rev)
    fcf_margin   = pct(fcf, rev)
    rev_growth   = pct(rev - rev_prev, abs(rev_prev)) if rev_prev != 0 else None
    roe          = pct(ni, equity)
    roa          = pct(ni, ta) if ta else None

    # ROIC = NOPAT / Invested Capital  (NOPAT = op_income × (1 - tax_rate))
    tax_rate = 0.21  # default
    if op and int_s is not None:
        ni_plus_int = ni + int_exp
        if ni_plus_int and (ni + int_exp) != 0:
            try: tax_rate = min(max(sf(first_valid(tax_s) or 0) / ni_plus_int, 0), 0.40)
            except Exception: pass
    nopat = op * (1 - tax_rate)
    roic  = pct(nopat, ic) if ic > 0 else None

    d_to_e      = ratio(debt, equity)
    d_to_ebitda = ratio(debt, ebitda) if ebitda > 0 else None
    curr_ratio  = ratio(ca, cl)  if cl  > 0 else None
    int_cov     = ratio(ebitda, int_exp) if int_exp > 0 else None
    net_debt    = debt - cash
    bvps        = fmt(equity / shares) if shares > 0 else None

    # ── Multi-year CAGRs for Big Five ────────────────────────────────────────
    # Compute 1Y / 3Y / 5Y / 10Y for Revenue and EPS from pandas series.
    # series_cagr handles the case where there aren't enough data points
    # (returns None rather than raising).
    rev_cagr_1y  = series_cagr(rev_s, 1)
    rev_cagr_3y  = series_cagr(rev_s, 3)
    rev_cagr_5y  = series_cagr(rev_s, 5)
    rev_cagr_10y = series_cagr(rev_s, 10)

    eps_cagr_1y  = series_cagr(eps_s, 1)
    eps_cagr_3y  = series_cagr(eps_s, 3)
    eps_cagr_5y  = series_cagr(eps_s, 5)
    eps_cagr_10y = series_cagr(eps_s, 10)

    # FCF CAGR — build from CF statement columns using shared helper
    fcf_series = _build_col_series(
        [cf, cf_q],
        lambda stmt, col: (
            lambda o, c: (o - c) if (o - c) != 0 else None
        )(
            sf(extract_field(stmt[[col]], "operating_cf").iloc[0]) or 0,
            abs(sf(extract_field(stmt[[col]], "capex").iloc[0]) or 0),
        )
    )
    def _series_cagr_raw(series, n):
        """
        CAGR over n periods from a plain list (newest-first).

        Special cases:
          • start == 0          → undefined, return None
          • start < 0, end < 0  → both negative; ratio is positive, CAGR is valid
          • start < 0, end > 0  → sign flip; CAGR is mathematically undefined
                                   (would require complex number), return None
          • start > 0, end < 0  → negative return; clamp to -100 % (total loss)
          • result is complex    → safety catch, return None
        """
        if not series or len(series) < n + 1:
            return None
        start, end = series[n], series[0]
        if start is None or end is None or start == 0:
            return None
        try:
            ratio = end / start
            # Negative ratio means a sign-flip occurred — CAGR is not meaningful
            if ratio < 0:
                return None
            result = (ratio ** (1.0 / n) - 1) * 100
            # Paranoia: reject complex, NaN, or Inf that somehow slipped through
            if isinstance(result, complex):
                return None
            if result != result or abs(result) > 1e9:   # NaN or Inf check
                return None
            return float(result)
        except Exception:
            return None

    fcf_cagr_1y  = _series_cagr_raw(fcf_series, 1)
    fcf_cagr_3y  = _series_cagr_raw(fcf_series, 3)
    fcf_cagr_5y  = _series_cagr_raw(fcf_series, 5)
    fcf_cagr_10y = _series_cagr_raw(fcf_series, 10)
    # Fallback: also try the classic logic for 5Y
    if fcf_cagr_5y is None and len(fcf_series) >= 2:
        n = min(len(fcf_series) - 1, 5)
        fcf_cagr_5y = cagr(fcf_series[n], fcf_series[0], n)

    # 3-year average FCF (raw dollars) — used by DCF as smoother base
    _fcf_valid      = [x for x in fcf_series if x is not None and x > 0]
    fcf_3yr_avg_raw = sum(_fcf_valid[:3]) / len(_fcf_valid[:3]) if _fcf_valid else None

    # BVPS CAGR — build from balance-sheet columns using same helper
    bvps_series = _build_col_series(
        [bs, bs_q],
        lambda stmt, col: (
            lambda eq: (eq / shares) if (eq and shares > 0) else None
        )(sf(extract_field(stmt[[col]], "equity").iloc[0]))
    )
    bvps_cagr_1y  = _series_cagr_raw(bvps_series, 1)
    bvps_cagr_3y  = _series_cagr_raw(bvps_series, 3)
    bvps_cagr_5y  = _series_cagr_raw(bvps_series, 5)
    bvps_cagr_10y = _series_cagr_raw(bvps_series, 10)
    if bvps_cagr_5y is None and len(bvps_series) >= 2:
        n = min(len(bvps_series) - 1, 5)
        bvps_cagr_5y = cagr(bvps_series[n], bvps_series[0], n)

    # ROIC trend — build from annual statements using same helper
    def _roic_for_col(stmt_pair, col):
        fin_s, bs_s = stmt_pair
        op_  = sf(extract_field(fin_s[[col]], "operating_income").iloc[0]) or 0
        eq_  = sf(extract_field(bs_s[[col]],  "equity").iloc[0]) or 0
        d_   = sf(extract_field(bs_s[[col]],  "total_debt").iloc[0]) or 0
        c_   = sf(extract_field(bs_s[[col]],  "cash").iloc[0]) or 0
        ic_  = max(eq_ + d_ - c_, eq_ + d_, 1)
        return op_ * (1 - 0.21) / ic_ * 100

    roic_series    = []
    roic_improving = None
    if fin is not None and bs is not None and not fin.empty and not bs.empty:
        for col in list(fin.columns)[:5]:
            try:
                roic_series.append(_roic_for_col((fin, bs), col))
            except Exception:
                continue
        if len(roic_series) >= 2:
            roic_improving = roic_series[0] > roic_series[-1]

    # ROIC doesn't compound like earnings — report avg ROIC over each period.
    def _roic_avg(series, n):
        if not series: return None
        vals = [v for v in series[:n] if v is not None]
        return sum(vals) / len(vals) if vals else None

    roic_avg_1y  = _roic_avg(roic_series, 1)
    roic_avg_3y  = _roic_avg(roic_series, 3)
    roic_avg_5y  = _roic_avg(roic_series, 5)
    roic_avg_10y = _roic_avg(roic_series, 10)

    # ── Rule #1 Big Five ──────────────────────────────────────────────────────
    def r1(val):
        if val is None: return "N/A"
        return "✅" if val >= 10 else "❌"

    r1_roic   = r1(roic)
    r1_eps    = r1(eps_cagr_5y)
    r1_sales  = r1(rev_cagr_5y)
    r1_equity = r1(bvps_cagr_5y)
    r1_fcf    = r1(fcf_cagr_5y)
    r1_passes = sum(1 for v in [r1_roic, r1_eps, r1_sales, r1_equity, r1_fcf] if v == "✅")

    # ── Longevity Score ───────────────────────────────────────────────────────
    # Full credit (1 pt each) if a metric has enough history for a 5Y CAGR.
    # EPS / Revenue use their pandas Series; BVPS + FCF use their built lists.
    # ROIC uses roic_series (needs 5 annual levels). Score: 0–5.
    rev_vals  = [sf(x) for x in (rev_s.values  if hasattr(rev_s,  "values") else [])]
    eps_vals  = [sf(x) for x in (eps_s.values  if hasattr(eps_s,  "values") else [])]
    rev_nvals = len([x for x in rev_vals if x is not None])
    eps_nvals = len([x for x in eps_vals if x is not None])

    longevity_score = (
        (1 if eps_nvals             >= 6 else 0) +   # EPS CAGR needs 6 pts
        (1 if rev_nvals             >= 6 else 0) +   # Revenue CAGR needs 6 pts
        (1 if len(bvps_series)      >= 6 else 0) +   # BVPS CAGR needs 6 pts
        (1 if len(fcf_series)       >= 6 else 0) +   # FCF CAGR needs 6 pts
        (1 if len(roic_series)      >= 5 else 0)     # ROIC trend needs 5 annual pts
    )
    longevity_rank = f"{longevity_score}/5"

    # ── Info fields ───────────────────────────────────────────────────────────
    price   = sf(info.get("currentPrice") or info.get("regularMarketPrice"))
    mkt_cap = sf(info.get("marketCap") or 0)
    ev      = sf(info.get("enterpriseValue") or 0)

    record = {
        # Identity
        "ticker":   ticker,
        "name":     info.get("longName") or info.get("shortName", ""),
        "sector":   info.get("sector",   "Unknown"),
        "industry": info.get("industry", "Unknown"),
        "country":  info.get("country",  "Unknown"),
        "exchange": info.get("exchange", ""),
        # Price / market data
        "price":        fmt(price),
        "prev_close":   fmt(info.get("previousClose")),
        "high52":       fmt(info.get("fiftyTwoWeekHigh")),
        "low52":        fmt(info.get("fiftyTwoWeekLow")),
        "mkt_cap":      fmt(mkt_cap / 1e9) if mkt_cap else None,
        "mkt_cap_raw":  mkt_cap,
        "ev":           fmt(ev / 1e9)      if ev else None,
        "ev_raw":       ev,
        # Valuation multiples
        "pe":           fmt(info.get("trailingPE")),
        "fwd_pe":       fmt(info.get("forwardPE")),
        "pb":           fmt(info.get("priceToBook")),
        "ps":           fmt(info.get("priceToSalesTrailing12Months")),
        "ev_ebitda":    fmt(info.get("enterpriseToEbitda")),
        "ev_revenue":   fmt(info.get("enterpriseToRevenue")),
        "peg":          fmt(info.get("pegRatio")),
        # Per share
        "eps_ttm":      fmt(info.get("trailingEps")),
        "eps_fwd":      fmt(info.get("forwardEps")),
        "bvps":         bvps,
        "div_yield":    fmt((info.get("dividendYield") or 0) * 100),
        "beta":         fmt(info.get("beta")),
        # Analyst
        "analyst_mean": fmt(info.get("targetMeanPrice")),
        "analyst_low":  fmt(info.get("targetLowPrice")),
        "analyst_high": fmt(info.get("targetHighPrice")),
        "analyst_rec":  (info.get("recommendationKey") or "").upper(),
        "num_analysts": info.get("numberOfAnalystOpinions"),
        # Earnings
        "earn_date":   _next_earnings_date(info) or _next_earnings_from_df(edf),
        "next_earnings": _next_earnings_date(info) or _next_earnings_from_df(edf),  # DB column alias for earn_date
        "eps_est":     fmt(info.get("forwardEps")),
        "eps_actual":  fmt(info.get("trailingEps")),
        "eps_surp":    _eps_surprise(edf),
        "eps_surprise": _eps_surprise(edf),  # DB column alias for eps_surp
        # Shares
        "shares": shares,
        # Income
        "rev_now":      fmt(rev / 1e9),
        "rev_prev":     fmt(rev_prev / 1e9),
        "rev_growth":   rev_growth,
        "gross_profit": fmt(gp / 1e9),
        "ebitda":       fmt(ebitda / 1e9),
        "op_income":    fmt(op / 1e9),
        "operating_income": fmt(op / 1e9),  # DB column alias for op_income
        "net_income":   fmt(ni / 1e9),
        "int_expense":  fmt(int_exp / 1e9),
        # Cash flow
        "ocf":      fmt(ocf / 1e9),
        "operating_cf": fmt(ocf / 1e9),  # DB column alias for ocf
        "capex":    fmt(capex / 1e9),
        "fcf":      fmt(fcf / 1e9),
        "fcf_raw":  fcf,  # raw float for DCF model
        # Balance sheet
        "total_debt":   fmt(debt / 1e9),
        "net_debt":     fmt(net_debt / 1e9),
        "cash":         fmt(cash / 1e9),
        "equity_raw":   equity,
        "equity":       fmt(equity / 1e9),  # DB column ($B, like other balance-sheet cols)
        "total_assets": fmt(ta / 1e9),
        "inv_cap":      fmt(ic / 1e9),
        # Margins & ratios
        "gross_margin": gross_margin,
        "op_margin":    op_margin,
        "net_margin":   net_margin,
        "fcf_margin":   fcf_margin,
        "roe":          roe,
        "roa":          roa,
        "roic":         roic,
        "d_to_e":       d_to_e,
        "d_to_ebitda":  d_to_ebitda,
        "curr_ratio":   curr_ratio,
        "int_cov":      int_cov,
        # CAGRs — multi-year (1Y / 3Y / 5Y / 10Y)
        "rev_cagr_1y":    rev_cagr_1y,   "rev_cagr_3y":    rev_cagr_3y,
        "rev_cagr_5y":    rev_cagr_5y,   "rev_cagr_10y":   rev_cagr_10y,
        "eps_cagr_1y":    eps_cagr_1y,   "eps_cagr_3y":    eps_cagr_3y,
        "eps_cagr_5y":    eps_cagr_5y,   "eps_cagr_10y":   eps_cagr_10y,
        "fcf_cagr_1y":    fcf_cagr_1y,   "fcf_cagr_3y":    fcf_cagr_3y,
        "fcf_cagr_5y":    fcf_cagr_5y,   "fcf_cagr_10y":   fcf_cagr_10y,
        "bvps_cagr_1y":   bvps_cagr_1y,  "bvps_cagr_3y":   bvps_cagr_3y,
        "bvps_cagr_5y":   bvps_cagr_5y,  "bvps_cagr_10y":  bvps_cagr_10y,
        "roic_avg_1y":    roic_avg_1y,   "roic_avg_3y":    roic_avg_3y,
        "roic_avg_5y":    roic_avg_5y,   "roic_avg_10y":   roic_avg_10y,
        "fcf_3yr_avg_raw": fcf_3yr_avg_raw,
        "roic_improving":  roic_improving,
        # Rule #1
        "rule1_roic":   r1_roic,
        "rule1_eps":    r1_eps,
        "rule1_sales":  r1_sales,
        "rule1_equity": r1_equity,
        "rule1_fcf":    r1_fcf,
        "rule1_passes": r1_passes,
        "longevity_score": longevity_score,
        "longevity_rank":  longevity_rank,
    }

    record["data_quality"] = data_quality_score(record)
    return record


# ── Batch fetch ───────────────────────────────────────────────────────────────

def fetch_batch(tickers):
    """Fetch one batch of tickers. Returns (results_dict, failed_list)."""
    results, failed = {}, []
    try:
        group = yf.Tickers(" ".join(tickers))
        for ticker in tickers:
            try:
                rec = extract_fundamentals(ticker, group.tickers[ticker])
                results[ticker] = rec
            except Exception:
                failed.append(ticker)
    except Exception:
        failed = list(tickers)
    return results, failed


# ── Main ──────────────────────────────────────────────────────────────────────

def _fresh_tickers(tickers):
    """Return tickers whose DB row is recent AND actually populated. A recent but
    sparse row (e.g. the bulk yfinance fetch failed for it) is NOT treated as
    fresh, so it gets retried next run instead of being skipped for STALE_DAYS."""
    try:
        from database import SessionLocal, Fundamentals
        from datetime import datetime as _dt
        db = SessionLocal()
        rows = (
            db.query(Fundamentals.ticker, Fundamentals.last_updated, Fundamentals.data_quality)
            .filter(Fundamentals.ticker.in_(tickers))
            .all()
        )
        db.close()
        fresh = set()
        for ticker, updated, dq in rows:
            if updated and (_dt.utcnow() - updated).days < STALE_DAYS and (dq or 0) >= 50:
                fresh.add(ticker)
        return fresh
    except Exception:
        return set()


def main():
    print("=" * 55)
    print("  fundamentals.py")
    print("=" * 55)

    # Load universe — prefer DB, fall back to JSON
    try:
        from database import SessionLocal, Stock, init_db
        init_db()
        db = SessionLocal()
        rows = db.query(Stock.ticker, Stock.name, Stock.sector, Stock.industry,
                        Stock.cap_size, Stock.rank).order_by(Stock.rank).all()
        db.close()
        if rows:
            uni_map = {r.ticker: {"ticker": r.ticker, "name": r.name,
                                  "sector": r.sector, "industry": r.industry,
                                  "cap_size": r.cap_size, "rank": r.rank}
                       for r in rows}
            tickers = [r.ticker for r in rows]
            print(f"Universe: {len(tickers)} tickers (from DB)")
        else:
            raise ValueError("DB empty")
    except Exception:
        try:
            with open(UNIVERSE_FILE) as f:
                uni = json.load(f)
            uni_map = {s["ticker"]: s for s in uni["stocks"]}
            tickers = [s["ticker"] for s in uni["stocks"]]
            print(f"Universe: {len(tickers)} tickers (from JSON)")
        except FileNotFoundError:
            print(f"❌ {UNIVERSE_FILE} not found — run universe.py first")
            return

    # Skip tickers that are fresh in the DB
    fresh = _fresh_tickers(tickers)
    stale = [t for t in tickers if t not in fresh]
    print(f"Fresh (skip): {len(fresh)}  |  Stale (fetch): {len(stale)}")

    cache = load_cache(CACHE_FILE)
    print(f"JSON cache: {len(cache)} entries")

    # Drop empty cached entries (no revenue) for the tickers we're about to fetch,
    # so a prior failed/rate-limited bulk run doesn't lock in blank fundamentals —
    # run_batches would otherwise treat them as "already cached" and skip the fetch.
    pruned = 0
    for t in stale:
        e = cache.get(t)
        if e is not None and not e.get("rev_now"):
            cache.pop(t, None); pruned += 1
    if pruned:
        print(f"Pruned {pruned} empty cache entries — will re-fetch live")

    # run_batches handles the main loop + retry pass — no duplicate loop needed
    cache = run_batches(
        items=stale,
        fetch_fn=fetch_batch,
        cache=cache,
        cache_path=CACHE_FILE,
        batch_size=BATCH_SIZE,
        sleep_sec=SLEEP_SEC,
        max_retries=MAX_RETRIES,
    )

    # Build final output — merge rank + cap_size from universe, deduplicate
    seen, final = set(), []
    for ticker in tickers:
        if ticker in seen:
            continue
        seen.add(ticker)
        s = cache.get(ticker)
        if not s:
            continue
        meta = uni_map.get(ticker, {})
        s["cap_size"] = meta.get("cap_size", "Unknown")
        s["rank"]     = meta.get("rank", 9999)
        if s.get("sector", "Unknown") == "Unknown" and meta.get("sector", "Unknown") != "Unknown":
            s["sector"] = meta["sector"]
        if not s.get("name") and meta.get("name"):
            s["name"] = meta["name"]
        final.append(s)

    # Quality summary
    quality_buckets = {"High (≥70%)": 0, "Medium (40-69%)": 0, "Low (<40%)": 0}
    for s in final:
        dq = s.get("data_quality", 0)
        if   dq >= 70: quality_buckets["High (≥70%)"]    += 1
        elif dq >= 40: quality_buckets["Medium (40-69%)"] += 1
        else:          quality_buckets["Low (<40%)"]       += 1

    out = {
        "generated":       datetime.now().isoformat(),
        "total":           len(final),
        "quality_summary": quality_buckets,
        "stocks":          final,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    _save_to_db(final)

    print(f"\n✅ Fundamentals: {len(final)} stocks → {OUTPUT_FILE}")
    print("\nData Quality:")
    for k, v in quality_buckets.items():
        print(f"  {k}: {v}")


def _save_to_db(stocks):
    _FUND_FIELDS = [
        "name","sector","industry","cap_size","rank","price",
        "rev_now","gross_profit","operating_income","net_income","ebitda",
        "eps_ttm","eps_fwd","operating_cf","capex","fcf","fcf_3yr_avg_raw",
        "total_debt","cash","equity","shares","bvps","net_debt","mkt_cap_raw",
        "pe","fwd_pe","ev_ebitda","ps","pb","peg",
        "d_to_e","d_to_ebitda","int_cov","curr_ratio",
        "roic","roe","roa","gross_margin","op_margin","net_margin","fcf_margin",
        "rev_cagr_1y","rev_cagr_3y","rev_cagr_5y","rev_cagr_10y",
        "eps_cagr_1y","eps_cagr_3y","eps_cagr_5y","eps_cagr_10y",
        "fcf_cagr_1y","fcf_cagr_3y","fcf_cagr_5y","fcf_cagr_10y",
        "bvps_cagr_1y","bvps_cagr_3y","bvps_cagr_5y","bvps_cagr_10y",
        "roic_avg_1y","roic_avg_3y","roic_avg_5y","roic_avg_10y","roic_improving",
        "rule1_roic","rule1_eps","rule1_sales","rule1_equity","rule1_fcf",
        "rule1_passes","longevity_score","longevity_rank",
        "data_quality","next_earnings","eps_surprise",
        "analyst_mean","analyst_rec","num_analysts","ret_1y","rsi","ma_ratio",
    ]
    try:
        from database import SessionLocal, Fundamentals, init_db, upsert
        from datetime import datetime as _dt
        init_db()
        db = SessionLocal()
        for s in stocks:
            row = {"ticker": s["ticker"], "last_updated": _dt.utcnow()}
            for f in _FUND_FIELDS:
                if f in s:
                    row[f] = s[f]
            upsert(db, Fundamentals, row)
        db.commit()
        db.close()
        print(f"  ✅ Saved {len(stocks)} fundamentals to database")
    except Exception as e:
        print(f"  ⚠️  DB save failed: {e}")


if __name__ == "__main__":
    main()
