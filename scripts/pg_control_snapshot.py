#!/usr/bin/env python3
"""Copy the pipeline's own evaluation and run measurements into PostgreSQL (schema `control`) for control charts.

One row per measurement in control.point, upserted on (check_id, source_ref): a reload inserts new points,
rewrites only points whose values changed, and leaves the rest alone (loaded_at moves only on a change). The
schema, the plan and the chart views live with the reporting refresh (control_schema.sql, control_views.sql); this
script only fills control.point and exits 1 when the table is missing. DuckDB is opened read-only and never
waited on: a held write lock exits 1 at once with a clear message. Postgres is the database `resume` unless --db
says otherwise.

    .venv/bin/python scripts/pg_control_snapshot.py [--duckdb db/jobsearch.duckdb] [--db NAME]

Checks loaded (rates and counts of the pipeline's own behaviour only; no employer, title or URL leaves here):
  jev_required_agree / _catch     one point per full Jev Required test (jev_evals, family 'required')
  judge2_required_agree / _catch  the retired second judge's tests (judge2_evals), for history
  verdict_meets / _partial / _fails  share of Jev's reviews per sweep cycle (jev_reviews)
  run_boards_failed / run_pull_minutes  per full sweep (runs with 100 or more boards)
  regrade_msa1_agreement          the user's own re-grade study (Postgres jobs.regrade_study)

Judge tests: a test whose reason starts "insufficient" was cut short and never scored (status 'cut short');
"inherited from" is a carried-over result, not a fresh measurement ('inherited'). Of the rest, each UTC day keeps
ONE test: its last passing test, else its last test ('pass' or 'fail'); earlier tests that day are 'superseded'.
Only 'pass' tests are scored (count toward the centre line and limits). The day rule reproduces the ten tests
published for 9/30-11/2 (agreement 261 of 294).

Exit 0 on success, 1 on any failure. One source failing prints a WARNING, the other sources still load, and the
exit is 1.
"""
import argparse
import datetime as dt
import os
import sys
import time
from collections import Counter

import duckdb
import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.finder import regrade_stats  # noqa: E402
from backend.tracker import db as trackerdb  # noqa: E402

POINT_COLS = ["check_id", "source_ref", "measured_at", "n", "k", "value", "model", "prompt_version",
              "scored", "status", "note"]
FULL_SWEEP_MIN_BOARDS = 100   # a sweep reading fewer boards is an onboarding or bridge run, not a nightly sweep
REGRADE_STUDY = "msa1"


def utc(ts):
    """DuckDB timestamps are naive UTC (backend/ats/sweep.py, jev_*.py); tag them so Postgres stores the instant."""
    return ts.replace(tzinfo=dt.timezone.utc) if ts is not None and ts.tzinfo is None else ts


def share(k, n):
    return round(k / n, 6) if n else None


def point(check_id, source_ref, measured_at, n=None, k=None, value=None, model=None, prompt_version=None,
          scored=True, status=None, note=None):
    return dict(check_id=check_id, source_ref=source_ref, measured_at=utc(measured_at), n=n, k=k, value=value,
                model=model, prompt_version=prompt_version, scored=scored, status=status, note=note)


# ---------------------------------------------------------------- judge tests

def classify_tests(tests):
    """tests: dicts with run_id, evaluated_at, passed, reason. Returns {run_id: status}.

    'cut short' (reason starts "insufficient") and 'inherited' (reason starts "inherited from") are never a
    day's test. Of the rest, per UTC day: the last passing test is kept ('pass'); with no pass that day, the
    last test ('fail'). Every other test that day is 'superseded'."""
    status, by_day = {}, {}
    for t in sorted(tests, key=lambda t: (t["evaluated_at"], t["run_id"])):
        reason = (t.get("reason") or "").lower()
        if reason.startswith("insufficient"):
            status[t["run_id"]] = "cut short"
        elif reason.startswith("inherited from"):
            status[t["run_id"]] = "inherited"
        else:
            by_day.setdefault(t["evaluated_at"].date(), []).append(t)
    for day_tests in by_day.values():
        passes = [t for t in day_tests if t["passed"]]
        keep = (passes or day_tests)[-1]
        for t in day_tests:
            status[t["run_id"]] = ("pass" if t["passed"] else "fail") if t is keep else "superseded"
    return status


