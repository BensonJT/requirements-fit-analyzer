"""Read-only dry run for the residence-restricted-remote rule (docs/SPRINT_PLAN.md §23).

Opens db/jobsearch.duckdb READ-ONLY -- never call backend.ats.store.connect() here, which runs
schema migrations and CREATE/ALTER statements against the file. Applies
backend.screen.residence_restriction to every active, not-yet-rejected posting with a JD that
reads as remote (backend.screen.is_remote), and reports what the new rule would do.

Do NOT run this while another process holds the DuckDB lock (a truthful open error means the lock
is still held -- wait for it to clear, do not retry in a loop).

Usage (once the lock is free):
    python -m scripts.residence_dryrun
    python -m scripts.residence_dryrun --db /path/to/jobsearch.duckdb
"""
import argparse
import json
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import profile as P  # noqa: E402
from backend import screen as S  # noqa: E402

DEFAULT_DB_PATH = os.path.join(ROOT, "db", "jobsearch.duckdb")


def _rows(con):
    """Active, not-rejected postings with a non-empty JD, joined to their latest screen verdict."""
    return con.execute("""
        SELECT p.posting_id, p.employer, p.title, p.location_primary, p.locations,
               p.workplace_type, p.description_text
        FROM postings p
        JOIN vw_screen_latest s USING (posting_id)
        WHERE p.status = 'active'
          AND s.verdict != 'reject'
          AND p.description_text IS NOT NULL
          AND length(trim(p.description_text)) > 0
    """).fetchall()


def evaluate(rows):
    """Runs the rule over `rows` (as returned by `_rows`). Returns (counts, newly_rejected) where
    `newly_rejected` is a list of (posting_id, employer, title, places, phrase)."""
    home_states = S.states_in(P.HOME or "")
    counts = {"not_remote": 0, "none": 0, "unclear": 0, "places_pass": 0, "places_reject": 0}
    newly_rejected = []

    for posting_id, employer, title, location_primary, locations_json, workplace_type, description_text in rows:
        try:
            locs = json.loads(locations_json) if locations_json else []
        except (TypeError, ValueError):
            locs = []
        listing = S.Listing(source="dryrun", search_pass="dryrun", title=title or "", company=employer or "",
                             location=location_primary or "", url="", description=description_text or "",
                             locations=locs, extra={"workplace_type": workplace_type})
        if not S.is_remote(listing):
            counts["not_remote"] += 1
            continue
        kind, places, phrase = S.residence_restriction(
            f"{listing.title}\n{listing.location}\n{listing.description}")
        if kind == "none":
            counts["none"] += 1
        elif kind == "unclear":
            counts["unclear"] += 1
        else:  # "places"
            matched = any(S.place_matches(pl, P.COMMUTABLE_PLACES) or (S.states_in(pl) & home_states)
                          for pl in places)
            if matched:
                counts["places_pass"] += 1
            else:
                counts["places_reject"] += 1
                newly_rejected.append((posting_id, employer, title, ", ".join(places), phrase))
    return counts, newly_rejected


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="path to jobsearch.duckdb (default: db/jobsearch.duckdb)")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    try:
        rows = _rows(con)
    finally:
        con.close()

    counts, newly_rejected = evaluate(rows)

    print(f"{len(rows)} active, not-rejected posting(s) with a JD scanned.")
    print("Counts by kind:")
    for k in ("not_remote", "none", "unclear", "places_pass", "places_reject"):
        print(f"  {k}: {counts[k]}")
    print(f"\n{len(newly_rejected)} would-be-newly-rejected row(s):")
    for posting_id, employer, title, places, phrase in newly_rejected:
        print(f"{posting_id} | {employer} | {title} | {places} | {phrase!r}")


if __name__ == "__main__":
    main()
