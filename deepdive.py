"""
deepdive.py — Per-stock deep dive → Excel workbook
====================================================
Maintains a saved list of tickers and writes a dedicated
sheet per ticker in DeepDive_YYYY-MM-DD.xlsx.

Usage:
  python deepdive.py              # refresh all saved tickers
  python deepdive.py AAPL MSFT   # add tickers + refresh all
  python deepdive.py --remove TSLA
  python deepdive.py --list

Each sheet contains:
  ▸ Company overview + live price + key ratios
  ▸ Valuation multiples
  ▸ DCF vs Comps side by side + sensitivity table
  ▸ Financial history (up to 10 years)
  ▸ Rule #1 Big Five CAGR table (1/3/5/7/10 year)
  ▸ Key profitability metrics + balance sheet
  ▸ Analyst consensus + momentum

Cache: deepdive_cache.json   (TTL: 6 hours)
List:  deepdive_tickers.json
"""

import argparse, json, math, os, time, warnings
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

from yf_client import yf_ticker
import numpy as np

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

from data_utils import sf, fmt, pct, ratio, cagr, series_cagr, series_values, \
                       extract_field, first_valid, get_row, load_cache, save_cache
from model import (
    calc_dcf, calc_comps, calc_model3, estimate_wacc, is_fund,
    DCF_YEARS, DCF_TERMINAL_G, DCF_MOS, RF_RATE, MARKET_PREMIUM,
    MULTIPLE_BOUNDS, DEFAULT_WACC,
)
from fundamentals import extract_fundamentals

# ── Config ────────────────────────────────────────────────────────────────────
TICKER_LIST_FILE = "deepdive_tickers.json"
CACHE_FILE       = "deepdive_cache.json"
CACHE_TTL_HOURS  = 6
OUTPUT_FILE      = f"DeepDive_{datetime.now().strftime('%Y-%m-%d')}.xlsx"


# ── openpyxl helpers ──────────────────────────────────────────────────────────

def _rgb(r_f, g_f, b_f):
    return f"{int(r_f*255):02X}{int(g_f*255):02X}{int(b_f*255):02X}"

def _fill(hex_or_tuple):
    if isinstance(hex_or_tuple, tuple):
        hex_or_tuple = _rgb(*hex_or_tuple)
    return PatternFill(fill_type="solid", fgColor=hex_or_tuple)

def _write_rows(ws, rows, start_row=1):
    for r_i, row in enumerate(rows, start_row):
        for c_i, val in enumerate(row, 1):
            ws.cell(row=r_i, column=c_i, value=val if val != "" else None)
    return start_row + len(rows)

def _auto_width(ws, min_w=12, max_w=50):
    for col in ws.columns:
        w = max((len(str(cell.value or "")) for cell in col), default=min_w)
        ws.column_dimensions[col[0].column_letter].width = max(min_w, min(w + 2, max_w))


def _has_white_font(cell):
    """
    Safely check if a cell already has a white font — without crashing on
    theme-color or indexed-color objects, which don't expose .rgb.
    openpyxl stores explicit white as 'FFFFFFFF' (with FF alpha prefix).
    """
    try:
        rgb = cell.font.color.rgb           # raises AttributeError on theme colors
        return rgb in ("FFFFFF", "FFFFFFFF")
    except (AttributeError, TypeError):
        return False                        # default/theme color → not white → apply bold


# ── Cell formatters ───────────────────────────────────────────────────────────

def _s(val, fallback="—"):
    if val is None or val == "" or (isinstance(val, float) and math.isnan(val)):
        return fallback
    return val

def _p(val, fallback="—"):
    if val is None: return fallback
    try:    return f"{float(val):.2f}%"
    except: return fallback

def _d(val, fallback="—"):
    if val is None: return fallback
    try:    return f"${float(val):.2f}"
    except: return fallback


# ── DCF Sensitivity table ─────────────────────────────────────────────────────

def dcf_sensitivity(stock):
    from model import _normalize_fcf
    growth_rates = [0.02, 0.05, 0.08, 0.12, 0.16, 0.20]
    waccs        = [0.07, 0.08, 0.09, 0.10, 0.11, 0.12, 0.14]
    price        = sf(stock.get("price"))

    fcf_raw, shares = _normalize_fcf(stock)
    if fcf_raw is None or shares <= 0:
        return []

    net_debt_raw = sf(stock.get("net_debt") or 0) * 1e9
    header = ["Growth \\ WACC"] + [f"{int(w*100)}%" for w in waccs]
    rows   = [header]

    for g in growth_rates:
        row = [f"{int(g*100)}%"]
        for wacc in waccs:
            if wacc <= DCF_TERMINAL_G:
                row.append("N/A"); continue
            pv, fcf_t = 0.0, fcf_raw
            for yr in range(1, DCF_YEARS + 1):
                fcf_t  = fcf_t * (1 + g)
                pv    += fcf_t / ((1 + wacc) ** yr)
            tv  = fcf_t * (1 + DCF_TERMINAL_G) / (wacc - DCF_TERMINAL_G)
            pv += tv / ((1 + wacc) ** DCF_YEARS)
            nd  = min(net_debt_raw, pv * 0.80)
            ev  = pv - nd
            if ev <= 0:
                row.append("N/A"); continue
            iv  = round(ev / shares, 2)
            tag = "★ " if price and abs(iv - price) / price <= 0.05 else ""
            row.append(f"{tag}${iv}")
        rows.append(row)
    return rows


# ── News fetch ────────────────────────────────────────────────────────────────

