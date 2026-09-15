#!/usr/bin/env python3
"""Job finder: screen the ATS corpus against the profile rules, write the Jobs_Found report,
and record decisions so nothing is shown twice.

Usage:
    .venv/bin/python finder.py screen                     # new / changed / version-changed rows
    .venv/bin/python finder.py rescreen-all               # every active row, prints the verdict diff
    .venv/bin/python finder.py report --out /tmp/x.md     # Jobs_Found file (+ snapshots)
    .venv/bin/python finder.py sync --verbose             # mirror Application_Tracker.md
    .venv/bin/python finder.py mark <posting_id|url|"employer|title"> pass --reason "travel"
    .venv/bin/python finder.py shortlist --days 7 --n 30

Every subcommand takes --db (default db/jobsearch.duckdb) and --vault (default $JOBSEARCH_VAULT_DIR).
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO, ".env"))
from backend.ats import store  # noqa: E402
from backend.finder import pipeline, report, tracker_sync, version  # noqa: E402
from backend.screen import company_keys, company_matches, norm_company, similar_title  # noqa: E402


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _latest_counts(con) -> dict:
    return dict(con.execute("""SELECT s.verdict, count(*) FROM vw_screen_latest s JOIN postings p USING (posting_id)
                               WHERE p.status = 'active' GROUP BY 1""").fetchall())


def cmd_screen(con, a):
    since = _utcnow() - timedelta(hours=a.since_hours) if a.since_hours else None
    pipeline.screen(con, since=since, full=a.full, limit=a.limit)


def cmd_rescreen_all(con, a):
    before = _latest_counts(con)
    pipeline.screen(con, full=True)
    after = _latest_counts(con)
    print(f"{'verdict':<10} {'before':>8} {'after':>8} {'diff':>8}")
    for v in sorted(set(before) | set(after)):
        print(f"{v:<10} {before.get(v, 0):>8} {after.get(v, 0):>8} {after.get(v, 0) - before.get(v, 0):>+8}")


def cmd_report(con, a):
    if not a.out and not a.vault:
        sys.exit("report: set JOBSEARCH_VAULT_DIR or pass --out")
    since = _utcnow() - timedelta(hours=a.since_hours)
    funnel = con.execute("""SELECT s.verdict, count(*) FROM vw_screen_latest s JOIN postings p USING (posting_id)
                            WHERE p.status = 'active' AND p.first_seen_at >= ? GROUP BY 1""", [since]).fetchall()
    meta = {"since": since, "screen": None, "passed_since": since,
            "funnel": {"screened": sum(n for _, n in funnel), "verdict": dict(funnel)},
            "rules_version": version.rules_version(), "model_version": "none"}
    path = report.write_jobs_found(con, a.vault, meta, max_blocks=a.max_blocks, block_min_band=a.block_min_band,
                                   table_min=a.table_min, out_path=a.out)
    print(path)
    if not a.no_snapshots:
        for p in report.snapshots(con):
            print(p)


def resolve_posting(con, target: str) -> list:
    """(posting_id, employer, title, status) hits for a posting id, a URL, or 'employer|title'."""
    t = target.strip()
    cols = "posting_id, employer, title, status"
    if re.fullmatch(r"[0-9a-f]{20}", t):
        return con.execute(f"SELECT {cols} FROM postings WHERE posting_id = ?", [t]).fetchall()
    if t.startswith(("http://", "https://")):
        hits = con.execute(f"SELECT {cols} FROM postings WHERE url = ?", [t]).fetchall()
        return hits or con.execute(f"SELECT {cols} FROM postings WHERE length(req_id) >= 4 "
                                   "AND position(req_id IN ?) > 0", [t]).fetchall()
    if "|" in t:
        employer, title = (x.strip() for x in t.split("|", 1))
        keys = company_keys(employer)
        employers = [e for (e,) in con.execute("SELECT DISTINCT employer FROM postings").fetchall()
                     if company_matches(norm_company(e), keys)]
        rows = con.execute(f"SELECT {cols} FROM postings WHERE employer IN (SELECT unnest(?::VARCHAR[]))",
                           [employers]).fetchall()
        hits = [r for r in rows if similar_title(title, r[2] or "")]
        active = [r for r in hits if r[3] == "active"]
        return active or hits
    return []


def cmd_mark(con, a):
    hits = resolve_posting(con, a.target)
    if len(hits) != 1:
        print(f"mark: {len(hits)} postings match {a.target!r}; need exactly one.")
        for pid, employer, title, status in hits[:20]:
            print(f"  {pid}  {status:<6}  {employer} | {title}")
        sys.exit(1)
    pid, employer, title, _ = hits[0]
    con.execute("INSERT INTO decisions VALUES (?, ?, ?, 'cli', NULL, ?)", [pid, a.decision, a.reason, _utcnow()])
    print(f"{a.decision}: {pid}  {employer} | {title}")


def cmd_sync(con, a):
    if not a.vault:
        sys.exit("sync: set JOBSEARCH_VAULT_DIR or pass --vault")
    print(tracker_sync.sync(con, a.vault, verbose=a.verbose))


def cmd_shortlist(con, a):
    rows = con.execute("""SELECT final_score, band, tier, employer, title, location_primary, days_since_first_seen,
                                 posting_id FROM vw_scored_new(?) LIMIT ?""", [a.days, a.n]).fetchall()
    print(f"{'score':>5} {'band':<11} {'t':>1} {'employer':<28} {'title':<60} {'location':<24} {'age':>3} posting_id")
    for score, band, tier, employer, title, loc, age, pid in rows:
        print(f"{score:>5} {band:<11} {tier if tier is not None else '-':>1} {(employer or '')[:28]:<28} "
              f"{(title or '')[:60]:<60} {(loc or '')[:24]:<24} {age:>3} {pid}")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="override the DuckDB path")
    common.add_argument("--vault", default=os.getenv("JOBSEARCH_VAULT_DIR"), help="vault / Job_Search directory")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", parents=[common], help="screen rows that need it")
    s.add_argument("--full", action="store_true", help="re-screen every active row")
    s.add_argument("--since-hours", type=float, help="only rows first seen / given a JD in the last N hours")
    s.add_argument("--limit", type=int)
    s.add_argument("--no-model", action="store_true", help="skip the TF-IDF model (Phase 2; no-op until built)")
    s.add_argument("--no-embed", action="store_true", help="skip embeddings (Phase 3; no-op until built)")
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("rescreen-all", parents=[common], help="screen --full, then the verdict-count diff")
    s.set_defaults(func=cmd_rescreen_all)

    s = sub.add_parser("report", parents=[common], help="write a Jobs_Found file")
    s.add_argument("--max-blocks", type=int, default=15)
    s.add_argument("--block-min-band", default="strong", help="lowest band that gets a JD block (default strong)")
    s.add_argument("--table-min", type=int, default=50)
    s.add_argument("--since-hours", type=float, default=24, help="window for the header and Passed rows (default 24)")
    s.add_argument("--out", help="output file or directory (default: the vault's Search_Results)")
    s.add_argument("--no-snapshots", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("mark", parents=[common], help="record a build / pass / hold decision")
    s.add_argument("target", help='posting_id, posting URL, or "employer|title"')
    s.add_argument("decision", choices=report.DECISIONS)
    s.add_argument("--reason")
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("sync", parents=[common], help="mirror Application_Tracker.md into the tracker table")
    s.add_argument("--verbose", action="store_true", help="print unmatched tracker rows")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("shortlist", parents=[common], help="top scored postings first seen in the last N days")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--n", type=int, default=30)
    s.set_defaults(func=cmd_shortlist)

    a = ap.parse_args()
    con = store.connect(a.db)
    try:
        a.func(con, a)
    finally:
        con.close()


if __name__ == "__main__":
    main()
