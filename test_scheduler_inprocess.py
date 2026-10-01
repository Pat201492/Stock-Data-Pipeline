"""test_scheduler_inprocess.py — the Fly in-process nightly job (issue #48).

On Fly the API runs the nightly sequence in-process (RUN_SCHEDULER=1), via
api._run_pipeline -> scheduler.run_all. These tests drive _run_pipeline with
subprocess stubbed (no real ingest), and check:
  - it runs run.py --skip-validate, pol_refresh.py, validate.py --strict, in order
  - a failing step never raises out of _run_pipeline, yet the CLI exits 1
  - last_run.json is written, and /health serves it under last_run with HTTP 200

DATA_DIR is pointed at a temp dir BEFORE importing api/scheduler, so last_run.json
and api's DATA_DIR resolve into it (mirrors test_api_db_endpoints).

Run:  python -m pytest test_scheduler_inprocess.py
"""
import os, json, tempfile, types

import pytest

_TMP = tempfile.mkdtemp(prefix="issue48_")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = os.path.join(_TMP, "stocks.db")

from fastapi.testclient import TestClient
import api
import scheduler

client = TestClient(api.app)


def _stub(monkeypatch, rc_by_script=None):
    """Stub scheduler.subprocess.run; record argv per call, return canned rc."""
    rc_by_script = rc_by_script or {}
    argvs = []

    def fake_run(args, **kwargs):
        argvs.append(args)
        name = args[1].replace("\\", "/").rsplit("/", 1)[-1]
        return types.SimpleNamespace(returncode=rc_by_script.get(name, 0))

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    return argvs


def _clear_last_run():
    try:
        os.remove(scheduler.LAST_RUN_JSON)
    except FileNotFoundError:
        pass


# ── the in-process job runs the full night, in order ─────────────────────────

def test_inprocess_runs_full_sequence_in_order(monkeypatch):
    argvs = _stub(monkeypatch)
    api._run_pipeline()
    by = [(a[1].replace("\\", "/").rsplit("/", 1)[-1], list(a[2:])) for a in argvs]
    assert by == [
        ("run.py", ["--skip-validate"]),
        ("pol_refresh.py", []),
        ("validate.py", ["--strict"]),
    ]


# ── a failing step must not raise out of _run_pipeline ───────────────────────

def test_failing_step_does_not_raise_out_of_inprocess_job(monkeypatch):
    _stub(monkeypatch, {"run.py": 2})
    api._run_pipeline()   # must not raise


def test_crash_in_run_all_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(scheduler, "run_all", boom)
    api._run_pipeline()   # swallowed, must not raise


# ── the CLI still exits 1 on failure ─────────────────────────────────────────

def test_cli_exits_1_on_failure(monkeypatch):
    _stub(monkeypatch, {"validate.py": 1})
    with pytest.raises(SystemExit) as exc:
        scheduler.run()
    assert exc.value.code == 1


def test_cli_exits_0_on_success(monkeypatch):
    _stub(monkeypatch)
    scheduler.run()   # no SystemExit


# ── last_run.json + /health ──────────────────────────────────────────────────

def test_last_run_json_records_ok_and_each_step(monkeypatch):
    _stub(monkeypatch)
    api._run_pipeline()
    with open(scheduler.LAST_RUN_JSON, encoding="utf-8") as f:
        rec = json.load(f)
    assert rec["ok"] is True
    assert "started_at" in rec and "finished_at" in rec
    assert [(s["label"], s["returncode"]) for s in rec["steps"]] == [
        ("market pipeline", 0),
        ("political refresh", 0),
        ("integrity gate", 0),
    ]


def test_health_null_last_run_before_first_run(monkeypatch):
    _clear_last_run()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["last_run"] is None


def test_health_serves_last_run_200_even_when_failed(monkeypatch):
    _stub(monkeypatch, {"pol_refresh.py": 3})
    api._run_pipeline()
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True                    # HTTP health stays green
    assert body["last_run"]["ok"] is False       # ...but the night is marked failed
    steps = {s["label"]: s["returncode"] for s in body["last_run"]["steps"]}
    assert steps["political refresh"] == 3


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
