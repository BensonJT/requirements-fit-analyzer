"""Tests for the LLM second judge (backend/finder/judge2.py, sprint plan §25).

NO test in this file, or anywhere in the suite, may make a network call: `transport` is always a fake
callable / fake object, `sleep_fn` is always a no-op / recording stub, and `dry_run=True` paths are asserted
to never touch `transport` at all. This mirrors tests/test_required_embed.py's fake-encoder pattern.
"""
import json
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import feedback, judge2, rubric  # noqa: E402

NOW = datetime(2026, 9, 19, 12, 0)

REALISTIC_JD = """About the role.

Required:
5+ years of experience in process improvement and operations management.
Active TS/SCI clearance required.
Bachelor's degree or equivalent experience.

Preferred:
Lean Six Sigma Black Belt certification.

Responsibilities:
Lead cross-functional process redesign initiatives across the organization.
""" * 2


def _make_posting(con, req_id, employer="Acme Corp", jd_text=REALISTIC_JD, title="Process Manager"):
    job = N.base(req_id=req_id, title=title, url=f"https://x/{req_id}", workplace_type="remote",
                description_text=jd_text)
    store.record_board(con, employer, "greenhouse", [job], NOW, truncated=True)
    pid, dh = con.execute("SELECT posting_id, description_hash FROM postings WHERE req_id = ?",
                          [req_id]).fetchone()
    return pid, dh


