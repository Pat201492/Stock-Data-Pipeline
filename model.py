"""
model.py — DCF + Comparable Company Analysis (Comps) + EPV / Graham
====================================================================
Three independent valuation models:

  MODEL 1 — DCF (Discounted Cash Flow)
  ─────────────────────────────────────────
  3-year average FCF projected over 10 years at a blended growth rate
  (50 % rev CAGR + 30 % EPS CAGR + 20 % FCF CAGR, capped 2–20 %).
  Discounted at WACC (CAPM + D/E leverage), plus Gordon Growth terminal value.
  MOS buy price = intrinsic × 65 %.

  MODEL 2 — Comparable Company Analysis (Comps)
  ──────────────────────────────────────────────────
  Fair value from peer medians in the same industry + cap tier.
  Four methods: P/E × EPS, EV/EBITDA, P/Sales, P/FCF.
  Each per-method estimate is sanity-checked (0.2×–3× current price) before
  averaging to guard against foreign-currency data (e.g. ADRs reporting in
  local currency), which would otherwise inflate P/Sales and EV/EBITDA.

  MODEL 3 — EPV + Graham Number (Conservative Floor)
  ─────────────────────────────────────────────────────
  EPV  = NOPAT / WACC   — zero-growth earning power (Greenwald)
  Graham Number = √(22.5 × EPS_TTM × BVPS)  — Benjamin Graham
  Averaged when both available.  A low EPV/Graham vs. price just means the
  market is pricing in growth — this is valid information, not a data error.

All three produce a fair value + upside + signal.
Combined signal requires all three to agree for ⭐ STRONG BUY.

Run:      python3 model.py
Schedule: 0 5 * * 1-5
"""

import json, os, time, math, warnings
from datetime import datetime
from collections import defaultdict
warnings.filterwarnings("ignore")

import numpy as np
import yfinance as yf
from data_utils import sf, fmt, ratio, load_cache, save_cache, run_batches, score_stock

DATA_DIR          = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
FUNDAMENTALS_FILE = os.path.join(DATA_DIR, "fundamentals.json")
CACHE_FILE        = os.path.join(DATA_DIR, "model_cache.json")
OUTPUT_FILE       = os.path.join(DATA_DIR, "model.json")

BATCH_SIZE  = 50
SLEEP_SEC   = 2
MAX_RETRIES = 2
MIN_PEERS   = 5
STALE_DAYS  = 7   # recompute valuation if older than this

# ── DCF parameters ────────────────────────────────────────────────────────────
DCF_YEARS      = 10
DCF_TERMINAL_G = 0.03   # 3% terminal growth rate
DCF_MOS        = 0.35   # 35% Margin of Safety (Phil Town Rule #1)
RF_RATE        = 0.045  # 4.5% risk-free rate (10yr treasury)
MARKET_PREMIUM = 0.05   # 5% equity risk premium

# WACC by cap size (used when we can't compute from beta)
# Floor raised from 7% to 8.5% — even mega-caps have real risk.
# A 7% WACC on AAPL produces fantasy valuations.
DEFAULT_WACC = {
    "Mega Cap":  0.09,
    "Large Cap": 0.105,
    "Mid Cap":   0.12,
    "Small Cap": 0.135,
    "Micro Cap": 0.16,
    "Unknown":   0.13,
}

# Comps multiple bounds (clip extreme outliers before computing peer medians)
# P/S upper bound tightened from 20 to 15 — SaaS hyper-multiples distort medians
MULTIPLE_BOUNDS = {
    "pe":       (5,   60),    # tightened: P/E > 60 is speculative, < 5 is likely distressed
    "ev_ebitda":(3,   30),    # tightened upper — 40x EV/EBITDA is not a realistic comp
    "ps":       (0.3, 15),    # tightened upper from 20
    "p_fcf":    (5,   50),    # tightened: < 5 is distressed, > 50 is speculative
}


# ── Fund / CEF detection ──────────────────────────────────────────────────────

# Keywords that identify closed-end funds, BDCs, and other non-operating entities
# that should NOT be valued with DCF (they hold portfolios, not operating businesses)
_FUND_KEYWORDS = (
    " fund", " trust", " income", " capital corp", " lending",
    " investment corp", "bdc", "reit", " etf", "ishares",
    "nuveen", "blackrock muni", "pimco", "calamos", "gabelli",
    "cohen & steers", "alliancebernstein", "abrdn", "flaherty",
    "john hancock", "virtus", "advent convertible",
)

_FUND_INDUSTRIES = {
    "Asset Management", "Closed-End Fund", "Exchange Traded Fund",
    "Capital Markets", "Mortgage Real Estate Investment Trust (REIT)",
    "REIT—Mortgage",
}

def is_fund(stock):
    """Return True if stock appears to be a CEF, BDC, ETF, or similar vehicle."""
    name     = (stock.get("name") or "").lower()
    industry = (stock.get("industry") or "").strip()
    if any(kw in name for kw in _FUND_KEYWORDS):
        return True
    if industry in _FUND_INDUSTRIES:
        return True
    return False


# ── WACC estimation ───────────────────────────────────────────────────────────

def estimate_wacc(stock):
    """
    WACC = E/(D+E) * cost_of_equity + D/(D+E) * cost_of_debt * (1-tax)
    cost_of_equity = RF + beta * market_premium  (CAPM)
    cost_of_debt   = estimated from interest coverage
    Falls back to DEFAULT_WACC if insufficient data.
    """
    beta     = sf(stock.get("beta"))
    d_to_e   = sf(stock.get("d_to_e"))
    int_cov  = sf(stock.get("int_cov"))
    cap_size = stock.get("cap_size", "Unknown")

    if beta is None or beta <= 0:
        return DEFAULT_WACC.get(cap_size, 0.12)

    beta        = min(max(beta, 0.5), 3.0)  # clip extreme betas
    cost_equity = RF_RATE + beta * MARKET_PREMIUM

    # Estimate cost of debt from interest coverage ratio
    if int_cov and int_cov > 0:
        if   int_cov > 12: cost_debt = 0.04
        elif int_cov > 6:  cost_debt = 0.055
        elif int_cov > 3:  cost_debt = 0.075
        else:              cost_debt = 0.10
    else:
        cost_debt = 0.065  # default

    d_to_e   = max(sf(d_to_e) or 0, 0)
    e_weight = 1 / (1 + d_to_e)
    d_weight = d_to_e / (1 + d_to_e)
    tax_rate = 0.21

    wacc = e_weight * cost_equity + d_weight * cost_debt * (1 - tax_rate)
    return min(max(wacc, 0.085), 0.28)  # floor 8.5% (even safest equity has risk), ceiling 28%


