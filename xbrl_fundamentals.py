"""
xbrl_fundamentals.py -- as-filed Magic Formula inputs from SEC companyfacts.

Trader-Screener #241 accepted the as-filed XBRL computation of `roc_greenblatt`
and `ebit_ev_yield` as the source of truth, with yfinance as the fallback. This
module is the reading side of that decision, ported into the pipeline as a
self-contained, stdlib-only unit. Wiring it into the fundamentals stage is a
separate issue.

It reads the seven canonical inputs Greenblatt's two metrics need directly from
a filer's SEC companyfacts JSON, restricted to the latest 10-K annual period
(as-filed), and reports which inputs were present, which were missing, and which
were provably derived under the #259 rules. A missing input is reported as
missing and left as `None` -- never a zero substituted for a number the filer did
not report.

Concept vocabulary and the two derivation rules are ported from Trader-Screener
`tools/edgar_scrubber/canonical_concepts.py` and `magic_inputs.py` at `bd8e53f`
(the #258 ppe alias and the #259 zero-current-debt / EBIT-fallback rules).

stdlib only. Run the self-check:  python xbrl_fundamentals.py
"""

import json
import os
import time
import urllib.request
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Canonical concepts
# ---------------------------------------------------------------------------
# The seven fields Greenblatt's ROC / earnings-yield consume, each mapped to the
# us-gaap concepts a filer might tag it under, MOST SPECIFIC / MOST RECENT FIRST
# -- that order is the collision priority (first concept that reports the period
# wins). Concept names are bare us-gaap tags, matching the keys under
# `facts["us-gaap"]` in a companyfacts document. Ported verbatim (order and the
# #258 ppe alias included) from Trader-Screener canonical_concepts.CANONICAL.
CANONICAL = {
    "operating_income": (
        "OperatingIncomeLoss",
    ),
    "assets_current": (
        "AssetsCurrent",
    ),
    "liabilities_current": (
        "LiabilitiesCurrent",
    ),
    "ppe_net": (
        "PropertyPlantAndEquipmentNet",
        "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization",
    ),
    "long_term_debt": (
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "LongTermDebtAndCapitalLeaseObligations",
        "LongTermNotesPayable",
    ),
    "short_term_debt": (
        "LongTermDebtCurrent",
        "ShortTermBorrowings",
        "DebtCurrent",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ),
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
}

# Concepts the two provable-derivation rules (#259) read directly, below the
# canonical-field layer. Each rule fires ONLY when the filer's own tags prove the
# value -- never a guessed zero.
_LONG_TERM_DEBT = "LongTermDebt"
_LONG_TERM_DEBT_NONCURRENT = "LongTermDebtNoncurrent"
_PRETAX_INCOME = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
    "ExtraordinaryItemsNoncontrollingInterest"
)
_INTEREST_NONOPERATING = "InterestExpenseNonoperating"
_NET_INTEREST_NONOPERATING = "InterestIncomeExpenseNonoperatingNet"

_ANNUAL_MIN_DAYS = 300
_ANNUAL_MAX_DAYS = 380


# ---------------------------------------------------------------------------
# companyfacts parsing
# ---------------------------------------------------------------------------

def _us_gaap(companyfacts):
    return (companyfacts.get("facts") or {}).get("us-gaap") or {}


def _entries(us_gaap, concept):
    """Every reported entry for a concept, flattened across units. Each entry is
    the raw companyfacts dict (`start`/`end`/`val`/`accn`/`form`/`filed`)."""
    node = us_gaap.get(concept)
    if not node:
        return []
    out = []
    for items in (node.get("units") or {}).values():
        out.extend(items)
    return out


def _is_annual(entry):
    """True for a duration entry ~one year long (an income / cash-flow line).
    Instant (balance-sheet) entries have no `start` and are never annual."""
    start, end = entry.get("start"), entry.get("end")
    if not start or not end:
        return False
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    return _ANNUAL_MIN_DAYS <= days <= _ANNUAL_MAX_DAYS