def test_points(tests, prefix, models=None):
    """Two points per test (agree and catch). Only 'pass' is scored; a cut-short test has no value."""
    models = models or {}
    status = classify_tests(tests)
    out = []
    for t in tests:
        st = status[t["run_id"]]
        note = {"cut short": "cut short: never scored",
                "inherited": (t.get("reason") or "")[:200],
                "superseded": "a later test the same day replaces this one",
                "fail": "below the pass bar: not used for the limits"}.get(st)
        for side in ("agree", "catch"):
            n, k = t[f"n_{side}"] or 0, t[f"n_{side}_hits"] or 0
            out.append(point(f"{prefix}_{side}", t["run_id"], t["evaluated_at"], n=n, k=k,
                             value=None if st == "cut short" else share(k, n),
                             model=models.get(t["prompt_version"]), prompt_version=t["prompt_version"],
                             scored=st == "pass", status=st, note=note))
    return out


JEV_TESTS_SQL = """
    SELECT run_id, prompt_version, evaluated_at, passed, reason,
           CAST(json_extract(metrics, '$.n_agree') AS INTEGER) AS n_agree,
           CAST(json_extract(metrics, '$.n_agree_hits') AS INTEGER) AS n_agree_hits,
           CAST(json_extract(metrics, '$.n_catch') AS INTEGER) AS n_catch,
           CAST(json_extract(metrics, '$.n_catch_hits') AS INTEGER) AS n_catch_hits
    FROM jev_evals WHERE family = 'required' ORDER BY evaluated_at"""
JUDGE2_TESTS_SQL = """
    SELECT run_id, prompt_version, evaluated_at, passed, reason, n_agree, n_agree_hits, n_catch, n_catch_hits
    FROM judge2_evals ORDER BY evaluated_at"""
JEV_MODEL_SQL = """
    SELECT prompt_version, mode(model_answered) FROM jev_reviews WHERE run_tag = '' GROUP BY prompt_version"""


def rows(duck, sql):
    cur = duck.execute(sql)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def jev_tests(duck):
    models = dict(duck.execute(JEV_MODEL_SQL).fetchall())
    return test_points(rows(duck, JEV_TESTS_SQL), "jev_required", models)


def judge2_tests(duck):
    return test_points(rows(duck, JUDGE2_TESTS_SQL), "judge2_required")


# ---------------------------------------------------------------- nightly verdict mix

# Each review belongs to the full sweep whose cycle it falls in (from that sweep's start to the next one's), so a
# point is one pipeline run. Repeat and injection runs (run_tag <> '') and the postings in the user's blind answer
# key are left out: a gold pass re-asks ~150 mostly-"fails" postings and would swing the night's mix.
VERDICT_SQL = f"""
WITH sweeps AS (
    SELECT run_id, started_at, lead(started_at) OVER (ORDER BY started_at) AS next_start
    FROM runs WHERE boards_attempted >= {FULL_SWEEP_MIN_BOARDS}
), rv AS (
    SELECT * FROM jev_reviews
    WHERE run_tag = ''
      AND posting_id NOT IN (SELECT posting_id FROM vw_report_feedback_blind WHERE required_fit IS NOT NULL)
)
SELECT s.run_id, s.started_at,
       count(rv.required_fit) AS n,
       count(*) FILTER (WHERE rv.required_fit = 'meets') AS meets,
       count(*) FILTER (WHERE rv.required_fit = 'partial') AS partial,
       count(*) FILTER (WHERE rv.required_fit = 'fails') AS fails,
       count(*) FILTER (WHERE rv.required_fit IS NULL) AS unanswered,
       count(*) FILTER (WHERE rv.version_drift) AS drifted,
       mode(rv.model_answered) AS model,
       mode(rv.prompt_version) AS prompt_version,
       count(DISTINCT rv.prompt_version) AS prompt_versions
FROM sweeps s
JOIN rv ON rv.reviewed_at >= s.started_at AND (s.next_start IS NULL OR rv.reviewed_at < s.next_start)
GROUP BY s.run_id, s.started_at
ORDER BY s.started_at"""


def verdict_points_from(rows_):
    out = []
    for r in rows_:
        if not r["n"]:
            continue
        bits = []
        if r["unanswered"]:
            bits.append(f"{r['unanswered']} reviews had no Required call")
        if r["drifted"]:
            bits.append(f"{r['drifted']} answered by a different model version")
        if r["prompt_versions"] > 1:
            bits.append(f"{r['prompt_versions']} prompt versions in this run")
        for verdict in ("meets", "partial", "fails"):
            out.append(point(f"verdict_{verdict}", r["run_id"], r["started_at"], n=r["n"], k=r[verdict],
                             value=share(r[verdict], r["n"]), model=r["model"], prompt_version=r["prompt_version"],
                             note="; ".join(bits) or None))
    return out


def verdict_points(duck):
    return verdict_points_from(rows(duck, VERDICT_SQL))


# ---------------------------------------------------------------- run health

RUNS_SQL = f"""
    SELECT run_id, started_at, finished_at, boards_attempted, boards_failed, elapsed_seconds
    FROM runs WHERE boards_attempted >= {FULL_SWEEP_MIN_BOARDS} ORDER BY started_at"""


