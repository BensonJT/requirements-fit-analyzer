"""Read-only dry run for the private employer-residence-note rule (docs/SPRINT_PLAN.md §23 amendment).

Opens db/jobsearch.duckdb READ-ONLY -- never call backend.ats.store.connect() here, which runs
schema migrations and CREATE/ALTER statements against the file. For one employer, evaluates every
active posting with a JD that reads as remote (backend.screen.is_remote) and whose JD text carries no
residence restriction of its own (backend.screen.residence_restriction kind == "none" -- a posting the
text rule already resolved is left to that rule; see backend.screen.employer_residence_note), under a
HYPOTHETICAL note with empty hubs (the worst case: always reject). It never reads
backend.profile.EMPLOYER_RESIDENCE_NOTES, so it prints nothing that depends on a real note's contents.

Do NOT run this while another process holds the DuckDB lock (a truthful open error means the lock is
still held -- wait for it to clear, do not retry in a loop). Check first with:
    ps -eo pid,cmd | grep "\\.venv/bin/python"

Usage (once the lock is free):
    python -m scripts.employer_note_dryrun --employer "Acme Payments"
    python -m scripts.employer_note_dryrun --db /path/to/jobsearch.duckdb --employer "Acme Payments"
"""
import argparse
import json
import os
import sys

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend import screen as S  # noqa: E402

DEFAULT_DB_PATH = os.path.join(ROOT, "db", "jobsearch.duckdb")


def _rows(con, employer: str):
    """Active, not-rejected postings for one employer (matched the same normalized way screen.py
    matches employers elsewhere), joined to their latest screen verdict."""
    all_rows = con.execute("""
        SELECT p.posting_id, p.employer, p.title, p.location_primary, p.locations,
               p.workplace_type, p.description_text
        FROM postings p
        JOIN vw_screen_latest s USING (posting_id)
        WHERE p.status = 'active'
          AND s.verdict != 'reject'
          AND p.description_text IS NOT NULL
          AND length(trim(p.description_text)) > 0
    """).fetchall()
    key = S.norm_company(employer)
    keys = S.company_keys(employer)
    return [r for r in all_rows if S.company_matches(S.norm_company(r[1] or ""), keys) or S.company_matches(key, S.company_keys(r[1] or ""))]


def evaluate(rows):
    """Runs the hypothetical empty-hubs note over `rows` (as returned by `_rows`). Returns
    (would_change, sample_titles) -- postings that would flip to reject, and up to 10 of their titles."""
    would_change = []
    for posting_id, employer, title, location_primary, locations_json, workplace_type, description_text in rows:
        try:
            locs = json.loads(locations_json) if locations_json else []
        except (TypeError, ValueError):
            locs = []
        listing = S.Listing(source="dryrun", search_pass="dryrun", title=title or "", company=employer or "",
                             location=location_primary or "", url="", description=description_text or "",
                             locations=locs, extra={"workplace_type": workplace_type})
        if not S.is_remote(listing):
            continue
        kind, _, _ = S.residence_restriction(f"{listing.title}\n{listing.location}\n{listing.description}")
        if kind != "none":
            continue  # already resolved by the JD-text rule; the employer note never gets a second vote
        would_change.append((posting_id, title))
    return would_change, [t for _, t in would_change[:10]]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB_PATH, help="path to jobsearch.duckdb (default: db/jobsearch.duckdb)")
    ap.add_argument("--employer", required=True, help="employer name to evaluate")
    args = ap.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    try:
        rows = _rows(con, args.employer)
    finally:
        con.close()

    would_change, sample = evaluate(rows)

    print(f"Employer: {args.employer}")
    print(f"{len(rows)} active, not-rejected, remote-or-unresolved posting(s) matched.")
    print(f"{len(would_change)} would change verdict to reject under a hypothetical empty-hubs note.")
    print("Sample titles:")
    for t in sample:
        print(f"  {t}")


if __name__ == "__main__":
    main()
