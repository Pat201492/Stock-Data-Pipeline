"""
pol_refresh.py — Daily orchestrator for political + insider trade ingestion

Built on the shared step_runner (issue #35): each ingest is a kind="call" Step.

Usage:
  python pol_refresh.py              # incremental (default: committees + house + senate_efd + insider_mirror)
  python pol_refresh.py --full       # full refresh from scratch
  python pol_refresh.py --house      # House Clerk official PTRs only
  python pol_refresh.py --senate-efd # Senate efdsearch official PTRs only
  python pol_refresh.py --edgar      # insider trades only
  python pol_refresh.py --committees # committee data only
  python pol_refresh.py --mirror     # insider daily-mirror only
  python pol_refresh.py --congress   # DEPRECATED legacy peez49 CSV (frozen 2026-02)
  python pol_refresh.py --senate     # DEPRECATED legacy senate mirror (dead 2020)
"""
import argparse, sys

from step_runner import Step, run_steps, default_log as log, DIV

# (name, module, flag_attr, in_default, needs_full)
# in_default steps run when no explicit per-step flag is given.
_SPEC = [
    ("committees",     "ingest_committees",     "committees", True,  False),
    ("house",          "ingest_house",          "house",      True,  True),
    ("senate_efd",     "ingest_senate_efd",     "senate_efd", True,  True),
    ("congress",       "ingest_congress",       "congress",   False, True),   # legacy peez49 (frozen)
    ("senate",         "ingest_senate",         "senate",     False, True),   # legacy mirror (dead)
    ("insider_mirror", "ingest_insider_mirror", "mirror",     True,  True),
    ("edgar",          "ingest_edgar",          "edgar",      False, True),   # slow full-history backfill
]


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Political data refresh")
    p.add_argument("--full", action="store_true")
    for _, _, flag, _, _ in _SPEC:
        p.add_argument(f"--{flag.replace('_', '-')}", dest=flag, action="store_true")
    return p.parse_args(argv)


def _loader(module):
    """Return a callable that imports `module` lazily and calls its ingest().

    Deferring the import keeps build_steps side-effect-free (so it is testable)
    and preserves the old behaviour of only importing the ingest modules that a
    given run actually uses."""
    def call(**kwargs):
        return getattr(__import__(module), "ingest")(**kwargs)
    return call


def build_steps(args):
    """Build the ordered Step list selected by the CLI flags."""
    explicit = any(getattr(args, flag) for _, _, flag, _, _ in _SPEC)
    steps = []
    for name, module, flag, in_default, needs_full in _SPEC:
        selected = getattr(args, flag) or (in_default and not explicit)
        if not selected:
            continue
        kwargs = {"full_refresh": args.full} if needs_full else {}
        steps.append(Step(name=name, kind="call", fn=_loader(module), kwargs=kwargs))
    return steps


def main(argv=None):
    args = parse_args(argv)

    log(DIV)
    log("  Political Data Refresh")
    log(f"  Mode: {'full' if args.full else 'incremental'}")
    log(DIV)

    steps = build_steps(args)
    ok, results = run_steps(steps, log=log)

    log(DIV)
    for name, step_ok in results.items():
        log(f"  {name:<20} {'✅ OK' if step_ok else '❌ FAILED'}")
    log(DIV)

    failed = [s for s, step_ok in results.items() if not step_ok]
    if failed:
        log(f"⚠  {len(failed)} step(s) failed.")
        sys.exit(1)
    else:
        log("🎉 Political data refresh complete.")
        sys.exit(0)


if __name__ == "__main__":
    main()