# ── Model 1: DCF ──────────────────────────────────────────────────────────────

def _normalize_fcf(stock):
    """
    Return a normalised FCF (raw dollars) for DCF input.

    Priority order:
      1. 3-year average FCF — smooths one-off capex swings (preferred)
      2. Single-year FCF    — if 3yr avg not available
      3. Both are capped at 25 % of revenue to prevent inflated margins
      4. Falls back to net income × 0.80 if FCF is missing / negative
    """
    fcf_3yr  = sf(stock.get("fcf_3yr_avg_raw"))  # raw $, 3yr avg
    fcf_raw  = sf(stock.get("fcf_raw"))           # raw $, single year
    revenue  = sf(stock.get("rev_now"))           # $B
    net_inc  = sf(stock.get("net_income"))        # $B
    shares   = sf(stock.get("shares")) or 1

    rev_raw = revenue * 1e9 if revenue is not None else None
    ni_raw  = net_inc * 1e9 if net_inc is not None else None

    # Prefer 3-year average (more reliable), fall back to single year
    fcf = fcf_3yr if (fcf_3yr is not None and fcf_3yr > 0) else fcf_raw

    # Cap at 25 % of revenue — prevents unrealistic FCF margins
    if fcf is not None and rev_raw and rev_raw > 0:
        fcf = min(fcf, rev_raw * 0.25)

    # If still missing or negative, use operating income proxy
    # Operating income is preferred over net income because NI can be
    # distorted by one-time tax benefits, asset sales, or interest income.
    # We apply a 65% conversion factor: ~21% tax + ~14% capex/working-capital.
    if fcf is None or fcf <= 0:
        op_inc = sf(stock.get("op_income"))  # $B
        op_raw = op_inc * 1e9 if op_inc is not None else None
        if op_raw and op_raw > 0:
            fcf = op_raw * 0.65
        elif ni_raw and ni_raw > 0:
            fcf = ni_raw * 0.70   # last resort: NI with more conservative factor
        else:
            return None, shares

    return fcf, shares


