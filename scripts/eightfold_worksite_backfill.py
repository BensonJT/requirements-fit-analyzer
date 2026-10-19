"""One-off backfill after the 2026-10-19 Eightfold work-site fix.

Microsoft's pcsx list stamped nearly every posting `onsite`, so the screen rejected remote and hybrid
Microsoft roles as "not remote and outside the commute area". The detail stage now reads the real work
site from pcsx/position_details, but it only runs on postings that still lack a JD. This refetches the
work site and travel for every ACTIVE posting on the fixed hosts and updates `workplace_type`,
`work_site` and `travel` only. It never touches description_text, so no review or Jev result goes
stale. All network calls happen first; the database is opened for one short write at the end.
Run `finder.py rescreen-all` afterwards.

    .venv/bin/python scripts/eightfold_worksite_backfill.py              # dry run: fetch and count
    .venv/bin/python scripts/eightfold_worksite_backfill.py --apply
"""
import collections
import os
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.ats import adapters as A  # noqa: E402
from backend.ats import store  # noqa: E402

EMPLOYERS = {"https://apply.careers.microsoft.com": ("Microsoft", "microsoft.com")}


def main(argv=None) -> int:
    apply = "--apply" in (argv or sys.argv[1:])
    con = store.connect() if apply else duckdb.connect(store.DEFAULT_DB_PATH, read_only=True)
    targets = []
    for host, (employer, domain) in EMPLOYERS.items():
        assert host in A._EIGHTFOLD_PCSX_DETAIL_HOSTS
        for pid, rid, wt in con.execute("SELECT posting_id, req_id, workplace_type FROM postings "
                                        "WHERE employer = ? AND platform = 'eightfold' AND status = 'active'",
                                        [employer]).fetchall():
            targets.append((host, domain, pid, rid, wt))
    con.close()
    print(f"{len(targets)} active posting(s) to refetch")

    updates, moves, gone, errors = [], collections.Counter(), 0, 0
    with A.client() as c:
        for i, (host, domain, pid, rid, wt) in enumerate(targets, 1):
            try:
                d = (A._request(c, "GET", f"{host}/api/pcsx/position_details?position_id={rid}"
                                          f"&domain={domain}&hl=en").json().get("data") or {})
            except A.Gone:
                gone += 1
                continue
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"  error {pid}: {str(e)[:100]}")
                continue
            site = A._eightfold_custom(d, A._EIGHTFOLD_WORK_SITE_KEYS)
            travel = A._eightfold_custom(d, A._EIGHTFOLD_TRAVEL_KEYS)
            new = A._work_site_type(site) or wt
            moves[(wt, new)] += 1
            updates.append((new, site, travel, pid))
            if i % 250 == 0:
                print(f"  {i}/{len(targets)} fetched")
            time.sleep(A.PAGE_DELAY * 2)

    print(f"fetched {len(updates)}, gone {gone} (left for the sweep to close), errors {errors}")
    for (old, new), n in moves.most_common():
        print(f"  {old or 'NULL':>7} -> {new or 'NULL':<7} {n}")
    if not apply:
        print("dry run: nothing written (re-run with --apply)")
        return 0
    con = store.connect()
    con.execute("BEGIN")
    con.executemany("UPDATE postings SET workplace_type = ?, work_site = ?, travel = ? WHERE posting_id = ?", updates)
    con.execute("COMMIT")
    con.close()
    print(f"wrote {len(updates)} row(s); next: .venv/bin/python finder.py rescreen-all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
