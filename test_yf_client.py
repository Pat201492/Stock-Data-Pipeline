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


# ── empty-upstream signal + caller tolerance + reporting (issue #38) ─────────────
import pytest


class _FakeTicker:
    """Stand-in for yf.Ticker: returns a canned ``.info`` payload. No network."""
    def __init__(self, symbol, payload):
        self.symbol = symbol
        self.info = payload


class _FakeDF:
    """Minimal pandas-DataFrame stand-in: only the ``.empty`` attribute matters."""
    def __init__(self, empty):
        self.empty = empty


def test_is_empty_recognises_empty_shapes():
    assert yf_client.is_empty(None)
    assert yf_client.is_empty({})
    assert yf_client.is_empty([])
    assert yf_client.is_empty("")
    assert yf_client.is_empty(_FakeDF(empty=True))
    # Real answers (including falsy scalars) are NOT empty
    assert not yf_client.is_empty({"symbol": "AAPL"})
    assert not yf_client.is_empty([{"title": "x"}])
    assert not yf_client.is_empty(_FakeDF(empty=False))
    assert not yf_client.is_empty(0)
    assert not yf_client.is_empty(False)


def test_info_empty_dict_raises_not_returns(monkeypatch):
    # Simulated throttle: stub yfinance .info -> {}. The core fetch path must
    # RAISE (the step fails), never hand back {} as if it were success.
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    monkeypatch.setattr(yf_client.yf, "Ticker", lambda s: _FakeTicker(s, {}))
    with pytest.raises(yf_client.EmptyUpstreamResponse):
        yf_client.yf_info_with_retry("THROTTLED")


def test_info_empty_dataframe_raises(monkeypatch):
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    monkeypatch.setattr(yf_client.yf, "Ticker",
                        lambda s: _FakeTicker(s, _FakeDF(empty=True)))
    with pytest.raises(yf_client.EmptyUpstreamResponse):
        yf_client.yf_info_with_retry("THROTTLED")


def test_info_real_payload_returns(monkeypatch):
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    monkeypatch.setattr(yf_client.yf, "Ticker",
                        lambda s: _FakeTicker(s, {"symbol": "AAPL", "shortRatio": 1.2}))
    info = yf_client.yf_info_with_retry("AAPL")
    assert info["symbol"] == "AAPL"


def test_tolerate_empties_mostly_empty_fails(monkeypatch, tmp_path):
    # Source down/throttled: most of the batch came back empty -> step FAILS.
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    yf_client.reset_empty_rates()
    with pytest.raises(yf_client.EmptyUpstreamResponse):
        yf_client.tolerate_empties("info", total=1000, empty=900, max_empty_rate=0.2)
    # even on failure the rate is recorded for a human to read
    rep = yf_client.empty_rate_report()["info"]
    assert rep["rate"] == 0.9 and rep["ok"] is False


def test_tolerate_empties_genuinely_empty_but_valid_succeeds(monkeypatch, tmp_path):
    # A handful of delisted names / symbols with no news among thousands is normal
    # — the step succeeds and the rate is still reported.
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    yf_client.reset_empty_rates()
    rate = yf_client.tolerate_empties("info", total=1000, empty=3, max_empty_rate=0.2)
    assert rate == 0.003
    rep = yf_client.empty_rate_report()["info"]
    assert rep["ok"] is True


def test_empty_rate_report_persisted_to_file(monkeypatch, tmp_path):
    # Empty-response rate is written somewhere a human / Stock-App #109 can read.
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    yf_client.reset_empty_rates()
    yf_client.tolerate_empties("news", total=500, empty=10, max_empty_rate=0.5)
    import json as _json
    path = tmp_path / "empty_rates.json"
    assert path.exists()
    saved = _json.loads(path.read_text(encoding="utf-8"))
    assert saved["news"]["total"] == 500 and saved["news"]["empty"] == 10


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