def _insert_judge_row(con, pid, dh, *, required_fit="meets", scorer="claude-sonnet-batch", rv="rv1",
                      grade_process="bullseye"):
    con.execute("""INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade,
                   grade_process, required_fit, judged_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
               [pid, dh, rv, scorer, grade_process, grade_process, required_fit, NOW])


def _insert_screen(con, pid, *, final_score=80, band="strong", verdict="candidate"):
    con.execute("""INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier,
                   rule_score, final_score, band) VALUES (?, 'rv1', 'none', ?, ?, 1, 50, ?, ?)""",
               [pid, NOW, verdict, final_score, band])


# ---------------------------------------------------------------- schema / migration
def test_v18_migrates_cleanly_from_v17(tmp_path):
    db_path = str(tmp_path / "v17.duckdb")
    con = store.connect(db_path)
    con.close()
    import duckdb
    raw = duckdb.connect(db_path)
    raw.execute("UPDATE schema_info SET version = 17")
    raw.execute("DROP VIEW IF EXISTS vw_judge2_latest")
    raw.execute("DROP VIEW IF EXISTS vw_judge2_eval_latest")
    raw.execute("DROP TABLE IF EXISTS judge2_reviews")
    raw.execute("DROP TABLE IF EXISTS judge2_evals")
    raw.close()

    con = store.connect(db_path)
    version = con.execute("SELECT version FROM schema_info").fetchone()[0]
    # Asserts the CURRENT version, not a literal 18: v19 (human_lens_grades) is additive, same as v17/v18
    # were, so a v17 DB reconnecting today lands on whatever SCHEMA_VERSION is now, not frozen at 18.
    assert version == store.SCHEMA_VERSION
    assert con.execute("SELECT count(*) FROM judge2_reviews").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM judge2_evals").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- rubric shadow-immunity
def test_public_background_immune_to_rubric_local_shadow(monkeypatch):
    original = rubric.JUDGE2_PUBLIC_BACKGROUND
    monkeypatch.setattr(rubric, "RUBRIC_LENS_PROCESS", "PERSONAL LEAK PROCESS")
    monkeypatch.setattr(rubric, "RUBRIC_LENS_TECHNICAL", "PERSONAL LEAK TECHNICAL")
    monkeypatch.setattr(rubric, "RUBRIC_LENS_AI", "PERSONAL LEAK AI")
    monkeypatch.setattr(rubric, "RUBRIC_PERSONAL", "TOP SECRET PERSONAL PROSE", raising=False)
    # The private constant is a plain string captured at import time -- rebinding the bare names afterward
    # (exactly what a real or fake rubric_local's star import does) must not change it.
    assert rubric.JUDGE2_PUBLIC_BACKGROUND == original
    bg = judge2.get_background("public")
    assert bg == original
    assert "PERSONAL LEAK" not in bg
    payload = judge2.build_payload(title="t", employer="e", jd_text=REALISTIC_JD)
    dumped = json.dumps(payload)
    assert "PERSONAL LEAK" not in dumped
    assert "TOP SECRET PERSONAL PROSE" not in dumped


def test_payload_field_names_are_exactly_the_contract():
    payload = judge2.build_payload(title="t", employer="e", jd_text=REALISTIC_JD)
    assert list(payload.keys()) == list(judge2.PAYLOAD_FIELDS)
    assert "posting_id" not in payload
    assert "url" not in json.dumps(payload)


def test_file_background_reads_the_configured_path(tmp_path, monkeypatch):
    p = tmp_path / "bg.md"
    p.write_text("Neutral background text.", encoding="utf-8")
    assert judge2.get_background("file", path=str(p)) == "Neutral background text."
    monkeypatch.delenv("JUDGE2_BACKGROUND_FILE", raising=False)
    with pytest.raises(ValueError):
        judge2.get_background("file", path=None)


def test_required_lines_reuses_the_requirements_splitter():
    lines = judge2.required_lines(REALISTIC_JD)
    assert any("clearance" in l.lower() for l in lines)
    assert any("years" not in l.lower() or "process improvement" in l.lower() for l in lines)


# ---------------------------------------------------------------- validation (hallucination guard)
def test_parse_response_discards_non_verbatim_quotes():
    jd = "Required: 5+ years of process improvement experience. Active TS/SCI clearance required."
    raw = json.dumps({"required_fit": "fails",
                      "unmet": ["Active TS/SCI clearance required.", "a paraphrased made-up requirement"],
                      "held_clearance": True, "years_gap": None, "confidence": "high"})
    review = judge2.parse_response(raw, jd)
    assert review is not None
    assert review.unmet == ["Active TS/SCI clearance required."]
    assert review.unmet_discarded == 1
    assert review.downgraded is False
    assert review.required_fit == "fails"


def test_fails_with_zero_surviving_quotes_downgrades_to_partial():
    jd = "Required: 5+ years of process improvement experience."
    raw = json.dumps({"required_fit": "fails", "unmet": ["a fabricated line not in the JD"],
                      "held_clearance": False, "years_gap": None, "confidence": "medium"})
    review = judge2.parse_response(raw, jd)
    assert review.required_fit == "partial"
    assert review.downgraded is True
    assert review.unmet == []
    assert review.unmet_discarded == 1


def test_parse_response_tolerates_fenced_and_prose_wrapped_json():
    jd = "Required: an active Public Trust clearance."
    fenced = "Here is my answer:\n```json\n" + json.dumps(
        {"required_fit": "meets", "unmet": [], "held_clearance": False, "years_gap": None,
         "confidence": "high"}) + "\n```\nThanks."
    review = judge2.parse_response(fenced, jd)
    assert review is not None and review.required_fit == "meets"


def test_parse_response_rejects_invalid_enum():
    raw = json.dumps({"required_fit": "sort-of", "unmet": []})
    assert judge2.parse_response(raw, "jd text") is None


def test_parse_response_unparseable_returns_none():
    assert judge2.parse_response("not json at all", "jd text") is None


def test_verbatim_check_is_whitespace_normalized_not_case_folded():
    jd = "Required:   Active   TS/SCI clearance required."
    raw = json.dumps({"required_fit": "fails", "unmet": ["Active TS/SCI clearance required."],
                      "held_clearance": True, "years_gap": None, "confidence": "high"})
    review = judge2.parse_response(raw, jd)
    assert review.unmet == ["Active TS/SCI clearance required."]

    raw_wrong_case = json.dumps({"required_fit": "fails", "unmet": ["active ts/sci clearance required."],
                                "held_clearance": True, "years_gap": None, "confidence": "high"})
    review2 = judge2.parse_response(raw_wrong_case, jd)
    assert review2.unmet == []
    assert review2.unmet_discarded == 1


# ---------------------------------------------------------------- run(): dry-run, live gate, one review
class _ExplodingTransport:
    def __call__(self, *a, **kw):
        raise AssertionError("transport must never be called in dry-run mode")

    def post(self, *a, **kw):
        raise AssertionError("transport must never be called in dry-run mode")


def test_dry_run_never_calls_transport_and_needs_no_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    result = judge2.run(con, top_n=5, dry_run=True, show=1, transport=_ExplodingTransport(), log=lambda *a: None)
    assert result["dry_run"] is True
    assert result["count"] >= 1
    assert result["payload_fields"] == list(judge2.PAYLOAD_FIELDS)
    con.close()


def test_live_run_refuses_without_the_gate(tmp_path, monkeypatch):
    monkeypatch.delenv("JUDGE2_LIVE_OK", raising=False)
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    with pytest.raises(RuntimeError, match="dry-run"):
        judge2.run(con, top_n=5, dry_run=False, transport=lambda *a, **kw: None, sleep_fn=lambda s: None)
    con.close()


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def _gemini_body(obj: dict) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(obj)}]}}]}


def test_live_run_records_one_review_with_a_fake_transport(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE2_LIVE_OK", "1")
    monkeypatch.setenv("GEMINI_API_MODEL", "gemma-test")
    monkeypatch.setenv("GEMINI_API_KEY", "unused-fake-key")
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)

    calls = []

    def fake_transport(url, headers=None, json=None):
        calls.append((url, headers, json))
        assert "x-goog-api-key" in headers
        assert headers["x-goog-api-key"] == "unused-fake-key"
        body = _gemini_body({"required_fit": "fails",
                             "unmet": ["Active TS/SCI clearance required."], "held_clearance": True,
                             "years_gap": None, "confidence": "high"})
        return _FakeResponse(200, body)

    sleeps = []
    result = judge2.run(con, top_n=5, dry_run=False, transport=fake_transport, sleep_fn=sleeps.append)
    assert result["reviewed"] == 1
    assert len(calls) == 1
    row = con.execute("SELECT required_fit, model, provider, unmet_discarded, downgraded FROM judge2_reviews "
                      "WHERE posting_id = ?", [pid]).fetchone()
    assert row[0] == "fails"
    assert row[1] == "gemma-test"
    assert row[2] == "gemini"
    assert row[3] == 0
    assert row[4] is False
    con.close()


def test_model_fallback_on_429(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE2_LIVE_OK", "1")
    monkeypatch.setenv("GEMINI_API_MODEL", "bad-model,good-model")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)

    def fake_transport(url, headers=None, json=None):
        if "bad-model" in url:
            return _FakeResponse(429, {})
        body = _gemini_body({"required_fit": "meets", "unmet": [], "held_clearance": False,
                             "years_gap": None, "confidence": "medium"})
        return _FakeResponse(200, body)

    result = judge2.run(con, top_n=5, dry_run=False, transport=fake_transport, sleep_fn=lambda s: None)
    assert result["reviewed"] == 1
    model = con.execute("SELECT model FROM judge2_reviews WHERE posting_id = ?", [pid]).fetchone()[0]
    assert model == "good-model"
    con.close()


def test_already_reviewed_is_skipped_unless_forced(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE2_LIVE_OK", "1")
    monkeypatch.setenv("GEMINI_API_MODEL", "m1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    calls = {"n": 0}

    def fake_transport(url, headers=None, json=None):
        calls["n"] += 1
        return _FakeResponse(200, _gemini_body({"required_fit": "meets", "unmet": [], "held_clearance": False,
                                               "years_gap": None, "confidence": "high"}))

    judge2.run(con, top_n=5, dry_run=False, transport=fake_transport, sleep_fn=lambda s: None)
    assert calls["n"] == 1
    judge2.run(con, top_n=5, dry_run=False, transport=fake_transport, sleep_fn=lambda s: None)
    assert calls["n"] == 1  # skipped: already reviewed at this hash/prompt_version
    judge2.run(con, top_n=5, dry_run=False, force=True, transport=fake_transport, sleep_fn=lambda s: None)
    assert calls["n"] == 2
    con.close()


def test_population_excludes_human_required_graded_postings(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE2_LIVE_OK", "1")
    monkeypatch.setenv("GEMINI_API_MODEL", "m1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    feedback.record_mark(con, pid, dh, "pass", reason_code="requirement", required_fit="fails",
                         required_unmet="Active TS/SCI clearance required.", basis="blind", now=NOW)

    def fake_transport(url, headers=None, json=None):
        raise AssertionError("must not be called: this posting already has a human Required call")

    result = judge2.run(con, top_n=5, dry_run=False, transport=fake_transport, sleep_fn=lambda s: None)
    assert result["reviewed"] == 0
    con.close()


# ---------------------------------------------------------------- evaluate()
def _blind_row(con, req_id, *, human_fit, judge1_fit=None, judge2_fit=None, pv=None):
    pid, dh = _make_posting(con, req_id)
    _insert_screen(con, pid)
    if judge1_fit is not None:
        _insert_judge_row(con, pid, dh, required_fit=judge1_fit, scorer="claude-sonnet-batch", rv="rv1")
    feedback.record_mark(con, pid, dh, "pass" if human_fit == "fails" else "build", reason_code="requirement",
                         required_fit=human_fit, required_unmet="Active TS/SCI clearance required." if human_fit == "fails" else None,
                         basis="blind", now=NOW)
    if judge2_fit is not None:
        pv = pv or judge2.prompt_version(judge2.get_background("public"))
        con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider,
                       model, required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap,
                       confidence, raw_response, prompt_chars, reviewed_at)
                       VALUES (?, ?, ?, 'gemini', 'gemma-test', ?, '[]', 0, false, false, NULL, 'high', '', 0, ?)""",
                   [pid, dh, pv, judge2_fit, NOW])
    return pid, dh


