"""One-off cleanup after the 2026-10-15 tracker-matcher fix (a req named in the Role cell now wins).

The old date-blind fuzzy match wrote `build` decisions (source 'tracker') onto SIBLING reqs whose titles merely
resembled the tracker row -- which hides those postings from Top Jobs. This lists every posting the OLD rule
matched and the NEW rule does not, keeps the ones whose title is identical to the new match (same job, other id
format), and with --apply deletes their tracker-sourced decisions, then re-syncs the tracker table.

    .venv/bin/python scripts/tracker_rematch_cleanup.py            # dry run
    .venv/bin/python scripts/tracker_rematch_cleanup.py --apply

One-off, kept for the record; do not re-run. Tracker matching has changed since (a 90-day window on fuzzy
matches, the Posting ID column), so the "old rule" here is no longer the rule it was written against. The
dry run opens DuckDB read-only and reads the tracker markdown under $JOBSEARCH_VAULT_DIR (not loaded from
.env); --apply opens DuckDB read-write. A held lock fails the open (duckdb.IOException).
"""
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.ats import store  # noqa: E402
from backend.finder import tracker_sync as T  # noqa: E402
from backend.screen import norm_title  # noqa: E402

VAULT = os.environ.get("JOBSEARCH_VAULT_DIR") or ""


def main(argv=None) -> int:
    if {"-h", "--help"} & set(argv or sys.argv[1:]):   # --help prints this docstring; it never runs the script
        print(__doc__)
        return 0
    apply = "--apply" in (argv or sys.argv[1:])
    con = duckdb.connect(store.DEFAULT_DB_PATH, read_only=not apply)
    rows = T.parse_tracker(T.job_search_dir(VAULT) / T.TRACKER_FILE)
    p5 = con.execute("SELECT posting_id, employer, title, first_seen_at, req_id FROM postings").fetchall()
    old = T.match_rows(rows, [r[:4] for r in p5])
    new = T.match_rows(rows, p5)
    title = {r[0]: r[2] or "" for r in p5}
    still = {b for _, b, _ in new if b}
    drop = []
    for (r, a, _), (_, b, _) in zip(old, new):
        if not a or a == b or a in still:
            continue
        if b is None and norm_title(title.get(a, "")) == norm_title(r.role.split("(")[0]):
            continue  # identical title, the req id just uses another format: same job, keep it
        if b and norm_title(title.get(a, "")) == norm_title(title.get(b, "")):
            continue
        drop.append((a, r.company, title.get(a, "")))
    have = {pid for (pid,) in con.execute(
        "SELECT posting_id FROM decisions WHERE source = 'tracker' AND posting_id IN (SELECT unnest(?::VARCHAR[]))",
        [[d[0] for d in drop]]).fetchall()} if drop else set()
    for pid, company, t in drop:
        print(f"  {'DROP' if pid in have else 'none'}  {pid}  {company[:24]} | {t[:70]}")
    print(f"{len(have)} tracker-sourced decision(s) to remove on wrongly matched sibling reqs")
    if not apply:
        print("dry run; pass --apply")
        return 0
    con.execute("DELETE FROM decisions WHERE source = 'tracker' AND posting_id IN (SELECT unnest(?::VARCHAR[]))",
                [list(have)])
    print("removed;", T.sync(con, VAULT, log=print))
    return 0


if __name__ == "__main__":
    sys.exit(main())
