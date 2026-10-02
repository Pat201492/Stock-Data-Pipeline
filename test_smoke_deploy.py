"""test_smoke_deploy.py — deploy-readiness (issue #49).

Three things under test:
  - smoke_deploy.py: exits 0 against a healthy stub, non-zero (naming the check)
    when any endpoint fails or a secret boolean is false. httpx MockTransport
    stands in for the live API — no network.
  - /health reports config as BOOLEANS only; a test asserts no secret VALUE leaks
    into the response body.
  - the cold-volume bootstrap: fires exactly one run when the volume is empty,
    none when disabled or already populated, and never overlaps the cron job.

DATA_DIR is pointed at a temp dir BEFORE importing api, as the other api tests do.

Run:  python -m pytest test_smoke_deploy.py
"""
import os, tempfile, threading, time

import httpx
import pytest

_TMP = tempfile.mkdtemp(prefix="issue49_")
os.environ["DATA_DIR"] = _TMP
os.environ["DB_PATH"] = os.path.join(_TMP, "stocks.db")

from fastapi.testclient import TestClient
import api
import smoke_deploy

SECRET_UA = "SmokeTester test@example.com"
SECRET_FRED = "abcdef0123456789abcdef0123456789"


# ── smoke_deploy stubs ───────────────────────────────────────────────────────

def _handler(health=None, stocks=None, macro=None, integrity=None):
    """Build a MockTransport handler; each arg overrides one endpoint's Response."""
    health = health or httpx.Response(200, json={
        "ok": True,
        "config": {"sec_user_agent": True, "fred_api_key": True, "run_scheduler": True}})
    stocks = stocks or httpx.Response(200, json={
        "total": 1, "stocks": [{"ticker": "AAPL", "magic_source": "xbrl"}]})
    macro = macro or httpx.Response(200, json={"vix": 15.0})
    integrity = integrity or httpx.Response(200, json={})
    routes = {"/health": health, "/api/stocks": stocks,
              "/api/macro": macro, "/api/integrity": integrity}

    def handle(request):
        return routes.get(request.url.path, httpx.Response(404))
    return handle


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test")


def test_smoke_passes_against_healthy_stub(capsys):
    ok = smoke_deploy.run_checks(_client(_handler()))
    out = capsys.readouterr().out
    assert ok is True
    assert out.count("[PASS]") == 4 and "[FAIL]" not in out


def test_smoke_fails_when_sec_user_agent_false(capsys):
    h = _handler(health=httpx.Response(200, json={
        "ok": True, "config": {"sec_user_agent": False, "fred_api_key": True}}))
    ok = smoke_deploy.run_checks(_client(h))
    out = capsys.readouterr().out
    assert ok is False
    assert "[FAIL] health" in out and "sec_user_agent" in out


def test_smoke_fails_when_macro_down(capsys):
    h = _handler(macro=httpx.Response(503, json={"error": "FRED_API_KEY not set"}))
    ok = smoke_deploy.run_checks(_client(h))
    out = capsys.readouterr().out
    assert ok is False
    assert "[FAIL] macro" in out


def test_smoke_empty_stocks_fails_unless_allowed(capsys):
    h = _handler(stocks=httpx.Response(200, json={"total": 0, "stocks": []}))
    assert smoke_deploy.run_checks(_client(h)) is False
    assert "[FAIL] stocks" in capsys.readouterr().out
    assert smoke_deploy.run_checks(_client(h), allow_empty=True) is True


def test_smoke_main_exit_codes(monkeypatch):
    # main() builds its own httpx.Client; swap it for a MockTransport-backed one.
    # Capture the real constructor first so the factory doesn't recurse into itself.
    real = httpx.Client

    def factory(handler):
        def fake(*a, **k):
            return real(transport=httpx.MockTransport(handler), base_url="http://test")
        return fake

    monkeypatch.setattr(smoke_deploy.httpx, "Client", factory(_handler()))
    assert smoke_deploy.main(["http://stub"]) == 0

    monkeypatch.setattr(smoke_deploy.httpx, "Client",
                        factory(_handler(integrity=httpx.Response(500))))
    assert smoke_deploy.main(["http://stub"]) == 1


# ── /health config is booleans only; no secret leaks ─────────────────────────

