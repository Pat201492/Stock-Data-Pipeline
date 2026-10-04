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
import subprocess, sys, os, json, logging, socket, time
from contextlib import contextmanager
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


# Hosts the night depends on; DNS for these is the "network is up" signal.
NETWORK_PROBE_HOSTS = ("query1.finance.yahoo.com", "data.sec.gov")


def wait_for_network(timeout=None, interval=15, resolve=socket.getaddrinfo, sleep=time.sleep):
    """On a PC the late (misfire-grace) run fires the moment it wakes, before
    Wi-Fi is back: 2026-10-04's run started with DNS failing. Wait until the probe
    hosts resolve, up to NETWORK_WAIT_SECONDS (default 600), then go anyway.
    Returns True if the network came up."""
    timeout = float(os.environ.get("NETWORK_WAIT_SECONDS") or 600) if timeout is None else timeout
    waited = 0.0
    while True:
        try:
            for h in NETWORK_PROBE_HOSTS:
                resolve(h, 443)
            if waited:
                log.info(f"network up after {waited:.0f}s")
            return True
        except OSError:
            if waited >= timeout:
                log.warning(f"network still down after {waited:.0f}s -- running anyway")
                return False
            if not waited:
                log.info("network not up yet -- waiting before the run")
            sleep(interval)
            waited += interval


ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def _set_execution_state(flags):
    import ctypes
    return ctypes.windll.kernel32.SetThreadExecutionState(flags)


@contextmanager
def keep_awake(setter=None):
    """Hold Windows awake for the length of a run (the PC slept mid-run on
    2026-10-02..04 and fundamentals took 15h of wall clock). Releases on exit, so
    the PC sleeps normally otherwise. No-op off Windows or with
    KEEP_AWAKE_DURING_RUN=0. Must enter and exit on the same thread."""
    if os.environ.get("KEEP_AWAKE_DURING_RUN") == "0" or (setter is None and sys.platform != "win32"):
        yield False
        return
    setter = setter or _set_execution_state
    held = bool(setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    try:
        yield held
    finally:
        if held:
            setter(ES_CONTINUOUS)


def run_all():
    """Run every pipeline in PIPELINES, in order, even if an earlier one fails.
    Never calls sys.exit -- safe to invoke in-process from the API's APScheduler
    job. Writes <DATA_DIR>/last_run.json and returns (ok, results), where results
    is a list of {label, returncode}."""
    with keep_awake():
        wait_for_network()
        return _run_all()


def _run_all():
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
