#!/usr/bin/env python3
"""Snapshot the pipeline's fit population from DuckDB into PostgreSQL (schema `jobs`).

Design: DESIGN_tracker_db.md section 7. Replaces jobs.fit_snapshot and jobs.decisions wholesale in ONE
transaction (the swap is atomic, but TRUNCATE holds an exclusive lock, so readers wait for the load's few
seconds; lock_timeout 60 s and statement_timeout 10 min keep a stuck lock from hanging the launcher),
records a jobs.snapshot_run row, then CLUSTERs and ANALYZEs. DuckDB is opened read-only; Postgres is the database `resume` unless --db says
otherwise.

    .venv/bin/python scripts/pg_snapshot.py [--duckdb db/jobsearch.duckdb] [--db NAME]

Population: active postings not screen-rejected, UNION every posting named in DuckDB `decisions`,
UNION every posting_id in tracker.application (any status; table may be empty or absent).
Exit 0 on success, 1 on any failure (a locked DuckDB prints a clear message). A Jev-status failure only
prints a WARNING; the snapshot still loads and exits 0.
"""
import argparse
import datetime as dt
import os
import sys
import time

import duckdb
import psycopg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.tracker import db as trackerdb  # noqa: E402
from backend.finder import jev_status  # noqa: E402

POSTING_COLS = ["posting_id", "employer", "platform", "req_id", "title", "url", "location_primary",
                "workplace_type", "work_site", "pay_min", "pay_max", "pay_interval", "posted_at",
                "posting_end_at", "first_seen_at", "last_seen_at", "closed_at", "status",
                "screen_verdict", "screen_score", "description_text"]
LENS_COLS = ["final_score", "band", "tier", "verdict", "fit_process", "fit_technical", "fit_ai",
             "fit_required", "fit_bullseye", "level_fit", "grade", "grade_process", "grade_technical",
             "grade_ai", "required_fit", "rank_score", "rank_why", "lens_best"]
DECISION_COLS = ["posting_id", "decision", "reason", "source", "source_ref", "decided_at"]
# jev_result / jev_note (2026-10-25): what Jev did with the row, or the gate that kept it from Jev (jev_status.py).
SNAPSHOT_COLS = POSTING_COLS + LENS_COLS + ["latest_decision", "jev_result", "jev_note", "in_tracker", "snapshot_at"]

# The population SQL. ? = list of tracker posting_ids. Order is by posting_id for a stable CLUSTER.
POPULATION_SQL = f"""
WITH ids AS (
    SELECT posting_id FROM postings WHERE status = 'active' AND screen_verdict IS DISTINCT FROM 'reject'
    UNION SELECT posting_id FROM decisions
    UNION SELECT unnest(?::VARCHAR[])
), latest AS (
    SELECT posting_id, decision FROM decisions
    QUALIFY row_number() OVER (PARTITION BY posting_id ORDER BY decided_at DESC) = 1
)
SELECT {", ".join("p." + c for c in POSTING_COLS)},
       {", ".join("f." + c for c in LENS_COLS)},
       l.decision AS latest_decision
FROM ids
JOIN postings p USING (posting_id)
LEFT JOIN vw_lens_fit f USING (posting_id)
LEFT JOIN latest l USING (posting_id)
ORDER BY p.posting_id
"""


def tracker_ids(pg):
    """posting_ids already in tracker.application; empty when the table is absent or empty."""
    if not pg.execute("SELECT to_regclass('tracker.application') AS t").fetchone()["t"]:
        return []
    rows = pg.execute("SELECT posting_id FROM tracker.application WHERE posting_id IS NOT NULL").fetchall()
    return [r["posting_id"] for r in rows]


def fetch(duck, ids):
    """Return (snapshot_rows, decision_rows) as lists of tuples in SNAPSHOT_COLS / DECISION_COLS order."""
    now = dt.datetime.now().replace(microsecond=0)
    tracked = set(ids)
    snap = []
    rows = duck.execute(POPULATION_SQL, [list(ids)]).fetchall()
    try:  # enrichment only: a Jev-status failure must never cost the night's snapshot
        jev = jev_status.statuses(duck, [r[0] for r in rows])
    except Exception as e:  # noqa: BLE001
        print(f"pg_snapshot: WARNING jev status skipped ({type(e).__name__}: {e})", file=sys.stderr)
        jev = {r[0]: (None, f"jev status unavailable this run: {type(e).__name__}") for r in rows}
    for r in rows:
        r = list(r)
        if r[POSTING_COLS.index("description_text")]:  # Postgres text cannot hold NUL
            r[POSTING_COLS.index("description_text")] = r[POSTING_COLS.index("description_text")].replace("\x00", "")
        snap.append(tuple(r) + jev.get(r[0], (None, None)) + (r[0] in tracked, now))
    decs = duck.execute(f"SELECT {', '.join(DECISION_COLS)} FROM decisions ORDER BY posting_id, decided_at").fetchall()
    return snap, decs


