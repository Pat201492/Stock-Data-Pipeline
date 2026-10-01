"""
test_step_runner.py — the shared step runner + both orchestrators' Step lists
(issue #35). Steps are stubbed; no real ingest or subprocess runs.

Run:  python -m pytest test_step_runner.py   (or: python test_step_runner.py)
"""
import types

import pytest

import config
import step_runner
from step_runner import Step, run_steps


@pytest.fixture
def run_log(tmp_path, monkeypatch):
    path = tmp_path / "run.log"
    monkeypatch.setattr(config, "RUN_LOG", str(path))
    return path


# ── run_steps contract ───────────────────────────────────────────────────────

def test_failures_reported_and_later_steps_run(run_log, monkeypatch):
    ran = []

    # A subprocess step that exits non-zero (config.py exists so the path check passes).
    monkeypatch.setattr(step_runner.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1))

    def raises():      ran.append("call_raise"); raise RuntimeError("boom")
    def succeeds():    ran.append("call_ok")

    steps = [
        Step(name="subproc_fail", kind="subprocess", script="config.py"),
        Step(name="call_raise",   kind="call", fn=raises),
        Step(name="call_ok",      kind="call", fn=succeeds),
    ]
    ok, results = run_steps(steps)

    assert ok is False
    assert results == {"subproc_fail": False, "call_raise": False, "call_ok": True}
    assert ran == ["call_raise", "call_ok"]   # later steps still ran


def test_from_step_skips_earlier(run_log):
    ran = []
    steps = [Step(name=n, kind="call", fn=(lambda n=n: ran.append(n)))
             for n in ("a", "b", "c")]
    ok, results = run_steps(steps, from_step="b")
    assert ok is True
    assert ran == ["b", "c"]
    assert list(results) == ["b", "c"]


def test_unknown_from_step_raises_before_running(run_log):
    ran = []
    steps = [Step(name="a", kind="call", fn=lambda: ran.append("a"))]
    with pytest.raises(ValueError):
        run_steps(steps, from_step="nope")
    assert ran == []


def test_disabled_steps_skipped(run_log):
    ran = []
    steps = [
        Step(name="on",  kind="call", fn=lambda: ran.append("on")),
        Step(name="off", kind="call", fn=lambda: ran.append("off"), enabled=False),
    ]
    ok, results = run_steps(steps)
    assert ran == ["on"]
    assert list(results) == ["on"]


def test_every_step_logs_one_line_with_name_status_duration(run_log):
    steps = [
        Step(name="alpha", kind="call", fn=lambda: None),
        Step(name="beta",  kind="call", fn=lambda: (_ for _ in ()).throw(ValueError("x"))),
    ]
    run_steps(steps)
    text = run_log.read_text(encoding="utf-8")
    lines = text.splitlines()

    alpha = [l for l in lines if "alpha" in l and ("OK" in l or "FAILED" in l)]
    beta  = [l for l in lines if "beta"  in l and ("OK" in l or "FAILED" in l)]
    assert len(alpha) == 1 and "OK" in alpha[0]
    assert "s" in alpha[0]        # duration present, e.g. "0.0s"
    assert len(beta) == 1 and "FAILED" in beta[0]


# ── run.py Step list matches the old --from selection ────────────────────────

def test_run_build_steps_and_from_selection():
    import run
    steps = run.build_steps()
    names = [s.name for s in steps]
    assert names == run.SCRIPTS + ["validate"]

    # --from model: model onward + validate, same as the old scripts[idx:].
    active = [s.name for s in steps if s.enabled]
    sliced = active[active.index("model"):]
    assert sliced == ["model", "news", "etf_universe", "commodities",
                      "options", "commodity_exposure", "validate"]


def test_run_skip_flags_disable_steps():
    import run
    steps = run.build_steps(skip_universe=True)
    assert all(s.name != "universe" or not s.enabled for s in steps)
    steps = run.build_steps(skip_validate=True)
    validate = [s for s in steps if s.name == "validate"][0]
    assert validate.enabled is False


# ── pol_refresh.py Step list matches the old per-flag selection ──────────────

def test_pol_refresh_default_steps():
    import pol_refresh
    steps = pol_refresh.build_steps(pol_refresh.parse_args([]))
    assert [s.name for s in steps] == ["committees", "house", "senate_efd", "insider_mirror"]


def test_pol_refresh_house_only():
    import pol_refresh
    steps = pol_refresh.build_steps(pol_refresh.parse_args(["--house"]))
    assert [s.name for s in steps] == ["house"]
    assert steps[0].kwargs == {"full_refresh": False}


def test_pol_refresh_full_passes_kwarg():
    import pol_refresh
    steps = pol_refresh.build_steps(pol_refresh.parse_args(["--house", "--full"]))
    assert steps[0].kwargs == {"full_refresh": True}


# ── scheduler runs both pipelines, exits 1 if either fails ───────────────────

def _stub_scheduler(monkeypatch, rc_by_script):
    import scheduler
    calls = []

    def fake_run(args, **kwargs):
        script = args[1]
        calls.append(script)
        name = script.replace("\\", "/").rsplit("/", 1)[-1]
        return types.SimpleNamespace(returncode=rc_by_script.get(name, 0))

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    return scheduler, calls


def test_scheduler_runs_both_in_order(monkeypatch):
    scheduler, calls = _stub_scheduler(monkeypatch, {})
    scheduler.run()
    assert [c.replace("\\", "/").rsplit("/", 1)[-1] for c in calls] == \
           ["run.py", "pol_refresh.py"]


def test_scheduler_exits_1_when_market_fails(monkeypatch):
    scheduler, calls = _stub_scheduler(monkeypatch, {"run.py": 2})
    with pytest.raises(SystemExit) as exc:
        scheduler.run()
    assert exc.value.code == 1
    # political refresh still ran despite the market failure
    assert any("pol_refresh.py" in c for c in calls)


def test_scheduler_exits_1_when_political_fails(monkeypatch):
    scheduler, calls = _stub_scheduler(monkeypatch, {"pol_refresh.py": 3})
    with pytest.raises(SystemExit) as exc:
        scheduler.run()
    assert exc.value.code == 1


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