def fetch_news(ticker, company_name):
    """
    Fetch recent news and SEC filings for a ticker.
    Returns a list of dicts: {date, type, source, title, url}
    Sources:
      1. yfinance .news      — general news articles
      2. SEC EDGAR search    — 8-K / 10-K / 10-Q filings (no API key needed)
    """
    items = []

    # ── 1. yfinance news ─────────────────────────────────────────────────────
    # yfinance changed its news item structure in v0.2.x.
    # Old  : {"title": ..., "publisher": ..., "link": ..., "providerPublishTime": int}
    # New  : {"id": ..., "content": {"title": ..., "pubDate": ...,
    #           "canonicalUrl": {"url": ...}, "provider": {"displayName": ...}}}
    # We handle both by checking both locations for each field.
    try:
        yft  = yf_ticker(ticker)
        news = yft.news or []
        found_urls = 0
        for n in news[:30]:
            # ── content block (new structure) ───────────────────────────────
            content = n.get("content") or {}

            # Title
            title = (n.get("title")
                     or content.get("title")
                     or "—")

            # Publisher / source
            source = (n.get("publisher")
                      or (content.get("provider") or {}).get("displayName")
                      or (content.get("provider") or {}).get("name")
                      or "—")

            # URL — try every known location
            url = (n.get("link")
                   or n.get("url")
                   or (content.get("canonicalUrl") or {}).get("url")
                   or (content.get("clickThroughUrl") or {}).get("url")
                   or "")

            # Date — old = unix int, new = ISO string
            ts = (n.get("providerPublishTime")
                  or n.get("providerPublishtime")
                  or 0)
            pub_date = content.get("pubDate") or ""
            try:
                if pub_date:
                    date_str = pub_date[:10]
                elif ts:
                    date_str = datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
                else:
                    date_str = "—"
            except Exception:
                date_str = "—"

            if url:
                found_urls += 1
            items.append({
                "date":   date_str,
                "type":   "News",
                "source": source,
                "title":  title,
                "url":    url,
            })
        print(f"    yfinance news: {len(news)} items, {found_urls} with URLs")
    except Exception as e:
        print(f"    ⚠️  yfinance news failed for {ticker}: {e}")

    # ── 2. SEC EDGAR full-text search ────────────────────────────────────────
    # Uses the EDGAR full-text search API (no API key required).
    # Correct endpoint: efts.sec.gov/LATEST/search-index  (NOT the old EFTS URL)
    # The _source param is NOT a valid query param here — removed.
    # Each hit's _source block contains form_type, file_date, etc.
    # We build a direct per-accession filing URL so the link opens the actual doc.
    try:
        start_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
        for q in [ticker]:          # ticker symbol is most reliable query
            params = urllib.parse.urlencode({
                "q":         q,
                "forms":     "10-K,10-Q,8-K",
                "dateRange": "custom",
                "startdt":   start_date,
            })
            url = f"https://efts.sec.gov/LATEST/search-index?{params}"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "StockTracker/1.0 research@example.com",
                         "Accept":     "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode()
            data = json.loads(raw)
            if not isinstance(data, dict):
                print(f"    ⚠️  EDGAR returned non-dict for {ticker}")
                continue
            hits_outer = data.get("hits", {})
            if not isinstance(hits_outer, dict):
                continue
            hits = hits_outer.get("hits", [])
            print(f"    EDGAR: {len(hits)} filings found for {ticker}")
            for h in hits[:15]:
                src       = h.get("_source", {}) or {}
                form_type = src.get("form_type") or "Filing"
                file_date = (src.get("file_date") or "")[:10]
                period    = (src.get("period_of_report") or "")[:10]

                # filer name from display_names list
                names      = src.get("display_names") or []
                if names and isinstance(names[0], dict):
                    filer_name = names[0].get("name", ticker)
                elif names and isinstance(names[0], str):
                    filer_name = names[0]
                else:
                    filer_name = ticker

                # Build direct accession URL: /Archives/edgar/data/CIK/ACCESSION-idx.htm
                acc_no = h.get("_id", "")          # e.g. "0001234567-24-000001"
                entity_id = src.get("entity_id") or src.get("file_num") or ""
                if acc_no:
                    # Canonical EDGAR accession link (always resolves)
                    acc_clean  = acc_no.replace("-", "")
                    filing_url = (
                        f"https://www.sec.gov/cgi-bin/browse-edgar"
                        f"?action=getcompany&CIK={urllib.parse.quote(ticker)}"
                        f"&type={urllib.parse.quote(form_type)}"
                        f"&dateb=&owner=include&count=10&search_text="
                    )
                else:
                    filing_url = (
                        f"https://www.sec.gov/cgi-bin/browse-edgar"
                        f"?action=getcompany&CIK={urllib.parse.quote(ticker)}"
                        f"&type=&dateb=&owner=include&count=10"
                    )

                label = f"{form_type}  —  {filer_name}"
                if period:
                    label += f"  (period: {period})"
                items.append({
                    "date":   file_date or "—",
                    "type":   f"SEC {form_type}",
                    "source": "SEC EDGAR",
                    "title":  label,
                    "url":    filing_url,
                })
            if hits:
                break
    except Exception as e:
        print(f"    ⚠️  SEC EDGAR fetch failed for {ticker}: {e}")

    # Sort by date descending (most recent first), blanks last
    items.sort(key=lambda x: x["date"] if x["date"] != "—" else "0000-00-00", reverse=True)
    return items


# ── Deep fetch ────────────────────────────────────────────────────────────────