ONE_SQL = f"""
SELECT {", ".join("p." + c for c in POSTING_COLS)},
       {", ".join("f." + c for c in LENS_COLS)},
       (SELECT decision FROM decisions d WHERE d.posting_id = p.posting_id
         ORDER BY decided_at DESC LIMIT 1) AS latest_decision
FROM postings p LEFT JOIN vw_lens_fit f USING (posting_id)
WHERE p.posting_id = ?
"""


def snapshot_one(pg, duck, posting_id):
    """Copy ONE posting into jobs.fit_snapshot (in_tracker = true) if it is not there yet. For `trk add`
    (2026-10-23): a posting added the same day that the nightly snapshot left out (a screen reject, or new
    since the run) otherwise has no row until the next night, so the sheet's "open" link and the add's
    field fill-in are missing. Returns True when a row was inserted."""
    if pg.execute("SELECT 1 FROM jobs.fit_snapshot WHERE posting_id = %s", [posting_id]).fetchone():
        return False
    r = duck.execute(ONE_SQL, [posting_id]).fetchone()
    if r is None:
        return False
    r = list(r)
    i = POSTING_COLS.index("description_text")
    if r[i]:
        r[i] = r[i].replace("\x00", "")
    row = tuple(r) + (None, "added by trk add after the nightly snapshot; next run fills it",
                      True, dt.datetime.now().replace(microsecond=0))
    pg.execute(f"INSERT INTO jobs.fit_snapshot ({', '.join(SNAPSHOT_COLS)}) "
               f"VALUES ({', '.join(['%s'] * len(SNAPSHOT_COLS))}) ON CONFLICT (posting_id) DO NOTHING", row)
    return True


def load(pg, snap, decs, t0, source):
    """Replace jobs.fit_snapshot and jobs.decisions in one transaction, then CLUSTER and ANALYZE."""
    pg.execute("SET lock_timeout = '60s'")        # fail instead of waiting forever behind a long reader
    pg.execute("SET statement_timeout = '10min'")
    with pg.transaction():
        pg.execute("TRUNCATE jobs.fit_snapshot, jobs.decisions")
        with pg.cursor() as cur:
            with cur.copy(f"COPY jobs.fit_snapshot ({', '.join(SNAPSHOT_COLS)}) FROM STDIN") as cp:
                for row in snap:
                    cp.write_row(row)
            with cur.copy(f"COPY jobs.decisions ({', '.join(DECISION_COLS)}) FROM STDIN") as cp:
                for row in decs:
                    cp.write_row(row)
        pg.execute("INSERT INTO jobs.snapshot_run (rows, decisions, seconds, source) VALUES (%s, %s, %s, %s)",
                   [len(snap), len(decs), round(time.time() - t0, 2), source])
    # autocommit connection, outside any transaction block: CLUSTER is allowed here
    pg.execute("CLUSTER jobs.fit_snapshot USING fit_snapshot_pkey")
    pg.execute("ANALYZE jobs.fit_snapshot")
    pg.execute("ANALYZE jobs.decisions")


def run(duckdb_path, dbname=None):
    t0 = time.time()
    try:
        duck = duckdb.connect(duckdb_path, read_only=True)
    except duckdb.Error as e:
        print(f"pg_snapshot: DuckDB is locked or unreadable ({duckdb_path}): {e}", file=sys.stderr)
        return 1
    try:
        with trackerdb.connect(dbname) as pg:
            snap, decs = fetch(duck, tracker_ids(pg))
            mtime = dt.datetime.fromtimestamp(os.path.getmtime(duckdb_path)).strftime("%Y-%m-%d %H:%M")
            source = f"duckdb {os.path.basename(duckdb_path)} mtime {mtime}"
            load(pg, snap, decs, t0, source)
    finally:
        duck.close()
    print(f"pg_snapshot: {len(snap)} fit rows, {len(decs)} decisions in {time.time() - t0:.1f}s")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duckdb", default="db/jobsearch.duckdb",
                    help="DuckDB file, opened read-only (default db/jobsearch.duckdb, relative to the current "
                         "directory); a held write lock exits 1")
    ap.add_argument("--db", default=None, help="Postgres database (default $TRACKER_PGDATABASE or resume)")
    args = ap.parse_args(argv)
    try:
        return run(args.duckdb, args.db)
    except psycopg.Error as e:
        print(f"pg_snapshot: Postgres error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