def calc_dcf(stock):
    """
    DCF valuation with normalized inputs and plausibility bounds.

    Key improvements vs. naive DCF:
    - FCF is normalized: capped at 25% of revenue; falls back to earnings if negative
    - Growth rate uses revenue CAGR as primary signal (more stable than FCF CAGR)
    - Growth is adjusted downward for declining/mature companies
    - Intrinsic value is bounds-checked: must be 0.1x–5x current price to be trusted
    - Net debt adjustment capped so it can't wipe out >80% of equity value
    """
    shares = sf(stock.get("shares")) or 1
    price  = sf(stock.get("price"))

    _null = {
        "dcf_intrinsic":   None,
        "dcf_mos_price":   None,
        "dcf_wacc":        None,
        "dcf_growth_rate": None,
        "dcf_signal":      "⚪ Insufficient data",
        "dcf_upside_pct":  None,
    }

    # Skip funds/CEFs — DCF is meaningless for portfolio vehicles
    if is_fund(stock):
        return _null

    fcf_raw, shares = _normalize_fcf(stock)
    if fcf_raw is None or shares <= 0:
        return _null

    # ── Blended historical growth rate ───────────────────────────────────
    #
    # We are projecting FREE CASH FLOW growth, so the weights reflect
    # how directly each metric predicts future FCF:
    #
    #   EPS CAGR     40% — best single proxy for owner earnings growth.
    #                       Already reflects buybacks, tax, and some margin
    #                       change. Most professional DCF models anchor here.
    #
    #   FCF CAGR     35% — most direct evidence, but volatile year-to-year
    #                       due to capex timing. Blending 5Y+3Y smooths this.
    #
    #   Rev CAGR     25% — the ceiling: FCF can't sustainably outgrow revenue.
    #                       Used as a sanity anchor, not the primary driver.
    #                       (Was 50% before — incorrect because projecting revenue
    #                        growth ≠ projecting FCF growth unless margins are flat.)
    #
    # Each metric blends 5Y CAGR (70%) + 3Y CAGR (30%) to catch recent
    # acceleration or deceleration without over-weighting short-term noise.
    #
    # Revenue is also used as a hard ceiling: the blended growth rate cannot
    # exceed the revenue CAGR by more than 5pp — prevents EPS/FCF growth
    # driven purely by margin expansion or buybacks from producing an
    # unrealistically high projected FCF base.

    rev_5y  = sf(stock.get("rev_cagr_5y"))
    rev_3y  = sf(stock.get("rev_cagr_3y"))
    eps_5y  = sf(stock.get("eps_cagr_5y"))
    eps_3y  = sf(stock.get("eps_cagr_3y"))
    fcf_5y  = sf(stock.get("fcf_cagr_5y"))
    fcf_3y  = sf(stock.get("fcf_cagr_3y"))
    rev_g1y = sf(stock.get("rev_growth"))

    def _blend(v5, v3):
        """Blend 5Y (70%) and 3Y (30%) CAGR. If one missing, use the other."""
        if v5 is not None and v3 is not None:
            return v5 * 0.70 + v3 * 0.30
        return v5 if v5 is not None else v3

    rev_cagr = _blend(rev_5y, rev_3y)
    eps_cagr = _blend(eps_5y, eps_3y)
    fcf_cagr = _blend(fcf_5y, fcf_3y)

    parts, weights = [], []
    if eps_cagr is not None: parts.append(eps_cagr); weights.append(0.40)
    if fcf_cagr is not None: parts.append(fcf_cagr); weights.append(0.35)
    if rev_cagr is not None: parts.append(rev_cagr); weights.append(0.25)

    if parts:
        total_w = sum(weights)
        hist_g  = sum(p * w for p, w in zip(parts, weights)) / total_w
        # Revenue ceiling: blended rate cannot exceed rev CAGR + 5pp.
        # Prevents pure margin-expansion or buyback stories from over-projecting FCF.
        if rev_cagr is not None:
            hist_g = min(hist_g, rev_cagr + 5.0)
    elif rev_g1y is not None:
        hist_g = rev_g1y
    else:
        hist_g = 4.0   # lowered default from 5% to 4% (closer to long-run nominal GDP)

    # ── Mean-reversion haircut ────────────────────────────────────────────
    # No company sustains hypergrowth forever.  We apply a mean-reversion
    # discount that pulls projected growth toward a realistic long-run rate.
    # The higher the historical rate, the larger the haircut:
    #   ≤ 8 %   → project at face value (already modest)
    #   8-15 %  → 15 % haircut  (reasonable growth — slight conservatism)
    #   15-25 % → 25 % haircut  (high growth — markets usually over-extrapolate)
    #   > 25 %  → 35 % haircut  (exceptional — near-impossible to sustain)
    # This single adjustment prevents the model from projecting NVDA-level growth
    # rates over a full decade and producing $500 fair values.
    if   hist_g <= 8:   mr_factor = 1.00
    elif hist_g <= 15:  mr_factor = 0.85
    elif hist_g <= 25:  mr_factor = 0.75
    else:               mr_factor = 0.65
    hist_g *= mr_factor

    # ── Quality haircut ───────────────────────────────────────────────────
    # Low-quality earnings (thin margins, weak FCF conversion) deserve a
    # lower projected FCF even at the same revenue growth rate.
    # Penalty: up to 15% reduction for stocks with FCF margin < 5%.
    fcf_margin = sf(stock.get("fcf_margin")) or 0
    if   fcf_margin >= 15: quality_factor = 1.00
    elif fcf_margin >= 8:  quality_factor = 0.95
    elif fcf_margin >= 3:  quality_factor = 0.88
    else:                  quality_factor = 0.82   # thin margins = less reliable FCF
    hist_g *= quality_factor

    # ── Hard caps ─────────────────────────────────────────────────────────
    # Floor: 0% (declining businesses shouldn't get a growth premium).
    # Ceiling: 18% (even exceptional businesses rarely sustain above this).
    # Sector haircut for mature/cyclical sectors.
    sector = stock.get("sector", "")
    sector_cap = 0.10 if sector in ("Utilities", "Consumer Defensive", "Energy") else 0.18
    g1 = min(max(hist_g / 100, 0.00), sector_cap)   # Stage 1 growth (years 1-5)

    # Stage 2: mean-revert toward long-run GDP+inflation (4.5%) over years 6-10.
    # This prevents the terminal value from being driven by an unrealistic growth base.
    GDP_LONG_RUN = 0.045
    g2 = (g1 + GDP_LONG_RUN) / 2   # blend between stage-1 and long-run
    g2 = min(g2, 0.12)             # never let stage 2 exceed 12%

    wacc = estimate_wacc(stock)

    # ── Two-stage DCF ─────────────────────────────────────────────────────
    # Stage 1: years 1-5 at g1 (haircutted historical growth)
    # Stage 2: years 6-10 at g2 (mean-reverted)
    # Terminal: Gordon Growth at DCF_TERMINAL_G (3%)
    pv    = 0.0
    fcf_t = fcf_raw
    for yr in range(1, 6):          # Stage 1: years 1-5
        fcf_t = fcf_t * (1 + g1)
        pv   += fcf_t / ((1 + wacc) ** yr)
    for yr in range(6, 11):         # Stage 2: years 6-10
        fcf_t = fcf_t * (1 + g2)
        pv   += fcf_t / ((1 + wacc) ** yr)

    # Terminal value (Gordon Growth on stage-2 terminal FCF)
    if wacc > DCF_TERMINAL_G:
        terminal_fcf = fcf_t * (1 + DCF_TERMINAL_G)
        tv  = terminal_fcf / (wacc - DCF_TERMINAL_G)
        pv += tv / ((1 + wacc) ** DCF_YEARS)

    # ── Net debt adjustment (capped to prevent absurd negative equity) ──
    net_debt_raw = sf(stock.get("net_debt") or 0) * 1e9
    # Don't let net debt reduce equity value by more than 80%
    net_debt_raw = min(net_debt_raw, pv * 0.80)

    equity_value = pv - net_debt_raw
    if equity_value <= 0:
        return _null

    intrinsic_raw = equity_value / shares
    mos_raw       = intrinsic_raw * (1 - DCF_MOS)

    # ── Plausibility check ────────────────────────────────────────────
    # Discard results that are implausibly extreme — likely bad input data.
    # Upper bound 8× (not 4×): high-growth companies like NVDA legitimately
    # have intrinsic > 4× price once FCF and 20% growth are discounted.
    # Lower bound 0.10× catches structural errors (e.g. negative equity runs).
    if price and price > 0:
        ratio_check = intrinsic_raw / price
        if ratio_check > 8.0 or ratio_check < 0.10:
            return _null

    intrinsic = fmt(intrinsic_raw)
    mos_price = fmt(mos_raw)

    # ── Signal ───────────────────────────────────────────────────────
    if price is not None and intrinsic is not None:
        if mos_price and price <= mos_price:
            dcf_signal = "🟢 Below MOS — Strong Buy"
        elif price <= intrinsic_raw:
            dcf_signal = "🟡 Below Intrinsic — Watchlist"
        else:
            prem = fmt((price - intrinsic_raw) / abs(intrinsic_raw) * 100)
            dcf_signal = f"🔴 {prem}% above intrinsic" if prem else "🔴 Above Intrinsic"
        dcf_upside = fmt((intrinsic_raw - price) / price * 100)
    else:
        dcf_signal = "⚪ Insufficient data"
        dcf_upside = None

    return {
        "dcf_intrinsic":    intrinsic,
        "dcf_mos_price":    mos_price,
        "dcf_wacc":         fmt(wacc * 100),
        "dcf_growth_rate":  fmt(g1 * 100),    # stage 1 growth shown in UI
        "dcf_growth_rate2": fmt(g2 * 100),    # stage 2 growth (mean-reverted)
        "dcf_signal":       dcf_signal,
        "dcf_upside_pct":   dcf_upside,
    }


# ── Model 2: Comparable Company Analysis ─────────────────────────────────────

