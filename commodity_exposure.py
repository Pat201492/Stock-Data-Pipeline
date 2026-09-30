"""
commodity_exposure.py — Per-stock commodity exposure
=====================================================
"What commodities drive this stock?" Two layers:

  1. CURATED  — hand-mapped, explainable economic links (refiners→crude+products,
     airlines→jet fuel, miners→metals, food→grains). Each link has a role
     (input cost / output revenue / margin) and a sign (how a commodity price
     RISE affects the stock: +1 helps, -1 hurts).

  2. COMPUTED — data-driven. Regress the stock's daily returns on each
     commodity's daily returns over ~1y → beta (sensitivity), correlation, R².
     Surfaces multi-commodity exposure (e.g. TSM) without hand-mapping.
     Off by default (needs yfinance); enable with EXPOSURE_COMPUTE=1.
     DO NOT run computed while the main pipeline is hitting yfinance — it will
     rate-limit. Run it standalone in a quiet window.

Output: exposure.json  { ticker: {curated:[...], computed:[...]} }
API:    GET /api/stocks/{ticker}/commodities

Run:      python commodity_exposure.py            # curated only (no network)
          EXPOSURE_COMPUTE=1 python commodity_exposure.py   # + regression betas
Schedule: nightly, after commodities.py (see run.py)
"""

import json, os, warnings
from datetime import datetime
warnings.filterwarnings("ignore")

import config

OUT        = config.EXPOSURE_JSON
UNIVERSE   = config.UNIVERSE_JSON

