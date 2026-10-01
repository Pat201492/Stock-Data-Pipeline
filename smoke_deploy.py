"""
smoke_deploy.py — one-command post-deploy check for the Stock Data Pipeline API.

Hits the live endpoints a consumer app depends on and prints PASS/FAIL per check,
exiting non-zero if any fails (so CI / a shell `&&` chain can gate on it). Catches
the two silent-degrade traps a fresh Fly deploy hits: SEC_USER_AGENT unset (no
as-filed XBRL Magic Formula) and FRED_API_KEY unset (/api/macro 503).

Usage:
    python smoke_deploy.py https://stock-data-pipeline-pat.fly.dev
    python smoke_deploy.py http://localhost:8000 --allow-empty

--allow-empty tolerates an empty /api/stocks while the cold-volume bootstrap is
still running (fresh volume: no data until the first pipeline run finishes).
"""
import argparse
import sys

import httpx


def check_health(client):
    r = client.get("/health")
    if r.status_code != 200:
        return False, f"/health returned {r.status_code}"
    cfg = r.json().get("config", {})
    missing = [k for k in ("sec_user_agent", "fred_api_key") if not cfg.get(k)]
    if missing:
        return False, f"/health config false: {', '.join(missing)} (secret unset)"
    return True, "/health 200, SEC_USER_AGENT + FRED_API_KEY set"


def check_stocks(client, allow_empty=False):
    r = client.get("/api/stocks")
    if r.status_code != 200:
        return False, f"/api/stocks returned {r.status_code}"
    rows = r.json().get("stocks", [])
    if not rows:
        if allow_empty:
            return True, "/api/stocks empty (allowed — bootstrap may be running)"
        return False, "/api/stocks is empty (volume cold? pass --allow-empty)"
    if "magic_source" not in rows[0]:
        return False, "/api/stocks rows missing magic_source field"
    return True, f"/api/stocks has {len(rows)} rows carrying magic_source"


def check_macro(client):
    r = client.get("/api/macro")
    if r.status_code != 200:
        return False, f"/api/macro returned {r.status_code} (FRED_API_KEY unset?)"
    if "vix" not in r.json():
        return False, "/api/macro 200 but no vix field"
    return True, "/api/macro 200 with vix"


def check_integrity(client):
    r = client.get("/api/integrity")
    if r.status_code != 200:
        return False, f"/api/integrity returned {r.status_code}"
    return True, "/api/integrity 200"


def run_checks(client, allow_empty=False):
    """Run every check against `client`; print PASS/FAIL lines. Return True iff all
    passed."""
    checks = [
        ("health",    lambda: check_health(client)),
        ("stocks",    lambda: check_stocks(client, allow_empty=allow_empty)),
        ("macro",     lambda: check_macro(client)),
        ("integrity", lambda: check_integrity(client)),
    ]
    all_ok = True
    for name, fn in checks:
        try:
            ok, msg = fn()
        except Exception as e:                       # a network/JSON error is a FAIL, not a crash
            ok, msg = False, f"{type(e).__name__}: {e}"
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {msg}")
    return all_ok


def main(argv=None):
    p = argparse.ArgumentParser(description="Post-deploy smoke test for the API.")
    p.add_argument("base_url", help="e.g. https://stock-data-pipeline-pat.fly.dev")
    p.add_argument("--allow-empty", action="store_true",
                   help="tolerate an empty /api/stocks (cold volume / bootstrap)")
    args = p.parse_args(argv)

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30) as client:
        ok = run_checks(client, allow_empty=args.allow_empty)

    print("SMOKE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