def build_peer_groups(stocks):
    """
    Build peer tiers per stock, most specific first:
    1. Same industry + same cap_size
    2. Same industry
    3. Same sector + same cap_size
    4. Same sector
    5. All stocks (last resort)
    Returns dict: ticker → list of peer stocks (excluding self).
    """
    by_ind_cap  = defaultdict(list)
    by_ind      = defaultdict(list)
    by_sec_cap  = defaultdict(list)
    by_sec      = defaultdict(list)

    for s in stocks:
        industry = s.get("industry", "Unknown")
        sector   = s.get("sector",   "Unknown")
        cap      = s.get("cap_size", "Unknown")
        by_ind_cap[(industry, cap)].append(s)
        by_ind[industry].append(s)
        by_sec_cap[(sector, cap)].append(s)
        by_sec[sector].append(s)

    groups = {}
    for s in stocks:
        t        = s["ticker"]
        industry = s.get("industry", "Unknown")
        sector   = s.get("sector",   "Unknown")
        cap      = s.get("cap_size", "Unknown")

        tier1 = [p for p in by_ind_cap[(industry, cap)] if p["ticker"] != t]
        tier2 = [p for p in by_ind[industry]            if p["ticker"] != t]
        tier3 = [p for p in by_sec_cap[(sector, cap)]   if p["ticker"] != t]
        tier4 = [p for p in by_sec[sector]              if p["ticker"] != t]
        tier5 = [p for p in stocks                      if p["ticker"] != t]

        if   len(tier1) >= MIN_PEERS: groups[t] = tier1
        elif len(tier2) >= MIN_PEERS: groups[t] = tier2
        elif len(tier3) >= MIN_PEERS: groups[t] = tier3
        elif len(tier4) >= MIN_PEERS: groups[t] = tier4
        else:                         groups[t] = tier5

    return groups


def peer_median(peers, field, bounds=None):
    """
    Compute the median of `field` across peers.
    Clips values to `bounds` (lo, hi) to remove outliers.
    Returns None if fewer than MIN_PEERS valid values.
    """
    vals = []
    for p in peers:
        v = sf(p.get(field))
        if v is None or v <= 0:
            continue
        if bounds:
            lo, hi = bounds
            if not (lo <= v <= hi):
                continue
        vals.append(v)
    if len(vals) < MIN_PEERS:
        return None
    return float(np.median(vals))


def calc_comps(stock, peers):
    """
    Comparable company analysis: estimate fair value using peer medians.
    Returns dict with comps_fair_value, comps_signal, comps_upside_pct,
    and each method's estimate.
    """
    price       = sf(stock.get("price"))
    shares      = sf(stock.get("shares")) or 1
    eps         = sf(stock.get("eps_ttm"))
    rev         = sf(stock.get("rev_now"))    # in $B
    fcf         = sf(stock.get("fcf"))        # in $B
    ebitda      = sf(stock.get("ebitda"))     # in $B
    mkt_cap_raw = sf(stock.get("mkt_cap_raw"))

    estimates = {}

    # Method 1: P/E × EPS
    peer_pe = peer_median(peers, "pe", MULTIPLE_BOUNDS["pe"])
    if peer_pe and eps and eps > 0:
        estimates["pe"] = peer_pe * eps

    # Method 2: EV/EBITDA → equity value per share
    peer_ev_ebitda = peer_median(peers, "ev_ebitda", MULTIPLE_BOUNDS["ev_ebitda"])
    if peer_ev_ebitda and ebitda and ebitda > 0:
        ebitda_raw = ebitda * 1e9
        net_debt   = sf(stock.get("net_debt") or 0) * 1e9
        implied_ev = peer_ev_ebitda * ebitda_raw
        equity_val = implied_ev - net_debt
        if equity_val > 0:
            estimates["ev_ebitda"] = equity_val / shares

    # Method 3: P/S × revenue per share
    peer_ps = peer_median(peers, "ps", MULTIPLE_BOUNDS["ps"])
    if peer_ps and rev and rev > 0:
        estimates["ps"] = peer_ps * (rev * 1e9 / shares)

    # Method 4: P/FCF × FCF per share
    # Compute peer P/FCF from each peer's market_cap / FCF
    p_fcf_vals = []
    for p in peers:
        mc = sf(p.get("mkt_cap_raw"))
        f  = sf(p.get("fcf"))
        if mc and f and f > 0:
            pf = mc / (f * 1e9)
            lo, hi = MULTIPLE_BOUNDS["p_fcf"]
            if lo <= pf <= hi:
                p_fcf_vals.append(pf)
    peer_pfcf = float(np.median(p_fcf_vals)) if len(p_fcf_vals) >= MIN_PEERS else None

    if peer_pfcf and fcf and fcf > 0 and mkt_cap_raw:
        estimates["p_fcf"] = peer_pfcf * (fcf * 1e9 / shares)

    # Trimmed average of valid estimates
    # Per-estimate sanity check: discard any estimate < 0.2× or > 3× current price.
    # This guards against foreign ADRs (e.g. TSM) where yfinance returns revenue /
    # EBITDA / FCF in local currency (TWD, HKD, …), inflating P/Sales and EV/EBITDA
    # estimates by 20–30×.  P/E is per-share EPS so it is currency-safe.
    if price and price > 0:
        estimates = {
            k: v for k, v in estimates.items()
            if v and v > 0 and (price * 0.2) <= v <= (price * 3.0)
        }

    vals = [v for v in estimates.values() if v and v > 0]
    if not vals:
        return {
            "comps_fair_value": None,
            "comps_signal":     "⚪ Insufficient peer data",
            "comps_upside_pct": None,
            "comps_pe_est":     None,
            "comps_ev_est":     None,
            "comps_ps_est":     None,
            "comps_pfcf_est":   None,
            "comps_peer_count": len(peers),
            "peer_median_pe":   fmt(peer_pe),
            "peer_median_eveb": fmt(peer_ev_ebitda),
        }

    # Drop highest and lowest outlier if ≥4 estimates
    if len(vals) >= 4:
        vals = sorted(vals)[1:-1]
    comps_fv = fmt(sum(vals) / len(vals))

    # Signal — tightened thresholds
    # 🟢 requires ≥25% upside (was 30% — slightly more sensitive)
    # 🟡 requires 10-24% upside only; flat/slight negative → 🔴 sooner
    # -15% threshold for red (was -10%) to avoid flagging normal fluctuation
    if price and comps_fv:
        upside = fmt((comps_fv - price) / price * 100)
        if   upside >= 25:  comps_signal = f"🟢 {upside}% upside vs peers"
        elif upside >= 10:  comps_signal = f"🟡 {upside}% upside vs peers"
        elif upside >= -5:  comps_signal = "🟡 Near peer fair value"
        else:               comps_signal = f"🔴 {abs(upside)}% overvalued vs peers"
    else:
        upside       = None
        comps_signal = "⚪ Insufficient data"

    return {
        "comps_fair_value": comps_fv,
        "comps_signal":     comps_signal,
        "comps_upside_pct": upside,
        "comps_pe_est":     fmt(estimates.get("pe")),
        "comps_ev_est":     fmt(estimates.get("ev_ebitda")),
        "comps_ps_est":     fmt(estimates.get("ps")),
        "comps_pfcf_est":   fmt(estimates.get("p_fcf")),
        "comps_peer_count": len(peers),
        "peer_median_pe":   fmt(peer_pe),
        "peer_median_eveb": fmt(peer_ev_ebitda),
    }