def fetch_deep(ticker):
    print(f"  Fetching {ticker} …")
    yft    = yf_ticker(ticker)
    record = extract_fundamentals(ticker, yft)

    info = {}
    try: info = yft.info or {}
    except Exception: pass

    # Extra fields
    try:
        record["short_pct"]    = fmt(sf(info.get("shortPercentOfFloat", 0)) * 100)
        record["short_ratio"]  = fmt(info.get("shortRatio"))
    except Exception:
        record["short_pct"] = record["short_ratio"] = None

    try:
        record["inst_own_pct"] = fmt(sf(info.get("heldPercentInstitutions", 0)) * 100)
        record["insider_pct"]  = fmt(sf(info.get("heldPercentInsiders", 0)) * 100)
    except Exception:
        record["inst_own_pct"] = record["insider_pct"] = None

    try:
        record["div_rate"]     = fmt(info.get("dividendRate"))
        record["payout_ratio"] = fmt(sf(info.get("payoutRatio", 0)) * 100)
        record["ex_div_date"]  = str(info.get("exDividendDate", ""))[:10]
    except Exception:
        record["div_rate"] = record["payout_ratio"] = record["ex_div_date"] = None

    record["high52"] = fmt(info.get("fiftyTwoWeekHigh"))
    record["low52"]  = fmt(info.get("fiftyTwoWeekLow"))

    # Financial history (up to 10 years)
    fin, cf, bs = None, None, None
    try: fin = yft.financials
    except Exception: pass
    try: cf  = yft.cashflow
    except Exception: pass
    try: bs  = yft.balance_sheet
    except Exception: pass

    def _years(df, n=10):
        if df is None or df.empty: return []
        return [str(c)[:4] for c in list(df.columns)[:n]]

    def _row(df, field, n=10):
        vals = series_values(extract_field(df, field), n)
        return [fmt(v / 1e9) if v is not None else "" for v in vals]

    def _row_raw(df, field, n=10):
        return series_values(extract_field(df, field), n)

    def _margins(num_s, den_s, n=10):
        num = series_values(extract_field(fin, num_s), n)
        den = series_values(extract_field(fin, den_s), n)
        out = []
        for a, b in zip(num, den):
            out.append(fmt(a / b * 100) if a is not None and b and b != 0 else "")
        return out

    years    = _years(fin)
    rev_h    = _row(fin, "revenue")
    ni_h     = _row(fin, "net_income")
    gp_h     = _row(fin, "gross_profit")
    op_h     = _row(fin, "operating_income")
    ebitda_h = _row(fin, "ebitda")
    ocf_h    = _row(cf,  "operating_cf")
    capex_h  = _row(cf,  "capex")
    fcf_h    = _row(cf,  "free_cash_flow")
    eps_h    = [fmt(e) if e is not None else "" for e in series_values(extract_field(fin, "eps_diluted"), 10)]
    gm_h     = _margins("gross_profit",     "revenue")
    om_h     = _margins("operating_income", "revenue")
    nm_h     = _margins("net_income",       "revenue")

    # Rule #1 raw series
    rev_raw    = _row_raw(fin, "revenue")
    ni_raw     = _row_raw(fin, "net_income")
    eps_raw    = series_values(extract_field(fin, "eps_diluted"), 11)

    fcf_direct = _row_raw(cf, "free_cash_flow")
    if len(fcf_direct) >= 2:
        fcf_raw_s = fcf_direct
    else:
        ocf_v  = _row_raw(cf, "operating_cf")
        cap_v  = _row_raw(cf, "capex")
        fcf_raw_s = []
        for o, c in zip(ocf_v, cap_v):
            if o is not None and c is not None: fcf_raw_s.append(o - abs(c))
            elif o is not None:                 fcf_raw_s.append(o)
            else:                               fcf_raw_s.append(None)

    equity_raw = _row_raw(bs, "equity")
    shares_val = sf(record.get("shares")) or 1
    bvps_raw   = [eq / shares_val if eq is not None else None for eq in equity_raw]

    op_raw  = _row_raw(fin, "operating_income")
    ic_raw  = _row_raw(bs,  "invested_capital")
    roic_raw = []
    for op, ic in zip(op_raw, ic_raw):
        roic_raw.append(op * 0.79 / ic * 100 if op is not None and ic and ic != 0 else None)

    record["r1_series"] = {
        "years": years, "revenue": rev_raw, "net_income": ni_raw,
        "eps": eps_raw, "fcf": fcf_raw_s, "bvps": bvps_raw, "roic": roic_raw,
    }
    print(f"    Rule #1 series: rev={len(rev_raw)}  eps={len(eps_raw)}  "
          f"fcf={len(fcf_raw_s)}  bvps={len(bvps_raw)}  roic={len(roic_raw)}")

    record["history"] = {
        "years": years, "revenue": rev_h, "net_income": ni_h, "gross_profit": gp_h,
        "operating_income": op_h, "ebitda": ebitda_h, "ocf": ocf_h,
        "capex": capex_h, "fcf": fcf_h, "eps": eps_h,
        "gross_margin": gm_h, "op_margin": om_h, "net_margin": nm_h,
    }

    # Momentum
    try:
        hist   = yft.history(period="1y", interval="1d")
        closes = [float(c) for c in hist["Close"] if not math.isnan(float(c))]
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains  = [d if d > 0 else 0 for d in deltas[-14:]]
            losses = [-d if d < 0 else 0 for d in deltas[-14:]]
            ag, al = sum(gains) / 14, sum(losses) / 14
            record["rsi"]     = round(100 - 100 / (1 + ag / al), 1) if al else 100.0
            ma50  = round(sum(closes[-50:])  / min(50,  len(closes)), 2) if len(closes) >= 50  else None
            ma200 = round(sum(closes[-200:]) / min(200, len(closes)), 2) if len(closes) >= 200 else None
            record["ma50"]    = ma50
            record["ma200"]   = ma200
            record["ma_ratio"]= fmt(ma50 / ma200) if ma50 and ma200 else None
            record["ret_1y"]  = fmt((closes[-1] - closes[0]) / closes[0] * 100) if closes[0] else None
        if closes:
            record["price"] = round(closes[-1], 2)
    except Exception:
        pass

    record["fetched_at"] = datetime.now().isoformat()

    # Fetch news + SEC filings
    company_name = record.get("name", ticker)
    print(f"    Fetching news for {ticker} …")
    record["news"] = fetch_news(ticker, company_name)
    print(f"    Found {len(record['news'])} news/filing items")

    return record