def latest_10k(companyfacts):
    """The filer's most recent 10-K: `{accession, filed, period_end}`, or `None`
    when the document has no 10-K facts. `period_end` is the fiscal-year end (the
    latest `end` reported under that accession)."""
    us_gaap = _us_gaap(companyfacts)
    best = None            # (filed, accession)
    ends = {}              # accession -> latest end reported under it
    for node in us_gaap.values():
        for items in (node.get("units") or {}).values():
            for it in items:
                if it.get("form") != "10-K":
                    continue
                accn, filed, end = it.get("accn"), it.get("filed") or "", it.get("end")
                if not accn:
                    continue
                if end:
                    ends[accn] = max(ends.get(accn, ""), end)
                if best is None or filed > best[0]:
                    best = (filed, accn)
    if best is None:
        return None
    filed, accn = best
    return {"accession": accn, "filed": filed, "period_end": ends.get(accn)}


def _value_at(us_gaap, concept, accession, period_end, *, annual=False):
    """The as-filed value of a single concept for the target period: an entry
    whose `accn` is the latest 10-K and whose `end` is that fiscal-year end.
    `annual=True` additionally requires a ~one-year duration (income lines)."""
    for it in _entries(us_gaap, concept):
        if it.get("accn") != accession or it.get("end") != period_end:
            continue
        if annual and not _is_annual(it):
            continue
        if it.get("val") is not None:
            return it["val"]
    return None


def _resolve_field(us_gaap, concepts, accession, period_end):
    """Resolve a canonical field to one value for the target period, honouring
    the concept-list priority (first concept that reports the period wins)."""
    for concept in concepts:
        val = _value_at(us_gaap, concept, accession, period_end)
        if val is not None:
            return val
    return None


def _apply_derivations(us_gaap, result, accession, period_end):
    """Apply the two provable-derivation rules (#259) to `result` in place and
    return the names of the rules that fired. Each fires ONLY when the filer's
    own tags prove the value -- never a guessed zero.

    * ``short_term_debt=0`` -- when `short_term_debt` resolved to nothing and the
      filer's `LongTermDebt` equals its `LongTermDebtNoncurrent` at the same
      `period_end` (total debt is entirely noncurrent, so the current portion is
      a provable zero).
    * ``ebit=pretax+interest`` -- when `operating_income` resolved to nothing,
      reconstruct EBIT as pre-tax income + nonoperating interest expense for the
      same annual period. Both inputs must be present, or the rule does not fire.
    * ``ebit=pretax-net_interest`` -- when neither `operating_income` nor
      `InterestExpenseNonoperating` is tagged but pre-tax income and
      `InterestIncomeExpenseNonoperatingNet` are present for the same annual
      period, reconstruct EBIT as pre-tax income minus net nonoperating interest
      (a positive net is interest *income*, so it is subtracted). The
      ``pretax+interest`` rule above keeps precedence.
    """
    derived = []

    if result.get("short_term_debt") is None:
        ltd = _value_at(us_gaap, _LONG_TERM_DEBT, accession, period_end)
        noncur = _value_at(us_gaap, _LONG_TERM_DEBT_NONCURRENT, accession, period_end)
        if ltd is not None and noncur is not None and ltd == noncur:
            result["short_term_debt"] = 0
            derived.append("short_term_debt=0")

    if result.get("operating_income") is None:
        pretax = _value_at(us_gaap, _PRETAX_INCOME, accession, period_end, annual=True)
        interest = _value_at(us_gaap, _INTEREST_NONOPERATING, accession, period_end,
                             annual=True)
        if pretax is not None and interest is not None:
            result["operating_income"] = pretax + interest
            derived.append("ebit=pretax+interest")
        elif pretax is not None:
            net = _value_at(us_gaap, _NET_INTEREST_NONOPERATING, accession, period_end,
                            annual=True)
            if net is not None:
                result["operating_income"] = pretax - net
                derived.append("ebit=pretax-net_interest")

    return derived


