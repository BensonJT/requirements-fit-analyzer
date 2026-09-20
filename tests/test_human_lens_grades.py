"""Unit tests for the `human_lens_grades` table (schema v19, sprint plan §22.3/§25/§26's F4 sheet): the
migration/table itself, and the per-lens training view override (vw_label_set_process/technical/ai) it feeds.
Fake postings and grades only, temp DuckDB files, no network -- mirrors tests/test_bullseye.py's plumbing.
"""
import os
import runpy
import sys
from datetime import datetime

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


FIT_JD = ("Lead process excellence and continuous improvement across operations. Map value streams, remove "
          "handoffs, own the operating model and lean six sigma deployment with cross-functional stakeholders. "
          ) * 12
AI_JD = ("Design and ship agentic AI workflows for internal teams, route requests across models for cost and "
         "latency, build evaluation and human-in-the-loop review for LLM-powered assistants. ") * 12


def _posting(con, req_id, employer="Acme", text=FIT_JD, now=None):
    now = now or datetime(2026, 9, 19, 12, 0)
    posting = N.base(req_id=req_id, title=f"Role {req_id}", url=f"https://x/{req_id}",
                     location="Remote - USA", workplace_type="remote")
    store.record_board(con, employer, "greenhouse", [posting], now)
    pid = con.execute("SELECT posting_id FROM postings WHERE req_id = ?", [req_id]).fetchone()[0]
    dh = store.description_hash(text)
    con.execute("UPDATE postings SET description_text = ?, description_hash = ? WHERE posting_id = ?",
               [text, dh, pid])
    return pid, dh


def _judge(con, pid, dh, *, grade="adjacent", grade_process=None, grade_technical=None, grade_ai=None,
          scorer="claude-sonnet-batch", now=None):
    now = now or datetime(2026, 9, 19, 12, 0)
    con.execute("""INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade,
                   grade_process, grade_technical, grade_ai, judged_at)
                   VALUES (?, ?, 'rv1', ?, ?, ?, ?, ?, ?)""",
               [pid, dh, scorer, grade, grade_process, grade_technical, grade_ai, now])


def _human_lens(con, pid, dh, lens, grade, *, basis="seen", level_fit=None, note=None, now=None):
    now = now or datetime(2026, 9, 19, 12, 0)
    con.execute("""INSERT INTO human_lens_grades (posting_id, description_hash, lens, grade, basis,
                   level_fit, note, source_file, graded_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'test.csv', ?)""",
               [pid, dh, lens, grade, basis, level_fit, note, now])


# ---------------------------------------------------------------- table / migration

