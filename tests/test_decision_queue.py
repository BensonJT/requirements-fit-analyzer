"""Tests for `finder.py apply-queue` (jobs.decision_queue -> DuckDB decisions). Throwaway Postgres database
resume_test_queue, skipped when Postgres is unreachable. Fake data only."""
from argparse import Namespace
from datetime import datetime

import pytest

psycopg = pytest.importorskip("psycopg")
import finder  # noqa: E402
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.tracker import db  # noqa: E402

TEST_DB = "resume_test_queue"


@pytest.fixture(scope="module")
def pgc():
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


@pytest.fixture(autouse=True)
def env(pgc, monkeypatch):
    monkeypatch.setenv("TRACKER_PGDATABASE", TEST_DB)
    pgc.execute("DELETE FROM jobs.decision_queue")


@pytest.fixture
def duck(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    store.record_board(con, "Acme", "greenhouse", [N.base(req_id="Q1", title="Process Lead", url="https://x/Q1")],
                       datetime(2026, 10, 1))
    yield con, con.execute("SELECT posting_id FROM postings").fetchone()[0]
    con.close()


def _queue(pgc, pid, decision, reason=None, **kw):
    cols = {"posting_id": pid, "decision": decision, "reason": reason, **kw}
    names = ", ".join(cols)
    return pgc.execute(f"INSERT INTO jobs.decision_queue ({names}) VALUES ({', '.join(['%s'] * len(cols))}) "
                       "RETURNING queue_id", list(cols.values())).fetchone()["queue_id"]


def _row(pgc, qid):
    return pgc.execute("SELECT applied_at, apply_error FROM jobs.decision_queue WHERE queue_id = %s", [qid]).fetchone()


def test_valid_pass_is_applied_and_stamped(pgc, duck):
    con, pid = duck
    qid = _queue(pgc, pid, "pass", "logistics: hybrid three days a week, far office")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert con.execute("SELECT decision, reason, source, source_ref FROM decisions").fetchall() == [
        ("pass", "logistics: hybrid three days a week, far office", "queue", f"queue:{qid}")]
    applied_at, err = _row(pgc, qid).values()
    assert applied_at is not None and err is None


def test_bad_reason_is_refused_not_applied(pgc, duck):
    con, pid = duck
    qid = _queue(pgc, pid, "pass", "too far away")          # no reason code
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    applied_at, err = _row(pgc, qid).values()
    assert applied_at is None and "--reason must start with" in err
    finder.cmd_apply_queue(con, Namespace(dry_run=False))   # a refused row is not retried
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_unknown_posting_is_refused(pgc, duck):
    con, _ = duck
    qid = _queue(pgc, "f" * 20, "hold")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert "matched 0 postings" in _row(pgc, qid)["apply_error"]


def test_cancelled_and_applied_rows_are_skipped(pgc, duck):
    con, pid = duck
    _queue(pgc, pid, "hold", cancelled_at=datetime(2026, 10, 2))
    _queue(pgc, pid, "build", applied_at=datetime(2026, 10, 2))
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_dry_run_writes_nothing(pgc, duck):
    con, pid = duck
    qid = _queue(pgc, pid, "hold")
    finder.cmd_apply_queue(con, Namespace(dry_run=True))
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    assert _row(pgc, qid)["applied_at"] is None


def test_rows_apply_in_submission_order_so_the_latest_call_wins(pgc, duck):
    con, pid = duck
    _queue(pgc, pid, "hold")
    _queue(pgc, pid, "pass", "level: too junior")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert con.execute("SELECT decision FROM decisions ORDER BY rowid").fetchall() == [("hold",), ("pass",)]
