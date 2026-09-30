"""
Offline checks for the Magic Formula fields (issue #14): roc_greenblatt and
ebit_ev_yield in data_utils, and their pass-through on /api/stocks.

Run:  python test_magic_formula.py      (or: pytest test_magic_formula.py)
"""

import json, os, sys, tempfile

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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  [ok] {t.__name__}")
    print("magic formula fields: PASS")