def test_health_config_is_booleans_only(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", SECRET_UA)
    monkeypatch.setenv("FRED_API_KEY", SECRET_FRED)
    monkeypatch.setenv("RUN_SCHEDULER", "1")
    client = TestClient(api.app)
    r = client.get("/health")
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert cfg == {"sec_user_agent": True, "fred_api_key": True, "run_scheduler": True}
    # the actual secret values must never appear anywhere in the body
    body = r.text
    assert SECRET_UA not in body
    assert SECRET_FRED not in body


def test_health_config_false_when_unset(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("RUN_SCHEDULER", raising=False)
    client = TestClient(api.app)
    cfg = client.get("/health").json()["config"]
    assert cfg == {"sec_user_agent": False, "fred_api_key": False, "run_scheduler": False}


# ── cold-volume bootstrap ────────────────────────────────────────────────────

@pytest.fixture
def _stub_pipeline(monkeypatch):
    """Stub _run_pipeline to a recorder (no subprocess / apscheduler), and clear any
    bootstrap_thread left on app.state by a prior test."""
    calls = []
    monkeypatch.setattr(api, "_run_pipeline", lambda: calls.append(1))
    if hasattr(api.app.state, "bootstrap_thread"):
        del api.app.state.bootstrap_thread
    return calls


def test_bootstrap_fires_once_when_volume_empty(monkeypatch, tmp_path, _stub_pipeline):
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))      # empty: no fundamentals.json
    monkeypatch.delenv("BOOTSTRAP_ON_EMPTY", raising=False)
    t = api._maybe_bootstrap()
    assert t is not None
    t.join(timeout=5)
    assert _stub_pipeline == [1]


def test_bootstrap_skipped_when_file_present(monkeypatch, tmp_path, _stub_pipeline):
    (tmp_path / "fundamentals.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BOOTSTRAP_ON_EMPTY", raising=False)
    assert api._maybe_bootstrap() is None
    assert _stub_pipeline == []


def test_bootstrap_disabled_by_env(monkeypatch, tmp_path, _stub_pipeline):
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))      # empty volume
    monkeypatch.setenv("BOOTSTRAP_ON_EMPTY", "0")
    assert api._maybe_bootstrap() is None
    assert _stub_pipeline == []


def test_bootstrap_and_cron_cannot_overlap(monkeypatch):
    """Both the cron job and the bootstrap are _run_pipeline; the shared lock must
    serialize them. Prove it: two concurrent _run_pipeline calls never overlap."""
    import scheduler
    active = []
    peak = [0]

    def fake_run_all():
        active.append(1)
        peak[0] = max(peak[0], len(active))
        time.sleep(0.1)
        active.pop()
        return True, []

    monkeypatch.setattr(scheduler, "run_all", fake_run_all)
    threads = [threading.Thread(target=api._run_pipeline) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1   # never two at once


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))


# ── catch-up on start (a PC asleep / off at the nightly hour) ────────────────

def _write_last_run(tmp_path, finished_at):
    import json as _json
    (tmp_path / "fundamentals.json").write_text("{}", encoding="utf-8")
    (tmp_path / "last_run.json").write_text(
        _json.dumps({"started_at": finished_at, "finished_at": finished_at,
                     "ok": True, "steps": []}), encoding="utf-8")


def test_catch_up_runs_when_last_run_is_stale(monkeypatch, tmp_path, _stub_pipeline):
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    _write_last_run(tmp_path, (now - timedelta(hours=30)).isoformat())
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CATCHUP_ON_START", raising=False)
    monkeypatch.delenv("CATCHUP_AFTER_HOURS", raising=False)
    t = api._maybe_catch_up(now=now)
    assert t is not None
    t.join(timeout=5)
    assert _stub_pipeline == [1]


def test_catch_up_skipped_when_last_run_is_recent(monkeypatch, tmp_path, _stub_pipeline):
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    _write_last_run(tmp_path, (now - timedelta(hours=10)).isoformat())
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CATCHUP_ON_START", raising=False)
    assert api._maybe_catch_up(now=now) is None
    assert _stub_pipeline == []


def test_catch_up_runs_when_no_run_ever_recorded(monkeypatch, tmp_path, _stub_pipeline):
    (tmp_path / "fundamentals.json").write_text("{}", encoding="utf-8")   # data, no last_run.json
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CATCHUP_ON_START", raising=False)
    t = api._maybe_catch_up()
    assert t is not None
    t.join(timeout=5)
    assert _stub_pipeline == [1]


def test_catch_up_leaves_empty_volume_to_bootstrap(monkeypatch, tmp_path, _stub_pipeline):
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))      # no fundamentals.json
    assert api._maybe_catch_up() is None
    assert _stub_pipeline == []


def test_catch_up_disabled_by_env(monkeypatch, tmp_path, _stub_pipeline):
    _write_last_run(tmp_path, "2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CATCHUP_ON_START", "0")
    assert api._maybe_catch_up() is None


def test_nightly_job_has_misfire_grace(monkeypatch):
    # A host asleep at the hour used to skip the night (APScheduler's ~1s default).
    monkeypatch.setenv("RUN_SCHEDULER", "1")
    monkeypatch.setenv("BOOTSTRAP_ON_EMPTY", "0")
    monkeypatch.setenv("CATCHUP_ON_START", "0")
    monkeypatch.delenv("PIPELINE_MISFIRE_GRACE_HOURS", raising=False)
    api._start_scheduler()
    try:
        job = api.app.state.scheduler.get_job("nightly_pipeline")
        assert job.misfire_grace_time == 6 * 3600
        assert job.coalesce is True
    finally:
        api.app.state.scheduler.shutdown(wait=False)