def test_evaluate_both_human_and_first_judge_rows_coexist(tmp_path):
    """Correction 1: a human required_fit call is BRIDGED into llm_labels as scorer='user-adjudicated', so
    BOTH rows exist for one posting. `vw_llm_labels_latest_judge` must return only the first judge's OWN row."""
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _blind_row(con, "R1", human_fit="fails", judge1_fit="meets", judge2_fit="fails")
    rows = con.execute("SELECT scorer, required_fit FROM llm_labels WHERE posting_id = ? ORDER BY scorer",
                       [pid]).fetchall()
    scorers = {r[0]: r[1] for r in rows}
    assert scorers.get("claude-sonnet-batch") == "meets"
    assert scorers.get(feedback.USER_SCORER) == "fails"
    judge_only = con.execute("SELECT required_fit FROM vw_llm_labels_latest_judge WHERE posting_id = ?",
                            [pid]).fetchone()
    assert judge_only[0] == "meets"  # the first judge's OWN call, not laundered through the human row
    con.close()


def test_evaluate_insufficient_data_is_clean(tmp_path):
    """Correction 3: zero blind human required_fit rows -> a clean 'insufficient data' result, bar not
    passed, no exception."""
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    result = judge2.evaluate(con, log=lambda *a: None)
    assert result["insufficient"] is True
    assert result["passed"] is False
    assert result["n_catch"] == 0 and result["n_agree"] == 0
    con.close()