def test_schema_version_is_19_and_table_exists(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    # Asserts the CURRENT version, not a literal 19: v20 (judge2_lines, sprint plan §29) is additive on top
    # of v19's human_lens_grades table, same as test_judge2.py's v18 migration test does for its own version.
    assert store.SCHEMA_VERSION >= 19
    assert con.execute("SELECT version FROM schema_info").fetchone()[0] == store.SCHEMA_VERSION
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'human_lens_grades'").fetchall()}
    assert {"posting_id", "description_hash", "lens", "grade", "basis", "level_fit", "note", "source_file",
           "graded_at"} <= cols
    con.close()


def test_fresh_db_creation_produces_the_table(tmp_path):
    path = str(tmp_path / "fresh.duckdb")
    assert not os.path.exists(path)
    con = store.connect(path)
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0
    con.close()


def test_older_schema_version_row_migrates_forward(tmp_path):
    """A DB stamped with an older schema_info version still ends up with the table on the next connect."""
    path = str(tmp_path / "old.duckdb")
    con = store.connect(path)
    con.close()
    con = duckdb.connect(path)
    con.execute("UPDATE schema_info SET version = 18")
    con.close()
    con = store.connect(path)
    assert con.execute("SELECT version FROM schema_info").fetchone()[0] == store.SCHEMA_VERSION
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0
    con.close()


def test_invalid_lens_and_grade_rejected_by_check_constraint(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1")
    with pytest.raises(duckdb.ConstraintException):
        _human_lens(con, pid, dh, "not-a-lens", "adjacent")
    with pytest.raises(duckdb.ConstraintException):
        _human_lens(con, pid, dh, "ai", "not-a-grade")
    with pytest.raises(duckdb.ConstraintException):
        _human_lens(con, pid, dh, "ai", "adjacent", basis="not-a-basis")
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- vw_human_lens_grades_current

def test_vw_human_lens_grades_current_drops_stale_hash(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1")
    _human_lens(con, pid, dh, "ai", "bullseye")
    _human_lens(con, pid, "stale-hash", "process", "wrong")
    got = con.execute("SELECT lens FROM vw_human_lens_grades_current WHERE posting_id = ?", [pid]).fetchall()
    assert got == [("ai",)]
    con.close()


# ---------------------------------------------------------------- vw_label_set_ai override

def test_human_ai_grade_replaces_judge_grade_in_ai_lens_only(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1", text=AI_JD)
    _judge(con, pid, dh, grade="stretch", grade_process="stretch", grade_technical="stretch", grade_ai="wrong")
    _human_lens(con, pid, dh, "ai", "bullseye")

    ai_rows = con.execute("SELECT source, label, grade FROM vw_label_set_ai WHERE posting_id = ?", [pid]).fetchall()
    assert ai_rows == [("user_adjudicated", 1, "bullseye")]   # judge's grade_ai='wrong' overridden

    process_rows = con.execute(
        "SELECT source, label, grade FROM vw_label_set_process WHERE posting_id = ?", [pid]).fetchall()
    assert process_rows == [("llm_judge", 0, "stretch")]      # untouched -- only the ai lens was graded by hand

    overall = con.execute("SELECT label, grade FROM vw_label_set WHERE posting_id = ?", [pid]).fetchall()
    assert overall == [(0, "stretch")]                        # the averaged overall grade is unchanged
    con.close()


def test_no_duplicate_rows_for_a_posting_with_both_judge_and_human_grade(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1", text=AI_JD)
    _judge(con, pid, dh, grade="adjacent", grade_ai="adjacent")
    _human_lens(con, pid, dh, "ai", "bullseye")
    rows = con.execute("SELECT count(*) FROM vw_label_set_ai WHERE posting_id = ?", [pid]).fetchone()[0]
    assert rows == 1
    con.close()


def test_human_only_posting_appears_in_ai_lens_with_no_judge_row(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1", text=AI_JD)
    _human_lens(con, pid, dh, "ai", "adjacent")
    assert con.execute("SELECT count(*) FROM llm_labels WHERE posting_id = ?", [pid]).fetchone()[0] == 0
    rows = con.execute("SELECT source, label, grade FROM vw_label_set_ai WHERE posting_id = ?", [pid]).fetchall()
    assert rows == [("user_adjudicated", 1, "adjacent")]
    con.close()


def test_stale_hash_human_grade_ignored_by_the_view(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1", text=AI_JD)
    _judge(con, pid, dh, grade="stretch", grade_ai="stretch")
    _human_lens(con, pid, "an-old-hash", "ai", "bullseye")   # JD moved on since this grade was given
    rows = con.execute("SELECT source, label, grade FROM vw_label_set_ai WHERE posting_id = ?", [pid]).fetchall()
    assert rows == [("llm_judge", 0, "stretch")]   # judge's own grade still applies, human grade ignored
    con.close()


def test_human_process_grade_does_not_change_technical_or_ai_lens(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1", text=FIT_JD)
    _judge(con, pid, dh, grade="adjacent", grade_process="adjacent", grade_technical="wrong", grade_ai="wrong")
    _human_lens(con, pid, dh, "process", "bullseye")
    assert con.execute("SELECT grade FROM vw_label_set_process WHERE posting_id = ?", [pid]).fetchone() == \
        ("bullseye",)
    assert con.execute("SELECT grade FROM vw_label_set_technical WHERE posting_id = ?", [pid]).fetchone() == \
        ("wrong",)
    assert con.execute("SELECT grade FROM vw_label_set_ai WHERE posting_id = ?", [pid]).fetchone() == ("wrong",)
    con.close()


def test_f4_ingest_leaves_llm_labels_row_count_and_contents_unchanged(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _posting(con, "R1", text=AI_JD)
    _judge(con, pid, dh, grade="adjacent", grade_process="adjacent", grade_technical="stretch", grade_ai="stretch")
    before = con.execute("SELECT * FROM llm_labels ORDER BY posting_id").fetchall()

    import csv
    from backend.finder import gold_ingest
    path = tmp_path / "ai_lens.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row", "posting_id", "employer", "title", "url", "posting_status", "human_grade_ai",
                   "level_fit", "confidence", "note"])
        w.writerow([1, pid, "Acme", "Role R1", "https://x/R1", "active", "bullseye", "in_range", "high", ""])
    result = gold_ingest.ingest(con, [(path, "seen")], log=lambda *_a, **_k: None)
    assert result["ok"] is True

    after = con.execute("SELECT * FROM llm_labels ORDER BY posting_id").fetchall()
    assert before == after
    assert con.execute("SELECT count(*) FROM human_lens_grades WHERE posting_id = ?", [pid]).fetchone()[0] == 1
    con.close()