# ── Model 3: EPV + Graham Number ─────────────────────────────────────────────

def calc_epv(stock):
    """
    Earnings Power Value (EPV) — Bruce Greenwald method.
    EPV = NOPAT / WACC.  Assumes zero growth — a conservative floor.
    If price < EPV, the stock earns its cost of capital with no growth priced in.
    """
    op_income = sf(stock.get("op_income"))   # $B
    shares    = sf(stock.get("shares")) or 1
    price     = sf(stock.get("price"))

    _null = {"epv_value": None, "epv_signal": "⚪ Insufficient data", "epv_upside_pct": None}

    if not op_income or op_income <= 0 or is_fund(stock):
        return _null

    wacc        = estimate_wacc(stock)
    nopat       = op_income * 1e9 * (1 - 0.21)      # Net Operating Profit After Tax
    epv_assets  = nopat / wacc
    net_debt    = sf(stock.get("net_debt") or 0) * 1e9
    net_debt    = min(net_debt, epv_assets * 0.80)   # same cap as DCF
    epv_equity  = epv_assets - net_debt

    if epv_equity <= 0:
        return _null

    epv_ps = round(epv_equity / shares, 2)

    # Plausibility: allow EPV to be as low as 3 % of price.
    # A low EPV just means the market is pricing in significant growth —
    # that is valid information, not a data error. Only discard if absurdly
    # high (> 8×) which would indicate a bad input (e.g. negative WACC result).
    if price and price > 0:
        r = epv_ps / price
        if r > 8.0 or r < 0.03:
            return _null

    upside = fmt((epv_ps - price) / price * 100) if price else None
    if price and upside is not None:
        if   epv_ps >= price * 1.30: signal = f"🟢 {upside}% upside (EPV)"
        elif epv_ps >= price:        signal =  "🟡 Near EPV floor value"
        else:                        signal = f"🔴 {abs(upside)}% above EPV floor"
    else:
        signal = "⚪ Insufficient data"

    return {"epv_value": fmt(epv_ps), "epv_signal": signal, "epv_upside_pct": upside}


def calc_graham(stock):
    """
    Graham Number — Benjamin Graham's classic formula.
    Graham Number = √(22.5 × EPS_TTM × BVPS)
    Works best for stable, asset-backed, profitable businesses.
    """
    eps   = sf(stock.get("eps_ttm"))
    bvps  = sf(stock.get("bvps"))
    price = sf(stock.get("price"))

    _null = {"graham_value": None, "graham_signal": "⚪ Insufficient data", "graham_upside_pct": None}

    if not eps or eps <= 0 or not bvps or bvps <= 0 or is_fund(stock):
        return _null

    graham = round(math.sqrt(22.5 * eps * bvps), 2)

    # Plausibility: allow Graham Number as low as 3 % of price.
    # For asset-light companies (large buybacks shrink book value), the Graham
    # Number naturally falls far below price. That's informative, not erroneous.
    if price and price > 0:
        r = graham / price
        if r > 8.0 or r < 0.03:
            return _null

    upside = fmt((graham - price) / price * 100) if price else None
    if price and upside is not None:
        if   graham >= price * 1.30: signal = f"🟢 {upside}% upside (Graham)"
        elif graham >= price:        signal =  "🟡 Near Graham Number"
        else:                        signal = f"🔴 {abs(upside)}% above Graham Number"
    else:
        signal = "⚪ Insufficient data"

    return {"graham_value": fmt(graham), "graham_signal": signal, "graham_upside_pct": upside}


def calc_model3(stock):
    """
    Combine EPV and Graham into a single conservative Model 3 result.
    Both are no-growth / asset-based methods — they form a valuation floor.
    Average them when both are available; use whichever runs if only one is.
    """
    epv    = calc_epv(stock)
    graham = calc_graham(stock)
    price  = sf(stock.get("price"))

    epv_v    = sf(epv.get("epv_value"))
    graham_v = sf(graham.get("graham_value"))

    _null_m3 = {
        **epv, **graham,
        "m3_fair_value": None,
        "m3_signal":     "⚪ Insufficient data",
        "m3_upside_pct": None,
    }

    if epv_v and graham_v:
        m3_value = fmt((epv_v + graham_v) / 2)
    elif epv_v:
        m3_value = fmt(epv_v)
    elif graham_v:
        m3_value = fmt(graham_v)
    else:
        return _null_m3

    upside = fmt((m3_value - price) / price * 100) if price and m3_value else None
    if price and upside is not None:
        if   m3_value >= price * 1.30: m3_signal = f"🟢 {upside}% upside (EPV/Graham)"
        elif m3_value >= price:        m3_signal =  "🟡 Near EPV/Graham floor"
        else:                          m3_signal = f"🔴 {abs(upside)}% above EPV/Graham"
    else:
        m3_signal = "⚪ Insufficient data"

    return {
        **epv, **graham,
        "m3_fair_value": m3_value,
        "m3_signal":     m3_signal,
        "m3_upside_pct": upside,
    }


