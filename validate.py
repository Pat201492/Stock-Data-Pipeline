"""
validate.py — data-integrity checks across stocks.db + politicians.db.

  python validate.py            # report only, always exits 0
  python validate.py --strict   # exit 1 if any finding exceeds its threshold
  python validate.py --fix      # also apply the safe fixes

Database paths come from `config` (config.DB_PATH / config.POL_DB_PATH), so this
checks the exact databases the pipeline writes (issue #10) rather than guessing
from os.environ / __file__.

Each finding has an allowed threshold in THRESHOLDS (default 0), overridable per
finding by env VALIDATE_MAX_<NAME> (e.g. VALIDATE_MAX_FUNDAMENTAL_OUTLIERS=5).
Under --strict the process exits 1 when any finding's count exceeds its
threshold and names the offending findings.

Safe fixes (--fix):
  - collapse duplicate insider trades (same logical trade ingested by both the
    EDGAR and the daily-mirror sources under different hash ids); keeps the
    EDGAR/non-mirror row.
  - delete stale dashed subcommittee rows in `committees` (old id scheme,
    zero members) and any orphan committee_memberships.
Report-only (never auto-changed): congressional trades with an unknown
bioguide, and extreme/invalid fundamentals.
"""
import os, sqlite3, sys

import config

LOGICAL = "ticker, transaction_date, insider_name, transaction_type, shares"

# One entry per finding: the allowed count before --strict fails, or None for
# REPORT-ONLY (printed, never gating -- the module docstring's contract for
# unknown-bioguide trades and fundamental outliers). Gating fundamentals at 0
# failed on legitimate data: negative equity is normal for buyback-heavy filers
# (MCD on 2026-10-01). Env VALIDATE_MAX_<NAME> sets a numeric allowance, which
# also opts a report-only finding into gating.
THRESHOLDS = {
    "insider_duplicates":      0,
    "orphan_memberships":      0,
    "stale_dashed_committees": 0,
    "unknown_bioguide_trades": None,
    "fundamental_outliers":    None,
}


def threshold(name):
    """Allowed count for a finding: env VALIDATE_MAX_<NAME> overrides the default."""
    env = os.environ.get(f"VALIDATE_MAX_{name.upper()}")
    if env is not None and env != "":
        return int(env)
    return THRESHOLDS[name]


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _has_cols(con, table, cols):
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    return all(c in have for c in cols)