def run_points_from(rows_):
    out = []
    for r in rows_:
        if r["finished_at"] is None:   # still running: its numbers are not final
            continue
        n, k = r["boards_attempted"], r["boards_failed"] or 0
        out.append(point("run_boards_failed", r["run_id"], r["started_at"], n=n, k=k, value=share(k, n)))
        if r["elapsed_seconds"] is not None:
            out.append(point("run_pull_minutes", r["run_id"], r["started_at"],
                             value=round(r["elapsed_seconds"] / 60, 2)))
    return out


def run_points(duck):
    return run_points_from(rows(duck, RUNS_SQL))


# ---------------------------------------------------------------- re-grade study (Postgres)

def regrade_points(pg, study=REGRADE_STUDY):
    if not pg.execute("SELECT to_regclass('jobs.regrade_study') AS t").fetchone()["t"]:
        return []
    got = pg.execute("SELECT original_fit, regrade_fit, regraded_at FROM jobs.regrade_study WHERE study_id = %s",
                     [study]).fetchall()
    done = [r for r in got if r["regrade_fit"]]
    if not done:
        return []
    pairs = [(r["original_fit"], r["regrade_fit"]) for r in done]
    agree = sum(a == b for a, b in pairs)
    note = f"Cohen's kappa {regrade_stats.cohen_kappa(pairs):.2f}"
    if len(done) < len(got):
        note += f"; {len(done)} of {len(got)} re-graded so far"
    return [point(f"regrade_{study}_agreement", study, max(r["regraded_at"] for r in done),
                  n=len(pairs), k=agree, value=share(agree, len(pairs)), note=note)]


# ---------------------------------------------------------------- load

UPSERT_SQL = f"""
INSERT INTO control.point ({", ".join(POINT_COLS)}) VALUES ({", ".join(["%s"] * len(POINT_COLS))})
ON CONFLICT (check_id, source_ref) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in POINT_COLS[2:])}, loaded_at = now()
WHERE ({", ".join("point." + c for c in POINT_COLS[2:])})
      IS DISTINCT FROM ({", ".join("EXCLUDED." + c for c in POINT_COLS[2:])})
RETURNING (xmax = 0) AS inserted"""


def load(pg, points):
    """Upsert in one transaction. Returns (inserted, changed, same)."""
    ins = chg = 0
    pg.execute("SET lock_timeout = '60s'")
    with pg.transaction():
        for p in points:
            r = pg.execute(UPSERT_SQL, [p[c] for c in POINT_COLS]).fetchone()
            if r is None:
                continue
            if r["inserted"]:
                ins += 1
            else:
                chg += 1
    return ins, chg, len(points) - ins - chg


DUCK_SOURCES = [("jev tests", jev_tests), ("judge2 tests", judge2_tests), ("verdict mix", verdict_points),
                ("run health", run_points)]


def run(duckdb_path, dbname=None):
    t0 = time.time()
    try:
        duck = duckdb.connect(duckdb_path, read_only=True)
    except duckdb.Error as e:
        print(f"pg_control_snapshot: DuckDB is locked or unreadable ({duckdb_path}); not waiting: {e}",
              file=sys.stderr)
        return 1
    failed, points = [], []
    try:
        with trackerdb.connect(dbname) as pg:
            if not pg.execute("SELECT to_regclass('control.point') AS t").fetchone()["t"]:
                print("pg_control_snapshot: table control.point is missing; run control_schema.sql "
                      "(the reporting refresh applies it) first", file=sys.stderr)
                return 1
            sources = DUCK_SOURCES + [("re-grade study", lambda _duck: regrade_points(pg))]
            for name, fn in sources:
                try:
                    points += fn(duck)
                except Exception as e:  # noqa: BLE001 -- one source must not cost the others
                    failed.append(name)
                    print(f"pg_control_snapshot: WARNING {name} skipped ({type(e).__name__}: {e})", file=sys.stderr)
            ins, chg, same = load(pg, points)
    finally:
        duck.close()
    per_check = Counter(p["check_id"] for p in points)
    print(f"pg_control_snapshot: {len(points)} points in {len(per_check)} checks ({ins} new, {chg} changed, "
          f"{same} same) in {time.time() - t0:.1f}s" + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duckdb", default="db/jobsearch.duckdb",
                    help="DuckDB file, opened read-only (default db/jobsearch.duckdb, relative to the current "
                         "directory); a held write lock exits 1 at once")
    ap.add_argument("--db", default=None, help="Postgres database (default $TRACKER_PGDATABASE or resume)")
    args = ap.parse_args(argv)
    try:
        return run(args.duckdb, args.db)
    except psycopg.Error as e:
        print(f"pg_control_snapshot: Postgres error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