def inputs_from_companyfacts(companyfacts):
    """The seven canonical Magic Formula inputs for a filer's latest 10-K.

    `companyfacts` is a parsed SEC companyfacts document. Returns a dict with:

    * one key per field in `CANONICAL` -- its as-filed value for the latest 10-K
      annual period, or `None` when the filer did not tag it (and no #259 rule
      could derive it). A missing input is `None`, never a substituted zero.
    * `period_end`, `accession`, `filed` -- identifying that 10-K.
    * `derived` -- the #259 rule names that supplied an input the filer did not
      tag directly (empty when none fired).
    * `missing` -- the sorted names of every field left `None`.

    When the document has no 10-K facts, every field is `None`/missing and the
    three identifiers are `None`.
    """
    us_gaap = _us_gaap(companyfacts)
    result = {field: None for field in CANONICAL}
    result.update(period_end=None, accession=None, filed=None, derived=[], missing=[])

    tenk = latest_10k(companyfacts)
    if tenk is None:
        result["missing"] = sorted(CANONICAL)
        return result

    accession, period_end = tenk["accession"], tenk["period_end"]
    result.update(period_end=period_end, accession=accession, filed=tenk["filed"])

    for field, concepts in CANONICAL.items():
        result[field] = _resolve_field(us_gaap, concepts, accession, period_end)

    result["derived"] = _apply_derivations(us_gaap, result, accession, period_end)
    result["missing"] = sorted(f for f in CANONICAL if result.get(f) is None)
    return result


# ---------------------------------------------------------------------------
# SEC companyfacts fetch (cached, rate-limited)
# ---------------------------------------------------------------------------

_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
_MIN_INTERVAL = 0.1          # >= 0.1s between requests -> at most 10 req/s
_last_request = [0.0]        # monotonic timestamp of the last real request


def _data_dir():
    return os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))


def _cache_dir():
    d = Path(_data_dir()) / "xbrl_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_cik(cik):
    """Accept an int, a digit string, or a `CIK##########` string; return the
    bare integer CIK."""
    s = str(cik).strip().upper()
    if s.startswith("CIK"):
        s = s[3:]
    return int(s)


def _throttle():
    wait = _MIN_INTERVAL - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.monotonic()


def fetch_companyfacts(cik, *, opener=None):
    """Fetch and parse a filer's SEC companyfacts document, cached under
    `DATA_DIR/xbrl_cache/`.

    A `User-Agent` is required by SEC and read from the `SEC_USER_AGENT` env var;
    this raises before making any request when it is unset, and never sends a
    placeholder address. A previously-cached CIK is returned from disk with no
    network call. Live requests are throttled to at most 10 per second.

    `opener` (defaulting to `urllib.request.urlopen`) is injectable so callers
    and tests can supply a stub without touching the network.
    """
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua or not ua.strip():
        raise RuntimeError(
            "SEC_USER_AGENT is not set. SEC requires a real contact address "
            "(e.g. 'Your Name your@email.com') in the User-Agent; refusing to "
            "contact data.sec.gov without one."
        )

    cik_int = _normalize_cik(cik)
    path = _cache_dir() / f"CIK{cik_int:010d}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    req = urllib.request.Request(
        _COMPANYFACTS_URL.format(cik=cik_int),
        headers={"User-Agent": ua.strip()},
    )
    _throttle()
    open_url = opener or urllib.request.urlopen
    with open_url(req) as resp:
        raw = resp.read().decode("utf-8")
    path.write_text(raw, encoding="utf-8")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def _self_check():
    fixture = Path(__file__).with_name("fixtures") / "companyfacts_320193.json"
    if fixture.exists():
        data = json.loads(fixture.read_text(encoding="utf-8"))
        out = inputs_from_companyfacts(data)
        assert out["operating_income"] == 114301000000, out["operating_income"]
        assert out["accession"] == "0000320193-23-000106", out["accession"]
        assert out["filed"] == "2023-11-03", out["filed"]
        assert out["period_end"] == "2023-09-30", out["period_end"]
        # The curated fixture tags none of the balance-sheet inputs -> missing,
        # never zero.
        for field in ("assets_current", "cash", "short_term_debt"):
            assert out[field] is None and field in out["missing"], field
        assert out["short_term_debt"] != 0
    print("xbrl_fundamentals self-check: PASS")


if __name__ == "__main__":
    _self_check()
