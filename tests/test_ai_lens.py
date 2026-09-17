"""Unit tests for the applied-AI lens (sprint plan §21): rubric, judge, features.

Runs against a temp DuckDB, like tests/test_finder.py. `llm_labels.grade_ai` is Agent B's column (schema v10);
these tests add it defensively with a guarded ALTER so this file stays harmless whether or not that migration
has landed in the copy of backend/ats/store.py under test.
"""
import json
import os
import re
import runpy
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import features, judge, rubric  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


def _quiet(*_a, **_k):
    pass


def _connect(tmp_path, name="t.duckdb"):
    """A temp DB with `llm_labels.grade_ai` guaranteed present, regardless of whether store.py's v10
    migration has landed yet in this working tree -- harmless once it has (ADD COLUMN IF NOT EXISTS)."""
    con = store.connect(str(tmp_path / name))
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'llm_labels'").fetchall()}
    if "grade_ai" not in cols:
        con.execute("ALTER TABLE llm_labels ADD COLUMN IF NOT EXISTS grade_ai VARCHAR")
    return con


def _postings(con, rows, now):
    """rows: list of (req_id, title, description_text). Neutral employer/location, Acme."""
    jobs = [N.base(req_id=req_id, title=title, url=f"https://x/{req_id}", location="Remote - USA",
                   workplace_type="remote") for req_id, title, _ in rows]
    store.record_board(con, "Acme", "greenhouse", jobs, now)
    for req_id, _, text in rows:
        con.execute("UPDATE postings SET description_text = ?, description_hash = ?, description_fetched_at = ? "
                    "WHERE req_id = ?", [text, f"h-{req_id}", now, req_id])
    return dict(con.execute("SELECT req_id, posting_id FROM postings").fetchall())


AI_JD = """About the role
Design and ship agentic AI workflows for internal teams.

Requirements
- Build LLM-powered assistants with evaluation and human-in-the-loop review
- Route requests across models for cost and latency
"""


# ---------------------------------------------------------------- rubric.py
def test_rubric_version_changes_when_the_ai_lens_changes(monkeypatch):
    before = rubric.rubric_version()
    monkeypatch.setattr(rubric, "RUBRIC_LENS_AI", rubric.RUBRIC_LENS_AI + " extra text")
    after = rubric.rubric_version()
    assert before != after


def test_rubric_public_carries_three_lenses_and_grade_ai_contract():
    assert "THREE LENSES" in rubric.RUBRIC_PUBLIC
    assert '"grade_ai"' in rubric.RUBRIC_PUBLIC
    assert "bullseye" in rubric.RUBRIC_LENS_AI and "wrong" in rubric.RUBRIC_LENS_AI


# ---------------------------------------------------------------- judge.py: batch header
def test_batch_file_carries_ai_lens_text_and_all_three_instruction(tmp_path):
    con = _connect(tmp_path)
    now = datetime(2026, 9, 17, 12)
    by_req = _postings(con, [("A1", "Principal AI Engineer", AI_JD)], now)
    out = tmp_path / "batches"
    stats = judge.write_batches(con, str(out), [(by_req["A1"], "high")], batch_size=5, log=_quiet)
    assert stats["postings"] == 1
    text = (out / "batch_001.md").read_text()
    assert rubric.RUBRIC_LENS_AI.strip()[:30] in text
    assert "ALL THREE" in text
    con.close()


# ---------------------------------------------------------------- judge.py: _validate
def test_validate_accepts_three_grades_and_null_ai_and_rejects_bad_ai():
    allowed = {"p1": "h1"}
    three = {"posting_id": "p1", "grade_process": "bullseye", "grade_technical": "wrong", "grade_ai": "adjacent"}
    assert judge._validate(three, allowed) is None
    two_no_ai = {"posting_id": "p1", "grade_process": "bullseye", "grade_technical": "wrong"}
    assert judge._validate(two_no_ai, allowed) is None                      # back-compat: grade_ai absent -> NULL
    two_null_ai = {"posting_id": "p1", "grade_process": "bullseye", "grade_technical": "wrong", "grade_ai": None}
    assert judge._validate(two_null_ai, allowed) is None                    # back-compat: grade_ai explicitly null
    bad_ai = {"posting_id": "p1", "grade_process": "bullseye", "grade_technical": "wrong", "grade_ai": "amazing"}
    assert judge._validate(bad_ai, allowed) is not None
    pre_split = {"posting_id": "p1", "grade": "adjacent"}
    assert judge._validate(pre_split, allowed) is None                      # still importable, unaffected