# score_stock() lives in data_utils.py (shared with sheets.py)
from data_utils import score_stock

# ── Sheet writer ──────────────────────────────────────────────────────────────

def write_deepdive_sheet(ws, stock, peers):
    """Write one ticker's full deep-dive layout to an openpyxl worksheet."""
    now     = datetime.now().strftime("%Y-%m-%d %H:%M")
    ticker  = stock.get("ticker", "?")
    name    = stock.get("name", ticker)
    price   = _s(stock.get("price"))
    mktcap  = _s(stock.get("mkt_cap"))
    sector  = _s(stock.get("sector"))
    industry= stock.get("industry", "—")

    dcf    = calc_dcf(stock)
    comps  = calc_comps(stock, peers)
    m3     = calc_model3(stock)
    sensit = dcf_sensitivity(stock)

    # Colour palette — alternating section headers
    HEADER_A = {"bg": (0.11, 0.24, 0.47), "fg": "FFFFFF"}   # deep blue
    HEADER_B = {"bg": (0.09, 0.36, 0.22), "fg": "FFFFFF"}   # dark green

    rows = []

    # Run scorecard before building rows so we have all scores ready
    scorecard = score_stock(stock, dcf, comps, m3)
    composite = scorecard["composite"]
    rec_label = scorecard["rec_label"]
    rec_stars = scorecard["rec_stars"]
    rec_bg    = scorecard["rec_bg"]
    rec_fg    = scorecard["rec_fg"]
    cats      = scorecard["categories"]

    # ── Title ─────────────────────────────────────────────────────────────────
    rows.append([f"🔬 {ticker}  —  {name}", "", "", f"Last updated: {now}"])
    rows.append([""])

    def sec(title):
        rows.append([f"▌ {title}", ""])

    # ── 0. Investment Scorecard ───────────────────────────────────────────────
    sec("⭐ Investment Recommendation Scorecard")

    # Overall verdict row (spans visually — put it in cols A-D)
    rows.append([
        f"{rec_stars}  {rec_label}",
        f"Composite Score:  {composite} / 100",
        f"Avg Model Upside:  {scorecard['avg_upside']}%" if scorecard['avg_upside'] is not None else "Avg Model Upside:  N/A",
        f"Combined Signal:  {stock.get('combined_signal','—')}",
    ])
    rows.append([""])

    # Scoring methodology note
    rows.append(["HOW THIS SCORE IS CALCULATED", "", "", ""])
    rows.append(["7 categories, each scored 0–100, then weighted into a composite:", "", "", ""])
    rows.append(["  Business Quality 25%  ·  Growth Track Record 20%  ·  Financial Health 15%", "", "", ""])
    rows.append(["  Valuation 20%  ·  Earnings Quality 10%  ·  Momentum 5%  ·  Analyst Consensus 5%", "", "", ""])
    rows.append([""])

    # Category summary table
    rows.append(["Category", "Score (0-100)", "Weight", "Weighted pts"])
    total_check = 0
    for cat_name, cat_data in cats.items():
        w  = cat_data["weight"]
        s  = cat_data["score"]
        wp = round(s * w / 100, 1)
        total_check += wp
        rows.append([cat_name, f"{s} / 100", f"{w}%", f"{wp:.1f}"])
    rows.append(["COMPOSITE TOTAL", f"{composite} / 100", "100%", f"{round(total_check,1)}"])
    rows.append([""])

    # Detailed breakdown for each category
    rows.append(["DETAILED BREAKDOWN — each criterion scored 0-10", "", "", ""])
    rows.append([""])
    for cat_name, cat_data in cats.items():
        rows.append([f"  {cat_name}  (score: {cat_data['score']}/100)", "", "", ""])
        rows.append(["    Criterion", "Value", "Points", "Max"])
        for item in cat_data["items"]:
            rows.append([f"      {item[0]}", item[3], item[1], item[2]])
        rows.append([""])

    # ── 1. Company Overview ───────────────────────────────────────────────────
    sec("Company Overview")
    rows.append(["Current Price",           _d(price),                   "Sector",                 _s(sector)])
    rows.append(["Market Cap",              f"${_s(mktcap)}B",           "Industry",               _s(industry)])
    rows.append(["52-Week High",            _d(stock.get("high52")),     "52-Week Low",            _d(stock.get("low52"))])
    rows.append(["Beta (Volatility)",       _s(stock.get("beta")),       "Exchange",               _s(stock.get("exchange"))])
    rows.append(["Dividend Yield",          _p(stock.get("div_yield")),  "Annual Dividend",        _d(stock.get("div_rate"))])
    rows.append(["Payout Ratio",            _p(stock.get("payout_ratio")),"Ex-Dividend Date",      _s(stock.get("ex_div_date"))])
    rows.append(["Short % of Float",        _p(stock.get("short_pct")),  "Short Ratio (days)",     _s(stock.get("short_ratio"))])
    rows.append(["Institutional Ownership", _p(stock.get("inst_own_pct")),"Insider Ownership",     _p(stock.get("insider_pct"))])
    rows.append(["Next Earnings Date",      _s(stock.get("earn_date")),  "Data Completeness",      f"{_s(stock.get('data_quality'))}%"])
    rows.append([""])

    # ── 2. Valuation Multiples ────────────────────────────────────────────────
    sec("Valuation Multiples  (what the market pays today)")
    rows.append(["Price / Earnings (TTM)",     _s(stock.get("pe")),        "Price / Earnings (Fwd)",  _s(stock.get("fwd_pe"))])
    rows.append(["Price / Book Value",         _s(stock.get("pb")),        "Price / Sales",            _s(stock.get("ps"))])
    rows.append(["EV / EBITDA",                _s(stock.get("ev_ebitda")), "EV / Revenue",             _s(stock.get("ev_revenue"))])
    rows.append(["PEG Ratio",                  _s(stock.get("peg")),       "EPS — Trailing 12 Months", _d(stock.get("eps_ttm"))])
    rows.append(["EPS — Forward Estimate",     _d(stock.get("eps_fwd")),   "", ""])
    rows.append([""])

    # ── 3. Valuation Models ───────────────────────────────────────────────────
    sec("Valuation Models  —  3 Independent Approaches")
    rows.append(["", "",
                 "Model 1  —  DCF",
                 "Model 2  —  Comps  (Peer Multiples)",
                 "Model 3  —  EPV / Graham Number"])
    rows.append(["Estimated Fair Value",       "",
                 _d(dcf.get("dcf_intrinsic")),
                 _d(comps.get("comps_fair_value")),
                 _d(m3.get("m3_fair_value"))])
    rows.append(["Upside / Downside vs Price", "",
                 _p(dcf.get("dcf_upside_pct")),
                 _p(comps.get("comps_upside_pct")),
                 _p(m3.get("m3_upside_pct"))])
    rows.append(["Model Signal",               "",
                 _s(dcf.get("dcf_signal")),
                 _s(comps.get("comps_signal")),
                 _s(m3.get("m3_signal"))])
    rows.append(["Method detail",              "",
                 f"MOS Buy Price: {_d(dcf.get('dcf_mos_price'))}  |  WACC: {_p(dcf.get('dcf_wacc'))}  |  Growth: {_p(dcf.get('dcf_growth_rate'))}",
                 f"{_s(comps.get('comps_peer_count'))} peers used",
                 f"EPV: {_d(m3.get('epv_value'))}  |  Graham #: {_d(m3.get('graham_value'))}"])
    rows.append(["⭐  Combined Verdict",        "",
                 _s(stock.get("combined_signal", "⚪ Insufficient data")), "", ""])
    rows.append([""])

    # ── 4. Comps Breakdown ────────────────────────────────────────────────────
    sec("Comps — All 4 Methods Breakdown")
    rows.append(["Method", "Estimated Fair Value", "Peer Multiple Used", ""])
    rows.append(["Price / Earnings (P/E)",       _d(comps.get("comps_pe_est")),
                 f"Peer median P/E: {_s(comps.get('peer_median_pe'))}x", ""])
    rows.append(["EV / EBITDA",                  _d(comps.get("comps_ev_est")),
                 f"Peer median EV/EBITDA: {_s(comps.get('peer_median_eveb'))}x", ""])
    rows.append(["Price / Sales (P/S)",          _d(comps.get("comps_ps_est")),   "Peer median P/S applied", ""])
    rows.append(["Price / Free Cash Flow (P/FCF)",_d(comps.get("comps_pfcf_est")),"Peer median P/FCF applied", ""])
    rows.append([""])

    # ── 5. DCF Sensitivity ────────────────────────────────────────────────────
    sec("DCF Sensitivity Table  —  Intrinsic Value by Growth Rate × Discount Rate (WACC)")
    rows.append(["★ marks where intrinsic value is within 5% of current price  "
                 "|  MOS buy price = intrinsic × 65%", ""])
    if sensit:
        rows += sensit
    else:
        rows.append(["Not enough data to build sensitivity table", ""])
    rows.append([""])

    # ── 6. Financial History ──────────────────────────────────────────────────
    hist    = stock.get("history", {})
    yr_list = hist.get("years", [])
    n_yrs   = len(yr_list)

    sec("Financial History  ($ Billions, newest year first)")
    rows.append(["", ""] + yr_list)
    rows.append(["Revenue",             ""] + hist.get("revenue",         []))
    rows.append(["Gross Profit",        ""] + hist.get("gross_profit",    []))
    rows.append(["EBITDA",              ""] + hist.get("ebitda",          []))
    rows.append(["Operating Income",    ""] + hist.get("operating_income",[]))
    rows.append(["Net Income",          ""] + hist.get("net_income",      []))
    rows.append(["Operating Cash Flow", ""] + hist.get("ocf",             []))
    rows.append(["Capital Expenditures",""] + hist.get("capex",           []))
    rows.append(["Free Cash Flow",      ""] + hist.get("fcf",             []))
    rows.append(["EPS (diluted)",       ""] + hist.get("eps",             []))
    rows.append(["— Margins —",         ""] + [""] * n_yrs)
    rows.append(["Gross Margin %",      ""] + hist.get("gross_margin",    []))
    rows.append(["Operating Margin %",  ""] + hist.get("op_margin",       []))
    rows.append(["Net Margin %",        ""] + hist.get("net_margin",      []))
    rows.append([""])

    # ── 7. Rule #1 CAGR Table ─────────────────────────────────────────────────
    sec("Rule #1 Investing — Big Five Growth Rates  (Phil Town, target: ≥10% per year)")
    rows.append(["Each cell = CAGR over that period.  ✅ = meets 10%   ❌ = below   — = not enough data", ""])
    rows.append([""])

    r1s = stock.get("r1_series", {})

    def _cagr_cell(series_list, n_years):
        if not series_list or len(series_list) < n_years + 1: return "—"
        start, end = series_list[n_years], series_list[0]
        if start is None or end is None or start == 0: return "—"
        try:
            ratio = end / start
            if ratio < 0: return "—"   # sign flip → complex → not meaningful
            val = (ratio ** (1.0 / n_years) - 1) * 100
            if isinstance(val, complex) or val != val: return "—"
            icon = "✅" if val >= 10 else "❌"
            return f"{icon}  {val:.1f}%"
        except Exception:
            return "—"

    def _roic_cell(roic_list, n_years):
        if not roic_list or len(roic_list) < n_years: return "—"
        vals = [v for v in roic_list[:n_years] if v is not None]
        if not vals: return "—"
        avg  = sum(vals) / len(vals)
        icon = "✅" if avg >= 10 else "❌"
        return f"{icon}  {avg:.1f}%"

    rows.append(["Period", "EPS Growth", "Revenue Growth",
                 "Book Value / Share Growth", "Free Cash Flow Growth", "ROIC  (avg over period)"])
    for h in [1, 3, 5, 7, 10]:
        rows.append([
            f"{h}-Year",
            _cagr_cell(r1s.get("eps",     []), h),
            _cagr_cell(r1s.get("revenue", []), h),
            _cagr_cell(r1s.get("bvps",    []), h),
            _cagr_cell(r1s.get("fcf",     []), h),
            _roic_cell(r1s.get("roic",    []), h),
        ])
    rows.append([""])

    # ── 8. Key Profitability ──────────────────────────────────────────────────
    sec("Key Profitability Metrics  (most recent year)")
    rows.append(["Return on Equity (ROE)",          _p(stock.get("roe")),        "Return on Invested Capital (ROIC)", _p(stock.get("roic"))])
    rows.append(["Return on Assets (ROA)",          _p(stock.get("roa")),        "Gross Margin",                     _p(stock.get("gross_margin"))])
    rows.append(["Operating Margin",                _p(stock.get("op_margin")),  "Net Margin",                       _p(stock.get("net_margin"))])
    rows.append(["Free Cash Flow Margin",           _p(stock.get("fcf_margin")), "Total Revenue",                    f"${_s(stock.get('rev_now'))}B"])
    rows.append([""])

    # ── 9. Balance Sheet ──────────────────────────────────────────────────────
    sec("Balance Sheet Health")
    rows.append(["Total Debt",                  f"${_s(stock.get('total_debt'))}B", "Cash & Equivalents",     f"${_s(stock.get('cash'))}B"])
    rows.append(["Net Debt  (Debt minus Cash)", f"${_s(stock.get('net_debt'))}B",   "Total Assets",           f"${_s(stock.get('total_assets'))}B"])
    rows.append(["Debt / Equity Ratio",         _s(stock.get("d_to_e")),            "Debt / EBITDA",          _s(stock.get("d_to_ebitda"))])
    rows.append(["Current Ratio",               _s(stock.get("curr_ratio")),        "Interest Coverage",      _s(stock.get("int_cov"))])
    rows.append([""])

    # ── 10. Analyst Consensus ─────────────────────────────────────────────────
    sec("Analyst Consensus")
    rows.append(["Mean Price Target",   _d(stock.get("analyst_mean")),  "Analyst Recommendation", _s(stock.get("analyst_rec"))])
    rows.append(["Lowest Price Target", _d(stock.get("analyst_low")),   "Highest Price Target",   _d(stock.get("analyst_high"))])
    try:
        am = sf(stock.get("analyst_mean"))
        pr = sf(stock.get("price"))
        up = f"{((am - pr) / pr * 100):.1f}%" if am and pr else "—"
    except Exception:
        up = "—"
    rows.append(["Number of Analysts",  _s(stock.get("num_analysts")),  "Upside to Mean Target",  up])
    rows.append([""])

    # ── 11. Momentum & Technicals ─────────────────────────────────────────────
    sec("Momentum & Technicals")
    rows.append(["RSI (14-day)",          _s(stock.get("rsi")),       "50-Day Moving Average",   _d(stock.get("ma50"))])
    rows.append(["MA50 / MA200 Ratio",    _s(stock.get("ma_ratio")),  "200-Day Moving Average",  _d(stock.get("ma200"))])
    rows.append(["1-Year Price Return",   _p(stock.get("ret_1y")),    "Momentum Signal",         _s(stock.get("mom_signal", ""))])
    rows.append([""])

    # ── 12. News & Filings ────────────────────────────────────────────────────
    sec("Recent News, Earnings Reports & SEC Filings")
    news_items = stock.get("news", [])
    if news_items:
        rows.append(["Date", "Type", "Source", "Headline / Filing  (click to open)", ""])
        rows.append([""])  # spacer row — actual hyperlink rows written separately after _write_rows
    else:
        rows.append(["No news data available. Re-run deepdive.py to fetch.", ""])
    rows.append([""])

    # ── Write text rows to worksheet ──────────────────────────────────────────
    news_header_row = None
    for i, row in enumerate(rows):
        if row and isinstance(row[0], str) and row[0] == "Date" and len(row) > 3 and "Headline" in str(row[3]):
            news_header_row = i + 1   # 1-based
            break

    _write_rows(ws, rows)

    # ── Colour the scorecard verdict row ─────────────────────────────────────
    # Find the verdict row — it's the one that starts with the stars+label
    # (first data row under the scorecard section header)
    verdict_row = None
    for r_i, row in enumerate(rows, 1):
        if row and isinstance(row[0], str) and rec_label in row[0]:
            verdict_row = r_i
            break
    if verdict_row:
        vfill = _fill(rec_bg)
        vfont = Font(bold=True, size=12, color=rec_fg)
        for c in range(1, 5):
            ws.cell(verdict_row, c).fill = vfill
            ws.cell(verdict_row, c).font = vfont

    # Colour the category summary rows — alternating light tint
    # Find the "Category / Score / Weight / Weighted pts" header row
    for r_i, row in enumerate(rows, 1):
        if row and isinstance(row[0], str) and row[0] == "Category":
            # Colour the header
            hfill = _fill("D9E1F2")
            hfont = Font(bold=True)
            for c in range(1, 5):
                ws.cell(r_i, c).fill = hfill
                ws.cell(r_i, c).font = hfont
            # Colour each category data row and the total row
            r = r_i + 1
            i = 0
            while r <= r_i + len(cats) + 1:   # +1 for TOTAL row
                row_val = ws.cell(r, 1).value or ""
                if "COMPOSITE" in str(row_val):
                    for c in range(1, 5):
                        ws.cell(r, c).fill = _fill("D6DCE4")
                        ws.cell(r, c).font = Font(bold=True)
                elif str(row_val).strip():
                    if i % 2 == 0:
                        for c in range(1, 5):
                            ws.cell(r, c).fill = _fill("EFF3FB")
                    i += 1
                r += 1
            break

    # ── Append hyperlinked news rows below the text rows ─────────────────────
    if news_items and news_header_row is not None:
        # Style the news column-header row
        hdr_fill = _fill("1C3D78")
        hdr_font = Font(bold=True, color="FFFFFF")
        for c in range(1, 5):
            ws.cell(news_header_row, c).fill = hdr_fill
            ws.cell(news_header_row, c).font = hdr_font

        link_font  = Font(color="0563C1", underline="single")
        plain_font = Font()
        news_start = len(rows) + 1   # first row after all text rows
        for idx, item in enumerate(news_items):
            r = news_start + idx
            ws.cell(r, 1).value = item.get("date",   "—")
            ws.cell(r, 2).value = item.get("type",   "—")
            ws.cell(r, 3).value = item.get("source", "—")
            title_cell       = ws.cell(r, 4)
            title_cell.value = item.get("title", "—")
            url = item.get("url", "")
            if url:
                title_cell.hyperlink = url
                title_cell.font      = link_font
            else:
                title_cell.font = plain_font
            if idx % 2 == 0:
                stripe = _fill("F0F4FA")
                for c in range(1, 5):
                    ws.cell(r, c).fill = stripe

    # ── Title row formatting ──────────────────────────────────────────────────
    n_cols     = max(6, 2 + len(yr_list))
    title_fill = _fill((0.11, 0.24, 0.47))
    title_font = Font(bold=True, size=13, color="FFFFFF")
    for c in range(1, n_cols + 1):
        cell      = ws.cell(1, c)
        cell.fill = title_fill
        cell.font = title_font

    # ── Section header rows ───────────────────────────────────────────────────
    header_idx = 0
    for r_i, row in enumerate(rows, 1):
        if row and isinstance(row[0], str) and row[0].startswith("▌"):
            combo = HEADER_A if header_idx % 2 == 0 else HEADER_B
            fill  = _fill(combo["bg"])
            font  = Font(bold=True, size=10, color=combo["fg"])
            for c in range(1, n_cols + 1):
                ws.cell(r_i, c).fill = fill
                ws.cell(r_i, c).font = font
            header_idx += 1

    # ── Bold column A labels ──────────────────────────────────────────────────
    # Cap at len(rows) so we never touch the news hyperlink rows that were
    # appended after _write_rows — those rows use link_font on col D and
    # we must not interfere with them.
    bold_font = Font(bold=True)
    for r in range(1, len(rows) + 1):
        cell = ws.cell(r, 1)
        if cell.value and not _has_white_font(cell):
            cell.font = bold_font

    # ── Column widths ─────────────────────────────────────────────────────────
    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 20
    ws.column_dimensions["D"].width = 70   # wide for news headlines + hyperlinks
    for i in range(5, n_cols + 1):
        ws.column_dimensions[get_column_letter(i)].width = 20

    ws.freeze_panes = "A2"


