"""PC-host robustness (2026-10-04): lock inheritance, waiting for the network
after wake, and holding the PC awake for the length of a run.

Run:  python -m pytest test_pc_host.py
"""
import os
import subprocess
import sys
import textwrap

import pytest

import config
import scheduler


# ── A: a child of a lock holder does not wait on its parent ──────────────────

def test_child_of_lock_holder_does_not_wait(tmp_path, monkeypatch):
    # Hold the real lock in THIS process, then start a child that asks for it,
    # exactly as run.py starts commodities.py. Without the parent marker the
    # child times out ("Another collector is already running") and exits 1.
    monkeypatch.setattr(config, "YFINANCE_LOCK_PATH", str(tmp_path / ".yfinance.lock"))
    child = textwrap.dedent(f"""
        import sys, config
        config.YFINANCE_LOCK_PATH = {str(tmp_path / '.yfinance.lock')!r}
        with config.yfinance_lock(wait_timeout=2, poll_interval=0.2):
            print("got-lock")
    """)
    here = os.path.dirname(os.path.abspath(__file__))
    with config.yfinance_lock():
        env_marked = {**os.environ, config.LOCK_HELD_ENV: "1"}
        ok = subprocess.run([sys.executable, "-c", child], cwd=here, env=env_marked,
                            capture_output=True, text=True, timeout=60)
        env_plain = {k: v for k, v in os.environ.items() if k != config.LOCK_HELD_ENV}
        blocked = subprocess.run([sys.executable, "-c", child], cwd=here, env=env_plain,
                                 capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and "got-lock" in ok.stdout
    assert blocked.returncode == 1 and "Another collector" in blocked.stdout


def test_run_py_marks_children_and_clears_after(monkeypatch):
    import run
    seen = {}

    def fake_run_steps(steps, from_step=None, log=None):
        seen["env"] = os.environ.get(config.LOCK_HELD_ENV)
        return True, {"universe": True}

    monkeypatch.setattr(run, "run_steps", fake_run_steps)
    monkeypatch.setattr(sys, "argv", ["run.py", "--skip-validate"])
    try:
        run.main()
    except SystemExit as e:
        assert e.code in (0, None)
    assert seen["env"] == "1"
    assert os.environ.get(config.LOCK_HELD_ENV) is None


# ── C: wait for the network after wake ───────────────────────────────────────

def test_wait_for_network_retries_until_dns_resolves():
    calls, sleeps = [], []

    def flaky(host, port):
        calls.append(host)
        if len(calls) <= 2:
            raise OSError("getaddrinfo failed")
        return [()]

    assert scheduler.wait_for_network(timeout=600, interval=15, resolve=flaky,
                                      sleep=sleeps.append) is True
    assert sleeps == [15, 15]


def test_wait_for_network_gives_up_after_timeout():
    sleeps = []

    def down(host, port):
        raise OSError("getaddrinfo failed")

    assert scheduler.wait_for_network(timeout=45, interval=15, resolve=down,
                                      sleep=sleeps.append) is False
    assert sum(sleeps) == 45


# ── D: hold the PC awake for the run, release after ──────────────────────────

def test_keep_awake_sets_and_releases(monkeypatch):
    monkeypatch.delenv("KEEP_AWAKE_DURING_RUN", raising=False)
    flags = []
    with scheduler.keep_awake(setter=lambda f: flags.append(f) or 1) as held:
        assert held is True
    assert flags == [scheduler.ES_CONTINUOUS | scheduler.ES_SYSTEM_REQUIRED,
                     scheduler.ES_CONTINUOUS]


def test_keep_awake_releases_even_when_the_run_raises(monkeypatch):
    monkeypatch.delenv("KEEP_AWAKE_DURING_RUN", raising=False)
    flags = []
    with pytest.raises(RuntimeError):
        with scheduler.keep_awake(setter=lambda f: flags.append(f) or 1):
            raise RuntimeError("step blew up")
    assert flags[-1] == scheduler.ES_CONTINUOUS


def test_keep_awake_disabled_by_env(monkeypatch):
    monkeypatch.setenv("KEEP_AWAKE_DURING_RUN", "0")
    flags = []
    with scheduler.keep_awake(setter=flags.append) as held:
        assert held is False
    assert flags == []


def test_run_all_waits_for_network_inside_keep_awake(monkeypatch):
    order = []
    monkeypatch.delenv("KEEP_AWAKE_DURING_RUN", raising=False)
    monkeypatch.setattr(scheduler, "_set_execution_state",
                        lambda f: order.append(("awake", f)) or 1)
    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    monkeypatch.setattr(scheduler, "wait_for_network", lambda: order.append("net") or True)
    monkeypatch.setattr(scheduler, "_run", lambda script, args=(): order.append(script) or 0)
    monkeypatch.setattr(scheduler, "_write_last_run", lambda *a: None)
    ok, _ = scheduler.run_all()
    assert ok
    assert order[0][0] == "awake" and order[1] == "net"
    assert order[-1] == ("awake", scheduler.ES_CONTINUOUS)
