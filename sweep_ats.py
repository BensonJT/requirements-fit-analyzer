#!/usr/bin/env python3
"""ATS-direct sweep: pull every registered employer's live board into DuckDB.

Every run: whole-board pull (no cap) -> upsert (existing reqs are updated, never
re-added) -> close what went missing -> spend a bounded budget on per-posting JD
detail calls for platforms whose list endpoint carries no JD (Workday, Oracle,
Workable, BambooHR, SmartRecruiters). Then query the views:

    SELECT employer, title, location_primary FROM new_postings(1);          -- new since yesterday
    SELECT employer, title, days_visible FROM taken_down(7);
    SELECT * FROM title_match('operational excellence|process improvement');
    SELECT * FROM vw_board_health WHERE NOT ok;

Usage:
    .venv/bin/python sweep_ats.py                       # everything, 2000 JD fetches
    .venv/bin/python sweep_ats.py --limit 10            # first 10 boards (smoke test)
    .venv/bin/python sweep_ats.py --employer "capital one"
    .venv/bin/python sweep_ats.py --platform workday --detail-budget 0
    .venv/bin/python sweep_ats.py --skip-sweep --detail-budget 1000   # JD backfill only
    .venv/bin/python sweep_ats.py --no-screen           # skip the finder stage (screen/report/snapshots)
"""
import argparse
import sys

sys.path.insert(0, ".")
from backend.ats.adapters import IMPLEMENTED_PLATFORMS
from backend.ats.sweep import run


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only the first N registered boards")
    ap.add_argument("--platform", help="one platform only: " + ", ".join(sorted(IMPLEMENTED_PLATFORMS)))
    ap.add_argument("--employer", help="boards whose employer name contains this text")
    ap.add_argument("--workers", type=int, default=8, help="boards pulled in parallel (default 8)")
    ap.add_argument("--max-pages", type=int, help="safety valve: stop a board after N pages and skip its close-pass")
    ap.add_argument("--new-detail-cap", type=int, default=5000,
                    help="max JD fetches for postings that are NEW this run, every title (0 = off; default 5000)")
    ap.add_argument("--detail-budget", type=int, default=2000,
                    help="JD fetches for the older backlog, title-prefiltered (0 = none; default 2000)")
    ap.add_argument("--detail-all", action="store_true", help="ignore the title prefilter when choosing JDs to fetch")
    ap.add_argument("--skip-sweep", action="store_true", help="only run the detail stage")
    ap.add_argument("--db", help="override the DuckDB path")
    ap.add_argument("--no-screen", action="store_true", help="skip the finder stage entirely")
    ap.add_argument("--no-report", action="store_true", help="screen and snapshot, but write no Jobs_Found file")
    ap.add_argument("--llm-top", type=int, default=0, help="LLM-score the top N shortlist rows (Phase 4)")
    ap.add_argument("--full-screen", action="store_true", help="re-screen every active posting, not just new/changed")
    a = ap.parse_args()
    run(db_path=a.db, platform=a.platform, limit=a.limit, employer=a.employer, workers=a.workers,
        max_pages=a.max_pages, detail_budget=a.detail_budget, detail_all=a.detail_all, skip_sweep=a.skip_sweep,
        new_detail_cap=a.new_detail_cap, screen=not a.no_screen, report=not a.no_report, llm_top=a.llm_top,
        full_screen=a.full_screen)


if __name__ == "__main__":
    main()
