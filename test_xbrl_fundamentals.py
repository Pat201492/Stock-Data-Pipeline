"""
Offline checks for xbrl_fundamentals (issue #16): as-filed Magic Formula inputs
from SEC companyfacts, the #258 ppe alias, the two #259 derivation rules, and the
cached / User-Agent-guarded fetch. No network.

Run:  python test_xbrl_fundamentals.py      (or: pytest test_xbrl_fundamentals.py)
"""

import json
import os
import tempfile

import xbrl_fundamentals as xf

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "companyfacts_320193.json")


def _load_fixture():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _cf(us_gaap):
    """Wrap a bare us-gaap concept map in a companyfacts envelope."""
    return {"cik": 1, "entityName": "Synthetic", "facts": {"us-gaap": us_gaap}}


def _annual(concept, start, end, val, accn, filed, form="10-K"):
    return {concept: {"units": {"USD": [
        {"start": start, "end": end, "val": val, "accn": accn, "fp": "FY",
         "form": form, "filed": filed}]}}}


def _instant(concept, end, val, accn, filed, form="10-K"):
    return {concept: {"units": {"USD": [
        {"end": end, "val": val, "accn": accn, "fp": "FY", "form": form,
         "filed": filed}]}}}


# ── Apple fixture ───────────────────────────────────────────────────────────────
def test_apple_operating_income_and_latest_10k():
    out = xf.inputs_from_companyfacts(_load_fixture())
    assert out["operating_income"] == 114301000000
    assert out["accession"] == "0000320193-23-000106"
    assert out["filed"] == "2023-11-03"
    assert out["period_end"] == "2023-09-30"


def test_apple_untagged_fields_are_missing_never_zero():
    out = xf.inputs_from_companyfacts(_load_fixture())
    for field in ("assets_current", "liabilities_current", "ppe_net",
                  "long_term_debt", "short_term_debt", "cash"):
        assert out[field] is None, field
        assert field in out["missing"], field
    assert out["short_term_debt"] != 0        # never a substituted zero
    assert out["derived"] == []               # nothing derived here


# ── #258 ppe alias ───────────────────────────────────────────────────────────────
def test_ppe_finance_lease_alias_resolves_ppe_net():
    accn = "0000000001-23-000001"
    us_gaap = _instant(
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
        "2023-12-31", 42000, accn, "2024-02-01")
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["ppe_net"] == 42000
    assert "ppe_net" not in out["missing"]


