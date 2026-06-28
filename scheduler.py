"""
scheduler.py — Nightly pipeline runner for Fly.io
Triggered by a Fly.io cron machine at 2am UTC daily.
Also runnable manually: python scheduler.py
"""
import subprocess, sys, os, logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def run():
    log.info("Scheduled pipeline starting...")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "run.py")],
        cwd=SCRIPT_DIR,
        env=env,
    )
    if result.returncode == 0:
        log.info("Pipeline completed successfully.")
    else:
        log.error(f"Pipeline failed with exit code {result.returncode}")
        sys.exit(result.returncode)


if __name__ == "__main__":
    run()
