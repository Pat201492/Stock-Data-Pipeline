"""
Offline checks for the Magic Formula fields: roc_greenblatt and ebit_ev_yield in
data_utils and their pass-through on /api/stocks (issue #14), plus the as-filed
XBRL preference with per-leg yfinance fallback and source provenance (issue #17).

No network: the XBRL companyfacts loader is patched with an in-memory document.

Run:  python test_magic_formula.py      (or: pytest test_magic_formula.py)
"""

import json, logging, os, sys, tempfile

from data_utils import roc_greenblatt, ebit_ev_yield


def test_roc_greenblatt_hand_computed():
    # 100 / ((300 - 100) + 50) = 40%
    assert round(roc_greenblatt(100.0, 300.0, 100.0, 50.0), 4) == 40.0


def test_ebit_ev_yield_hand_computed():
    # EV = 1000 + 250 - 100 = 1150 ; 100 / 1150 = 8.6957% (pct() rounds to 2dp)
    assert ebit_ev_yield(100.0, 1000.0, 250.0, 100.0) == round(100 / 1150 * 100, 2)


def test_missing_inputs_give_none_never_zero():
    assert roc_greenblatt(None, 300.0, 100.0, 50.0) is None
    assert roc_greenblatt(100.0, 300.0, 100.0, None) is None
    assert ebit_ev_yield(100.0, None, 250.0, 100.0) is None
    assert ebit_ev_yield(None, 1000.0, 250.0, 100.0) is None
    assert ebit_ev_yield(float("nan"), 1000.0, 250.0, 100.0) is None


def test_zero_denominator_gives_none():
    assert roc_greenblatt(100.0, 100.0, 150.0, 50.0) is None   # (100-150)+50 = 0
    assert ebit_ev_yield(100.0, 100.0, 0.0, 100.0) is None      # EV = 0


def test_api_stocks_rows_carry_both_fields():
    with tempfile.TemporaryDirectory() as d:
        def dump(name, rows):
            with open(os.path.join(d, name), "w") as f:
                json.dump({"stocks": rows}, f)
        dump("universe.json", [{"ticker": "AAA", "name": "A", "rank": 1},
                               {"ticker": "BBB", "name": "B", "rank": 2}])
        dump("fundamentals.json", [{"ticker": "AAA", "roic": 20.0,
                                    "roc_greenblatt": 40.0, "ebit_ev_yield": 8.7}])
        dump("model.json", [])
        os.environ["DATA_DIR"] = d
        sys.modules.pop("api", None)
        import api
        api.DATA_DIR = d
        body = api.list_stocks(sort="rank", order="asc", limit=100, offset=0,
                               sector=None, cap_size=None, search=None,
                               min_score=None, max_score=None)
    rows = body["stocks"] if isinstance(body, dict) else body
    by = {r["ticker"]: r for r in rows}
    assert by["AAA"]["roc_greenblatt"] == 40.0
    assert by["AAA"]["ebit_ev_yield"] == 8.7
    assert by["AAA"]["roic"] == 20.0            # unchanged, separate field
    assert by["BBB"]["roc_greenblatt"] is None  # missing -> null, not 0
    assert by["BBB"]["ebit_ev_yield"] is None


# ── issue #17: as-filed XBRL preference + provenance ────────────────────────────

def _companyfacts(include_ppe=True):
    """A minimal SEC companyfacts document with one 10-K carrying every Magic
    Formula input (drop net PP&E with include_ppe=False)."""
    def dur(val):   # annual duration line (income statement)
        return {"units": {"USD": [{"start": "2023-01-01", "end": "2023-12-31",
                                    "val": val, "accn": "ACC-1", "form": "10-K",
                                    "filed": "2024-01-01"}]}}

    def inst(val):  # instant line (balance sheet)
        return {"units": {"USD": [{"end": "2023-12-31", "val": val,
                                   "accn": "ACC-1", "form": "10-K",
                                   "filed": "2024-01-01"}]}}

    gaap = {
        "OperatingIncomeLoss": dur(100),
        "AssetsCurrent": inst(300),
        "LiabilitiesCurrent": inst(100),
        "LongTermDebtNoncurrent": inst(200),
        "LongTermDebtCurrent": inst(50),
        "CashAndCashEquivalentsAtCarryingValue": inst(100),
    }
    if include_ppe:
        gaap["PropertyPlantAndEquipmentNet"] = inst(50)
    return {"facts": {"us-gaap": gaap}}


