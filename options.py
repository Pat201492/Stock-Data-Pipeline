"""
options.py — Options-chain feed
================================
For a watchlist / top-N subset of the universe, pulls option chains via
yfinance, computes per-contract Black-Scholes Greeks (yfinance gives IV but
not Greeks) and per-ticker summary metrics (ATM IV, put/call, total OI).

Part of the SHARED pipeline — collect once, served to every consumer app.

Risk-free rate: FRED 3-month T-bill (DGS3MO) when FRED_API_KEY is set, else a
sane constant. Dividend yield read from fundamentals.json when available.

Output:  options.json  { ticker: {summary, expirations:[...]} }
Caveat:  yfinance options are delayed snapshots; Greeks here use European
         Black-Scholes (approximation for American-style equity options).

Run:      python options.py
Env:      OPTIONS_TOP_N (default 25), OPTIONS_MAX_EXP (default 6)
Schedule: nightly, after fundamentals (see run.py)
"""

import json, os, math, warnings
from datetime import datetime, timezone
warnings.filterwarnings("ignore")

from yf_client import yf_ticker
from data_utils import run_batches

import config

OUT        = config.OPTIONS_JSON            # /data volume on Fly
UNIVERSE   = config.UNIVERSE_JSON
FUND       = config.FUNDAMENTALS_JSON

TOP_N    = config.OPTIONS_TOP_N
MAX_EXP  = config.OPTIONS_MAX_EXP
DEFAULT_RF = 0.04  # fallback risk-free if FRED unavailable


def _risk_free():
    try:
        import fred
        if fred.configured():
            obs = fred.latest_with_change("DGS3MO", limit=4)
            if obs and obs.get("value") is not None:
                return obs["value"] / 100.0
    except Exception:
        pass
    return DEFAULT_RF


def _f(x):
    """float, treating NaN/None/blank as 0.0 (pandas yields NaN for missing)."""
    try:
        v = float(x)
        return v if v == v else 0.0   # NaN != NaN
    except (TypeError, ValueError):
        return 0.0


def _i(x):
    return int(_f(x))


