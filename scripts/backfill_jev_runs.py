#!/usr/bin/env python3
"""One-time backfill of jev_runs from the launch logs (2026-11-12).

jev.run stores its own summary in jev_runs from 2026-11-12 on (store.record_jev_run). Earlier runs exist only as
the `jev.run: RunSummary(...)` line each one printed into logs/launch_<stamp>.log. This reads those lines and
stores one row per summary with source 'log', so the control charts' failed-calls check has its history (the
10/28 out-of-credits run among it).

    .venv/bin/python scripts/backfill_jev_runs.py [--logs logs] [--dry-run]

Only launch_*.log files are read: their first line gives the date ("launch: YYYY-MM-DD HH:MM:SS") and each step
starts with "════ HH:MM:SS  <command>", so a summary takes its step's start as started_at (a time earlier than the
previous step's means the run crossed midnight). The .out copies and the hand-run catch-up logs have no dates and
are not pipeline runs; they are skipped. Old summaries lack later fields (reused, carried, stopped_by_error) and
read as 0 / None. not_reached is rebuilt for a run stopped by a cap or an account error as
to_review - reviewed - reused - carried - errors. Re-running replaces the same rows (run_id log-<file>-<n>).
Live rows (source 'live') are never touched. Opens the DuckDB read-write: run it when no pipeline step is running.
"""
import argparse
import ast
import datetime as dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.finder.jev_types import RunSummary  # noqa: E402

HEAD_RE = re.compile(r"^launch: (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
STEP_RE = re.compile(r"^════ (\d{2}:\d{2}:\d{2})\s+(.*)$")
SUMMARY_RE = re.compile(r"^jev\.run: RunSummary\((.*)\)\s*$")
FIELD_RE = re.compile(r"(\w+)=('[^']*'|None|True|False|-?\d+)")
TAG_RE = re.compile(r"--run-tag\s+(\S+)")


def parse_summary(body):
    """The key=value pairs of a RunSummary repr, as a RunSummary (unknown keys ignored)."""
    known = set(RunSummary.__dataclass_fields__)
    kw = {k: ast.literal_eval(v) for k, v in FIELD_RE.findall(body) if k in known}
    return RunSummary(**kw)


def parse_log(path):
    """Yields (started_at naive UTC-less local datetime, run_tag, RunSummary) for each summary in one launch log."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if not lines or not HEAD_RE.match(lines[0]):
        return
    day = dt.date.fromisoformat(HEAD_RE.match(lines[0]).group(1))
    prev = dt.time.fromisoformat(HEAD_RE.match(lines[0]).group(2))
    step_at, tag = dt.datetime.combine(day, prev), ""
    for line in lines[1:]:
        m = STEP_RE.match(line)
        if m:
            t = dt.time.fromisoformat(m.group(1))
            if t < prev:
                day += dt.timedelta(days=1)
            prev = t
            step_at = dt.datetime.combine(day, t)
            tm = TAG_RE.search(m.group(2))
            tag = tm.group(1) if tm else ""
            continue
        m = SUMMARY_RE.match(line)
        if m:
            yield step_at, tag, parse_summary(m.group(1))


def rows_from(logs_dir, tz):
    out = []
    for path in sorted(Path(logs_dir).glob("launch_*.log")):
        for i, (local, tag, s) in enumerate(parse_log(path), start=1):
            if s.dry_run:
                continue
            to_review = s.considered - s.skipped_cached
            if s.stopped_by_cap or s.stopped_by_error:
                s.not_reached = max(0, to_review - s.reviewed - s.reused - s.carried - s.errors)
            started = local.replace(tzinfo=tz)
            out.append(dict(run_id=f"log-{path.stem}-{i}", summary=s, run_tag=tag, to_review=to_review,
                            started_at=started))
    return out


def main(argv=None):
    from zoneinfo import ZoneInfo
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", default=str(ROOT / "logs"))
    ap.add_argument("--tz", default="America/New_York", help="the launch logs' local time zone")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    rows = rows_from(args.logs, ZoneInfo(args.tz))
    bad = [r for r in rows if r["summary"].errors or r["summary"].not_reached]
    print(f"backfill_jev_runs: {len(rows)} runs from the logs; {len(bad)} with failed or unreached reviews")
    for r in bad:
        s = r["summary"]
        print(f"  {r['started_at']:%Y-%m-%d %H:%M} {r['run_id']}: to_review={r['to_review']} errors={s.errors} "
              f"not_reached={s.not_reached} cap={s.stopped_by_cap} error={s.stopped_by_error}")
    if args.dry_run:
        return 0
    from backend.ats import store
    con = store.connect()
    try:
        for r in rows:
            store.record_jev_run(con, r["summary"], prompt_version=None, run_tag=r["run_tag"],
                                 to_review=r["to_review"], started_at=r["started_at"], finished_at=None,
                                 source="log", run_id=r["run_id"])
    finally:
        con.close()
    print(f"backfill_jev_runs: stored {len(rows)} rows (source 'log')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