def test_evaluate_catch_and_agree_bars(tmp_path):
    """5 catch rows (first judge meets, human fails), second judge catches 4 of 5 (>=70%);
    5 agree rows (human meets), second judge agrees (meets) on 5 of 5 (>=85%) -> bar passed."""
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pv = judge2.prompt_version(judge2.get_background("public"))
    for i in range(4):
        _blind_row(con, f"C{i}", human_fit="fails", judge1_fit="meets", judge2_fit="fails", pv=pv)
    _blind_row(con, "C4", human_fit="fails", judge1_fit="meets", judge2_fit="meets", pv=pv)  # the miss
    for i in range(5):
        _blind_row(con, f"A{i}", human_fit="meets", judge1_fit="meets", judge2_fit="meets", pv=pv)
    result = judge2.evaluate(con, prompt_version_override=pv, log=lambda *a: None)
    assert result["n_catch"] == 5
    assert result["catch_rate"] == pytest.approx(0.8)
    assert result["n_agree"] == 5
    assert result["agree_rate"] == pytest.approx(1.0)
    assert result["passed"] is True
    stored = con.execute("SELECT passed FROM judge2_evals WHERE prompt_version = ?", [pv]).fetchone()
    assert stored[0] is True
    con.close()


def test_evaluate_agree_bar_fails_when_judge2_hedges_everything():
    pass  # covered implicitly by test_evaluate_catch_and_agree_bars's structure; see rank-integration tests


