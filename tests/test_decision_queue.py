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


def _with_jd(con, pid):
    con.execute("UPDATE postings SET description_text = 'Lead process work.', description_hash = 'h1' WHERE posting_id = ?", [pid])


def test_lens_grades_land_in_human_lens_grades(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    qid = _queue(pgc, pid, "build", grade_process="adjacent", grade_technical="bullseye")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert sorted(con.execute("SELECT lens, grade, basis, source_file FROM human_lens_grades").fetchall()) == [
        ("process", "adjacent", "seen", f"queue:{qid}"), ("technical", "bullseye", "seen", f"queue:{qid}")]
    assert con.execute("SELECT decision FROM decisions").fetchall() == [("build",)]
    assert _row(pgc, qid)["applied_at"] is not None


def test_lens_grades_without_jd_refuse_the_whole_call(pgc, duck):
    con, pid = duck                      # no JD text: lens grades cannot be anchored to a description
    qid = _queue(pgc, pid, "pass", "logistics: far", grade_ai="wrong")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    assert "no JD text" in _row(pgc, qid)["apply_error"]


def test_bad_lens_grade_is_refused(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    qid = _queue(pgc, pid, "hold", grade_ai="great")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert "lens grade must be one of" in _row(pgc, qid)["apply_error"]
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0


def test_grade_only_row_writes_lens_grades_and_no_decision(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    qid = _queue(pgc, pid, "grade", basis="blind", grade_ai="stretch")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    assert con.execute("SELECT lens, grade, basis FROM human_lens_grades").fetchall() == [("ai", "stretch", "blind")]
    assert _row(pgc, qid)["applied_at"] is not None


def test_grade_only_row_without_grades_is_refused(pgc, duck):
    con, pid = duck
    qid = _queue(pgc, pid, "grade")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert "needs at least one lens grade" in _row(pgc, qid)["apply_error"]


# ---------------------------------------------------------------- Grades only: overall grade + Required fit
def _feedback(con):
    return con.execute("SELECT human_grade, verdict, basis, required_fit, required_unmet, reason_code "
                       "FROM report_feedback").fetchall()


def test_blind_grade_only_required_call_becomes_a_gold_row(pgc, duck):
    from backend.finder import judge2
    con, pid = duck
    _with_jd(con, pid)
    qid = _queue(pgc, pid, "grade", basis="blind", required_fit="fails", unmet="Line A || Line B")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert _row(pgc, qid)["applied_at"] is not None
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0     # no build / pass / hold label
    assert con.execute("SELECT required_fit, required_unmet FROM vw_report_feedback_blind").fetchall() == [
        ("fails", "Line A ; Line B")]
    assert [r[0] for r in con.execute(judge2.EVAL_SET_SQL).fetchall()] == [pid]
    assert con.execute("SELECT scorer, required_fit FROM llm_labels").fetchall() == [("user-adjudicated", "fails")]
    assert _feedback(con)[0][1] == "consider"                                  # neutral verdict, as a blind sheet


def test_grade_only_overall_grade_alone(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    qid = _queue(pgc, pid, "grade", grade="adjacent")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert _row(pgc, qid)["applied_at"] is not None
    assert _feedback(con) == [("adjacent", "consider", "seen", None, None, None)]
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM llm_labels").fetchone()[0] == 0   # no Required call, no bridge
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0


def test_grade_only_rolls_back_when_lens_grades_are_rejected(pgc, duck, monkeypatch):
    from backend.finder import gold_ingest
    con, pid = duck
    _with_jd(con, pid)

    def reject(con_, fr, rows, *a, **k):
        fr.rejected.append({"line": 1, "posting_id": pid, "field": "x", "value": "y"})
    monkeypatch.setattr(gold_ingest, "ingest_f4", reject)
    qid = _queue(pgc, pid, "grade", basis="blind", required_fit="meets", grade_ai="adjacent")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert "lens grades not accepted" in _row(pgc, qid)["apply_error"]
    for table in ("report_feedback", "llm_labels", "human_lens_grades", "decisions"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_grade_only_overall_or_required_needs_jd(pgc, duck):
    con, pid = duck                      # no JD text
    qid = _queue(pgc, pid, "grade", required_fit="meets")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert "no JD text" in _row(pgc, qid)["apply_error"]


def test_grade_only_keeps_an_earlier_required_call_it_leaves_blank(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    _queue(pgc, pid, "grade", basis="blind", required_fit="fails", unmet="Line A")
    _queue(pgc, pid, "grade", basis="blind", grade="stretch")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert _feedback(con) == [("stretch", "consider", "blind", "fails", "Line A", None)]


def test_grade_only_basis_mismatch_is_refused(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    _queue(pgc, pid, "grade", basis="blind", required_fit="meets")
    qid = _queue(pgc, pid, "grade", basis="seen", required_fit="fails")    # a seen call must not replace gold
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert "already has a blind human call" in _row(pgc, qid)["apply_error"]
    assert con.execute("SELECT required_fit FROM vw_report_feedback_blind").fetchall() == [("meets",)]


def test_unmet_without_fails_and_required_fit_on_a_build_are_refused(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    q1 = _queue(pgc, pid, "grade", required_fit="meets", unmet="Line A")
    q2 = _queue(pgc, pid, "build", required_fit="fails")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert "only go with a Required fit of 'fails'" in _row(pgc, q1)["apply_error"]
    assert "read only on a Grades only row" in _row(pgc, q2)["apply_error"]
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_build_with_overall_grade_writes_feedback_through_mark(pgc, duck):
    con, pid = duck
    _with_jd(con, pid)
    _queue(pgc, pid, "build", grade="bullseye")
    finder.cmd_apply_queue(con, Namespace(dry_run=False))
    assert con.execute("SELECT decision FROM decisions").fetchall() == [("build",)]
    assert _feedback(con) == [("bullseye", "build", "seen", "meets", None, None)]
