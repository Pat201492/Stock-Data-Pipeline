"""
sheets.py — Write all tabs to an Excel workbook
=================================================
Reads universe.json, fundamentals.json, model.json.
Zero live yfinance calls — all data from JSON files.
Produces: StockTracker_YYYY-MM-DD.xlsx

Run:  python sheets.py
"""

import csv, json, os, warnings
from datetime import datetime
from collections import defaultdict
warnings.filterwarnings("ignore")

from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font
from data_utils import score_stock

# ── CONFIG ────────────────────────────────────────────────────────────────────
UNIVERSE_FILE     = "universe.json"
FUNDAMENTALS_FILE = "fundamentals.json"
MODEL_FILE        = "model.json"
TICKER_FILE       = "tickers.csv"
OUTPUT_FILE       = f"StockTracker_{datetime.now().strftime('%Y-%m-%d')}.xlsx"

BUFFETT_FILTERS = {
    "min_roe": 15.0,  "min_roic": 10.0, "min_gross_margin": 40.0,
    "min_op_margin": 15.0, "min_fcf_margin": 5.0, "min_rev_growth": 5.0,
    "max_debt_to_equity": 1.5, "max_debt_to_ebitda": 3.0,
    "min_current_ratio": 1.0, "min_interest_cov": 5.0,
}


# ── openpyxl helpers ──────────────────────────────────────────────────────────

def _rgb(r_f, g_f, b_f):
    return f"{int(r_f*255):02X}{int(g_f*255):02X}{int(b_f*255):02X}"

def _fill(hex_or_tuple):
    if isinstance(hex_or_tuple, tuple):
        hex_or_tuple = _rgb(*hex_or_tuple)
    return PatternFill(fill_type="solid", fgColor=hex_or_tuple)

def write_rows(ws, rows):
    for r_i, row in enumerate(rows, 1):
        for c_i, val in enumerate(row, 1):
            ws.cell(row=r_i, column=c_i, value=val if val != "" else None)

def freeze_row1(ws):
    ws.freeze_panes = "A2"

def color_header(ws, ncols, row=1, color=(0.18, 0.35, 0.58)):
    fill = _fill(color)
    font = Font(bold=True, color="FFFFFF")
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = fill
        cell.font = font

def auto_width(ws, min_w=10, max_w=45):
    for col in ws.columns:
        w = max((len(str(cell.value or "")) for cell in col), default=min_w)
        ws.column_dimensions[col[0].column_letter].width = max(min_w, min(w + 2, max_w))


# ── Data utilities ────────────────────────────────────────────────────────────

def pf(val, threshold, direction="min"):
    try:
        fv = float(val)
        return ("✅" if fv >= threshold else "❌") if direction == "min" \
               else ("✅" if fv <= threshold else "❌")
    except Exception:
        return "N/A"

def v(val, fallback=""):
    if val is None or (isinstance(val, float) and val != val):
        return fallback
    return val

def pct(val, fallback=""):
    if val is None or (isinstance(val, float) and val != val):
        return fallback
    try:    return f"{float(val):.2f}%"
    except: return fallback

def _price_chg(price, prev_close):
    try:
        return round((float(price) - float(prev_close)) / float(prev_close) * 100, 2) \
               if price and prev_close else ""
    except Exception:
        return ""

def _analyst_upside(price, analyst_mean):
    try:
        if not price or not analyst_mean: return ""
        val = round((float(analyst_mean) - float(price)) / float(price) * 100, 2)
        return f"{val:.2f}%"
    except Exception:
        return ""

def load_watchlists():
    wl = {}
    if not os.path.exists(TICKER_FILE):
        return wl
    with open(TICKER_FILE, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.reader(f)):
            if i == 0 and row and row[0].strip().lower() in ("ticker", "symbol"):
                continue
            if len(row) < 2: continue
            t, w = row[0].strip().upper(), row[1].strip()
            if t and w:
                wl.setdefault(w, [])
                if t not in wl[w]: wl[w].append(t)
    return wl


# ── Tab writers ───────────────────────────────────────────────────────────────