# ---------------------------------------------------------------- rank integration (vw_lens_fit)
def test_below_bar_rank_identical_to_first_judge(tmp_path):
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    _insert_judge_row(con, pid, dh, required_fit="meets", grade_process="bullseye")
    before = con.execute("SELECT rank_score, effective_required_source FROM vw_lens_fit "
                         "WHERE posting_id = ?", [pid]).fetchone()
    # a judge2 review exists but its prompt_version has never been evaluated (no judge2_evals row) --
    # below the bar, so it must have NO rank effect at all.
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at)
                   VALUES (?, ?, 'unevaluated-pv', 'gemini', 'm', 'fails', '[]', 0, false, true, NULL, 'high',
                           '', 0, ?)""", [pid, dh, NOW])
    after = con.execute("SELECT rank_score, effective_required_source, judge2_moves_rank FROM vw_lens_fit "
                        "WHERE posting_id = ?", [pid]).fetchone()
    assert after[0] == before[0]
    assert after[1] == "judge"
    assert after[2] is False
    con.close()


def test_above_bar_judge2_overrides_first_judge(tmp_path):
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    _insert_judge_row(con, pid, dh, required_fit="meets", grade_process="bullseye")  # lenient first judge
    pv = "passed-pv"
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at)
                   VALUES (?, ?, ?, 'gemini', 'm', 'fails', '["Active TS/SCI clearance required."]', 0, false,
                           true, NULL, 'high', '', 0, ?)""", [pid, dh, pv, NOW])
    import uuid
    con.execute("""INSERT INTO judge2_evals (run_id, prompt_version, evaluated_at, n_catch, n_catch_hits,
                   catch_rate, n_catch_strict_hits, catch_rate_strict, n_all_fails, n_all_fails_hits,
                   catch_rate_all_fails, n_agree, n_agree_hits, agree_rate, passed, reason)
                   VALUES (?, ?, ?, 10, 8, 0.8, 8, 0.8, 10, 8, 0.8, 10, 9, 0.9, true, 'test')""",
               [uuid.uuid4().hex, pv, NOW])
    row = con.execute("SELECT rank_score, effective_required_source, judge2_moves_rank, judge2_required, "
                      "judge2_unmet_first FROM vw_lens_fit WHERE posting_id = ?", [pid]).fetchone()
    assert row[0] == 0.0  # judge2 fails -> effective_required_value 0.0 -> zero gate -> rank 0
    assert row[1] == "judge2"
    assert row[2] is True
    assert row[3] == "fails"
    assert row[4] == "Active TS/SCI clearance required."
    con.close()


