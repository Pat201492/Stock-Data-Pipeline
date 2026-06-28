"""
validate.py — data-integrity checks across stocks.db + politicians.db.

  python validate.py          # report only
  python validate.py --fix    # also apply the safe fixes

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

HERE = os.path.dirname(os.path.abspath(__file__))
POL_DB = os.environ.get("POL_DB_PATH", os.path.join(HERE, "politicians.db"))
STK_DB = os.environ.get("DB_PATH",     os.path.join(HERE, "stocks.db"))

LOGICAL = "ticker, transaction_date, insider_name, transaction_type, shares"


def _has_cols(con, table, cols):
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    return all(c in have for c in cols)


def report(fix=False):
    pol = sqlite3.connect(POL_DB)
    findings = {}

    # 1) insider duplicates (logical key)
    tot = pol.execute("SELECT COUNT(*) FROM insider_trades").fetchone()[0]
    distinct = pol.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM insider_trades GROUP BY {LOGICAL})"
    ).fetchone()[0]
    findings["insider_duplicates"] = tot - distinct

    # 2) orphan committee memberships
    findings["orphan_memberships"] = pol.execute("""
        SELECT COUNT(*) FROM committee_memberships m
        WHERE NOT EXISTS (SELECT 1 FROM committees c WHERE c.committee_id = m.committee_id)
    """).fetchone()[0]

    # 3) stale dashed subcommittee rows (old id scheme, no members)
    findings["stale_dashed_committees"] = pol.execute("""
        SELECT COUNT(*) FROM committees c
        WHERE c.committee_id LIKE '%-%'
          AND NOT EXISTS (SELECT 1 FROM committee_memberships m WHERE m.committee_id = c.committee_id)
    """).fetchone()[0]

    # 4) congressional trades with unknown bioguide (report only)
    findings["unknown_bioguide_trades"] = pol.execute("""
        SELECT COUNT(*) FROM congressional_trades t
        WHERE NOT EXISTS (SELECT 1 FROM politicians p WHERE p.bioguide_id = t.bioguide_id)
    """).fetchone()[0]

    # 5) extreme/invalid fundamentals (report only, stocks.db)
    fund = {}
    try:
        stk = sqlite3.connect(STK_DB)
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
    findings["fundamental_outliers"] = fund

    fixed = {}
    if fix:
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

    pol.close()
    return findings, fixed


def main():
    fix = "--fix" in sys.argv
    findings, fixed = report(fix=fix)

    print("=" * 52)
    print("  DATA INTEGRITY REPORT" + ("  (--fix applied)" if fix else ""))
    print("=" * 52)
    for k, v in findings.items():
        print(f"  {k:28} {v}")
    if fixed:
        print("-" * 52)
        for k, v in fixed.items():
            print(f"  {k:34} {v}")
    print("=" * 52)


if __name__ == "__main__":
    main()
