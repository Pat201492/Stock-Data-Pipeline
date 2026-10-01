"""
test_ingest_commodities.py — Tests for commodity futures ingestion (issue #40)
Tests stub the yfinance source. Per-symbol empty is legitimate; aggregate empty
fails the step per piece 1's tolerance rule. Empty-rate is reported.

Run:  python -m pytest test_ingest_commodities.py      (or: python test_ingest_commodities.py)
"""

import pytest
import config
import ingest_commodities
import yf_client


class _FakeDataFrame:
    """Minimal pandas DataFrame stand-in for yfinance data."""
    def __init__(self, empty):
        self.empty = empty


def test_ingest_commodities_all_symbols_empty_fails(monkeypatch, tmp_path):
    """When ALL commodity symbols return empty, step fails + rate is reported."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    yf_client.reset_empty_rates()

    # Stub the underlying yf.download (not the wrapper) to return empty
    monkeypatch.setattr(yf_client.yf, "download",
                       lambda *a, **k: _FakeDataFrame(empty=True))

    success = ingest_commodities.ingest()
    assert not success, "ingest should fail when all symbols are empty"

    # Rate is still reported (for human monitoring)
    rep = yf_client.empty_rate_report()["ingest_commodities"]
    assert rep["empty"] == 30  # all commodities
    assert rep["ok"] is False


def test_ingest_commodities_few_empties_succeeds(monkeypatch, tmp_path):
    """A few empty symbols (delisted / thin) is normal; step succeeds."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    yf_client.reset_empty_rates()

    call_count = [0]
    def mock_download(*a, **k):
        call_count[0] += 1
        # First few are empty, rest have data
        return _FakeDataFrame(empty=(call_count[0] <= 3))

    monkeypatch.setattr(yf_client.yf, "download", mock_download)

    success = ingest_commodities.ingest()
    assert success, "ingest should succeed with few empties"

    rep = yf_client.empty_rate_report()["ingest_commodities"]
    assert rep["empty"] == 3
    assert rep["ok"] is True
    assert rep["rate"] == pytest.approx(3 / 30, abs=0.01)


def test_ingest_commodities_mostly_empty_fails(monkeypatch, tmp_path):
    """When >20% of symbols are empty, step fails (throttle not delisting)."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    yf_client.reset_empty_rates()

    call_count = [0]
    def mock_download(*a, **k):
        call_count[0] += 1
        # 25% empty (7.5 out of 30, so 7 or 8)
        return _FakeDataFrame(empty=(call_count[0] <= 8))

    monkeypatch.setattr(yf_client.yf, "download", mock_download)

    success = ingest_commodities.ingest()
    assert not success, "ingest should fail with 27% empty (exceeds 20% tolerance)"

    rep = yf_client.empty_rate_report()["ingest_commodities"]
    assert rep["ok"] is False


def test_ingest_commodities_boundary_tolerance(monkeypatch, tmp_path):
    """At exactly the tolerance threshold (20%), step succeeds."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    yf_client.reset_empty_rates()

    # 30 symbols × 20% = 6, so 6 empties should pass, 7 should fail
    call_count = [0]
    def mock_download(*a, **k):
        call_count[0] += 1
        return _FakeDataFrame(empty=(call_count[0] <= 6))

    monkeypatch.setattr(yf_client.yf, "download", mock_download)

    success = ingest_commodities.ingest()
    assert success, "6/30 = 20% <= 20% tolerance"

    rep = yf_client.empty_rate_report()["ingest_commodities"]
    assert rep["ok"] is True


def test_ingest_commodities_empty_rate_persisted(monkeypatch, tmp_path):
    """Empty-rate is written to empty_rates.json for observability."""
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(yf_client, "pace", lambda: None)
    yf_client.reset_empty_rates()

    call_count = [0]
    def mock_download(*a, **k):
        call_count[0] += 1
        return _FakeDataFrame(empty=(call_count[0] <= 2))

    monkeypatch.setattr(yf_client.yf, "download", mock_download)

    ingest_commodities.ingest()

    import json
    path = tmp_path / "empty_rates.json"
    assert path.exists(), "empty_rates.json should be written"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "ingest_commodities" in saved
    assert saved["ingest_commodities"]["empty"] == 2
    assert saved["ingest_commodities"]["total"] == 30


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