def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def black_scholes_greeks(S, K, T, r, sigma, q=0.0, is_call=True):
    """Return delta/gamma/theta/vega for a European option. T in years."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return {}
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    disc_q = math.exp(-q * T)
    disc_r = math.exp(-r * T)
    if is_call:
        delta = disc_q * _norm_cdf(d1)
        theta = (-S * disc_q * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
                 - r * K * disc_r * _norm_cdf(d2) + q * S * disc_q * _norm_cdf(d1))
    else:
        delta = -disc_q * _norm_cdf(-d1)
        theta = (-S * disc_q * _norm_pdf(d1) * sigma / (2 * math.sqrt(T))
                 + r * K * disc_r * _norm_cdf(-d2) - q * S * disc_q * _norm_cdf(-d1))
    gamma = disc_q * _norm_pdf(d1) / (S * sigma * math.sqrt(T))
    vega  = S * disc_q * _norm_pdf(d1) * math.sqrt(T)
    return {
        "delta": round(delta, 4),
        "gamma": round(gamma, 5),
        "theta": round(theta / 365, 4),   # per-day
        "vega":  round(vega / 100, 4),    # per 1 vol point
    }


def _load_watchlist():
    try:
        uni = json.load(open(UNIVERSE))
        stocks = uni if isinstance(uni, list) else uni.get("stocks", uni.get("universe", []))
        return [s["ticker"] for s in stocks[:TOP_N]]
    except Exception as e:
        print(f"  ⚠️  could not read {UNIVERSE}: {e}")
        return []


def _div_yields():
    try:
        fund = json.load(open(FUND))
        rows = fund if isinstance(fund, list) else fund.get("stocks", [])
        out = {}
        for r in rows:
            dy = r.get("div_yield") or r.get("dividend_yield")
            if dy is not None:
                out[r.get("ticker")] = (dy / 100.0) if dy > 1 else dy
        return out
    except Exception:
        return {}


def _years_to_exp(exp_str):
    exp = datetime.strptime(exp_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return max((exp - datetime.now(timezone.utc)).days, 0) / 365.0


def _process_ticker(sym, r, q):
    tk = yf_ticker(sym)
    spot = None
    try:
        fi = tk.fast_info            # FastInfo object, not a plain dict
        spot = fi.get("last_price") or fi.get("lastPrice")
    except Exception:
        spot = None
    if not spot:                     # fallback: last daily close
        try:
            h = tk.history(period="1d")
            if not h.empty:
                spot = float(h["Close"].iloc[-1])
        except Exception:
            spot = None
    expirations = list(getattr(tk, "options", []) or [])[:MAX_EXP]
    if not expirations or not spot:
        return None

    exp_out, all_ivs, call_oi, put_oi, call_vol, put_vol = [], [], 0, 0, 0, 0
    atm_iv = None
    atm_dist = None          # |strike - spot| of the current ATM pick (nearest expiry)
    for exp_idx, exp in enumerate(expirations):
        try:
            chain = tk.option_chain(exp)
        except Exception:
            continue
        T = _years_to_exp(exp)
        rows = []
        for is_call, df in ((True, chain.calls), (False, chain.puts)):
            for _, c in df.iterrows():
                iv = _f(c.get("impliedVolatility"))
                K  = _f(c.get("strike"))
                oi = _i(c.get("openInterest"))
                vol = _i(c.get("volume"))
                if is_call: call_oi += oi; call_vol += vol
                else:       put_oi += oi;  put_vol += vol
                # yfinance reports absurd IV on the wings; restrict iv_mean to
                # near-the-money strikes (±20% of spot) in a sane band so it
                # isn't polluted by deep-ITM/far-OTM garbage.
                if 0.01 < iv < 5.0 and abs(K - spot) <= 0.20 * spot:
                    all_ivs.append(iv)
                greeks = black_scholes_greeks(spot, K, T, r, iv, q, is_call) if iv > 0 else {}
                rows.append({
                    "type": "call" if is_call else "put",
                    "strike": K,
                    "bid": _f(c.get("bid")),
                    "ask": _f(c.get("ask")),
                    "last": _f(c.get("lastPrice")),
                    "volume": vol,
                    "open_interest": oi,
                    "iv": round(iv, 4),
                    "itm": bool(c.get("inTheMoney")),
                    **greeks,
                })
                # ATM IV = IV of the strike strictly NEAREST spot on the nearest
                # expiry, among sane-IV contracts (avoids junk near-zero IV that
                # a "first within 3%" pick would grab).
                if exp_idx == 0 and 0.01 < iv < 5.0:
                    d = abs(K - spot)
                    if atm_dist is None or d < atm_dist:
                        atm_dist = d
                        atm_iv = round(iv, 4)
        exp_out.append({"expiration": exp, "t_years": round(T, 4), "contracts": rows})

    summary = {
        "spot": round(spot, 2),
        "atm_iv": atm_iv,
        "iv_mean": round(sum(all_ivs) / len(all_ivs), 4) if all_ivs else None,
        "put_call_oi": round(put_oi / call_oi, 3) if call_oi else None,
        "put_call_vol": round(put_vol / call_vol, 3) if call_vol else None,
        "total_oi": call_oi + put_oi,
        "n_expirations": len(exp_out),
    }
    return {"summary": summary, "expirations": exp_out}


def main():
    with config.yfinance_lock():
        watch = _load_watchlist()
        if not watch:
            print("No watchlist tickers — run universe.py first."); return
        r = _risk_free()
        dy = _div_yields()
        print(f"Options feed — {len(watch)} tickers, ≤{MAX_EXP} expirations each, r={r:.3%}")

        out, asof = {}, datetime.utcnow().strftime("%Y-%m-%d")

        def fetch_batch(batch):
            """run_batches fetch_fn: process a batch of tickers, returning the usual
            (results, failed). A whole batch failing is the rate-limit signal that
            trips the shared backoff in run_batches — the same path fundamentals and
            model use."""
            results, failed = {}, []
            for sym in batch:
                try:
                    res = _process_ticker(sym, r, dy.get(sym, 0.0))
                except Exception as e:
                    print(f"  ⚠️  {sym}: {e}"); res = None
                if res:
                    res["asof"] = asof
                    results[sym] = res
                    s = res["summary"]
                    print(f"  {sym:<6} spot {s['spot']:>8}  ATM IV {s['atm_iv']}  P/C {s['put_call_oi']}")
                else:
                    failed.append(sym)
            return results, failed

        # run_batches owns pacing, the whole-batch backoff, and incremental saves to
        # OUT (the output is already {ticker: record}, so the cache IS the output).
        run_batches(watch, fetch_batch, out, OUT)

        with open(OUT, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\n✅ Wrote options for {len(out)} tickers → {OUT}")


if __name__ == "__main__":
    main()
