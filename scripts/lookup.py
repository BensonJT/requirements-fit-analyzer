#!/usr/bin/env python3
"""Look up one posting: its JD, every score the pipeline holds for it, and any decision. Read-only.

Usage (from the repo root):
    .venv/bin/python scripts/lookup.py <target>              # summary: identity, scores, judge, Jev, coverage, decision
    .venv/bin/python scripts/lookup.py <target> --jd         # also print the full job description
    .venv/bin/python scripts/lookup.py --search "<regex>"    # list postings whose title or JD matches (newest first)

<target> is anything `finder.py mark` accepts, resolved by the same `resolve_posting`:
    a posting_id (20 hex chars)      e.g. 3f9a0c...   (the id shown in reports and decisions)
    a posting URL                    the link in a Top_Jobs / Jobs_Found row; a URL that only contains
                                     the req id also matches
    "employer|title"                 fuzzy employer + similar title, active postings first

Options:
    --jd            print description_text after the summary
    --search RE     case-insensitive regex over title + description; add --all to include closed postings
    --limit N       rows for --search (default 25)
    --db PATH       database (default db/jobsearch.duckdb)

What the summary shows, and where it comes from:
    identity     postings (employer, title, url, location, workplace, pay, status, dates)
    screen       vw_lens_fit: final_score, band, verdict, lens probabilities (process / technical / AI),
                 level_fit, required-fit model, human or judge lens grades, required_fit / required_unmet, reasons
    judge        vw_llm_labels_latest_judge: the latest LLM judge grade, required_fit, rationale
    Jev          vw_jev_latest: per-lens grades, required_fit, derive_why (the end-of-pipeline demotion reviewer)
    coverage     vw_coverage_latest: share of Required lines matched to evidence, and the gaps
    decision     vw_decisions + tracker: build / pass / hold with reason, and any Application_Tracker match

The connection is read_only, so this never migrates or writes. A running sweep holds DuckDB's write lock;
if the database is locked, wait for the sweep (or overnight run) to finish and retry.
"""
import argparse
import os
import sys
import textwrap
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from finder import resolve_posting  # noqa: E402  (same target rules as `finder.py mark`)

DEFAULT_DB = REPO / "db" / "jobsearch.duckdb"
# Same cap store.connect uses: the WSL VM has 8 GB and DuckDB's default (80% of RAM) can starve it.
MEMORY_LIMIT = os.environ.get("JOBSEARCH_DUCKDB_MEMORY_LIMIT", "3500MB")


def connect(path):
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.IOException as exc:
        sys.exit(f"lookup: cannot open {path} read-only ({exc}).\n"
                 "A sweep or overnight run probably holds the write lock; retry when it finishes.")
    con.execute(f"SET memory_limit = '{MEMORY_LIMIT}'")
    return con


def one(con, sql, pid):
    """First row of a per-posting view as a dict, or {} (views are optional on older schemas)."""
    try:
        cur = con.execute(sql, [pid])
    except duckdb.CatalogException:
        return {}
    row = cur.fetchone()
    return dict(zip([d[0] for d in cur.description], row)) if row else {}


def show(label, d, keys):
    vals = [(k, d.get(k)) for k in keys if d.get(k) not in (None, "", [])]
    if not vals:
        print(f"\n{label}: none")
        return
    print(f"\n{label}:")
    for k, v in vals:
        v = round(v, 3) if isinstance(v, float) else v
        text = textwrap.shorten(str(v), 300) if not isinstance(v, (int, float)) else v
        print(f"  {k:<18} {text}")


def summary(con, pid, with_jd):
    p = one(con, "SELECT * FROM postings WHERE posting_id = ?", pid)
    print(f"{p['employer']} | {p['title']}  [{p['status']}]")
    print(f"  posting_id         {pid}")
    show("identity", p, ["url", "location_primary", "workplace_type", "employment_type", "pay_min", "pay_max",
                         "pay_interval", "posted_at", "first_seen_at", "closed_at"])
    show("screen (vw_lens_fit)", one(con, "SELECT * FROM vw_lens_fit WHERE posting_id = ?", pid),
         ["final_score", "band", "verdict", "tier", "fit_process", "fit_technical", "fit_ai", "fit_required",
          "fit_bullseye", "level_fit", "grade_process", "grade_technical", "grade_ai", "lens_grade_source",
          "required_fit", "required_unmet", "blocker", "reasons", "flags"])
    show("judge (latest)", one(con, "SELECT * FROM vw_llm_labels_latest_judge WHERE posting_id = ?", pid),
         ["scorer", "grade", "required_fit", "required_unmet", "rationale", "judged_at"])
    show("Jev (latest)", one(con, "SELECT * FROM vw_jev_latest WHERE posting_id = ?", pid),
         ["lens_process_grade", "lens_technical_grade", "lens_ai_grade", "required_fit", "derive_why", "prompt_version"])
    show("coverage", one(con, "SELECT * FROM vw_coverage_latest WHERE posting_id = ?", pid),
         ["coverage_required", "n_required", "n_required_strong", "n_required_partial", "gaps"])
    show("decision", one(con, "SELECT * FROM vw_decisions WHERE posting_id = ? ORDER BY decided_at DESC", pid),
         ["decision", "reason", "source", "decided_at"])
    show("tracker", one(con, "SELECT * FROM tracker WHERE coalesce(matched_posting_id, posting_id) = ?", pid),
         ["company", "role", "status", "date_applied", "match_kind"])
    if with_jd:
        print("\njob description:\n")
        print(p.get("description_text") or "(no JD fetched yet; see vw_jd_missing)")


def search(con, regex, include_closed, limit):
    status = "" if include_closed else "AND p.status = 'active'"
    rows = con.execute(f"""
        SELECT p.posting_id, p.employer, p.title, p.status, round(l.final_score, 1), l.verdict
        FROM postings p LEFT JOIN vw_lens_fit l USING (posting_id)
        WHERE regexp_matches(coalesce(p.title, '') || ' ' || coalesce(p.description_text, ''), ?, 'i') {status}
        ORDER BY p.first_seen_at DESC LIMIT ?""", [regex, limit]).fetchall()
    for r in rows:
        print(f"{r[0]}  {r[4] if r[4] is not None else '-':>5}  {r[5] or '-':<8} {r[3]:<7} {r[1]} | {r[2]}")
    if not rows:
        print("no matches")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("target", nargs="?")
    ap.add_argument("--jd", action="store_true")
    ap.add_argument("--search")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    a = ap.parse_args()
    if not a.target and not a.search:
        ap.print_help()
        print(__doc__)
        return 2
    con = connect(a.db)
    if a.search:
        search(con, a.search, a.all, a.limit)
        return 0
    hits = resolve_posting(con, a.target)
    if not hits:
        sys.exit(f"lookup: no posting matches {a.target!r}. Try --search '<regex>'.")
    if len(hits) > 1:
        print(f"{len(hits)} postings match; showing the first. Others:")
        for h in hits[1:10]:
            print(f"  {h[0]}  {h[3]:<7} {h[1]} | {h[2]}")
        print()
    summary(con, hits[0][0], a.jd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
