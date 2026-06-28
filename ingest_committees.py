"""
ingest_committees.py — Pull @unitedstates/congress committee + member data
Sources:
  legislators-current.yaml  — active members with bioguide IDs
  legislators-historical.yaml — historical members
  committee-membership-current.yaml — current committee assignments
"""
import urllib.request, time, sys, re
from datetime import date

try:
    import yaml
except ImportError:
    import subprocess, sys as _sys
    subprocess.check_call([_sys.executable, "-m", "pip", "install", "pyyaml", "-q"])
    import yaml

from politicians_database import (
    init_pol_db, SessionLocal,
    Politician, Committee, CommitteeMembership,
)

BASE_URL = (
    "https://raw.githubusercontent.com/unitedstates/congress-legislators/main/"
)

URLS = {
    "current":     BASE_URL + "legislators-current.yaml",
    "historical":  BASE_URL + "legislators-historical.yaml",
    "committees":  BASE_URL + "committee-membership-current.yaml",
    "com_list":    BASE_URL + "committees-current.yaml",
}


def _fetch_yaml(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.urlopen(url, timeout=30)
            return yaml.safe_load(req.read().decode("utf-8"))
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def _parse_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def ingest():
    init_pol_db()
    db = SessionLocal()
    try:
        # ── Committee list ────────────────────────────────────────
        print("[committees] Fetching committee list…")
        com_data = _fetch_yaml(URLS["com_list"])
        com_count = 0
        for com in com_data:
            cid = com.get("thomas_id") or com.get("id", "")
            if not cid:
                continue
            db.merge(Committee(
                committee_id        = cid,
                name                = com.get("name", ""),
                chamber             = com.get("type", ""),
                parent_committee_id = None,
            ))
            for sub in com.get("subcommittees", []):
                # Subcommittee id must match committee-membership-current.yaml keys,
                # which concatenate parent + subcommittee thomas_id with NO separator
                # (e.g. parent HSAG + sub 15 -> "HSAG15"). A dash here broke the join,
                # leaving subcommittees with zero members.
                sid = f"{cid}{sub.get('thomas_id', '')}"
                db.merge(Committee(
                    committee_id        = sid,
                    name                = sub.get("name", ""),
                    chamber             = com.get("type", ""),
                    parent_committee_id = cid,
                ))
            com_count += 1
        db.commit()
        print(f"[committees] {com_count} committees upserted")

        # ── Legislators ───────────────────────────────────────────
        for key, active in [("current", True), ("historical", False)]:
            print(f"[legislators] Fetching {key}…")
            members = _fetch_yaml(URLS[key])
            count = 0
            for m in members:
                bio = m.get("id", {}).get("bioguide")
                if not bio:
                    continue
                name  = m.get("name", {})
                terms = m.get("terms", [{}])
                last_term = terms[-1] if terms else {}
                db.merge(Politician(
                    bioguide_id = bio,
                    first_name  = name.get("first", ""),
                    last_name   = name.get("last", ""),
                    chamber     = last_term.get("type", ""),   # 'sen' | 'rep'
                    party       = last_term.get("party", ""),
                    state       = last_term.get("state", ""),
                    district    = str(last_term.get("district", "")) if last_term.get("district") else None,
                    active      = active,
                ))
                count += 1
            db.commit()
            print(f"[legislators] {count} {key} members upserted")

        # ── Committee memberships ─────────────────────────────────
        print("[memberships] Fetching current committee memberships…")
        mem_data = _fetch_yaml(URLS["committees"])
        mem_count = 0
        for cid, members in mem_data.items():
            for m in members:
                bio = m.get("bioguide")
                if not bio:
                    continue
                # Use a fixed sentinel date for current memberships so
                # re-runs don't grow the table — actual start dates aren't
                # in the membership YAML, only in terms data.
                db.merge(CommitteeMembership(
                    bioguide_id  = bio,
                    committee_id = cid,
                    start_date   = date(2000, 1, 1),
                    role         = m.get("title", "Member"),
                    end_date     = None,
                ))
                mem_count += 1
        db.commit()
        print(f"[memberships] {mem_count} memberships upserted")

    finally:
        db.close()


if __name__ == "__main__":
    ingest()
