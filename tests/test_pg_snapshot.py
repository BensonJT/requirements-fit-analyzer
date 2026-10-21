"""Tests for scripts/pg_snapshot.py. Throwaway Postgres database (skipped when unreachable) and a tiny
temp DuckDB holding just the tables/view the script reads. Fake data only."""
import datetime as dt
import importlib.util
from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
psycopg = pytest.importorskip("psycopg")
from backend.tracker import db  # noqa: E402

spec = importlib.util.spec_from_file_location("pg_snapshot", Path(__file__).resolve().parents[1] / "scripts" / "pg_snapshot.py")
snap_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snap_mod)

TEST_DB = "resume_test_wp4"
A, B, C, D, E = ("%020x" % i for i in (1, 2, 3, 4, 5))  # active-ok, active-reject, closed+decided, closed+tracked, closed plain


@pytest.fixture()
def pg():
    try:
        admin = db.connect("postgres")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres unreachable: {e}")
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
    c = db.connect(TEST_DB)
    db.migrate(c)
    yield c
    c.close()
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.close()


@pytest.fixture()
def duck_path(tmp_path):
    path = str(tmp_path / "fake.duckdb")
    d = duckdb.connect(path)
    d.execute("""CREATE TABLE postings (posting_id VARCHAR, employer VARCHAR, platform VARCHAR, req_id VARCHAR,
        title VARCHAR, url VARCHAR, location_primary VARCHAR, workplace_type VARCHAR, work_site VARCHAR,
        pay_min INTEGER, pay_max INTEGER, pay_interval VARCHAR, posted_at DATE, posting_end_at DATE,
        first_seen_at TIMESTAMP, last_seen_at TIMESTAMP, closed_at TIMESTAMP, status VARCHAR,
        screen_verdict VARCHAR, screen_score INTEGER, description_text VARCHAR)""")
    rows = [(A, "active", "review"), (B, "active", "reject"), (C, "closed", "reject"),
            (D, "closed", "review"), (E, "closed", "review")]
    for pid, status, verdict in rows:
        d.execute("INSERT INTO postings (posting_id, employer, title, status, screen_verdict, description_text) "
                  "VALUES (?, 'Acme Corp', 'Analyst', ?, ?, ?)", [pid, status, verdict, "Fake JD\x00 text"])
    d.execute("""CREATE TABLE decisions (posting_id VARCHAR, decision VARCHAR, reason VARCHAR, source VARCHAR,
        source_ref VARCHAR, decided_at TIMESTAMP)""")
    d.execute("INSERT INTO decisions VALUES (?, 'hold', NULL, 'cli', NULL, '2026-10-01 10:00:00')", [C])
    d.execute("INSERT INTO decisions VALUES (?, 'build', 'fits', 'cli', NULL, '2026-10-02 10:00:00')", [C])
    d.execute("""CREATE VIEW vw_lens_fit AS SELECT posting_id, 77 AS final_score, 'strong' AS band, 1 AS tier,
        'review' AS verdict, 0.5 AS fit_process, 0.4 AS fit_technical, 0.3 AS fit_ai, 0.2 AS fit_required,
        0.1 AS fit_bullseye, 'at' AS level_fit, 'adjacent' AS grade, 'adjacent' AS grade_process,
        'adjacent' AS grade_technical, 'adjacent' AS grade_ai, 'meets' AS required_fit, 55.5 AS rank_score,
        'why' AS rank_why, 0.6 AS lens_best FROM postings WHERE posting_id = '%s'""" % A)
    d.close()
    return path


def _ids(pg):
    return {r["posting_id"] for r in pg.execute("SELECT posting_id FROM jobs.fit_snapshot")}


def test_population_and_columns(pg, duck_path):
    # the tracker table has an application for D (closed, not decided); absent-table case is its own test
    pg.execute("INSERT INTO tracker.application (company, role, posting_id, status, status_date) VALUES ('Acme Corp', 'Analyst', %s, 'built', '2026-10-01')", [D])
    assert snap_mod.run(duck_path, TEST_DB) == 0
    assert _ids(pg) == {A, C, D}  # B rejected, E closed and unnamed
    rows = {r["posting_id"]: r for r in pg.execute("SELECT * FROM jobs.fit_snapshot")}
    assert rows[A]["final_score"] == 77 and rows[A]["rank_score"] == 55.5 and not rows[A]["in_tracker"]
    assert rows[C]["latest_decision"] == "build" and rows[C]["final_score"] is None
    assert rows[D]["in_tracker"] is True and rows[D]["latest_decision"] is None
    assert rows[A]["description_text"] == "Fake JD text"  # NUL stripped
    assert pg.execute("SELECT count(*) AS n FROM jobs.decisions").fetchone()["n"] == 2


def test_rerun_replaces(pg, duck_path):
    assert snap_mod.run(duck_path, TEST_DB) == 0
    assert snap_mod.run(duck_path, TEST_DB) == 0
    assert pg.execute("SELECT count(*) AS n FROM jobs.fit_snapshot").fetchone()["n"] == 2
    assert pg.execute("SELECT count(*) AS n FROM jobs.decisions").fetchone()["n"] == 2
    runs = pg.execute("SELECT * FROM jobs.snapshot_run ORDER BY run_at").fetchall()
    assert len(runs) == 2 and runs[-1]["rows"] == 2 and runs[-1]["decisions"] == 2
    assert "fake.duckdb" in runs[-1]["source"] and runs[-1]["seconds"] >= 0


def test_absent_tracker_table_tolerated(pg, duck_path):
    pg.execute("DROP SCHEMA tracker CASCADE")
    assert snap_mod.run(duck_path, TEST_DB) == 0
    assert _ids(pg) == {A, C}


def test_locked_duckdb_exits_nonzero(pg, duck_path, capsys):
    holder = duckdb.connect(duck_path)  # read-write handle in this process: a read-only open conflicts
    try:
        assert snap_mod.run(duck_path, TEST_DB) == 1
    finally:
        holder.close()
    assert "locked" in capsys.readouterr().err