def _fresh_fundamentals(sec_user_agent):
    """Import fundamentals with a known SEC_USER_AGENT state and per-run flags
    reset, so the one-warning-per-run behaviour is deterministic."""
    import fundamentals
    if sec_user_agent is None:
        os.environ.pop("SEC_USER_AGENT", None)
    else:
        os.environ["SEC_USER_AGENT"] = sec_user_agent
    fundamentals._xbrl_warned = False
    fundamentals._cik_map = None
    return fundamentals


def test_xbrl_complete_row_is_pure_xbrl():
    f = _fresh_fundamentals("Tester tester@example.com")
    f._companyfacts_for_ticker = lambda t: _companyfacts(include_ppe=True)
    row = f._magic_formula("AAPL", mkt_cap=1000.0, yf_roc=None, yf_eey=None)
    assert row["roc_greenblatt"] == roc_greenblatt(100, 300, 100, 50)   # 40.0
    assert row["ebit_ev_yield"] == ebit_ev_yield(100, 1000, 250, 100)   # EV=1150
    assert row["magic_source"] == "xbrl"
    assert row["magic_period_end"] == "2023-12-31"
    assert row["magic_accession"] == "ACC-1"


def test_missing_xbrl_ppe_falls_back_to_yfinance_for_roc_only():
    f = _fresh_fundamentals("Tester tester@example.com")
    f._companyfacts_for_ticker = lambda t: _companyfacts(include_ppe=False)
    row = f._magic_formula("AAPL", mkt_cap=1000.0, yf_roc=33.3, yf_eey=None)
    assert row["roc_greenblatt"] == 33.3                 # yfinance fallback leg
    assert row["ebit_ev_yield"] == ebit_ev_yield(100, 1000, 250, 100)  # still XBRL
    assert row["magic_source"] == "mixed"


def test_no_user_agent_falls_back_and_warns_once():
    f = _fresh_fundamentals(None)
    # Should never touch the loader when SEC_USER_AGENT is unset.
    def _boom(t):
        raise AssertionError("companyfacts loader must not run without SEC_USER_AGENT")
    f._companyfacts_for_ticker = _boom

    handler = logging.Handler()
    records = []
    handler.emit = records.append
    f.log.addHandler(handler)
    try:
        r1 = f._magic_formula("AAA", mkt_cap=1000.0, yf_roc=1.0, yf_eey=2.0)
        r2 = f._magic_formula("BBB", mkt_cap=1000.0, yf_roc=3.0, yf_eey=4.0)
    finally:
        f.log.removeHandler(handler)

    for r in (r1, r2):
        assert r["magic_source"] == "yfinance"
        assert r["magic_period_end"] is None
        assert r["magic_accession"] is None
    assert r1["roc_greenblatt"] == 1.0 and r1["ebit_ev_yield"] == 2.0
    warnings = [rec for rec in records if rec.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"


def test_api_stocks_rows_carry_magic_provenance():
    with tempfile.TemporaryDirectory() as d:
        def dump(name, rows):
            with open(os.path.join(d, name), "w") as fh:
                json.dump({"stocks": rows}, fh)
        dump("universe.json", [{"ticker": "AAA", "name": "A", "rank": 1}])
        dump("fundamentals.json", [{"ticker": "AAA", "roc_greenblatt": 40.0,
                                    "ebit_ev_yield": 8.7, "magic_source": "xbrl",
                                    "magic_period_end": "2023-12-31",
                                    "magic_accession": "ACC-1",
                                    "magic_derived": "short_term_debt=0"}])
        dump("model.json", [])
        os.environ["DATA_DIR"] = d
        sys.modules.pop("api", None)
        import api
        api.DATA_DIR = d
        body = api.list_stocks(sort="rank", order="asc", limit=100, offset=0,
                               sector=None, cap_size=None, search=None,
                               min_score=None, max_score=None)
    row = (body["stocks"] if isinstance(body, dict) else body)[0]
    assert row["magic_source"] == "xbrl"
    assert row["magic_period_end"] == "2023-12-31"
    assert row["magic_accession"] == "ACC-1"
    assert row["magic_derived"] == "short_term_debt=0"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  [ok] {t.__name__}")
    print("magic formula fields: PASS")