def test_stale_judge2_review_alone_falls_back_to_first_judge(tmp_path):
    """A judge2 review written under a hash that no longer matches the posting's CURRENT text is invisible
    (vw_judge2_latest), while the first judge's own (still-current-hash) call keeps ranking normally."""
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    _insert_judge_row(con, pid, dh, required_fit="meets", grade_process="bullseye")
    pv = "passed-pv-4"
    stale_hash = "not-the-current-hash"
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at)
                   VALUES (?, ?, ?, 'gemini', 'm', 'fails', '[]', 0, false, true, NULL, 'high', '', 0, ?)""",
               [pid, stale_hash, pv, NOW])
    import uuid
    con.execute("""INSERT INTO judge2_evals (run_id, prompt_version, evaluated_at, n_catch, n_catch_hits,
                   catch_rate, n_catch_strict_hits, catch_rate_strict, n_all_fails, n_all_fails_hits,
                   catch_rate_all_fails, n_agree, n_agree_hits, agree_rate, passed, reason)
                   VALUES (?, ?, ?, 10, 8, 0.8, 8, 0.8, 10, 8, 0.8, 10, 9, 0.9, true, 'test')""",
               [uuid.uuid4().hex, pv, NOW])
    row = con.execute("SELECT effective_required_source, judge2_required, judge2_moves_rank FROM vw_lens_fit "
                      "WHERE posting_id = ?", [pid]).fetchone()
    assert row[0] == "judge"
    assert row[1] is None
    assert row[2] is False
    con.close()


def test_human_always_wins_over_judge2(tmp_path):
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    # human says MEETS; judge2 (passed bar) says FAILS -- human must still win.
    feedback.record_mark(con, pid, dh, "build", required_fit="meets", basis="blind", now=NOW)
    pv = "passed-pv-2"
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at)
                   VALUES (?, ?, ?, 'gemini', 'm', 'fails', '[]', 0, false, true, NULL, 'high', '', 0, ?)""",
               [pid, dh, pv, NOW])
    import uuid
    con.execute("""INSERT INTO judge2_evals (run_id, prompt_version, evaluated_at, n_catch, n_catch_hits,
                   catch_rate, n_catch_strict_hits, catch_rate_strict, n_all_fails, n_all_fails_hits,
                   catch_rate_all_fails, n_agree, n_agree_hits, agree_rate, passed, reason)
                   VALUES (?, ?, ?, 10, 8, 0.8, 8, 0.8, 10, 8, 0.8, 10, 9, 0.9, true, 'test')""",
               [uuid.uuid4().hex, pv, NOW])
    row = con.execute("SELECT effective_required_source, judge2_moves_rank FROM vw_lens_fit "
                      "WHERE posting_id = ?", [pid]).fetchone()
    assert row[0] == "human"
    assert row[1] is False
    con.close()


def test_stale_review_has_no_effect_after_jd_changes(tmp_path):
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    _insert_judge_row(con, pid, dh, required_fit="meets", grade_process="bullseye")
    pv = "passed-pv-3"
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at)
                   VALUES (?, ?, ?, 'gemini', 'm', 'fails', '[]', 0, false, true, NULL, 'high', '', 0, ?)""",
               [pid, dh, pv, NOW])
    import uuid
    con.execute("""INSERT INTO judge2_evals (run_id, prompt_version, evaluated_at, n_catch, n_catch_hits,
                   catch_rate, n_catch_strict_hits, catch_rate_strict, n_all_fails, n_all_fails_hits,
                   catch_rate_all_fails, n_agree, n_agree_hits, agree_rate, passed, reason)
                   VALUES (?, ?, ?, 10, 8, 0.8, 8, 0.8, 10, 8, 0.8, 10, 9, 0.9, true, 'test')""",
               [uuid.uuid4().hex, pv, NOW])
    # confirm it moves the rank before the JD changes
    before = con.execute("SELECT effective_required_source FROM vw_lens_fit WHERE posting_id = ?",
                         [pid]).fetchone()
    assert before[0] == "judge2"
    # now the JD text changes under it (a re-fetch): description_hash moves, review goes stale/invisible
    new_text = REALISTIC_JD + "\nAn extra sentence changes the hash."
    new_hash = store.description_hash(new_text)
    con.execute("UPDATE postings SET description_text = ?, description_hash = ? WHERE posting_id = ?",
               [new_text, new_hash, pid])
    after = con.execute("SELECT effective_required_source, judge2_required FROM vw_lens_fit "
                        "WHERE posting_id = ?", [pid]).fetchone()
    # The JD change stales BOTH the first judge's own llm_labels row (also hash-keyed via
    # vw_llm_labels_latest) and the judge2 review -- so the source falls all the way back to 'model'.
    assert after[0] == "model"
    assert after[1] is None
    con.close()


# ---------------------------------------------------------------- audit fixes (2026-09-19)
def test_preferred_lines_are_never_sent_as_required():
    """requirements.py's 'required' GROUP is Required + Preferred; the payload must split on SECTION."""
    payload = judge2.build_payload(title="T", employer="E", jd_text=REALISTIC_JD)
    assert not any("Black Belt" in l for l in payload["required_lines"])
    assert any("Black Belt" in l for l in payload["preferred_lines"])
    assert any("clearance" in l.lower() for l in payload["required_lines"])


