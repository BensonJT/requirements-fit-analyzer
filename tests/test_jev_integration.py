"""End-to-end Jev path on a tmp DuckDB (docs/JEV_PLAN.md §3-§4): jev.run with the DEFAULT StorePersist over a
real `postings` row, a fake transport that answers every question it is sent, then the stored review read back
through store, the views and jev_eval. The pieces WP1 (jev.py) and WP2 (store v23) could not test alone.

No network (the transport is a fake), no live DB (tmp_path), synthetic JD and fact-sheet text only.
"""
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import jev, jev_eval  # noqa: E402
from backend.finder import jev_questions as Q  # noqa: E402
from backend.finder.jev_types import Caps, Posting  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0)

JD = """About the role.

Required:
5+ years of experience in process improvement and operations management.
Bachelor's degree or equivalent experience.
Experience building reports in a SQL database.

Responsibilities:
Lead cross-functional process redesign initiatives across the organization.
Build dashboards that track operational throughput for leadership.
"""

FACT_SHEET = """# Experience
- Redesigned an intake workflow shared by three departments.
- Built SQL reporting pipelines for a regional operations team.
# Education
- Bachelor's degree in industrial engineering.
"""

RANK_COLUMNS = ("posting_id", "rank_score", "rank_why", "effective_required_source", "lens_best")


class _Response:
    """The smoke fixture's response shape: status_code, headers, json()."""

    def __init__(self, body):
        self.status_code = 200
        self.headers = {jev.REQUEST_ID_HEADER: "req-test"}
        self._body = body
        self.text = ""

    def json(self):
        return self._body


def _answer(qid: str, question: dict) -> dict:
    """Answers one question by its declared type, in the smoke fixture's answer shape. Choices pick their
    first option (so a lines request picks the first fact id as evidence), except a verdict, which is `met`."""
    qtype = question["type"]
    if qtype == "noul":
        return {"type": "noul", "noul": 0.1}
    if qtype == "score":
        n = len(question["criteria"])
        probs = {str(i): 0.0 for i in range(n)}
        probs[str(n - 1)] = 0.8
        probs[str(n - 2)] = 0.2
        return {"type": "score", "score": (n - 1) * 0.8 + (n - 2) * 0.2, "confidence": 0.8,
                "legend": {str(i): text for i, text in enumerate(question["criteria"])}, "probabilities": probs}
    options = list(question["criteria"])
    choice = "met" if qid.startswith("verdict_") else options[0]
    rest = [o for o in options if o != choice]
    probs = {o: round(0.1 / len(rest), 6) for o in rest}
    probs[choice] = 0.9
    return {"type": "choice", "choice": choice, "confidence": 0.8, "probabilities": probs}


