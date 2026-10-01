"""
scheduler.py — Nightly pipeline runner for Fly.io
Triggered by a Fly.io cron machine at 2am UTC daily.
Also runnable manually: python scheduler.py

Runs the market pipeline (run.py) then the political refresh (pol_refresh.py,
incremental). Both run even if the first fails; the process exits non-zero if
either failed, so the Fly cron surfaces it (issue #35).
"""
import subprocess, sys, os, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Ordered pipelines: (label, script). Political refresh runs incremental (no --full).
PIPELINES = [
    ("market pipeline",   "run.py"),
    ("political refresh", "pol_refresh.py"),
]


def _run(script):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, script)],
        cwd=SCRIPT_DIR,
        env=env,
    ).returncode


def run():
    log.info("Scheduled pipeline starting...")
    failed = []
    for label, script in PIPELINES:
        log.info(f"Starting {label} ({script}) ...")
        rc = _run(script)
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
