"""
test_validate.py — integrity gate + exit contract (issue #34)

Seeds temp SQLite databases with and without each finding, points
config.DB_PATH / config.POL_DB_PATH at them, and asserts validate.main()'s
exit code and output. Also checks run.py runs validate last with the stages
stubbed.

Run:  python -m pytest test_validate.py   (or: python test_validate.py)
"""
import importlib
import sqlite3

import pytest

import config
import validate


# ── seeding helpers ──────────────────────────────────────────────────────────

def _seed_pol(path, *, insider_dups=0, orphans=0, stale=0, unknown_bio=0):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE insider_trades (
            filing_id TEXT PRIMARY KEY, ticker TEXT, transaction_date TEXT,
            insider_name TEXT, transaction_type TEXT, shares INTEGER,
            source_url TEXT);
        CREATE TABLE committees (committee_id TEXT PRIMARY KEY,
            parent_committee_id TEXT);
        CREATE TABLE committee_memberships (bioguide_id TEXT, committee_id TEXT);
        CREATE TABLE congressional_trades (bioguide_id TEXT, transaction_date TEXT);
        CREATE TABLE politicians (bioguide_id TEXT PRIMARY KEY);
    """)
    # A baseline valid politician + committee so clean findings are 0.
    con.execute("INSERT INTO politicians (bioguide_id) VALUES ('P000001')")
    con.execute("INSERT INTO committees (committee_id) VALUES ('HSAG')")
    con.execute("INSERT INTO committee_memberships VALUES ('P000001', 'HSAG')")

    # insider duplicates: same logical key, distinct filing_id -> (n+1) rows per dup
    for i in range(insider_dups + (1 if insider_dups else 0)):
        con.execute(
            "INSERT INTO insider_trades VALUES (?, 'AAPL', '2024-01-01', "
            "'Tim Cook', 'P', 100, ?)",
            (f"f{i}", "edgar" if i == 0 else f"mirror:{i}"))

    for i in range(orphans):
        con.execute("INSERT INTO committee_memberships VALUES (?, ?)",
                    (f"X{i}", f"NO_SUCH_{i}"))

    for i in range(stale):
        con.execute("INSERT INTO committees (committee_id) VALUES (?)",
                    (f"HSAG-{i}",))  # dashed id, no members

    for i in range(unknown_bio):
        con.execute("INSERT INTO congressional_trades VALUES (?, '2024-01-01')",
                    (f"UNKNOWN{i}",))

    con.commit()
    con.close()


def _seed_stk(path, *, outliers=0):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE fundamentals (ticker TEXT, pe REAL, roic REAL, equity REAL);
        CREATE TABLE stocks (ticker TEXT, price REAL);
    """)
    # Each outlier is one pe>1000 row.
    for i in range(outliers):
        con.execute("INSERT INTO fundamentals VALUES (?, 5000, 1, 1)", (f"T{i}",))
    con.commit()
    con.close()


@pytest.fixture
def dbs(tmp_path, monkeypatch):
    """Point config at fresh temp DB paths; return (pol_path, stk_path)."""
    pol = tmp_path / "politicians.db"
    stk = tmp_path / "stocks.db"
    monkeypatch.setattr(config, "POL_DB_PATH", str(pol))
    monkeypatch.setattr(config, "DB_PATH", str(stk))
    return pol, stk


# ── findings / exit contract ───────────────────────────────────────────────

