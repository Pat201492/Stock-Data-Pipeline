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


def test_catch_up_skipped_while_a_run_holds_the_lock(monkeypatch, tmp_path, _stub_pipeline):
    # The hourly check must not queue a second run behind one in progress.
    _write_last_run(tmp_path, "2020-01-01T00:00:00+00:00")
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CATCHUP_ON_START", raising=False)
    with api._pipeline_lock:
        assert api._maybe_catch_up() is None
    assert _stub_pipeline == []


def _scheduler_with(monkeypatch, **env):
    monkeypatch.setenv("RUN_SCHEDULER", "1")
    monkeypatch.setenv("BOOTSTRAP_ON_EMPTY", "0")
    monkeypatch.setenv("CATCHUP_ON_START", "0")
    for k in ("PIPELINE_TZ", "PIPELINE_HOUR", "PIPELINE_HOUR_UTC", "CATCHUP_HOURLY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    api._start_scheduler()
    return api.app.state.scheduler


def test_nightly_hour_in_local_time(monkeypatch):
    # The PC host: 22:00 in the machine's own zone, DST-aware.
    from tzlocal import get_localzone
    sched = _scheduler_with(monkeypatch, PIPELINE_TZ="local", PIPELINE_HOUR="22")
    try:
        trig = sched.get_job("nightly_pipeline").trigger
        assert str(trig.timezone) == str(get_localzone())
        assert str(trig.fields[trig.FIELD_NAMES.index("hour")]) == "22"
    finally:
        sched.shutdown(wait=False)


def test_nightly_default_is_still_02_utc(monkeypatch):
    sched = _scheduler_with(monkeypatch)
    try:
        trig = sched.get_job("nightly_pipeline").trigger
        assert str(trig.timezone) == "UTC"
        assert str(trig.fields[trig.FIELD_NAMES.index("hour")]) == "2"
    finally:
        sched.shutdown(wait=False)


def test_hourly_catch_up_job(monkeypatch):
    sched = _scheduler_with(monkeypatch)
    try:
        assert sched.get_job("catch_up_check") is not None
    finally:
        sched.shutdown(wait=False)
    sched = _scheduler_with(monkeypatch, CATCHUP_HOURLY="0")
    try:
        assert sched.get_job("catch_up_check") is None
    finally:
        sched.shutdown(wait=False)


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


# â”€â”€ a slept-through nightly slot (#63) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _slot_env(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CATCHUP_ON_START", raising=False)
    monkeypatch.delenv("CATCHUP_AFTER_HOURS", raising=False)
    monkeypatch.delenv("PIPELINE_MISFIRE_GRACE_HOURS", raising=False)
    monkeypatch.setenv("PIPELINE_TZ", "America/New_York")
    monkeypatch.setenv("PIPELINE_HOUR", "22")


def _ny(*a):
    from zoneinfo import ZoneInfo
    from datetime import datetime
    return datetime(*a, tzinfo=ZoneInfo("America/New_York"))


def _write_run(tmp_path, started, finished):
    import json as _json
    (tmp_path / "fundamentals.json").write_text("{}", encoding="utf-8")
    (tmp_path / "last_run.json").write_text(
        _json.dumps({"started_at": started.isoformat(), "finished_at": finished.isoformat(),
                     "ok": True, "steps": []}), encoding="utf-8")


def test_catch_up_runs_a_slept_through_slot(monkeypatch, tmp_path, _stub_pipeline):
    # 2026-10-19: last run 22:00-23:47 the night before; asleep at 22:00; awake 22:10.
    _slot_env(monkeypatch, tmp_path)
    _write_run(tmp_path, _ny(2026, 10, 18, 22), _ny(2026, 10, 18, 23, 47))
    t = api._maybe_catch_up(now=_ny(2026, 10, 19, 22, 10))
    assert t is not None            # 22h23m since the last finish: the 26h rule alone would wait
    t.join(timeout=5)
    assert _stub_pipeline == [1]


def test_catch_up_skips_when_tonights_run_started(monkeypatch, tmp_path, _stub_pipeline):
    _slot_env(monkeypatch, tmp_path)
    _write_run(tmp_path, _ny(2026, 10, 19, 22), _ny(2026, 10, 19, 22, 5))
    assert api._maybe_catch_up(now=_ny(2026, 10, 19, 22, 10)) is None
    assert _stub_pipeline == []


def test_past_the_grace_window_only_the_26h_rule_applies(monkeypatch, tmp_path, _stub_pipeline):
    _slot_env(monkeypatch, tmp_path)
    # Slot 22:00, now 05:00 next morning: 7h, past the 6h grace. The last run
    # (started before the slot) finished 5h ago, so the 26h rule waits too.
    _write_run(tmp_path, _ny(2026, 10, 19, 21), _ny(2026, 10, 20, 0))
    assert api._maybe_catch_up(now=_ny(2026, 10, 20, 5)) is None
    assert _stub_pipeline == []
    # ...and a 30h-old run still catches up under the 26h rule.
    _write_run(tmp_path, _ny(2026, 10, 18, 21), _ny(2026, 10, 18, 23))
    t = api._maybe_catch_up(now=_ny(2026, 10, 20, 5))
    assert t is not None
    t.join(timeout=5)


def test_last_slot_is_tonight_after_the_hour_and_yesterday_before_it(monkeypatch):
    monkeypatch.setenv("PIPELINE_TZ", "America/New_York")
    monkeypatch.setenv("PIPELINE_HOUR", "22")
    assert api._last_slot(_ny(2026, 10, 19, 22, 10)) == _ny(2026, 10, 19, 22)
    assert api._last_slot(_ny(2026, 10, 19, 21, 59)) == _ny(2026, 10, 18, 22)


def test_late_cron_fire_after_catch_up_does_not_run_twice(monkeypatch, tmp_path, _stub_pipeline):
    # The cron timer fires late after a sleep; catch-up already ran tonight.
    _slot_env(monkeypatch, tmp_path)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    _write_run(tmp_path, now, now)          # a run started just now, after any slot
    api._nightly()
    assert _stub_pipeline == []


def test_cron_fire_runs_when_nothing_started_since_the_slot(monkeypatch, tmp_path, _stub_pipeline):
    _slot_env(monkeypatch, tmp_path)
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("PIPELINE_MISFIRE_GRACE_HOURS", "25")   # whatever time the test runs
    _write_run(tmp_path, now - timedelta(days=2), now - timedelta(days=2))
    api._nightly()
    assert _stub_pipeline == [1]


def test_catch_up_check_runs_every_15_minutes(monkeypatch):
    sched = _scheduler_with(monkeypatch)
    try:
        trig = sched.get_job("catch_up_check").trigger
        assert trig.interval.total_seconds() == 15 * 60
        assert sched.get_job("nightly_pipeline").func is api._nightly
    finally:
        sched.shutdown(wait=False)
