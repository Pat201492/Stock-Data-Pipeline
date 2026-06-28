"""
pol_refresh.py — Daily orchestrator for political + insider trade ingestion
Usage:
  python pol_refresh.py            # incremental (default: committees + house + insider_mirror)
  python pol_refresh.py --full     # full refresh from scratch
  python pol_refresh.py --house    # House Clerk official PTRs only
  python pol_refresh.py --senate-efd # Senate efdsearch official PTRs only
  python pol_refresh.py --edgar    # insider trades only
  python pol_refresh.py --committees # committee data only
  python pol_refresh.py --congress # DEPRECATED legacy peez49 CSV (frozen 2026-02)
  python pol_refresh.py --senate   # DEPRECATED legacy senate mirror (dead 2020)
"""
import sys, time
from datetime import datetime

FULL     = "--full"      in sys.argv
HOUSE    = "--house"     in sys.argv
SENATE_EFD = "--senate-efd" in sys.argv
SENATE   = "--senate"    in sys.argv   # legacy mirror (dead) — explicit only
CONGRESS = "--congress"  in sys.argv   # legacy peez49 (frozen) — explicit only
EDGAR    = "--edgar"     in sys.argv
MIRROR   = "--mirror"    in sys.argv
COMS     = "--committees" in sys.argv
ALL      = not any([HOUSE, SENATE_EFD, SENATE, CONGRESS, EDGAR, MIRROR, COMS])

DIV = "=" * 58

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


def run_step(name, fn, *args, **kwargs):
    log(f"▶  Starting {name}…")
    t0 = time.time()
    try:
        fn(*args, **kwargs)
        log(f"✅ {name} completed in {round(time.time()-t0,1)}s")
        return True
    except Exception as e:
        log(f"❌ {name} failed: {e}")
        return False


def main():
    log(DIV)
    log("  Political Data Refresh")
    log(f"  Mode: {'full' if FULL else 'incremental'}")
    log(DIV)

    results = {}

    if ALL or COMS:
        from ingest_committees import ingest as ingest_committees
        results["committees"] = run_step("committees", ingest_committees)

    if ALL or HOUSE:
        from ingest_house import ingest as ingest_house
        results["house"] = run_step("house", ingest_house, full_refresh=FULL)

    if ALL or SENATE_EFD:
        from ingest_senate_efd import ingest as ingest_senate_efd
        results["senate_efd"] = run_step("senate_efd", ingest_senate_efd, full_refresh=FULL)

    if CONGRESS:   # explicit only — legacy peez49 CSV (frozen since 2026-02)
        from ingest_congress import ingest as ingest_congress
        results["congress"] = run_step("congress", ingest_congress, full_refresh=FULL)

    if SENATE:   # explicit only — legacy senate-stock-watcher mirror (dead since 2020)
        from ingest_senate import ingest as ingest_senate
        results["senate"] = run_step("senate", ingest_senate, full_refresh=FULL)

    if ALL or MIRROR:
        from ingest_insider_mirror import ingest as ingest_mirror
        results["insider_mirror"] = run_step("insider_mirror", ingest_mirror, full_refresh=FULL)

    if EDGAR:   # explicit only — slow full-history backfill
        from ingest_edgar import ingest as ingest_edgar
        results["edgar"] = run_step("edgar", ingest_edgar, full_refresh=FULL)

    log(DIV)
    for step, ok in results.items():
        log(f"  {step:<20} {'✅ OK' if ok else '❌ FAILED'}")
    log(DIV)

    failed = [s for s, ok in results.items() if not ok]
    if failed:
        log(f"⚠  {len(failed)} step(s) failed.")
        sys.exit(1)
    else:
        log("🎉 Political data refresh complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