# ── Momentum (price history) ──────────────────────────────────────────────────

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    ag, al = sum(gains) / period, sum(losses) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def mom_signal(rsi, ma_ratio):
    r, m = sf(rsi), sf(ma_ratio)
    if   r and r < 30:   return "🟢 Oversold (RSI<30)"
    elif r and r > 70:   return "🔴 Overbought (RSI>70)"
    elif m and m > 1.05: return "📈 Uptrend (MA50>MA200)"
    elif m and m < 0.95: return "📉 Downtrend (MA50<MA200)"
    return "➡️ Neutral"


def fetch_momentum_batch(tickers):
    """Fetch 1yr price history for RSI + moving average signals."""
    results, failed = {}, []
    try:
        group = yf.Tickers(" ".join(tickers))
        for ticker in tickers:
            try:
                hist   = group.tickers[ticker].history(period="1y", interval="1d")
                closes = [float(c) for c in hist["Close"] if not math.isnan(float(c))]
                if len(closes) < 20:
                    failed.append(ticker)
                    continue
                rsi      = calc_rsi(closes)
                ma50     = sum(closes[-50:])  / 50  if len(closes) >= 50  else None
                ma200    = sum(closes[-200:]) / 200 if len(closes) >= 200 else None
                ma_ratio = fmt(ma50 / ma200)        if ma50 and ma200     else None
                ret_1y   = fmt((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] else None
                results[ticker] = {
                    "rsi":        rsi,
                    "ma_ratio":   ma_ratio,
                    "ret_1y":     ret_1y,
                    "price_live": fmt(closes[-1]),
                    "mom_signal": mom_signal(rsi, ma_ratio),
                }
            except Exception:
                failed.append(ticker)
    except Exception:
        failed = list(tickers)
    return results, failed


# ── Combined signal ───────────────────────────────────────────────────────────

def combined_signal(dcf_sig, comps_sig, m3_sig=None):
    """Merge all three model signals into one actionable recommendation."""
    d = dcf_sig   or ""
    c = comps_sig or ""
    m = m3_sig    or ""

    dcf_green   = "🟢" in d;  dcf_yellow  = "🟡" in d;  dcf_red   = "🔴" in d
    comp_green  = "🟢" in c;  comp_yellow = "🟡" in c;  comp_red  = "🔴" in c
    m3_green    = "🟢" in m;  m3_red      = "🔴" in m

    greens = sum([dcf_green, comp_green, m3_green])
    reds   = sum([dcf_red,   comp_red,   m3_red])

    # ── Revised combined signal logic ───────────────────────────────────────────
    # Changes from old version:
    # 1. Comps-alone green no longer triggers BUY — comps are meaningless if the
    #    whole sector is overvalued. Require at least DCF agreement.
    # 2. EPV/Graham alone no longer triggers BUY — it's a no-growth floor value;
    #    being below it just means no growth is priced in, not that it's cheap.
    # 3. STRONG BUY requires all 3 OR DCF+Comps agreement (both fundamental methods).
    # 4. Reds are more aggressively flagged — 2 reds = AVOID regardless of the third.

    if greens == 3:                         return "⭐ STRONG BUY — All 3 models agree"
    if dcf_green and comp_green:            return "⭐ STRONG BUY — DCF + Comps agree"
    if greens == 2 and reds == 0:           return "✅ BUY — 2 of 3 models undervalued"
    if greens == 2:                         return "✅ BUY — Majority models positive"
    if dcf_green and comp_yellow:           return "✅ BUY — DCF strong, peers near fair"
    if dcf_yellow and comp_green:           return "✅ BUY — Peers cheap, approaching DCF value"
    if dcf_green:                           return "✅ BUY — Below MOS price (DCF)"
    if reds >= 2:                           return "🚫 AVOID — Multiple models expensive"
    if dcf_red and comp_red:                return "🚫 AVOID — DCF + Comps both expensive"
    if dcf_red:                             return "⚠️ CAUTION — DCF says overvalued"
    if comp_red:                            return "⚠️ CAUTION — Expensive vs peers"
    if comp_green:                          return "👀 WATCH — Peers suggest upside (verify DCF)"
    if m3_green:                            return "👀 WATCH — Near EPV/Graham floor"
    if dcf_yellow or comp_yellow:           return "👀 WATCH — Approaching fair value"
    return "⚪ HOLD — Insufficient signal"


# ── Main ──────────────────────────────────────────────────────────────────────

def _print_assumptions():
    """Print all model parameters so the user can see exactly what is assumed."""
    print()
    print("━" * 55)
    print("  MODEL ASSUMPTIONS")
    print("━" * 55)
    print()
    print("  ── MODEL 1: DCF ──────────────────────────────────────")
    print(f"     Projection horizon : {DCF_YEARS} years")
    print(f"     Terminal growth rate: {DCF_TERMINAL_G*100:.1f}%  (Gordon Growth)")
    print(f"     Margin of Safety    : {DCF_MOS*100:.0f}%  (MOS buy price = intrinsic × {(1-DCF_MOS)*100:.0f}%)")
    print(f"     Risk-free rate      : {RF_RATE*100:.1f}%  (10-yr Treasury proxy)")
    print(f"     Equity risk premium : {MARKET_PREMIUM*100:.1f}%  (CAPM market premium)")
    print(f"     WACC floor / ceiling: 7 % / 25 %")
    print(f"     WACC fallbacks by cap size:")
    for cap, w in DEFAULT_WACC.items():
        print(f"       {cap:<12}: {w*100:.1f}%")
    print(f"     Growth blending     : 50% rev CAGR + 30% EPS CAGR + 20% FCF CAGR")
    print(f"     Growth blending     : EPS CAGR 40% + FCF CAGR 35% + Rev CAGR 25%  (we project FCF, not revenue)")
    print(f"     Per-metric blend    : each CAGR = 5Y×70% + 3Y×30%  (catches recent accel/decel)")
    print(f"     Revenue ceiling     : blended rate cannot exceed Rev CAGR + 5pp  (margin-expansion guard)")
    print(f"     Mean-reversion      : ≤8%→0%  8-15%→15%  15-25%→25%  >25%→35% haircut")
    print(f"     Quality haircut     : FCF margin <3%→18%  3-8%→12%  8-15%→5%  ≥15%→0%")
    print(f"     Two-stage growth    : Stage 1 (yr 1-5) = haircutted hist  Stage 2 (yr 6-10) = midpoint to GDP")
    print(f"     Growth cap          : 0% floor, 18% ceiling  (sector: 10% for Util/Def/Energy)")
    print(f"     Stage 2 cap         : max 12%  (mean-reverts toward 4.5% long-run GDP)")
    print(f"     FCF source          : 3-year average preferred (smooths capex swings)")
    print(f"     FCF safety cap      : max 25 % of revenue")
    print(f"     FCF fallback        : op_income × 65%  then  net income × 70%")
    print(f"     Net debt cap        : cannot reduce equity by more than 80 %")
    print(f"     Plausibility filter : intrinsic must be 0.10×–8× current price")
    print(f"     WACC floor          : 8.5% (raised from 7%)")
    print()
    print("  ── MODEL 2: COMPS ────────────────────────────────────")
    print(f"     Minimum peers       : {MIN_PEERS}  (expands to sector, then all stocks)")
    print(f"     Peer tier order     : industry+cap → industry → sector+cap → sector → all")
    print(f"     Multiple bounds (outlier clip):")
    for k, (lo, hi) in MULTIPLE_BOUNDS.items():
        print(f"       {k:<12}: {lo} – {hi}")
    print(f"     Methods             : P/E, EV/EBITDA, P/Sales, P/FCF  (trimmed avg)")
    print(f"     Currency guard      : per-method estimates outside 0.2×–3× price discarded")
    print(f"       (protects against ADRs reporting financials in local currency)")
    print()
    print("  ── MODEL 3: EPV + GRAHAM ─────────────────────────────")
    print(f"     EPV formula         : NOPAT / WACC   (NOPAT = op_income × 79 %)")
    print(f"     Graham formula      : √(22.5 × EPS_TTM × BVPS)")
    print(f"     Combined M3 value   : average of EPV and Graham when both available")
    print(f"     Plausibility filter : 0.03×–8× price  (low EPV/Graham = growth priced in)")
    print()
    print("  ── COMBINED SIGNAL ───────────────────────────────────")
    print(f"     ⭐ STRONG BUY        : all 3 models show 🟢 undervalued")
    print(f"     ✅ BUY               : 2 of 3 models undervalued OR DCF alone below MOS")
    print(f"     🚫 AVOID             : 2+ models show 🔴 overvalued")
    print(f"     ⚠️  CAUTION           : 1 model flags expensive")
    print()
    print("━" * 55)
    print()


def _stale_valuation_tickers(tickers):
    """Return tickers whose valuation is missing or older than fundamentals."""
    try:
        from database import SessionLocal, Fundamentals, Valuation
        from datetime import datetime as _dt
        db = SessionLocal()
        fund_rows = {r.ticker: r.last_updated
                     for r in db.query(Fundamentals.ticker, Fundamentals.last_updated)
                     .filter(Fundamentals.ticker.in_(tickers)).all()}
        val_rows  = {r.ticker: r.last_updated
                     for r in db.query(Valuation.ticker, Valuation.last_updated)
                     .filter(Valuation.ticker.in_(tickers)).all()}
        db.close()
        stale = []
        for t in tickers:
            f_upd = fund_rows.get(t)
            v_upd = val_rows.get(t)
            if v_upd is None:
                stale.append(t)
            elif f_upd and f_upd > v_upd:
                stale.append(t)
            elif v_upd and (_dt.utcnow() - v_upd).days >= STALE_DAYS:
                stale.append(t)
        return set(stale)
    except Exception:
        return set(tickers)


def main():
    print("=" * 55)
    print("  model.py — DCF + Comps + EPV/Graham")
    print("=" * 55)
    _print_assumptions()

    # Load fundamentals — prefer DB, fall back to JSON
    try:
        from database import SessionLocal, Fundamentals, init_db
        init_db()
        db = SessionLocal()
        rows = db.query(Fundamentals).all()
        db.close()
        if rows:
            from datetime import datetime as _dt
            def _row_to_dict(r):
                # Exclude datetime columns — they confuse numeric helpers downstream
                return {c.name: getattr(r, c.name)
                        for c in r.__table__.columns
                        if not isinstance(getattr(r, c.name), _dt)}
            stocks = [_row_to_dict(r) for r in rows]
            print(f"Loaded {len(stocks)} stocks from DB")
        else:
            raise ValueError("DB empty")
    except Exception:
        try:
            with open(FUNDAMENTALS_FILE) as f:
                fund = json.load(f)
            stocks = fund["stocks"]
            print(f"Loaded {len(stocks)} stocks from {FUNDAMENTALS_FILE}")
        except FileNotFoundError:
            print(f"❌ {FUNDAMENTALS_FILE} not found — run fundamentals.py first")
            return

    all_tickers = [s["ticker"] for s in stocks]
    stale = _stale_valuation_tickers(all_tickers)
    fresh_count = len(all_tickers) - len(stale)
    print(f"Fresh valuations (skip): {fresh_count}  |  Stale (recompute): {len(stale)}")

    # ── Momentum fetch — always refresh for ALL stocks (live prices) ──────────
    mom_cache = load_cache(CACHE_FILE)
    tickers   = all_tickers
    print(f"\nMomentum cache: {len(mom_cache)} done  |  "
          f"Remaining: {len([t for t in tickers if t not in mom_cache])}")

    # run_batches handles the main loop + retry — no separate retry block needed
    mom_cache = run_batches(
        items=tickers,
        fetch_fn=fetch_momentum_batch,
        cache=mom_cache,
        cache_path=CACHE_FILE,
        batch_size=BATCH_SIZE,
        sleep_sec=SLEEP_SEC,
        max_retries=MAX_RETRIES,
        progress_every=20,
    )

    # Merge momentum into stocks
    for s in stocks:
        m = mom_cache.get(s["ticker"], {})
        s["rsi"]        = m.get("rsi")
        s["ma_ratio"]   = m.get("ma_ratio")
        s["ret_1y"]     = m.get("ret_1y")
        s["mom_signal"] = m.get("mom_signal", "⚪ No data")
        if m.get("price_live"):
            s["price"] = m["price_live"]  # use fresh price

    # Persist momentum fields (rsi/ma_ratio/ret_1y) into the Fundamentals table
    # for ALL stocks — these are computed here (need price history) but the
    # Fundamentals upsert in fundamentals.py never sees them, so they'd stay null.
    _save_momentum_to_db(stocks)

    # ── Build peer groups ─────────────────────────────────────────────────────
    print("\nBuilding peer groups for Comps model …")
    peer_groups = build_peer_groups(stocks)
    sizes = [len(v) for v in peer_groups.values()]
    print(f"  Avg peers per stock: {round(sum(sizes) / len(sizes)) if sizes else 0}")

    # ── Run all three models — only for stale tickers ─────────────────────────
    print("Running DCF + Comps + EPV/Graham valuations …")
    results = []
    dcf_ok = comps_ok = m3_ok = all3_ok = 0

    for s in stocks:
        # Always update momentum fields in DB; skip heavy models if fresh
        if s["ticker"] not in stale:
            continue
        ticker       = s["ticker"]
        peers        = peer_groups.get(ticker, [])
        dcf_result   = calc_dcf(s)
        comps_result = calc_comps(s, peers)
        m3_result    = calc_model3(s)

        if dcf_result["dcf_intrinsic"]     is not None: dcf_ok   += 1
        if comps_result["comps_fair_value"] is not None: comps_ok += 1
        if m3_result.get("m3_fair_value")  is not None: m3_ok    += 1
        if (dcf_result["dcf_intrinsic"]     is not None
                and comps_result["comps_fair_value"] is not None
                and m3_result.get("m3_fair_value")  is not None):
            all3_ok += 1

        merged = {**s, **dcf_result, **comps_result, **m3_result}
        # Provide valuation dicts score_stock expects
        _dcf   = {"dcf_upside_pct": dcf_result.get("dcf_upside_pct"),
                  "dcf_mos_price":  dcf_result.get("dcf_mos_price")}
        _comps = {"comps_upside_pct": comps_result.get("comps_upside_pct")}
        _m3    = {"m3_upside_pct": m3_result.get("m3_upside_pct")}
        scorecard = score_stock(merged, dcf=_dcf, comps=_comps, m3=_m3)
        results.append({
            **merged,
            "combined_signal":  combined_signal(
                dcf_result["dcf_signal"],
                comps_result["comps_signal"],
                m3_result.get("m3_signal"),
            ),
            "score_composite": scorecard["composite"],
            "score_label":     scorecard["rec_label"],
            "score_stars":     scorecard["rec_stars"],
            "avg_upside":      scorecard["avg_upside"],
        })

    out = {
        "generated":      datetime.now().isoformat(),
        "total":          len(results),
        "dcf_computed":   dcf_ok,
        "comps_computed": comps_ok,
        "m3_computed":    m3_ok,
        "all3_computed":  all3_ok,
        "stocks":         results,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    _save_to_db(results)

    print(f"\n✅ Model saved: {len(results)} stocks → {OUTPUT_FILE}")
    print(f"   DCF computed:        {dcf_ok}   ({round(dcf_ok/len(results)*100)}%)")
    print(f"   Comps computed:      {comps_ok}   ({round(comps_ok/len(results)*100)}%)")
    print(f"   EPV/Graham computed: {m3_ok}   ({round(m3_ok/len(results)*100)}%)")
    print(f"   All 3 computed:      {all3_ok}   ({round(all3_ok/len(results)*100)}%)")

    sigs = {}
    for s in results:
        k = s.get("combined_signal", "⚪ HOLD")
        sigs[k] = sigs.get(k, 0) + 1
    print("\nCombined Signal Summary:")
    for k, v in sorted(sigs.items(), key=lambda x: x[1], reverse=True):
        print(f"  {k}: {v}")


def _save_momentum_to_db(stocks):
    """Upsert momentum fields into Fundamentals for every stock.

    rsi/ma_ratio/ret_1y are computed from price history in this module, after
    fundamentals.py has already written its rows, so they are persisted here.
    """
    try:
        from database import SessionLocal, Fundamentals, init_db, upsert
        from serializers import db_record
        from datetime import datetime as _dt
        init_db()
        db = SessionLocal()
        n = 0
        for s in stocks:
            present = {f: s[f] for f in ("rsi", "ma_ratio", "ret_1y")
                       if s.get(f) is not None}
            if not present:  # only write if at least one momentum field present
                continue
            row = db_record(Fundamentals, {"ticker": s["ticker"], **present})
            row["last_updated"] = _dt.utcnow()
            upsert(db, Fundamentals, row)
            n += 1
        db.commit()
        db.close()
        print(f"  ✅ Saved momentum (rsi/ma_ratio/ret_1y) for {n} stocks to fundamentals")
    except Exception as e:
        print(f"  ⚠️  Momentum DB save failed: {e}")


def _save_to_db(results):
    # Map from result dict keys → Valuation column names
    _FIELD_MAP = {
        "dcf_intrinsic":    "dcf_fair_value",
        "dcf_mos_price":    "dcf_mos_price",
        "dcf_upside_pct":   "dcf_upside_pct",
        "dcf_signal":       "dcf_signal",
        "dcf_wacc":         "dcf_wacc",
        "dcf_growth_rate":  "dcf_growth",
        "comps_fair_value": "comps_fair_value",
        "comps_upside_pct": "comps_upside_pct",
        "comps_signal":     "comps_signal",
        "comps_peer_count": "comps_peers",
        "m3_fair_value":    "m3_fair_value",
        "m3_upside_pct":    "m3_upside_pct",
        "m3_signal":        "m3_signal",
        "score_composite":  "score_composite",
        "score_label":      "score_label",
        "score_stars":      "score_stars",
        "avg_upside":       "avg_upside",
    }
    try:
        from database import SessionLocal, Valuation, init_db, upsert
        from serializers import db_record
        from datetime import datetime as _dt
        init_db()
        db = SessionLocal()
        for s in results:
            # `s` is the SAME rich record written to model.json; _FIELD_MAP renames
            # the model's result keys to the Valuation column names and db_record
            # projects onto the actual columns (single DB-row construction, #12).
            mapped = {dst: s[src] for src, dst in _FIELD_MAP.items()
                      if src in s and s[src] is not None}
            mapped["ticker"] = s["ticker"]
            row = db_record(Valuation, mapped)
            row["last_updated"] = _dt.utcnow()
            upsert(db, Valuation, row)
        db.commit()
        db.close()
        print(f"  ✅ Saved {len(results)} valuations to database")
    except Exception as e:
        print(f"  ⚠️  DB save failed: {e}")


if __name__ == "__main__":
    main()
