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
import subprocess, sys, os, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

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


def run():
    log.info("Scheduled pipeline starting...")
    failed = []
    for label, script, args in PIPELINES:
        log.info(f"Starting {label} ({script} {' '.join(args)}) ...".replace(" )", ")"))
        rc = _run(script, args)
        if rc == 0:
            log.info(f"{label} completed successfully.")
        else:
            log.error(f"{label} failed with exit code {rc}")
            failed.append((label, rc))

    if failed:
        log.error(f"{len(failed)} pipeline(s) failed: {[l for l, _ in failed]}")
        sys.exit(1)
    log.info("All pipelines completed successfully.")


if __name__ == "__main__":
    run()