# ── Ticker list management ────────────────────────────────────────────────────

def load_ticker_list():
    if os.path.exists(TICKER_LIST_FILE):
        with open(TICKER_LIST_FILE) as f:
            return json.load(f)
    return []

def save_ticker_list(tickers):
    with open(TICKER_LIST_FILE, "w") as f:
        json.dump(sorted(set(tickers)), f, indent=2)
    print(f"  Saved {len(tickers)} tickers → {TICKER_LIST_FILE}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stock deep dive → Excel")
    parser.add_argument("tickers",   nargs="*",    help="Ticker(s) to add and refresh")
    parser.add_argument("--remove",  nargs="+",    metavar="T",
                        help="Remove one or more tickers  e.g. --remove AAPL MSFT")
    parser.add_argument("--clear",   action="store_true",
                        help="Remove ALL tickers and wipe the cache")
    parser.add_argument("--list",    action="store_true", help="List saved tickers")
    args = parser.parse_args()

    ticker_list = load_ticker_list()
    cache       = load_cache(CACHE_FILE)

    # ── --list ────────────────────────────────────────────────────────────
    if args.list:
        if ticker_list:
            print(f"Deep dive tickers ({len(ticker_list)}):")
            for t in ticker_list:
                cached = cache.get(t, {})
                age    = "no cache"
                if cached.get("fetched_at"):
                    try:
                        from datetime import timezone
                        fetched = datetime.fromisoformat(cached["fetched_at"])
                        hrs     = (datetime.now() - fetched).total_seconds() / 3600
                        age     = f"cached {hrs:.1f}h ago"
                    except Exception:
                        age = "cached"
                print(f"  {t:<8}  {age}")
        else:
            print("No tickers saved.  Add one:  python deepdive.py AAPL")
        return

    # ── --clear ───────────────────────────────────────────────────────────
    if args.clear:
        n = len(ticker_list)
        save_ticker_list([])
        # Wipe cache entries for all removed tickers
        for t in ticker_list:
            cache.pop(t, None)
        save_cache(cache, CACHE_FILE)
        print(f"✅ Cleared {n} ticker(s) and their cached data.")
        return

    # ── --remove ──────────────────────────────────────────────────────────
    if args.remove:
        removed, not_found = [], []
        for raw in args.remove:
            t = raw.upper().strip()
            if t in ticker_list:
                ticker_list.remove(t)
                cache.pop(t, None)   # also delete cached data
                removed.append(t)
            else:
                not_found.append(t)
        if removed:
            save_ticker_list(ticker_list)
            save_cache(cache, CACHE_FILE)
            print(f"✅ Removed: {', '.join(removed)}")
        if not_found:
            print(f"⚠️  Not in list: {', '.join(not_found)}")
        if ticker_list:
            print(f"Remaining tickers: {', '.join(ticker_list)}")
        else:
            print("Deep dive list is now empty.")
        return

    for t in args.tickers:
        t = t.upper().strip()
        if t not in ticker_list:
            ticker_list.append(t)
            print(f"  Added {t} to deep dive list")
    if args.tickers:
        save_ticker_list(ticker_list)

    if not ticker_list:
        print("No deep dive tickers. Run: python deepdive.py AAPL")
        return

    print("=" * 55)
    print("  deepdive.py — Stock Deep Dive")
    print(f"  Tickers: {', '.join(ticker_list)}")
    print("=" * 55)

    # Load peer context from pipeline
    fund_map, model_map = {}, {}
    try:
        with open("fundamentals.json") as f:
            fd = json.load(f)
        fund_map = {s["ticker"]: s for s in fd["stocks"]}
        print(f"Loaded {len(fund_map)} stocks for peer group context")
    except FileNotFoundError:
        print("⚠️  fundamentals.json not found — peer comps will use limited data")

    try:
        with open("model.json") as f:
            md = json.load(f)
        model_map = {s["ticker"]: s for s in md["stocks"]}
    except FileNotFoundError:
        pass

    # Fetch / use cache
    stocks_data = {}
    for ticker in ticker_list:
        cached = cache.get(ticker, {})
        age_ok = False
        if cached.get("fetched_at"):
            try:
                fetched = datetime.fromisoformat(cached["fetched_at"])
                age_ok  = (datetime.now() - fetched).total_seconds() / 3600 < CACHE_TTL_HOURS
            except Exception:
                pass
        # Also invalidate cache if key data sections are missing
        # (handles records cached before history/news fields were added)
        if age_ok and not cached.get("history"):
            age_ok = False
            print(f"  {ticker}: cache missing history — re-fetching")
        if age_ok:
            print(f"  {ticker}: using cache ({cached['fetched_at'][:16]})")
            stocks_data[ticker] = cached
        else:
            try:
                data = fetch_deep(ticker)
                for k, val in model_map.get(ticker, {}).items():
                    if val is not None and k not in data: data[k] = val
                cache[ticker]       = data
                stocks_data[ticker] = data
                time.sleep(1)
            except Exception as e:
                print(f"  ❌ {ticker}: fetch failed — {e}")
                if cached:
                    stocks_data[ticker] = cached
                    print(f"     Falling back to cached data")

    save_cache(cache, CACHE_FILE)

    # Peer groups
    all_stocks = list(fund_map.values())
    for t, d in stocks_data.items():
        if t not in fund_map: all_stocks.append(d)

    def get_peers(stock):
        sector   = stock.get("sector", "")
        industry = stock.get("industry", "")
        ticker   = stock.get("ticker", "")
        peers    = [s for s in all_stocks
                    if s.get("ticker") != ticker
                    and s.get("industry", "") == industry
                    and industry not in ("", "Unknown")]
        if len(peers) >= 5: return peers
        peers = [s for s in all_stocks
                 if s.get("ticker") != ticker
                 and s.get("sector", "") == sector
                 and sector not in ("", "Unknown")]
        return peers if len(peers) >= 5 else [s for s in all_stocks if s.get("ticker") != ticker]

    # Build workbook
    wb = Workbook()
    wb.remove(wb.active)

    sheets_written = 0
    for ticker in ticker_list:
        if ticker not in stocks_data:
            print(f"  ⚠️  {ticker}: no data — skipping")
            continue
        stock = stocks_data[ticker]
        peers = get_peers(stock)
        tab   = f"🔬 {ticker}"
        print(f"  Writing {tab} …")
        ws = wb.create_sheet(title=tab)
        try:
            write_deepdive_sheet(ws, stock, peers)
            # Quick sanity check — row 1 col 1 should have ticker text
            if ws.cell(1, 1).value:
                sheets_written += 1
                print(f"    ✅ {ticker}: {ws.max_row} rows written")
            else:
                print(f"    ⚠️  {ticker}: sheet appears empty after write")
        except Exception as e:
            import traceback
            print(f"  ❌ {ticker}: write_deepdive_sheet crashed:")
            traceback.print_exc()
            # Leave the (possibly partial) sheet in so the file is still openable

    if sheets_written == 0 and len(ticker_list) > 0:
        # Workbook needs at least one sheet to be valid — add a placeholder
        ws_err = wb.create_sheet(title="⚠️ Error")
        ws_err.cell(1, 1).value = (
            "All deep dive sheets failed to render. "
            "Check your terminal for traceback details."
        )

    out = os.path.abspath(OUTPUT_FILE)
    wb.save(out)
    print(f"\n✅ Deep dive complete → {out}  ({sheets_written} sheets written)")
    print(f"   Folder: {os.path.dirname(out)}")


if __name__ == "__main__":
    main()