def report(fix=False):
    """Return (findings, fund_detail, fixed).

    `findings` maps each THRESHOLDS name to an integer count. `fund_detail`
    breaks fundamental_outliers down by rule for the printed report.
    """
    # A check whose tables do not exist yet is SKIPPED (None), not a crash. On a
    # fresh volume the market pipeline runs (and used to validate) before the
    # political refresh has ever created its tables, so the first nightly run
    # died on "no such table" -- a database that is empty is not a database
    # that is wrong. Read-only so validating never creates politicians.db.
    findings = {}
    pol = None
    if os.path.exists(config.POL_DB_PATH):
        pol = sqlite3.connect(f"file:{config.POL_DB_PATH}?mode=ro", uri=True) if not fix             else sqlite3.connect(config.POL_DB_PATH)
    have = _tables(pol) if pol else set()

    def _count(name, needs, sql):
        findings[name] = pol.execute(sql).fetchone()[0] if needs <= have else None

    # 1) insider duplicates (logical key)
    _count("insider_duplicates", {"insider_trades"}, f"""
        SELECT (SELECT COUNT(*) FROM insider_trades)
             - (SELECT COUNT(*) FROM (SELECT 1 FROM insider_trades GROUP BY {LOGICAL}))
    """)

    # 2) orphan committee memberships
    _count("orphan_memberships", {"committee_memberships", "committees"}, """
        SELECT COUNT(*) FROM committee_memberships m
        WHERE NOT EXISTS (SELECT 1 FROM committees c WHERE c.committee_id = m.committee_id)
    """)

    # 3) stale dashed subcommittee rows (old id scheme, no members)
    _count("stale_dashed_committees", {"committees", "committee_memberships"}, """
        SELECT COUNT(*) FROM committees c
        WHERE c.committee_id LIKE '%-%'
          AND NOT EXISTS (SELECT 1 FROM committee_memberships m WHERE m.committee_id = c.committee_id)
    """)

    # 4) congressional trades with unknown bioguide (report only)
    _count("unknown_bioguide_trades", {"congressional_trades", "politicians"}, """
        SELECT COUNT(*) FROM congressional_trades t
        WHERE NOT EXISTS (SELECT 1 FROM politicians p WHERE p.bioguide_id = t.bioguide_id)
    """)

    # 5) extreme/invalid fundamentals (report only, stocks.db)
    fund = {}
    try:
        stk = sqlite3.connect(config.DB_PATH)
        if _has_cols(stk, "fundamentals", ["pe"]):
            fund["pe_gt_1000"]   = stk.execute("SELECT COUNT(*) FROM fundamentals WHERE pe > 1000").fetchone()[0]
        if _has_cols(stk, "fundamentals", ["roic"]):
            fund["roic_gt_100"]  = stk.execute("SELECT COUNT(*) FROM fundamentals WHERE roic > 100").fetchone()[0]
        if _has_cols(stk, "fundamentals", ["equity"]):
            fund["neg_equity"]   = stk.execute("SELECT COUNT(*) FROM fundamentals WHERE equity < 0").fetchone()[0]
        if _has_cols(stk, "stocks", ["price"]):
            fund["price_le_0"]   = stk.execute("SELECT COUNT(*) FROM stocks WHERE price <= 0").fetchone()[0]
        stk.close()
    except Exception as e:
        fund["error"] = str(e)
    # Collapse to a single count for thresholding; keep the breakdown for the report.
    findings["fundamental_outliers"] = sum(v for v in fund.values() if isinstance(v, int))

    fixed = {}
    if fix and pol is not None and {"insider_trades", "committees", "committee_memberships"} <= have:
        # dedupe insider — keep non-mirror (EDGAR) row per logical key
        cur = pol.execute(f"""
            DELETE FROM insider_trades WHERE rowid NOT IN (
                SELECT rowid FROM (
                    SELECT rowid, ROW_NUMBER() OVER (
                        PARTITION BY {LOGICAL}
                        ORDER BY (source_url LIKE 'mirror:%') ASC, filing_id
                    ) rn FROM insider_trades
                ) WHERE rn = 1
            )
        """)
        fixed["insider_duplicates_removed"] = cur.rowcount
        # delete orphan memberships
        cur = pol.execute("""
            DELETE FROM committee_memberships
            WHERE committee_id NOT IN (SELECT committee_id FROM committees)
        """)
        fixed["orphan_memberships_removed"] = cur.rowcount
        # delete stale dashed committee rows with no members
        cur = pol.execute("""
            DELETE FROM committees WHERE committee_id LIKE '%-%'
              AND committee_id NOT IN (SELECT committee_id FROM committee_memberships)
        """)
        fixed["stale_dashed_committees_removed"] = cur.rowcount
        pol.commit()

    if pol is not None:
        pol.close()
    return findings, fund, fixed


def breaches(findings):
    """Findings whose count exceeds its (env-overridable) threshold."""
    return {k: (v, threshold(k)) for k, v in findings.items()
            if v is not None and threshold(k) is not None and v > threshold(k)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    fix    = "--fix" in argv
    strict = "--strict" in argv
    findings, fund, fixed = report(fix=fix)

    print("=" * 52)
    print("  DATA INTEGRITY REPORT" + ("  (--fix applied)" if fix else ""))
    print("=" * 52)
    for k, v in findings.items():
        extra = ""
        if k == "fundamental_outliers" and fund:
            extra = "  " + ", ".join(f"{fk}={fv}" for fk, fv in fund.items())
        tag = "" if v is None or threshold(k) is not None else "  (report-only)"
        print(f"  {k:28} {'skipped (table not created yet)' if v is None else v}{tag}{extra}")
    if fixed:
        print("-" * 52)
        for k, v in fixed.items():
            print(f"  {k:34} {v}")
    print("=" * 52)

    bad = breaches(findings)
    if strict and bad:
        print("FAIL: findings exceed threshold:")
        for k, (v, thr) in bad.items():
            print(f"  {k:28} {v} > {thr}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
