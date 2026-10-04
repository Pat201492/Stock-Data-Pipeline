"""
run.py — Pipeline runner
=========================
Runs scripts in order with timing and logging, via the shared step_runner.
All scripts are incremental — only stale data is re-fetched.

Usage:
  python run.py                       # full pipeline
  python run.py --skip-universe       # skip universe discovery
  python run.py --from fundamentals   # start from fundamentals onwards
  python run.py --from model          # model + news only
  python run.py --from news           # news + prices only
  python run.py --skip-validate       # skip the final integrity gate

Pipeline: universe → fundamentals → model → news → … → validate
The run always ends with `validate.py --strict` (unless --skip-validate); a
failing validate makes the whole run exit 1 (issue #34).
Log: run.log under DATA_DIR (config.RUN_LOG, appended each run)
"""

import argparse, os, sys, time

import config
from step_runner import Step, run_steps, default_log as log, DIV

SCRIPTS  = ["universe", "fundamentals", "model", "news",
            "etf_universe", "commodities", "options", "commodity_exposure"]


def build_steps(skip_universe=False, skip_validate=False):
    """Build the ordered Step list; validate.py --strict is always last."""
    steps = [
        Step(name=s, kind="subprocess", script=f"{s}.py",
             enabled=not (skip_universe and s == "universe"))
        for s in SCRIPTS
    ]
    steps.append(Step(name="validate", kind="subprocess", script="validate.py",
                      args=["--strict"], enabled=not skip_validate))
    return steps


def parse_args():
    p = argparse.ArgumentParser(description="Stock Tracker pipeline runner")
    p.add_argument("--skip-universe",  action="store_true")
    p.add_argument("--skip-validate",  action="store_true")
    p.add_argument("--from", dest="from_script", metavar="SCRIPT",
                   choices=SCRIPTS)
    return p.parse_args()


def main():
    args  = parse_args()
    steps = build_steps(skip_universe=args.skip_universe,
                        skip_validate=args.skip_validate)

    log(DIV)
    log("  Stock Tracker — Pipeline Run")
    log(f"  Scripts: {', '.join(s.name for s in steps if s.enabled)}")
    log(DIV)

    with config.yfinance_lock():
        # Children inherit the environment: tell them their parent holds the lock
        # (see config.yfinance_lock) so they don't wait on it and fail.
        os.environ[config.LOCK_HELD_ENV] = "1"
        t0 = time.time()
        try:
            ok, results = run_steps(steps, from_step=args.from_script, log=log)
        finally:
            os.environ.pop(config.LOCK_HELD_ENV, None)

        log(DIV)
        log(f"  Complete — {round(time.time() - t0, 1)}s total")
        log(DIV)
        for name, step_ok in results.items():
            log(f"  {name:<22}  {'✅ OK' if step_ok else '❌ FAILED'}")
        log(DIV)

        failed = [s for s, step_ok in results.items() if not step_ok]
        if failed:
            log(f"\n⚠️  {len(failed)} step(s) failed. Check run.log.")
            sys.exit(1)
        else:
            log("\n🎉 All scripts completed successfully.")
            sys.exit(0)


if __name__ == "__main__":
    main()
