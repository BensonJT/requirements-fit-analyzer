"""Re-derive `postings.workplace_type` from the stored platform flag (DEFECT 2026-09-28, docs/STATUS.md).

Workday's list feed keeps `remoteType` in `raw_json` ("Office Worker (NOT Remote)", "Virtual Worker", "Field",
"Hybrid", ...); `normalize.workplace_type` read it wrong until 2026-09-30 and the list adapter ignored it.
This recomputes the value for every posting whose raw_json carries `remoteType`, writes only the rows that
change, and prints the before -> after counts. The rescreen itself comes from RULES_CODE_VERSION (bumped the
same day): the next screen stage re-screens every active row, so the commute rule sees the corrected flag.

    .venv/bin/python scripts/renormalize_workplace.py            # dry run: counts only
    .venv/bin/python scripts/renormalize_workplace.py --apply    # write the changes
"""
import argparse
import collections
import json
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402

SQL = """
    SELECT posting_id, employer, workplace_type, json_extract_string(raw_json, '$.remoteType') AS flag,
           location_primary, locations, status
    FROM postings WHERE raw_json LIKE '%remoteType%'
"""


def plan(con) -> list:
    """[(posting_id, employer, old, new, flag, status)] for rows whose value would change."""
    out = []
    for pid, employer, old, flag, loc, locs, status in con.execute(SQL).fetchall():
        try:
            extra = json.loads(locs) if locs else []
        except ValueError:
            extra = []
        new = N.workplace_type(flag, *[t for t in [loc, *extra] if isinstance(t, str) and t])
        if new != old:
            out.append((pid, employer, old, new, flag, status))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=os.environ.get("JOBSEARCH_DB") or store.DEFAULT_DB_PATH)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    a = ap.parse_args(argv)
    con = duckdb.connect(a.db, read_only=not a.apply)
    changes = plan(con)
    by = collections.Counter((emp, old, new, flag) for _pid, emp, old, new, flag, _st in changes)
    active = sum(1 for c in changes if c[5] == "active")
    print(f"{len(changes)} row(s) change ({active} active) of the rows carrying a platform flag:")
    for (emp, old, new, flag), n in sorted(by.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5d}  {emp}: {flag!r}: {old} -> {new}")
    if not a.apply:
        print("dry run; pass --apply to write")
        return 0
    con.execute("BEGIN")
    con.executemany("UPDATE postings SET workplace_type = ? WHERE posting_id = ?",
                    [[new, pid] for pid, _e, _o, new, _f, _s in changes])
    con.execute("COMMIT")
    print(f"updated {len(changes)} row(s); the next screen stage re-screens them (rules version bumped)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
