#!/usr/bin/env python3
"""Find employer boilerplate the splitter still files as requirements, by repetition (read-only).

A footer repeats in nearly every posting an employer publishes (one bank's entity notice: 231 of 231); a real
requirement line, even a templated one, tops out far lower (the same bank's most-reused requirement: 24 of 231).
This script splits each active posting with the CURRENT splitter, counts each unit's source line per employer, and
lists lines that appear in at least --share of an employer's postings. Those are candidates for FOOTER_LINE in
backend/finder/requirements.py, added by hand after reading them: repetition is a detector here, never a runtime
filter, so a common real requirement is never dropped automatically.

Qualification-shaped lines (years / degree / leading "Must") are listed separately and are never candidates.

    .venv/bin/python scripts/splitter_audit.py [--min-postings 20] [--share 0.5] [--workers 6] [--db ...]

Splitting runs about a second per JD, so a full audit of the large employers takes a few minutes on 6 workers.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from multiprocessing import Pool  # noqa: E402

import duckdb  # noqa: E402

from backend.finder.requirements import SPLITTER_VERSION, qualification_shaped, split_requirements  # noqa: E402

SQL = """SELECT employer, posting_id, description_text FROM postings
         WHERE status = 'active' AND coalesce(track, 'fit') = 'fit' AND length(coalesce(description_text, '')) > 200
           AND employer IN (SELECT employer FROM postings WHERE status = 'active' AND coalesce(track, 'fit') = 'fit'
                            AND description_text IS NOT NULL GROUP BY 1 HAVING count(*) >= ?)"""


def _sources(row):
    employer, _pid, text = row
    return employer, {u.source.strip() for u in split_requirements(text)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=os.environ.get("JOBSEARCH_DB", "db/jobsearch.duckdb"))
    ap.add_argument("--min-postings", type=int, default=20, help="employers with at least this many active JDs")
    ap.add_argument("--share", type=float, default=0.5, help="report lines in at least this share of an employer's JDs")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args(argv)

    con = duckdb.connect(args.db, read_only=True)
    con.execute("SET enable_progress_bar = false")
    rows = con.execute(SQL, [args.min_postings]).fetchall()
    con.close()
    counts, totals = defaultdict(Counter), Counter()
    with Pool(args.workers) as pool:
        for employer, sources in pool.imap_unordered(_sources, rows, chunksize=20):
            totals[employer] += 1
            counts[employer].update(sources)

    print(f"splitter {SPLITTER_VERSION} · {len(totals)} employers with >= {args.min_postings} active JDs · "
          f"lines in >= {args.share:.0%} of an employer's postings\n")
    found = 0
    for employer in sorted(totals, key=lambda e: -totals[e]):
        n = totals[employer]
        hits = [(c, s) for s, c in counts[employer].items() if c / n >= args.share]
        if not hits:
            continue
        print(f"## {employer} ({n} postings)")
        for c, s in sorted(hits, reverse=True):
            tag = "protected (qualification-shaped)" if qualification_shaped(s) else "CANDIDATE"
            found += tag == "CANDIDATE"
            print(f"  {c:>4}/{n} {c / n:4.0%}  {tag:<32} {s[:150]}")
        print()
    print(f"{found} candidate footer line(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