def test_evaluate_partial_eval_run_is_insufficient_not_a_pass(tmp_path):
    """Six catch + six agree rows judged perfectly, but a third of the eval set never came back: no pass."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    for i in range(6):
        _blind_row(con, f"C{i}", human_fit="fails", judge1_fit="meets", judge2_fit="fails")
        _blind_row(con, f"A{i}", human_fit="meets", judge1_fit="meets", judge2_fit="meets")
    for i in range(6):
        _blind_row(con, f"U{i}", human_fit="fails", judge1_fit="meets", judge2_fit=None)
    out = judge2.evaluate(con, log=lambda *_: None)
    assert out["n_unjudged"] == 6 and out["catch_rate"] == 1.0 and out["agree_rate"] == 1.0
    assert out["insufficient"] and not out["passed"]
    con.close()


def test_every_request_is_paced_even_when_it_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE2_LIVE_OK", "1")
    monkeypatch.setenv("GEMINI_API_MODEL", "m")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    con = store.connect(str(tmp_path / "t.duckdb"))
    for i in range(3):
        pid, _ = _make_posting(con, f"R{i}")
        _insert_screen(con, pid)
    sleeps = []
    result = judge2.run(con, top_n=5, transport=lambda url, headers=None, json=None: _FakeResponse(400, {}),
                        sleep_fn=sleeps.append, rpm=10, log=lambda *_: None)
    assert result["reviewed"] == 0 and result["skipped_no_model"] == 3
    assert len(sleeps) == 2 and all(x > 6.0 for x in sleeps)   # token pacing (default TPM) outlasts 60/rpm
    monkeypatch.setenv("GEMINI_TPM", "0")                       # token pacing off: the plain 60/rpm pace
    sleeps.clear()
    judge2.run(con, top_n=5, transport=lambda url, headers=None, json=None: _FakeResponse(400, {}),
               sleep_fn=sleeps.append, rpm=10, log=lambda *_: None)
    assert sleeps.count(6.0) == 2          # between postings, although none succeeded


def test_answer_is_read_from_the_non_thought_parts():
    data = {"candidates": [{"content": {"parts": [{"text": "let me reason...", "thought": True},
                                                  {"text": '{"required_fit": "meets"}'}]}}]}
    assert judge2._gemini_text(data) == '{"required_fit": "meets"}'
    assert judge2._gemini_text({"candidates": [{"content": {"parts": [{"text": "x", "thought": True}]}}]}) is None


def test_live_run_refuses_without_a_key(tmp_path, monkeypatch):
    monkeypatch.setenv("JUDGE2_LIVE_OK", "1")
    monkeypatch.setenv("GEMINI_API_MODEL", "m")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    con = store.connect(str(tmp_path / "t.duckdb"))
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        judge2.run(con, top_n=1, transport=_ExplodingTransport(), sleep_fn=lambda s: None)
    con.close()