# ── #259 zero-current-debt rule ─────────────────────────────────────────────────
def test_zero_current_debt_fires_when_total_equals_noncurrent():
    accn = "0000000002-23-000001"
    us_gaap = {}
    us_gaap.update(_instant("LongTermDebt", "2023-12-31", 5000, accn, "2024-02-01"))
    us_gaap.update(_instant("LongTermDebtNoncurrent", "2023-12-31", 5000, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["short_term_debt"] == 0
    assert "short_term_debt=0" in out["derived"]
    assert "short_term_debt" not in out["missing"]


def test_zero_current_debt_does_not_fire_when_amounts_differ():
    accn = "0000000003-23-000001"
    us_gaap = {}
    us_gaap.update(_instant("LongTermDebt", "2023-12-31", 6000, accn, "2024-02-01"))
    us_gaap.update(_instant("LongTermDebtNoncurrent", "2023-12-31", 5000, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["short_term_debt"] is None
    assert out["short_term_debt"] != 0
    assert "short_term_debt=0" not in out["derived"]
    assert "short_term_debt" in out["missing"]


# ── #259 EBIT fallback rule ─────────────────────────────────────────────────────
def test_ebit_fallback_fires_when_operating_income_absent():
    accn = "0000000004-23-000001"
    us_gaap = {}
    us_gaap.update(_annual(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "2023-01-01", "2023-12-31", 90000, accn, "2024-02-01"))
    us_gaap.update(_annual("InterestExpenseNonoperating",
                           "2023-01-01", "2023-12-31", 3000, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["operating_income"] == 93000
    assert "ebit=pretax+interest" in out["derived"]
    assert "operating_income" not in out["missing"]


def test_ebit_fallback_does_not_fire_without_interest():
    accn = "0000000005-23-000001"
    us_gaap = _annual(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "2023-01-01", "2023-12-31", 90000, accn, "2024-02-01")
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["operating_income"] is None
    assert "ebit=pretax+interest" not in out["derived"]
    assert "operating_income" in out["missing"]


def test_operating_income_present_skips_ebit_fallback():
    accn = "0000000006-23-000001"
    us_gaap = {}
    us_gaap.update(_annual("OperatingIncomeLoss",
                           "2023-01-01", "2023-12-31", 100000, accn, "2024-02-01"))
    us_gaap.update(_annual(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "2023-01-01", "2023-12-31", 90000, accn, "2024-02-01"))
    us_gaap.update(_annual("InterestExpenseNonoperating",
                           "2023-01-01", "2023-12-31", 3000, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["operating_income"] == 100000     # tagged value, not the fallback
    assert out["derived"] == []


# ── #29 net-interest EBIT fallback (NKE) ────────────────────────────────────────
def test_ebit_net_interest_fallback_subtracts_positive_net():
    # NKE: no OperatingIncomeLoss, no InterestExpenseNonoperating, only a positive
    # net (interest income) -> EBIT = pretax - net.
    accn = "0000000007-23-000001"
    us_gaap = {}
    us_gaap.update(_annual(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "2023-01-01", "2023-12-31", 3.90e9, accn, "2024-02-01"))
    us_gaap.update(_annual("InterestIncomeExpenseNonoperatingNet",
                           "2023-01-01", "2023-12-31", 0.05e9, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["operating_income"] == 3.85e9
    assert "ebit=pretax-net_interest" in out["derived"]
    assert "operating_income" not in out["missing"]


def test_ebit_net_interest_fallback_adds_back_negative_net():
    # A negative net (net interest expense) adds back: EBIT = pretax - (-0.40e9).
    accn = "0000000008-23-000001"
    us_gaap = {}
    us_gaap.update(_annual(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "2023-01-01", "2023-12-31", 3.00e9, accn, "2024-02-01"))
    us_gaap.update(_annual("InterestIncomeExpenseNonoperatingNet",
                           "2023-01-01", "2023-12-31", -0.40e9, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["operating_income"] == 3.40e9
    assert "ebit=pretax-net_interest" in out["derived"]


def test_interest_expense_present_uses_pretax_plus_interest_not_net():
    # When InterestExpenseNonoperating is present, the existing pretax+interest
    # rule wins even if a net is also tagged.
    accn = "0000000009-23-000001"
    us_gaap = {}
    us_gaap.update(_annual(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "2023-01-01", "2023-12-31", 3.90e9, accn, "2024-02-01"))
    us_gaap.update(_annual("InterestExpenseNonoperating",
                           "2023-01-01", "2023-12-31", 0.10e9, accn, "2024-02-01"))
    us_gaap.update(_annual("InterestIncomeExpenseNonoperatingNet",
                           "2023-01-01", "2023-12-31", 0.05e9, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["operating_income"] == 4.00e9
    assert "ebit=pretax+interest" in out["derived"]
    assert "ebit=pretax-net_interest" not in out["derived"]


# ── #29 LongTermNotesPayable alias (ORCL) ───────────────────────────────────────
def test_long_term_notes_payable_resolves_long_term_debt():
    accn = "0000000010-23-000001"
    us_gaap = _instant("LongTermNotesPayable", "2023-12-31", 122.34e9, accn, "2024-02-01")
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["long_term_debt"] == 122.34e9
    assert "long_term_debt" not in out["missing"]


def test_long_term_debt_noncurrent_wins_over_notes_payable():
    accn = "0000000011-23-000001"
    us_gaap = {}
    us_gaap.update(_instant("LongTermDebtNoncurrent", "2023-12-31", 100.0e9, accn, "2024-02-01"))
    us_gaap.update(_instant("LongTermNotesPayable", "2023-12-31", 122.34e9, accn, "2024-02-01"))
    out = xf.inputs_from_companyfacts(_cf(us_gaap))
    assert out["long_term_debt"] == 100.0e9      # priority: Noncurrent first


# ── fetch_companyfacts ───────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, raw):
        self._raw = raw
    def read(self):
        return self._raw
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_fetch_raises_without_user_agent():
    calls = []

    def opener(req):
        calls.append(req)
        return _Resp(b"{}")

    old = os.environ.pop("SEC_USER_AGENT", None)
    try:
        raised = False
        try:
            xf.fetch_companyfacts(320193, opener=opener)
        except RuntimeError:
            raised = True
        assert raised, "expected RuntimeError when SEC_USER_AGENT is unset"
        assert calls == [], "must not make a request without a User-Agent"
    finally:
        if old is not None:
            os.environ["SEC_USER_AGENT"] = old


def test_second_fetch_of_cached_cik_makes_no_network_call():
    raw = json.dumps(_load_fixture()).encode("utf-8")
    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_DIR"] = d
        os.environ["SEC_USER_AGENT"] = "Tester tester@example.com"
        try:
            hits = []

            def opener_ok(req):
                hits.append(req.full_url)
                return _Resp(raw)

            first = xf.fetch_companyfacts(320193, opener=opener_ok)
            assert first["cik"] == 320193
            assert len(hits) == 1

            def opener_boom(req):
                raise AssertionError("cached CIK must not hit the network")

            second = xf.fetch_companyfacts(320193, opener=opener_boom)
            assert second["cik"] == 320193
            assert len(hits) == 1                # still one -- served from cache
        finally:
            os.environ.pop("DATA_DIR", None)
            os.environ.pop("SEC_USER_AGENT", None)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  [ok] {t.__name__}")
    print("xbrl_fundamentals: PASS")
