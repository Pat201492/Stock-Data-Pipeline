"""
Offline checks for the shared yfinance access layer (issue #18): pacing/backoff
read their knobs from config.py, and a whole-batch failure trips the SAME shared
backoff on both the commodities path (yf_download wrapper) and the options path
(data_utils.run_batches). No network, no real sleeping.

Run:  python -m pytest test_yf_client.py      (or: python test_yf_client.py)
"""

import config
import data_utils
import yf_client


# ── pacing / backoff knobs come from config.py (no per-script duplicates) ────────
def test_pace_waits_config_yf_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(yf_client.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(yf_client.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(config, "YF_SLEEP", 7.5)
    yf_client._last_call[0] = 0.0
    yf_client.pace()
    assert slept and abs(slept[-1] - 7.5) < 1e-6


def test_backoff_sleeps_config_yf_backoff(monkeypatch):
    slept = []
    monkeypatch.setattr(yf_client.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(config, "YF_BACKOFF", 3.0)
    yf_client.backoff("ctx")
    assert slept == [3.0]


def test_is_rate_limited_recognises_429():
    assert yf_client.is_rate_limited(Exception("HTTP Error 429: Too Many Requests"))
    assert yf_client.is_rate_limited(Exception("YFRateLimitError: rate limit"))
    assert not yf_client.is_rate_limited(Exception("connection reset"))


# ── shared backoff, commodities path: the yf_download wrapper ────────────────────
def test_download_rate_limit_uses_shared_backoff(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(yf_client, "backoff", lambda context="", attempt=0: calls.__setitem__("n", calls["n"] + 1))
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    monkeypatch.setattr(config, "YF_RETRIES", 2)

    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(yf_client.yf, "download", boom)
    try:
        yf_client.yf_download("CL=F", period="5y")
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "a persistent rate limit must still surface"
    assert calls["n"] == 2, "backed off once per retry (YF_RETRIES) before giving up"


# ── shared backoff, options path: data_utils.run_batches ─────────────────────────
def test_run_batches_whole_batch_failure_uses_shared_backoff(monkeypatch, tmp_path):
    calls = {"n": 0}
    monkeypatch.setattr(yf_client, "backoff", lambda context="": calls.__setitem__("n", calls["n"] + 1))
    monkeypatch.setattr(data_utils.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "YF_BATCH", 2)
    monkeypatch.setattr(config, "YF_RETRIES", 0)
    monkeypatch.delenv("YF_BATCH", raising=False)
    monkeypatch.delenv("YF_RETRIES", raising=False)

    def fetch_all_fail(batch):
        return {}, list(batch)          # the whole batch failed — the rate-limit signal

    data_utils.run_batches(["AAA", "BBB"], fetch_all_fail, {}, str(tmp_path / "c.json"))
    assert calls["n"] >= 1, "a fully-failed batch must trip the shared backoff"


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))


def test_stage_pacing_comes_from_config_not_per_script_constants(monkeypatch):
    # #11: "Pacing knobs come from config.py; no per-script duplicate defaults."
    # fundamentals/model used to hardcode BATCH_SIZE/SLEEP_SEC/MAX_RETRIES, so
    # YF_BATCH etc. were silently ignored by the two heaviest stages.
    import importlib, re
    from pathlib import Path
    for script in ("fundamentals.py", "model.py"):
        src = Path(__file__).with_name(script).read_text(encoding="utf-8")
        for knob in ("BATCH_SIZE", "SLEEP_SEC", "MAX_RETRIES"):
            m = re.search(rf"^{knob}\s*=\s*(.+)$", src, re.M)
            assert m and m.group(1).strip().startswith("config."), f"{script}: {knob} = {m.group(1) if m else None}"

    monkeypatch.setenv("YF_BATCH", "7")
    monkeypatch.setenv("YF_BATCH_FUNDAMENTALS", "5")
    monkeypatch.setenv("YF_RETRIES", "4")
    cfg = importlib.reload(config)
    try:
        assert (cfg.YF_BATCH, cfg.YF_BATCH_FUNDAMENTALS, cfg.YF_RETRIES) == (7, 5, 4)
    finally:
        monkeypatch.undo()
        importlib.reload(config)
    assert config.YF_BATCH_FUNDAMENTALS == 20 and config.YF_SLEEP_FUNDAMENTALS == 3.0, \
        "fundamentals keeps its slower default batch/sleep when nothing is set"