def write_readme(ws, model_data):
    dcf_pct   = model_data.get("dcf_computed", 0)
    comps_pct = model_data.get("comps_computed", 0)
    total     = model_data.get("total", 1)
    now       = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [
        ["📘 STOCK TRACKER MASTER — README", ""],
        ["Data generated", model_data.get("generated", "")],
        ["File written", now], ["", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["SCRIPTS", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["universe.py",     "Weekly — pulls top 2500 by market cap → universe.json"],
        ["fundamentals.py", "Nightly — batch financials + CAGRs + quality score → fundamentals.json"],
        ["model.py",        "Nightly — DCF + Comps valuations + momentum → model.json"],
        ["sheets.py",       "Nightly — reads JSON only, zero live API calls → Excel file"],
        ["deepdive.py",     "On demand — per-stock rich analysis → DeepDive Excel file"],
        ["run.py",          "Manual runner — runs all scripts in order"],
        ["", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["TAB DESCRIPTIONS", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["📘 README",    "This page"],
        ["🌍 Universe",  "Top 2500 by market cap — rank, cap size, sector"],
        ["👑 Rule #1",   "Phil Town Big 5 — top 15 per cap tier + top 15 per sector"],
        ["📊 Dashboard", "Prices, analyst targets, multiples"],
        ["📈 Scorecard", "Margins, ratios, 5yr CAGRs"],
        ["🔍 Filter",    "Auto PASS/FAIL vs Buffett quality thresholds"],
        ["🔮 Valuation", "DCF + Comps fair values side by side + combined signal"],
        ["📅 Earnings",  "Next earnings date, EPS estimates"],
        ["💡 My Ideas",  "Personal buy zones + notes template"],
        ["📋 Watchlist", "One tab per category in tickers.csv"],
        ["", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["🔮 VALUATION MODELS", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["MODEL 1 — DCF (Intrinsic Value)", ""],
        ["Basis",          "Future free cash flow discounted to present value"],
        ["FCF projection", "10 years at 5yr revenue CAGR (capped 2–25%)"],
        ["Discount rate",  "WACC estimated from beta + capital structure"],
        ["Terminal value",  "FCF yr10 × (1+3%) / (WACC–3%)  — Gordon Growth"],
        ["MOS Buy Price",  "DCF intrinsic × 65%  (35% margin of safety, Phil Town)"],
        ["Coverage", f"{dcf_pct}/{total} stocks ({round(dcf_pct/total*100) if total else 0}%)"],
        ["", ""],
        ["MODEL 2 — Comparable Company Analysis (Comps)", ""],
        ["Basis",           "What the market pays for similar companies today"],
        ["Peer group",      "Same industry + cap tier (expands to sector if needed)"],
        ["Methods",         "4 parallel: P/E, EV/EBITDA, P/Sales, P/FCF"],
        ["Fair value",      "Trimmed average of all valid method estimates"],
        ["Coverage", f"{comps_pct}/{total} stocks ({round(comps_pct/total*100) if total else 0}%)"],
        ["", ""],
        ["COMBINED SIGNAL", ""],
        ["⭐ STRONG BUY", "DCF below MOS AND Comps >30% upside"],
        ["✅ BUY",        "One model strongly positive"],
        ["⚠️ CAUTION",   "One model flags expensive"],
        ["🚫 AVOID",      "Both models flag expensive"],
        ["👀 WATCH",      "Approaching fair value"],
        ["⚪ HOLD",        "Insufficient data"],
        ["", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["👑 RULE #1 BIG 5 (Phil Town — target ≥10% each)", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["ROIC > 10%",       "Return on Invested Capital"],
        ["EPS CAGR > 10%",   "5yr earnings per share growth"],
        ["Sales CAGR > 10%", "5yr revenue growth"],
        ["Equity CAGR > 10%","5yr book value per share growth"],
        ["FCF CAGR > 10%",   "5yr free cash flow growth"],
        ["", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["⏳ LONGEVITY RANK", ""],
        ["━━━━━━━━━━━━━━━━━━━━━━━", ""],
        ["What it measures", "How many of the 5 Rule #1 metrics have a full 5 years of historical data"],
        ["Full credit (5/5)", "All 5 metrics — EPS, Revenue, Book Value/Share, FCF, ROIC — have ≥5yr history"],
        ["Why it matters",   "A company with 5/5 lets you verify consistency of growth over a full business cycle"],
        ["Score",            "0/5 to 5/5  —  shown in Rule #1 tab, Scorecard tab, and Valuation tab"],
    ]
    write_rows(ws, rows)
    ws.cell(1, 1).font = Font(bold=True, size=14)
    sections = {"SCRIPTS", "TAB DESCRIPTIONS", "🔮 VALUATION MODELS",
                "MODEL 1 — DCF (Intrinsic Value)",
                "MODEL 2 — Comparable Company Analysis (Comps)",
                "COMBINED SIGNAL", "👑 RULE #1 BIG 5 (Phil Town — target ≥10% each)"}
    grey_fill = _fill("EEEEEE")
    for i, row in enumerate(rows):
        if row[0].startswith("━") or row[0] in sections:
            cell      = ws.cell(i + 1, 1)
            cell.font = Font(bold=True)
            cell.fill = grey_fill
    auto_width(ws)


def write_universe(ws, uni_stocks):
    h   = ["Rank","Ticker","Company","Cap Size","Sector","Industry",
           "Country","Price ($)","Mkt Cap ($B)","Exchange","Last Updated"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [h] + [
        [v(s.get("rank")), s["ticker"], v(s.get("name")),
         v(s.get("cap_size")), v(s.get("sector")), v(s.get("industry")),
         v(s.get("country")), v(s.get("price")), v(s.get("mkt_cap")),
         v(s.get("exchange")), now]
        for s in uni_stocks
    ]
    write_rows(ws, rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=(0.10, 0.25, 0.45))
    auto_width(ws)


def write_rule1(ws, stocks):
    CAP_ORDER = ["Mega Cap","Large Cap","Mid Cap","Small Cap","Micro Cap","Unknown"]
    h = ["Group","Ticker","Company","Cap Size","Sector","Price ($)",
         "ROIC≥10%","ROIC (%)","EPS CAGR≥10%","EPS CAGR 5Y",
         "Sales CAGR≥10%","Sales CAGR 5Y","Equity CAGR≥10%","Equity CAGR 5Y",
         "FCF CAGR≥10%","FCF CAGR 5Y","# Passing","Longevity","P/E","Fwd P/E",
         "Analyst Target","Data Quality %"]

    def make_row(label, s):
        return [label, s["ticker"], v(s.get("name")), v(s.get("cap_size")),
                v(s.get("sector")), v(s.get("price")),
                v(s.get("rule1_roic")),   v(s.get("roic")),
                v(s.get("rule1_eps")),    v(s.get("eps_cagr_5y")),
                v(s.get("rule1_sales")),  v(s.get("rev_cagr_5y")),
                v(s.get("rule1_equity")), v(s.get("bvps_cagr_5y")),
                v(s.get("rule1_fcf")),    v(s.get("fcf_cagr_5y")),
                v(s.get("rule1_passes", 0)),
                v(s.get("longevity_rank", "—")),
                v(s.get("pe")),
                v(s.get("fwd_pe")), v(s.get("analyst_mean")),
                v(s.get("data_quality", ""))]

    top15    = lambda g: sorted(g, key=lambda x: x.get("rule1_passes", 0), reverse=True)[:15]
    sep      = [""] * len(h)
    all_rows = [h]

    def _section(header_label, groups_dict, key_order=None):
        all_rows.append([header_label] + [""] * (len(h) - 1))
        for key in (key_order or sorted(groups_dict.keys())):
            g = groups_dict.get(key, [])
            if not g: continue
            all_rows.append([f"── {key} ──"] + [""] * (len(h) - 1))
            for s in top15(g): all_rows.append(make_row(key, s))

    by_cap = defaultdict(list)
    for s in stocks: by_cap[s.get("cap_size", "Unknown")].append(s)
    _section("━━ TOP 15 BY CAP SIZE ━━", by_cap, CAP_ORDER)
    all_rows += [sep, sep]

    by_sec = defaultdict(list)
    for s in stocks: by_sec[s.get("sector", "Unknown")].append(s)
    _section("━━ TOP 15 BY SECTOR ━━", by_sec)

    write_rows(ws, all_rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=(0.50, 0.30, 0.05))
    auto_width(ws)


def write_dashboard(ws, stocks):
    h = ["Rank","Ticker","Company","Cap Size","Sector","Country",
         "Price ($)","1D Chg (%)","52W High","52W Low","Mkt Cap ($B)",
         "P/E","Fwd P/E","P/B","P/S","EV/EBITDA","EPS TTM","Div Yield (%)","Beta",
         "Analyst Mean","Analyst Low","Analyst High","Analyst Upside (%)","# Analysts","Rec",
         "Data Quality %","Last Updated"]
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [h,
            ["💡 Use column filters to screen by sector, cap size, signal, etc."]
            + [""] * (len(h) - 1)]
    for s in stocks:
        rows.append([
            v(s.get("rank")), s["ticker"], v(s.get("name")), v(s.get("cap_size")),
            v(s.get("sector")), v(s.get("country")), v(s.get("price")),
            _price_chg(s.get("price"), s.get("prev_close")),
            v(s.get("high52")), v(s.get("low52")), v(s.get("mkt_cap")),
            v(s.get("pe")), v(s.get("fwd_pe")), v(s.get("pb")), v(s.get("ps")),
            v(s.get("ev_ebitda")), v(s.get("eps_ttm")), v(s.get("div_yield")), v(s.get("beta")),
            v(s.get("analyst_mean")), v(s.get("analyst_low")), v(s.get("analyst_high")),
            _analyst_upside(s.get("price"), s.get("analyst_mean")),
            v(s.get("num_analysts")), v(s.get("analyst_rec")),
            v(s.get("data_quality", "")), now,
        ])
    write_rows(ws, rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=(0.13, 0.29, 0.53))
    hint_fill = _fill("F7F7F7")
    for c in range(1, len(h) + 1):
        cell = ws.cell(2, c)
        cell.font = Font(italic=True)
        cell.fill = hint_fill
    auto_width(ws)


def write_scorecard(ws, stocks):
    # ── Column groups ──────────────────────────────────────────────────────────
    # Group A: identity
    # Group B: fundamentals (unchanged from before)
    # Group C: ★ Composite scorecard — weighted model score + 7 category scores
    #
    # Category scores come from score_stock() in data_utils.py — the same
    # function that drives the deep dive recommendation section.  Every stock
    # in the universe gets scored here so you can sort / filter by any dimension.

    h = [
        # A — identity
        "Ticker", "Company", "Cap Size", "Sector",
        # B — fundamentals
        "Revenue ($B)", "Rev Growth (%)", "EBITDA ($B)", "Op Income ($B)", "FCF ($B)",
        "Gross Margin", "Op Margin", "Net Margin", "FCF Margin",
        "ROE (%)", "ROIC (%)", "ROA (%)",
        "Debt/Equity", "Debt/EBITDA", "Current Ratio", "Interest Coverage", "Net Debt ($B)",
        "Rev CAGR 5Y", "EPS CAGR 5Y", "FCF CAGR 5Y", "BVPS CAGR 5Y", "ROIC Improving",
        "Longevity", "Data Quality %",
        # C — composite scorecard
        "★ Score (0-100)", "★ Recommendation",
        "Score: Business Quality",
        "Score: Growth Track Record",
        "Score: Financial Health",
        "Score: Valuation",
        "Score: Earnings Quality",
        "Score: Momentum",
        "Score: Analyst Consensus",
    ]

    # Number of fundamentals columns (B group) — used for colour boundary
    N_FUND_COLS = 26   # cols 1-4 identity + cols 5-26 fundamentals (adjusted below)
    N_SCORE_START = 27  # first scorecard column (1-based, after identity + fundamentals)

    rows = [h]
    for s in stocks:
        sc = score_stock(s)   # runs in ~0.1ms per stock, no I/O
        cats = sc["categories"]

        rows.append([
            # A — identity
            s["ticker"], v(s.get("name")), v(s.get("cap_size")), v(s.get("sector")),
            # B — fundamentals
            v(s.get("rev_now")), v(s.get("rev_growth")), v(s.get("ebitda")),
            v(s.get("op_income")), v(s.get("fcf")),
            v(s.get("gross_margin")), v(s.get("op_margin")), v(s.get("net_margin")), v(s.get("fcf_margin")),
            v(s.get("roe")), v(s.get("roic")), v(s.get("roa")),
            v(s.get("d_to_e")), v(s.get("d_to_ebitda")), v(s.get("curr_ratio")),
            v(s.get("int_cov")), v(s.get("net_debt")),
            v(s.get("rev_cagr_5y")), v(s.get("eps_cagr_5y")),
            v(s.get("fcf_cagr_5y")), v(s.get("bvps_cagr_5y")),
            "✅" if s.get("roic_improving") else ("❌" if s.get("roic_improving") is False else "N/A"),
            v(s.get("longevity_rank", "—")),
            v(s.get("data_quality", "")),
            # C — scorecard
            sc["composite"],
            f"{sc['rec_stars']} {sc['rec_label']}",
            cats["Business Quality"]["score"],
            cats["Growth Track Record"]["score"],
            cats["Financial Health"]["score"],
            cats["Valuation"]["score"],
            cats["Earnings Quality"]["score"],
            cats["Momentum & Technicals"]["score"],
            cats["Analyst Consensus"]["score"],
        ])

    write_rows(ws, rows)
    freeze_row1(ws)

    # Colour the fundamentals header columns (green) and scorecard header (navy)
    n_total = len(h)
    n_score_cols = 9   # ★ Score + Rec + 7 categories

    # Use green for fundamentals group
    color_header(ws, n_total - n_score_cols, color=(0.18, 0.42, 0.25))

    # Override the last 9 header cells with navy to visually separate the group
    navy_fill = _fill((0.08, 0.18, 0.42))
    navy_font = Font(bold=True, color="FFFFFF")
    for c in range(n_total - n_score_cols + 1, n_total + 1):
        ws.cell(1, c).fill = navy_fill
        ws.cell(1, c).font = navy_font

    # Colour-code the composite score cells (rows 2 onward) using a heat map:
    # ≥85 green · ≥70 blue · ≥55 yellow · ≥40 orange · else red
    score_col = n_total - n_score_cols + 1   # "★ Score" is first scorecard col
    HEAT = [
        (85, "C6EFCE", "375623"),
        (70, "DDEBF7", "1F497D"),
        (55, "FFEB9C", "7F6000"),
        (40, "FCE4D6", "843C0C"),
        (0,  "FFC7CE", "9C0006"),
    ]
    for row_i in range(2, len(rows) + 1):
        cell = ws.cell(row_i, score_col)
        score_val = cell.value
        if isinstance(score_val, (int, float)):
            for threshold, bg, fg in HEAT:
                if score_val >= threshold:
                    cell.fill = _fill(bg)
                    cell.font = Font(color=fg, bold=True)
                    break

    auto_width(ws)


def write_filter(ws, stocks):
    f = BUFFETT_FILTERS
    h = ["Ticker","Company","Cap Size","Sector","Overall ✅/Total",
         f"ROE≥{f['min_roe']}%",    f"ROIC≥{f['min_roic']}%",
         f"GM≥{f['min_gross_margin']}%", f"OpM≥{f['min_op_margin']}%",
         f"FCF≥{f['min_fcf_margin']}%",  f"RevG≥{f['min_rev_growth']}%",
         f"D/E≤{f['max_debt_to_equity']}", f"D/EBITDA≤{f['max_debt_to_ebitda']}",
         f"CurrR≥{f['min_current_ratio']}", f"IntCov≥{f['min_interest_cov']}",
         "Data Quality %"]
    rows = [h]
    for s in stocks:
        res = [
            pf(s.get("roe"),          f["min_roe"]),
            pf(s.get("roic"),         f["min_roic"]),
            pf(s.get("gross_margin"), f["min_gross_margin"]),
            pf(s.get("op_margin"),    f["min_op_margin"]),
            pf(s.get("fcf_margin"),   f["min_fcf_margin"]),
            pf(s.get("rev_growth"),   f["min_rev_growth"]),
            pf(s.get("d_to_e"),       f["max_debt_to_equity"], "max"),
            pf(s.get("d_to_ebitda"),  f["max_debt_to_ebitda"], "max"),
            pf(s.get("curr_ratio"),   f["min_current_ratio"]),
            pf(s.get("int_cov"),      f["min_interest_cov"]),
        ]
        ps  = sum(1 for r in res if r == "✅")
        tot = sum(1 for r in res if r != "N/A")
        ov  = ("N/A" if tot == 0
               else f"✅ ALL {ps}/{tot}" if ps == tot
               else f"⚠️ {ps}/{tot}"    if ps >= tot * 0.7
               else f"❌ {ps}/{tot}")
        rows.append([s["ticker"], v(s.get("name")), v(s.get("cap_size")), v(s.get("sector")), ov]
                    + res + [v(s.get("data_quality", ""))])
    write_rows(ws, rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=(0.5, 0.15, 0.15))
    auto_width(ws)


def write_valuation(ws, stocks):
    h = [
        "Ticker", "Company", "Cap Size", "Sector", "Price ($)",
        # ── Core fair values, all 3 models ──
        "DCF Intrinsic ($)", "Comps Fair Value ($)", "EPV / Graham ($)",
        "DCF Upside (%)",    "Comps Upside (%)",     "EPV/Graham Upside (%)",
        "DCF Signal",        "Comps Signal",          "EPV/Graham Signal",
        "⭐ Combined Signal",
        # ── DCF detail ──
        "DCF MOS Buy Price ($)", "DCF WACC (%)", "DCF Growth Rate Used (%)",
        # ── Comps detail ──
        "Comps: P/E Est ($)", "Comps: EV/EBITDA Est ($)",
        "Comps: P/Sales Est ($)", "Comps: P/FCF Est ($)",
        "Peer Median P/E", "Peer Median EV/EBITDA", "Peer Count",
        # ── Model 3 detail ──
        "EPV ($)", "Graham Number ($)",
        # ── Momentum ──
        "RSI", "MA50/MA200", "Momentum", "1Y Return (%)",
        # ── Meta ──
        "Analyst Mean ($)", "Analyst Upside (%)", "Longevity", "Data Quality %", "Last Updated",
    ]
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [h,
            ["💡 Filter by Combined Signal — ⭐ STRONG BUY = all 3 models agree the stock is undervalued"]
            + [""] * (len(h) - 1)]
    for s in stocks:
        rows.append([
            s["ticker"], v(s.get("name")), v(s.get("cap_size")), v(s.get("sector")), v(s.get("price")),
            # Core
            v(s.get("dcf_intrinsic")),     v(s.get("comps_fair_value")),    v(s.get("m3_fair_value")),
            pct(s.get("dcf_upside_pct")),  pct(s.get("comps_upside_pct")),  pct(s.get("m3_upside_pct")),
            v(s.get("dcf_signal")),         v(s.get("comps_signal")),        v(s.get("m3_signal")),
            v(s.get("combined_signal")),
            # DCF detail
            v(s.get("dcf_mos_price")), v(s.get("dcf_wacc")), v(s.get("dcf_growth_rate")),
            # Comps detail
            v(s.get("comps_pe_est")),  v(s.get("comps_ev_est")),
            v(s.get("comps_ps_est")),  v(s.get("comps_pfcf_est")),
            v(s.get("peer_median_pe")), v(s.get("peer_median_eveb")), v(s.get("comps_peer_count")),
            # Model 3 detail
            v(s.get("epv_value")), v(s.get("graham_value")),
            # Momentum
            v(s.get("rsi")), v(s.get("ma_ratio")), v(s.get("mom_signal")), v(s.get("ret_1y")),
            # Meta
            v(s.get("analyst_mean")),
            _analyst_upside(s.get("price"), s.get("analyst_mean")),
            v(s.get("longevity_rank", "—")),
            v(s.get("data_quality", "")), now,
        ])
    write_rows(ws, rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=(0.20, 0.20, 0.45))
    hint_fill = _fill("F7F7F7")
    for c in range(1, len(h) + 1):
        cell = ws.cell(2, c)
        cell.font = Font(italic=True)
        cell.fill = hint_fill
    auto_width(ws)


def write_earnings(ws, stocks):
    h = ["Ticker","Company","Cap Size","Sector","Next Earnings",
         "EPS Estimate","EPS Actual (Last)","EPS Surprise %",
         "Revenue ($B)","Data Quality %","Last Updated"]
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = [h] + [
        [s["ticker"], v(s.get("name")), v(s.get("cap_size")), v(s.get("sector")),
         v(s.get("earn_date", "")), v(s.get("eps_est", "")),
         v(s.get("eps_actual", "")), v(s.get("eps_surp", "")),
         v(s.get("rev_now", "")), v(s.get("data_quality", "")), now]
        for s in stocks
    ]
    write_rows(ws, rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=(0.60, 0.38, 0.0))
    auto_width(ws)


def write_top_picks(ws, stocks):
    """
    Four-section leaderboard:
      S1 — Unified Leaderboard     : Rule #1 quality + all 3 model upsides (5Y CAGRs)
      S2 — Multi-Year Track Record : stocks hitting ≥10 % across 1Y / 3Y / 5Y / 10Y
      S3 — Best of Both Worlds     : ≥3 Rule #1 passes AND a BUY signal
    """
    # ─── shared helpers ─────────────────────────────────────────────────────
    N_COLS = 22

    def _sfx(x):
        try:
            f = float(x)
            return None if f != f else f
        except Exception:
            return None

    def _avg_upside(s):
        vals = [_sfx(s.get(k)) for k in
                ("dcf_upside_pct", "comps_upside_pct", "m3_upside_pct")]
        valid = [u for u in vals if u is not None]
        return sum(valid) / len(valid) if valid else None

    def _cfmt(val):
        f = _sfx(val)
        return f"{f:.1f}%" if f is not None else "—"

    GREEN = _fill("C6EFCE")
    AMBER = _fill("FFEB9C")
    RED   = _fill("FFC7CE")

    def _ccolor(val):
        f = _sfx(val)
        if f is None:  return RED
        if f >= 10.0:  return GREEN
        if f >=  5.0:  return AMBER
        return RED

    def sec_header(ws, row, label, color_tuple, n=N_COLS):
        fill = _fill(color_tuple)
        font = Font(bold=True, size=12, color="FFFFFF")
        ws.cell(row, 1).value = label
        ws.cell(row, 1).fill  = fill
        ws.cell(row, 1).font  = font
        try:
            ws.merge_cells(start_row=row, start_column=1,
                           end_row=row, end_column=n)
        except Exception:
            pass

    def col_hdr(ws, row, headers, color_tuple):
        fill = _fill(color_tuple)
        font = Font(bold=True, color="FFFFFF")
        for c_i, h in enumerate(headers, 1):
            cell = ws.cell(row, c_i)
            cell.value = h
            cell.fill  = fill
            cell.font  = font

    def write_colored(ws, start_row, rows_data, cagr_cols, stripe="EDF3FB"):
        """Write rows, CAGR-colour the specified 1-based column indices."""
        r = start_row
        for i, row in enumerate(rows_data):
            bg = _fill(stripe) if i % 2 == 0 else None
            for c_i, val in enumerate(row, 1):
                cell = ws.cell(r, c_i)
                cell.value = val if val is not None else ""
                if c_i in cagr_cols:
                    cell.fill = _ccolor(
                        _sfx(str(val).rstrip("%")) if val not in (None, "—", "") else None
                    )
                elif bg:
                    cell.fill = bg
            r += 1
        return r

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 1 COLUMNS — unified: Rule #1 Big Five (5Y CAGR) + 3 model upsides
    # ──────────────────────────────────────────────────────────────────────────
    S1_COLS = [
        "Rank","Ticker","Company","Sector","Cap","Price ($)",
        "R1 Passes","Yrs Data",
        "ROIC (5Y avg)","EPS CAGR 5Y","Rev CAGR 5Y","BVPS CAGR 5Y","FCF CAGR 5Y",
        "DCF Upside","Comps Upside","EPV/Graham Upside","Avg Upside",
        "Combined Signal","Analyst Mean ($)","Analyst Upside",
    ]
    S1_CAGR = [9,10,11,12,13]   # 1-based cols with CAGR colour

    def sort_key(s):
        passes = s.get("rule1_passes") or 0
        lng    = s.get("longevity_score") or 0
        avg_u  = _avg_upside(s) or 0
        return -(passes * 25 + lng * 4 + min(max(avg_u, -100), 200) * 0.5)

    def analyst_upside(s):
        try:
            am = float(s.get("analyst_mean") or 0)
            pr = float(s.get("price") or 0)
            return f"{(am-pr)/pr*100:.1f}%" if am and pr else "—"
        except Exception:
            return "—"

    def make_s1_row(rank, s):
        avg_u = _avg_upside(s)
        lng   = s.get("longevity_score") or 0
        return [
            rank, s.get("ticker",""), v(s.get("name")),
            v(s.get("sector")), v(s.get("cap_size")), v(s.get("price")),
            f"{s.get('rule1_passes',0)} / 5",
            f"{lng} / 5",
            _cfmt(s.get("roic_avg_5y") or s.get("roic")),
            _cfmt(s.get("eps_cagr_5y")),
            _cfmt(s.get("rev_cagr_5y")),
            _cfmt(s.get("bvps_cagr_5y")),
            _cfmt(s.get("fcf_cagr_5y")),
            pct(s.get("dcf_upside_pct")),
            pct(s.get("comps_upside_pct")),
            pct(s.get("m3_upside_pct")),
            f"{avg_u:.1f}%" if avg_u is not None else "—",
            v(s.get("combined_signal","—")),
            v(s.get("analyst_mean")),
            analyst_upside(s),
        ]

    s1_cands = [
        s for s in stocks
        if (s.get("rule1_passes") or 0) >= 1
        or "🟢" in (s.get("dcf_signal") or "")
        or "🟢" in (s.get("comps_signal") or "")
        or "🟢" in (s.get("m3_signal") or "")
    ]
    s1_rows = [make_s1_row(i+1, s)
               for i, s in enumerate(sorted(s1_cands, key=sort_key)[:75])]

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 2 COLUMNS — multi-year track record
    # Shows 1Y / 3Y / 5Y / 10Y for each Big Five metric.
    # A stock appears here only if it hits ≥10 % on BOTH 3Y AND 5Y
    # for at least 3 of the 5 metrics (sustained performance, not a fluke).
    # ──────────────────────────────────────────────────────────────────────────
    S2_COLS = [
        "Rank","Ticker","Company","Sector","Cap","Price ($)",
        "R1 Passes","Yrs Data",
        # EPS
        "EPS 1Y","EPS 3Y","EPS 5Y","EPS 10Y",
        # Revenue
        "Rev 1Y","Rev 3Y","Rev 5Y","Rev 10Y",
        # BVPS
        "BVPS 1Y","BVPS 3Y","BVPS 5Y","BVPS 10Y",
        # FCF
        "FCF 1Y","FCF 3Y","FCF 5Y","FCF 10Y",
        # ROIC (averages, not CAGRs)
        "ROIC 1Y","ROIC 3Y","ROIC 5Y","ROIC 10Y",
        "Combined Signal",
    ]
    S2_CAGR = list(range(9, 29))   # cols 9-28 are all CAGR / ROIC avg cells

    def _consistent_passes(s):
        """Count metrics that pass ≥10% on BOTH 3Y and 5Y periods."""
        count = 0
        pairs = [
            ("eps_cagr_3y",  "eps_cagr_5y"),
            ("rev_cagr_3y",  "rev_cagr_5y"),
            ("bvps_cagr_3y", "bvps_cagr_5y"),
            ("fcf_cagr_3y",  "fcf_cagr_5y"),
            ("roic_avg_3y",  "roic_avg_5y"),
        ]
        for k3, k5 in pairs:
            v3 = _sfx(s.get(k3))
            v5 = _sfx(s.get(k5))
            if v3 is not None and v5 is not None and v3 >= 10 and v5 >= 10:
                count += 1
        return count

    def make_s2_row(rank, s):
        lng = s.get("longevity_score") or 0
        return [
            rank, s.get("ticker",""), v(s.get("name")),
            v(s.get("sector")), v(s.get("cap_size")), v(s.get("price")),
            f"{s.get('rule1_passes',0)} / 5",
            f"{lng} / 5",
            _cfmt(s.get("eps_cagr_1y")),   _cfmt(s.get("eps_cagr_3y")),
            _cfmt(s.get("eps_cagr_5y")),   _cfmt(s.get("eps_cagr_10y")),
            _cfmt(s.get("rev_cagr_1y")),   _cfmt(s.get("rev_cagr_3y")),
            _cfmt(s.get("rev_cagr_5y")),   _cfmt(s.get("rev_cagr_10y")),
            _cfmt(s.get("bvps_cagr_1y")),  _cfmt(s.get("bvps_cagr_3y")),
            _cfmt(s.get("bvps_cagr_5y")),  _cfmt(s.get("bvps_cagr_10y")),
            _cfmt(s.get("fcf_cagr_1y")),   _cfmt(s.get("fcf_cagr_3y")),
            _cfmt(s.get("fcf_cagr_5y")),   _cfmt(s.get("fcf_cagr_10y")),
            _cfmt(s.get("roic_avg_1y")),   _cfmt(s.get("roic_avg_3y")),
            _cfmt(s.get("roic_avg_5y")),   _cfmt(s.get("roic_avg_10y")),
            v(s.get("combined_signal","—")),
        ]

    s2_cands = [s for s in stocks if _consistent_passes(s) >= 3]
    def s2_sort(s):
        return -(_consistent_passes(s) * 10 + (s.get("longevity_score") or 0))
    s2_rows = [make_s2_row(i+1, s)
               for i, s in enumerate(sorted(s2_cands, key=s2_sort)[:60])]

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 3 — Best of Both Worlds (same cols as S1)
    # ──────────────────────────────────────────────────────────────────────────
    s3_cands = [
        s for s in stocks
        if (s.get("rule1_passes") or 0) >= 3
        and "BUY" in (s.get("combined_signal") or "")
    ]
    s3_rows = [make_s1_row(i+1, s)
               for i, s in enumerate(sorted(s3_cands, key=sort_key)[:30])]

    # ──────────────────────────────────────────────────────────────────────────
    # WRITE TO WORKSHEET
    # ──────────────────────────────────────────────────────────────────────────
    cur = 1

    # Master title
    ws.cell(cur, 1).value = (
        f"⭐ TOP PICKS  |  Rule #1 Quality + Valuation + Multi-Year Track Record"
        f"  |  Generated: {now}"
    )
    ws.cell(cur, 1).font = Font(bold=True, size=13, color="FFFFFF")
    ws.cell(cur, 1).fill = _fill((0.06, 0.14, 0.30))
    try:
        ws.merge_cells(start_row=cur, start_column=1, end_row=cur, end_column=N_COLS)
    except Exception:
        pass
    cur += 1

    # Hint row
    hint = ("🟩 ≥10% (Rule #1 pass)  🟨 5–9%  🟥 <5% or N/A  |  "
            "Yrs Data = metrics with 5+ years of annual history  |  "
            "Multi-Year = must hit ≥10% on both 3Y AND 5Y for ≥3 of 5 metrics")
    ws.cell(cur, 1).value = hint
    ws.cell(cur, 1).font  = Font(italic=True, size=9, color="444444")
    try:
        ws.merge_cells(start_row=cur, start_column=1, end_row=cur, end_column=N_COLS)
    except Exception:
        pass
    cur += 2

    # ── S1 ────────────────────────────────────────────────────────────────────
    sec_header(ws, cur,
        "👑  UNIFIED LEADERBOARD  —  Rule #1 quality + all 3 model upsides  "
        "(sorted: R1 passes → longevity → avg upside)", (0.13, 0.28, 0.54))
    cur += 1
    col_hdr(ws, cur, S1_COLS, (0.22, 0.40, 0.68))
    cur += 1
    cur = write_colored(ws, cur, s1_rows, S1_CAGR, "EEF4FF")
    cur += 3

    # ── S2 ────────────────────────────────────────────────────────────────────
    sec_header(ws, cur,
        "📈  MULTI-YEAR TRACK RECORD  —  Sustained ≥10 % growth across 1Y / 3Y / 5Y / 10Y  "
        "(must hit ≥10% on 3Y AND 5Y for ≥3 of 5 metrics)", (0.10, 0.32, 0.18))
    cur += 1
    if s2_rows:
        col_hdr(ws, cur, S2_COLS, (0.16, 0.50, 0.28))
        cur += 1
        cur = write_colored(ws, cur, s2_rows, S2_CAGR, "EEFAF3")
    else:
        ws.cell(cur, 1).value = (
            "No stocks currently meet the sustained multi-year threshold. "
            "This will populate after the next full pipeline run with updated CAGRs."
        )
        ws.cell(cur, 1).font = Font(italic=True, color="777777")
        cur += 1
    cur += 3

    # ── S3 ────────────────────────────────────────────────────────────────────
    sec_header(ws, cur,
        "⭐  BEST OF BOTH WORLDS  —  Rule #1 passes ≥ 3  AND  model says BUY  "
        "(strongest quality + value combo)", (0.30, 0.13, 0.48))
    cur += 1
    if s3_rows:
        col_hdr(ws, cur, S1_COLS, (0.48, 0.24, 0.70))
        cur += 1
        write_colored(ws, cur, s3_rows, S1_CAGR, "F2EAFF")
    else:
        ws.cell(cur, 1).value = (
            "No stocks currently meet Rule #1 ≥3 passes AND a BUY signal."
        )
        ws.cell(cur, 1).font = Font(italic=True, color="777777")

    # ── Column widths ─────────────────────────────────────────────────────────
    ws.freeze_panes = "A4"
    widths = {
        "A": 6,  "B": 9,  "C": 26, "D": 18, "E": 9,  "F": 10,
        "G": 10, "H": 9,  "I": 13, "J": 13, "K": 13, "L": 13,
        "M": 11, "N": 11, "O": 11, "P": 11, "Q": 14, "R": 14,
        "S": 16, "T": 38, "U": 14, "V": 14,
    }
    for col, w in widths.items():
        ws.column_dimensions[col].width = w


def write_ideas(ws, stocks):
    h = ["Ticker","Company","Cap Size","Sector","Price ($)",
         "DCF Intrinsic","DCF MOS","Comps Fair Value","Combined Signal",
         "My Buy Zone Low","My Buy Zone High","My Price Target",
         "Status","Conviction (1-5)","Thesis / Notes","Date Added"]
    today = datetime.now().strftime("%Y-%m-%d")
    rows  = [h] + [
        [s["ticker"], v(s.get("name")), v(s.get("cap_size")), v(s.get("sector")),
         v(s.get("price")), v(s.get("dcf_intrinsic", "")), v(s.get("dcf_mos_price", "")),
         v(s.get("comps_fair_value", "")), v(s.get("combined_signal", "")),
         "", "", "", "Watching", "", "", today]
        for s in stocks
    ]
    write_rows(ws, rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=(0.35, 0.20, 0.55))
    auto_width(ws)


def write_watchlist(ws, name, tickers, stock_map):
    COLORS = {
        "Tech": (0.10, 0.35, 0.55), "AI & Semiconductors": (0.20, 0.20, 0.50),
        "Consumer & Retail": (0.55, 0.30, 0.10), "Healthcare & Biotech": (0.10, 0.45, 0.35),
        "Financials & Banking": (0.15, 0.40, 0.20), "Energy & Commodities": (0.45, 0.30, 0.05),
        "Industrials & Defense": (0.35, 0.35, 0.35), "Real Estate & Infrastructure": (0.30, 0.20, 0.45),
        "International & Emerging": (0.45, 0.15, 0.30), "Speculative & High Growth": (0.50, 0.20, 0.10),
    }
    h = ["Ticker","Company","Cap Size","Sector","Price ($)","1D Chg (%)",
         "Mkt Cap ($B)","P/E","Fwd P/E","EV/EBITDA",
         "ROE (%)","ROIC (%)","Rev Growth (%)","Gross Margin","Op Margin",
         "D/E","Analyst Mean","Analyst Upside (%)","Rec",
         "DCF Intrinsic","DCF MOS","DCF Signal",
         "Comps Fair Value","Comps Upside %","Comps Signal",
         "⭐ Combined Signal","Data Quality %"]
    rows = [h]
    for ticker in tickers:
        s = stock_map.get(ticker)
        if not s:
            rows.append([ticker] + ["N/A"] * (len(h) - 1)); continue
        rows.append([
            ticker, v(s.get("name")), v(s.get("cap_size")), v(s.get("sector")),
            v(s.get("price")), _price_chg(s.get("price"), s.get("prev_close")),
            v(s.get("mkt_cap")), v(s.get("pe")), v(s.get("fwd_pe")), v(s.get("ev_ebitda")),
            v(s.get("roe")), v(s.get("roic")), v(s.get("rev_growth")),
            v(s.get("gross_margin")), v(s.get("op_margin")), v(s.get("d_to_e")),
            v(s.get("analyst_mean")), _analyst_upside(s.get("price"), s.get("analyst_mean")),
            v(s.get("analyst_rec")),
            v(s.get("dcf_intrinsic", "")), v(s.get("dcf_mos_price", "")), v(s.get("dcf_signal", "")),
            v(s.get("comps_fair_value", "")), pct(s.get("comps_upside_pct", "")), v(s.get("comps_signal", "")),
            v(s.get("combined_signal", "")), v(s.get("data_quality", "")),
        ])
    write_rows(ws, rows)
    freeze_row1(ws)
    color_header(ws, len(h), color=COLORS.get(name, (0.25, 0.25, 0.25)))
    auto_width(ws)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading JSON files …")
    try:
        with open(FUNDAMENTALS_FILE) as f:
            fund = json.load(f)
        fund_map = {s["ticker"]: s for s in fund["stocks"]}
        print(f"  Fundamentals: {len(fund_map)} stocks")
    except FileNotFoundError:
        print(f"❌ {FUNDAMENTALS_FILE} not found"); return

    model_data = {"generated": "", "total": 0, "dcf_computed": 0, "comps_computed": 0, "stocks": []}
    try:
        with open(MODEL_FILE) as f:
            model_data = json.load(f)
        model_map = {s["ticker"]: s for s in model_data["stocks"]}
        print(f"  Model:        {len(model_map)} stocks  "
              f"(DCF: {model_data.get('dcf_computed',0)}, Comps: {model_data.get('comps_computed',0)})")
    except FileNotFoundError:
        model_map = {}
        print("⚠️  model.json missing")

    try:
        with open(UNIVERSE_FILE) as f:
            uni = json.load(f)
        uni_list = sorted(uni["stocks"], key=lambda x: x.get("rank", 9999))
        uni_map  = {s["ticker"]: s for s in uni_list}
        print(f"  Universe:     {len(uni_map)} stocks")
    except FileNotFoundError:
        uni_list, uni_map = [], {}
        print("⚠️  universe.json missing")

    # Merge fundamentals + model + universe
    stocks = list(fund_map.values())
    for s in stocks:
        for k, val in model_map.get(s["ticker"], {}).items():
            if val is not None: s[k] = val
        u = uni_map.get(s["ticker"], {})
        if u.get("cap_size"):  s["cap_size"] = u["cap_size"]
        if u.get("rank"):      s["rank"]     = u["rank"]
        if s.get("sector", "Unknown") == "Unknown" and u.get("sector", "Unknown") != "Unknown":
            s["sector"] = u["sector"]

    seen, deduped = set(), []
    for s in sorted(stocks, key=lambda x: x.get("rank", 9999)):
        if s["ticker"] not in seen:
            seen.add(s["ticker"])
            deduped.append(s)
    stocks     = deduped
    stock_map  = {s["ticker"]: s for s in stocks}
    watchlists = load_watchlists()

    print(f"\nBuilding Excel workbook …")
    wb = Workbook()
    wb.remove(wb.active)

    tabs = [
        ("📘 README",    lambda ws: write_readme(ws, model_data)),
        ("🌍 Universe",  lambda ws: write_universe(ws, uni_list)),
        ("👑 Rule #1",   lambda ws: write_rule1(ws, stocks)),
        ("📊 Dashboard", lambda ws: write_dashboard(ws, stocks)),
        ("📈 Scorecard", lambda ws: write_scorecard(ws, stocks)),
        ("🔍 Filter",    lambda ws: write_filter(ws, stocks)),
        ("🔮 Valuation", lambda ws: write_valuation(ws, stocks)),
        ("⭐ Top Picks", lambda ws: write_top_picks(ws, stocks)),
        ("📅 Earnings",  lambda ws: write_earnings(ws, stocks)),
    ]
    for title, fn in tabs:
        print(f"  Writing {title} …")
        ws = wb.create_sheet(title=title)
        fn(ws)

    for wl_name, wl_tickers in watchlists.items():
        print(f"  Writing 📋 {wl_name} …")
        ws = wb.create_sheet(title=f"📋 {wl_name}")
        write_watchlist(ws, wl_name, wl_tickers, stock_map)

    wb.save(OUTPUT_FILE)
    print(f"\n✅ Done → {OUTPUT_FILE}\n")


if __name__ == "__main__":
    main()