# Curated economic links. sign = effect on the STOCK of a commodity price RISE.
# role: input (cost), output (revenue), margin (spread-driven).
CURATED_EXPOSURE = {
    # Refiners — crude is the input, refined products the output; margin = crack spread
    "PSX":  [("CL=F","input",-1,"crude feedstock"), ("RB=F","output",+1,"gasoline"), ("HO=F","output",+1,"distillate")],
    "VLO":  [("CL=F","input",-1,"crude feedstock"), ("RB=F","output",+1,"gasoline"), ("HO=F","output",+1,"distillate")],
    "MPC":  [("CL=F","input",-1,"crude feedstock"), ("RB=F","output",+1,"gasoline"), ("HO=F","output",+1,"distillate")],
    # Integrated / E&P — crude & gas are revenue
    "XOM":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    "CVX":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    "COP":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    # Airlines — jet fuel (≈ heating oil / crude) is a top input cost
    "DAL":  [("HO=F","input",-1,"jet fuel"), ("CL=F","input",-1,"crude")],
    "UAL":  [("HO=F","input",-1,"jet fuel"), ("CL=F","input",-1,"crude")],
    "AAL":  [("HO=F","input",-1,"jet fuel"), ("CL=F","input",-1,"crude")],
    "LUV":  [("HO=F","input",-1,"jet fuel"), ("CL=F","input",-1,"crude")],
    # Miners — metals are output revenue
    "FCX":  [("HG=F","output",+1,"copper"), ("GC=F","output",+1,"gold")],
    "NEM":  [("GC=F","output",+1,"gold")],
    "GOLD": [("GC=F","output",+1,"gold")],
    "SCCO": [("HG=F","output",+1,"copper")],
    # Chemicals — nat gas / crude feedstock
    "DOW":  [("NG=F","input",-1,"ethane/nat gas feedstock"), ("CL=F","input",-1,"crude derivatives")],
    "LYB":  [("NG=F","input",-1,"nat gas feedstock"), ("CL=F","input",-1,"crude derivatives")],
    # Food / packaged — grains & softs input
    "GIS":  [("ZW=F","input",-1,"wheat"), ("ZC=F","input",-1,"corn"), ("SB=F","input",-1,"sugar")],
    "K":    [("ZC=F","input",-1,"corn"), ("ZW=F","input",-1,"wheat"), ("ZS=F","input",-1,"soybeans")],
    "ADM":  [("ZC=F","input",+1,"corn (processor margin)"), ("ZS=F","input",+1,"soybeans")],
    "TSN":  [("ZC=F","input",-1,"corn feed"), ("ZS=F","input",-1,"soymeal feed"), ("LE=F","output",+1,"cattle")],
    # Semis — multiple smaller input exposures (energy, copper, gold bonding wire)
    "TSM":  [("HG=F","input",-1,"copper interconnect"), ("GC=F","input",-1,"gold bonding wire"),
             ("NG=F","input",-1,"fab energy"), ("ALI=F","input",-1,"aluminum")],
    "INTC": [("HG=F","input",-1,"copper"), ("GC=F","input",-1,"gold"), ("NG=F","input",-1,"fab energy")],
    # Autos — steel/aluminum (aluminum only one on yfinance) + energy
    "F":    [("ALI=F","input",-1,"aluminum body"), ("HG=F","input",-1,"copper wiring")],
    "GM":   [("ALI=F","input",-1,"aluminum body"), ("HG=F","input",-1,"copper wiring")],
    # More E&P — crude & gas are revenue
    "EOG":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    "OXY":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    "DVN":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    "HES":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    "FANG": [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    "CTRA": [("NG=F","output",+1,"natural gas"), ("CL=F","output",+1,"crude")],
    "APA":  [("CL=F","output",+1,"crude"), ("NG=F","output",+1,"natural gas")],
    # More refiners
    "PBF":  [("CL=F","input",-1,"crude feedstock"), ("RB=F","output",+1,"gasoline"), ("HO=F","output",+1,"distillate")],
    "DK":   [("CL=F","input",-1,"crude feedstock"), ("RB=F","output",+1,"gasoline"), ("HO=F","output",+1,"distillate")],
    # Aluminum producer — output revenue
    "AA":   [("ALI=F","output",+1,"aluminum")],
    # Precious-metal miners — output revenue
    "AEM":  [("GC=F","output",+1,"gold")],
    "KGC":  [("GC=F","output",+1,"gold")],
    "PAAS": [("SI=F","output",+1,"silver"), ("GC=F","output",+1,"gold")],
    "AG":   [("SI=F","output",+1,"silver")],
    "HL":   [("SI=F","output",+1,"silver"), ("GC=F","output",+1,"gold")],
    "CDE":  [("SI=F","output",+1,"silver"), ("GC=F","output",+1,"gold")],
    "TECK": [("HG=F","output",+1,"copper")],
    # Fertilizer — nat gas is the dominant input cost; crop prices drive demand
    "CF":   [("NG=F","input",-1,"nat gas feedstock"), ("ZC=F","output",+1,"corn-driven demand")],
    "MOS":  [("NG=F","input",-1,"nat gas feedstock"), ("ZC=F","output",+1,"corn-driven demand")],
    "NTR":  [("NG=F","input",-1,"nat gas feedstock"), ("ZC=F","output",+1,"corn-driven demand")],
    # More chemicals — nat gas / crude feedstock
    "DD":   [("NG=F","input",-1,"nat gas feedstock")],
    "EMN":  [("NG=F","input",-1,"nat gas feedstock"), ("CL=F","input",-1,"crude derivatives")],
    "CE":   [("NG=F","input",-1,"nat gas feedstock")],
    "WLK":  [("NG=F","input",-1,"nat gas feedstock")],
    # Ags processors & packaged food
    "BG":   [("ZS=F","input",+1,"soy crush margin"), ("ZC=F","input",+1,"corn")],
    "INGR": [("ZC=F","input",-1,"corn input")],
    "HSY":  [("CC=F","input",-1,"cocoa"), ("SB=F","input",-1,"sugar")],
    "SJM":  [("KC=F","input",-1,"coffee")],
    "CAG":  [("ZC=F","input",-1,"corn"), ("ZW=F","input",-1,"wheat")],
    "CPB":  [("ZW=F","input",-1,"wheat")],
    # Beverages & packaging — sugar + aluminum cans
    "KO":   [("SB=F","input",-1,"sugar"), ("ALI=F","input",-1,"aluminum cans")],
    "PEP":  [("ZC=F","input",-1,"corn/sweetener"), ("ALI=F","input",-1,"aluminum cans")],
    "BALL": [("ALI=F","input",-1,"aluminum (can maker)")],
    # More airlines
    "ALK":  [("HO=F","input",-1,"jet fuel"), ("CL=F","input",-1,"crude")],
    "JBLU": [("HO=F","input",-1,"jet fuel"), ("CL=F","input",-1,"crude")],
}


def beta_corr(sr, cr):
    """Regression of stock returns sr on commodity returns cr (aligned lists).
    Returns {beta, corr, r2} or None if insufficient/degenerate."""
    n = min(len(sr), len(cr))
    if n < 20:
        return None
    sr, cr = sr[-n:], cr[-n:]
    ms = sum(sr) / n; mc = sum(cr) / n
    cov  = sum((sr[i] - ms) * (cr[i] - mc) for i in range(n)) / n
    varc = sum((cr[i] - mc) ** 2 for i in range(n)) / n
    vars_ = sum((sr[i] - ms) ** 2 for i in range(n)) / n
    if varc == 0 or vars_ == 0:
        return None
    beta = cov / varc
    corr = cov / ((varc ** 0.5) * (vars_ ** 0.5))
    return {"beta": round(beta, 3), "corr": round(corr, 3), "r2": round(corr * corr, 3)}


def _returns(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]


def _compute(tickers):
    """Download stock + commodity history via yfinance, regress returns.
    Heavy + rate-limit-prone — only when EXPOSURE_COMPUTE=1 and pipeline idle."""
    import yf_client
    from commodities import COMMODITIES
    roots = list(COMMODITIES.keys())
    comm = yf_client.yf_download(roots, period="1y", interval="1d", group_by="ticker",
                                 progress=False, threads=True)
    comm_ret = {}
    for r in roots:
        try:
            closes = [float(c) for c in comm[r]["Close"].dropna().tolist()]
            comm_ret[r] = _returns(closes)
        except Exception:
            continue

    out = {}
    for t in tickers:
        try:
            h = yf_client.yf_ticker(t).history(period="1y")
            sret = _returns([float(c) for c in h["Close"].dropna().tolist()])
        except Exception:
            continue
        scored = []
        for r, cret in comm_ret.items():
            bc = beta_corr(sret, cret)
            if bc and abs(bc["corr"]) >= 0.15:   # keep meaningful links only
                scored.append({"root": r, **bc})
        scored.sort(key=lambda x: -abs(x["corr"]))
        out[t] = scored[:6]
    return out


def main():
    compute = config.EXPOSURE_COMPUTE
    asof = datetime.utcnow().strftime("%Y-%m-%d")

    # curated always; computed optionally
    tickers = sorted(CURATED_EXPOSURE.keys())
    computed = _compute(tickers) if compute else {}

    out = {}
    for t in tickers:
        rec = {"asof": asof, "curated": [
            {"root": r, "role": role, "sign": sign, "note": note}
            for (r, role, sign, note) in CURATED_EXPOSURE[t]
        ]}
        if t in computed:
            rec["computed"] = computed[t]
        out[t] = rec

    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    mode = "curated + computed" if compute else "curated only"
    print(f"✅ Wrote exposure for {len(out)} tickers ({mode}) → {OUT}")


if __name__ == "__main__":
    main()