class FakeJev:
    """A transport answering every question in each request; counts calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, headers=None, json=None):
        assert url == Q.ENDPOINTS["typesafe"]
        self.calls += 1
        answers = {qid: _answer(qid, q) for qid, q in json["questions"].items()}
        return _Response({"model": Q.PINNED_MODEL, "answers": answers,
                          "usage": {"input_tokens": 700, "output_tokens": 0}})


class _NoCalls:
    def __call__(self, *a, **kw):
        raise AssertionError("a fully cached run must not call the transport")


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv(Q.KEY_ENV["typesafe"], "test-key-not-real")
    monkeypatch.delenv(Q.ENDPOINT_ENV, raising=False)
    con = store.connect(str(tmp_path / "t.duckdb"))
    job = N.base(req_id="R1", title="Process Analyst", url="https://x/R1", workplace_type="remote",
                 description_text=JD)
    store.record_board(con, "Acme Corp", "greenhouse", [job], NOW, truncated=True)
    pid, dh, title, employer, text = con.execute(
        "SELECT posting_id, description_hash, title, employer, description_text FROM postings").fetchone()
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                "rule_score, final_score, band, fit_process, fit_technical, fit_ai) "
                "VALUES (?, 'rv', 'mv', ?, 'review', 60, 70, 'strong', 0.8, 0.4, 0.1)", [pid, NOW])
    posting = Posting(posting_id=pid, description_hash=dh, title=title, employer=employer, description_text=text)
    return con, posting


def _run(con, posting, transport, facts, **kw):
    return jev.run(con, [posting], transport=transport, sleep_fn=lambda _s: None, live_ok=True,
                   caps=Caps(max_calls_per_run=50, daily_token_cap=10_000_000), facts=facts,
                   endpoint="typesafe", log=lambda *_a: None, **kw)


def _rank(con):
    return con.execute(f"SELECT {', '.join(RANK_COLUMNS)} FROM vw_lens_fit ORDER BY posting_id").fetchall()


def test_run_store_views_cache_rerun_and_repeatability_end_to_end(tmp_path, monkeypatch):
    con, posting = _setup(tmp_path, monkeypatch)
    facts = jev.parse_facts(FACT_SHEET)
    pv = jev.prompt_version(facts, "typesafe")
    rank_before = _rank(con)
    assert len(rank_before) == 1

    # 1. the canonical run, persisted by the DEFAULT StorePersist
    transport = FakeJev()
    summary = _run(con, posting, transport, facts)
    assert (summary.reviewed, summary.errors, summary.calls, transport.calls) == (1, 0, 2, 2)
    assert summary.input_tokens == 1400 and summary.drift == 0 and summary.canary_flags == 0

    # 2. load_jev_review round-trip equals a direct review of the same posting (bar the timestamp)
    loaded = store.load_jev_review(con, posting.posting_id, posting.description_hash, pv)
    expected = jev.review_posting(posting, facts, transport=FakeJev(), sleep_fn=lambda _s: None,
                                  endpoint="typesafe", key="k", pv=pv, log=lambda *_a: None)
    expected.reviewed_at = loaded.reviewed_at
    assert loaded == expected
    assert loaded.run_tag is None and loaded.model_answered == Q.PINNED_MODEL
    assert loaded.lens_process_grade == "bullseye" and loaded.injection_p == pytest.approx(0.1)
    assert [ln.section for ln in loaded.lines] == ["required"] * 3 + ["responsibility"] * 2
    assert all(ln.verdict == "met" and ln.evidence_fact_id == "f01" for ln in loaded.lines)
    assert loaded.required_fit is not None

    # 3. the views: vw_jev_latest shows it; vw_lens_fit's j3_* show it; the rank did not move
    assert con.execute("SELECT posting_id, prompt_version, run_tag FROM vw_jev_latest").fetchall() == [
        (posting.posting_id, pv, "")]
    j3 = con.execute("SELECT j3_required_fit, j3_lens_process_grade, j3_lens_technical_grade, j3_lens_ai_grade, "
                     "j3_injection_p, j3_prompt_version, jev_bar_passed FROM vw_lens_fit").fetchone()
    assert j3 == (loaded.required_fit, loaded.lens_process_grade, loaded.lens_technical_grade,
                  loaded.lens_ai_grade, pytest.approx(loaded.injection_p), pv, False)
    assert _rank(con) == rank_before

    # 4. a second run is fully cache-skipped: zero calls
    again = _run(con, posting, _NoCalls(), facts)
    assert (again.skipped_cached, again.reviewed, again.calls) == (1, 0, 0)

    # 5. a run_tag='r2' rerun is stored beside the canonical review, never over it
    rerun = _run(con, posting, FakeJev(), facts, run_tag="r2")
    assert (rerun.reviewed, rerun.calls) == (1, 2)
    keys = con.execute("SELECT prompt_version, run_tag FROM jev_reviews ORDER BY run_tag").fetchall()
    assert keys == [(pv, ""), (f"{pv}:r2", "r2")]
    assert store.load_jev_review(con, posting.posting_id, posting.description_hash, f"{pv}:r2", "r2") is not None
    assert con.execute("SELECT count(*) FROM vw_jev_latest").fetchone()[0] == 1
    assert _rank(con) == rank_before

    # 6. repeatability over the pair: identical answers, so no flip and no probability move
    rep = jev_eval.evaluate_repeatability(con, pv, ("r2",), run_id="it1")
    assert rep["n_pairs"] == 1 and rep["flip_rate"] == 0.0 and rep["max_abs_delta"] == 0.0
    assert rep["n_categorical"] > 0 and rep["n_probabilities"] > 0
    assert rep["passed"] is False and rep["reason"].startswith("insufficient data")   # 1 pair < MIN_N
    assert con.execute("SELECT family, passed FROM jev_evals").fetchall() == [("repeatability", False)]
    con.close()