def test_strict_fails_on_insider_duplicate(dbs, capsys):
    pol, stk = dbs
    _seed_pol(str(pol), insider_dups=1)
    _seed_stk(str(stk))
    rc = validate.main(["--strict"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "insider_duplicates" in out


def test_strict_passes_on_clean_db(dbs):
    pol, stk = dbs
    _seed_pol(str(pol))
    _seed_stk(str(stk))
    assert validate.main(["--strict"]) == 0


def test_no_strict_exits_zero_on_dirty_db(dbs):
    pol, stk = dbs
    _seed_pol(str(pol), insider_dups=3, orphans=2)
    _seed_stk(str(stk), outliers=4)
    assert validate.main([]) == 0


def test_fundamental_outliers_threshold(dbs, monkeypatch):
    pol, stk = dbs
    _seed_pol(str(pol))
    monkeypatch.setenv("VALIDATE_MAX_FUNDAMENTAL_OUTLIERS", "5")

    _seed_stk(str(stk), outliers=5)
    assert validate.main(["--strict"]) == 0   # 5 <= 5 passes

    stk.unlink()
    _seed_stk(str(stk), outliers=6)
    assert validate.main(["--strict"]) == 1   # 6 > 5 fails


def test_each_finding_can_fail_strict(dbs):
    pol, stk = dbs
    _seed_stk(str(stk))
    for kwargs, name in [
        (dict(orphans=1),      "orphan_memberships"),
        (dict(stale=1),        "stale_dashed_committees"),
        (dict(unknown_bio=1),  "unknown_bioguide_trades"),
    ]:
        pol.unlink(missing_ok=True)
        _seed_pol(str(pol), **kwargs)
        assert validate.main(["--strict"]) == 1, name


# ── config-driven paths (issue #10) ──────────────────────────────────────────

def test_reads_config_paths_under_data_dir(tmp_path, monkeypatch):
    """With DATA_DIR set, validate opens the DBs under that directory."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # Another test module sets DB_PATH/POL_DB_PATH in os.environ at import; those
    # would override data_path(). Clear them so DATA_DIR drives the resolution.
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("POL_DB_PATH", raising=False)
    importlib.reload(config)
    importlib.reload(validate)
    try:
        assert config.DB_PATH == str(tmp_path / "stocks.db")
        assert config.POL_DB_PATH == str(tmp_path / "politicians.db")
        _seed_pol(config.POL_DB_PATH, insider_dups=1)
        _seed_stk(config.DB_PATH)
        findings, _, _ = validate.report()
        assert findings["insider_duplicates"] == 1
    finally:
        monkeypatch.delenv("DATA_DIR", raising=False)
        importlib.reload(config)
        importlib.reload(validate)


# ── run.py integration (stages stubbed) ──────────────────────────────────────

def _stub_run(monkeypatch, validate_ok):
    """Stub step execution so no real script/subprocess runs. run.py now drives
    the shared step_runner, so stub step_runner._run_one (issue #35)."""
    import run
    import step_runner
    calls = []

    def fake_run_one(step, log):
        calls.append((step.name, tuple(step.args)))
        return validate_ok if step.name == "validate" else True

    import contextlib
    monkeypatch.setattr(step_runner, "_run_one", fake_run_one)
    monkeypatch.setattr(run, "log", lambda *a, **k: None)
    monkeypatch.setattr(run.config, "yfinance_lock", contextlib.nullcontext)
    return run, calls


def test_run_runs_validate_last_and_fails(monkeypatch):
    run, calls = _stub_run(monkeypatch, validate_ok=False)
    monkeypatch.setattr("sys.argv", ["run.py"])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 1
    assert calls[-1] == ("validate", ("--strict",))


def test_run_validate_passes_exits_zero(monkeypatch):
    run, calls = _stub_run(monkeypatch, validate_ok=True)
    monkeypatch.setattr("sys.argv", ["run.py"])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 0
    assert calls[-1] == ("validate", ("--strict",))


def test_run_skip_validate(monkeypatch):
    run, calls = _stub_run(monkeypatch, validate_ok=False)
    monkeypatch.setattr("sys.argv", ["run.py", "--skip-validate"])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 0
    assert all(s != "validate" for s, _ in calls)


def test_run_from_still_ends_with_validate(monkeypatch):
    run, calls = _stub_run(monkeypatch, validate_ok=True)
    monkeypatch.setattr("sys.argv", ["run.py", "--from", "news"])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert exc.value.code == 0
    assert calls[-1] == ("validate", ("--strict",))
    assert calls[0][0] == "news"


if __name__ == "__main__":
    import subprocess, sys
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", __file__]))