# ---------------------------------------------------------------- judge.py: load_results round trip
def test_load_results_round_trips_grade_ai(tmp_path):
    con = _connect(tmp_path)
    now = datetime(2026, 9, 17, 12)
    by_req = _postings(con, [("A1", "Principal AI Engineer", AI_JD),
                             ("A2", "Director, Process Excellence", AI_JD.replace("agentic", "operational"))], now)
    out = tmp_path / "batches"
    queue = [(by_req["A1"], "high"), (by_req["A2"], "high")]
    stats = judge.write_batches(con, str(out), queue, batch_size=5, log=_quiet)
    manifest = json.loads((out / "manifest.json").read_text())
    judged = [p for b in manifest["batches"].values() for p in b["postings"]]
    assert set(judged) == {by_req["A1"], by_req["A2"]}
    (out / "batch_001.result.json").write_text(json.dumps([
        {"posting_id": by_req["A1"], "grade_process": "wrong", "grade_technical": "adjacent",
         "grade_ai": "bullseye", "lane": "primary", "confidence": "high", "blocker": "",
         "rationale": "agentic workflow delivery"},
        {"posting_id": by_req["A2"], "grade_process": "bullseye", "grade_technical": "wrong",
         "lane": "primary", "confidence": "high", "blocker": "", "rationale": "no AI content, older batch shape"},
    ]))
    result = judge.load_results(con, str(out), scorer="test-scorer", log=_quiet)
    assert result["errors"] == [] and result["ai_missing"] == 1
    rows = dict(con.execute("SELECT posting_id, grade_ai FROM vw_llm_labels_latest").fetchall())
    assert rows[by_req["A1"]] == "bullseye"
    assert rows[by_req["A2"]] is None
    con.close()


def test_to_csv_includes_grade_ai_column(tmp_path):
    con = _connect(tmp_path)
    now = datetime(2026, 9, 17, 12)
    by_req = _postings(con, [("A1", "Principal AI Engineer", AI_JD)], now)
    con.execute("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                "grade_process, grade_technical, grade_ai, lane, confidence, blocker, rationale, batch, "
                "judged_at) VALUES (?, 'h-A1', 'rv', 'test', 'adjacent', 'wrong', 'adjacent', 'bullseye', "
                "'primary', 'high', '', '', 'b', ?)", [by_req["A1"], now])
    path = tmp_path / "labels.csv"
    judge.to_csv(con, str(path), log=_quiet)
    header = path.read_text().splitlines()[0]
    assert "grade_ai" in header
    con.close()


# ---------------------------------------------------------------- judge.py: agreement() lens independence
def test_agreement_reports_ai_lens_independence(tmp_path):
    con = _connect(tmp_path)
    now = datetime(2026, 9, 17, 12)
    by_req = _postings(con, [("A1", "Principal AI Engineer", AI_JD),
                             ("A2", "Director, Process Excellence", AI_JD)], now)
    rows = [[by_req["A1"], "h-A1", "rv", "test", "bullseye", "wrong", "wrong", "bullseye", "primary", "high", "",
             "", "b", now],                                                    # differs from both -> independent
            [by_req["A2"], "h-A2", "rv", "test", "bullseye", "bullseye", "wrong", "bullseye", "primary", "high",
             "", "", "b", now]]                                                # matches grade_process -> not
    con.executemany("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                    "grade_process, grade_technical, grade_ai, lane, confidence, blocker, rationale, batch, "
                    "judged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    out = judge.agreement(con, log=_quiet)
    assert out["ai_independence_pct"] == 50.0
    assert out["ai_term_estimate"]["ai_title"] == 1                            # only A1's title matches
    con.close()


# ---------------------------------------------------------------- judge.py: AI_TITLE_RE and the ai_title pool
def test_ai_title_re_matches_ai_titles_and_not_unrelated_ones():
    assert re.search(judge.AI_TITLE_RE, "Principal AI Engineer (Agentic AI)", re.I)
    assert re.search(judge.AI_TITLE_RE, "Senior Manager, AI Transformation", re.I)
    assert not re.search(judge.AI_TITLE_RE, "Retail Associate", re.I)
    assert not re.search(judge.AI_TITLE_RE, "Maintenance", re.I)


def test_pools_only_ai_title_returns_only_ai_titled_active_postings(tmp_path):
    con = _connect(tmp_path)
    now = datetime(2026, 9, 17, 12)
    by_req = _postings(con, [("A1", "Principal AI Engineer", AI_JD),
                             ("A2", "Senior Manager, AI Transformation", AI_JD),
                             ("B1", "Retail Associate", "Stock shelves and assist customers."),
                             ("B2", "Maintenance Technician", "Repair HVAC equipment on site.")], now)
    pool = judge.pools(con, only=["ai_title"])
    assert set(pool.get("ai_title", [])) == {by_req["A1"], by_req["A2"]}
    assert pool.get("high") == [] and pool.get("low") == [] and pool.get("reject") == []
    con.close()


def test_ai_lens_names_opportunity_evaluation_and_the_engineering_seat_rule():
    from backend.finder import rubric
    assert "EVALUATING AI / automation / RPA opportunities" in rubric.RUBRIC_LENS_AI
    assert "The Required block decides the seat" in rubric.RUBRIC_LENS_AI
