"""
scheduler.py — Nightly pipeline runner for Fly.io
Triggered by a Fly.io cron machine at 2am UTC daily.
Also runnable manually: python scheduler.py

Runs the market pipeline (run.py), then the political refresh (pol_refresh.py,
incremental), then the integrity gate (validate.py --strict) LAST, so it checks
both pipelines' fresh output -- run.py's own validate stage is skipped here,
because it would check the political tables before tonight's refresh. All
three run even if an earlier one fails; the process exits non-zero if any
failed, so the Fly cron surfaces it (issues #34, #35).
"""
import subprocess, sys, os, json, logging
from datetime import datetime, timezone

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Record of how the last night went, written under DATA_DIR so it survives on the
# Fly volume and /health can report it after a restart.
LAST_RUN_JSON = config.data_path("last_run.json")

# Ordered pipelines: (label, script, args). Political refresh runs incremental
# (no --full); validation runs once, after both have written.
PIPELINES = [
    ("market pipeline",   "run.py",         ["--skip-validate"]),
    ("political refresh", "pol_refresh.py", []),
    ("integrity gate",    "validate.py",    ["--strict"]),
]


def _run(script, args=()):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script), *args],
        cwd=SCRIPT_DIR,
        env=env,
    ).returncode


def run_all():
    """Run every pipeline in PIPELINES, in order, even if an earlier one fails.
    Never calls sys.exit -- safe to invoke in-process from the API's APScheduler
    job. Writes <DATA_DIR>/last_run.json and returns (ok, results), where results
    is a list of {label, returncode}."""
    log.info("Scheduled pipeline starting...")
    started_at = datetime.now(timezone.utc).isoformat()
    results = []
    for label, script, args in PIPELINES:
        log.info(f"Starting {label} ({script} {' '.join(args)}) ...".replace(" )", ")"))
        rc = _run(script, args)
        if rc == 0:
            log.info(f"{label} completed successfully.")
        else:
            log.error(f"{label} failed with exit code {rc}")
        results.append({"label": label, "returncode": rc})

    ok = all(r["returncode"] == 0 for r in results)
    finished_at = datetime.now(timezone.utc).isoformat()
    _write_last_run(started_at, finished_at, ok, results)

    if ok:
        log.info("All pipelines completed successfully.")
    else:
        failed = [r["label"] for r in results if r["returncode"] != 0]
        log.error(f"{len(failed)} pipeline(s) failed: {failed}")
    return ok, results


def _write_last_run(started_at, finished_at, ok, steps):
    record = {"started_at": started_at, "finished_at": finished_at,
              "ok": ok, "steps": steps}
    try:
        with open(LAST_RUN_JSON, "w", encoding="utf-8") as f:
            json.dump(record, f)
    except Exception as e:                        # a write failure must not sink the run
        log.error(f"could not write {LAST_RUN_JSON}: {e}")


def run():
    """CLI wrapper: run every pipeline, then exit 1 if any failed."""
    ok, _ = run_all()
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    run()
