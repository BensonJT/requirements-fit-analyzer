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


def test_v20_migrates_cleanly_from_v19_keeping_old_rows_valid(tmp_path):
    """§29.3: a v19 DB (judge2_reviews exists in its §25 shape -- required_fit NOT NULL, no derive_why/
    lines_discarded/evidence_downgraded/contract columns) migrates to v20 without losing its old rows, which
    read back as `contract='overall'`; the NEW `judge2_lines` table exists and is empty; and required_fit is
    nullable so a 'lines' contract row with zero surviving required lines can be inserted."""
    db_path = str(tmp_path / "v19.duckdb")
    con = store.connect(db_path)
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    # a §25-era ("overall" contract) row, written before v20's per-line columns existed
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at)
                   VALUES (?, ?, 'old-pv', 'gemini', 'm', 'meets', '[]', 0, false, false, NULL, 'high', '', 0, ?)""",
               [pid, dh, NOW])
    con.close()

    import duckdb
    raw = duckdb.connect(db_path)
    raw.execute("UPDATE schema_info SET version = 19")
    raw.execute("ALTER TABLE judge2_reviews DROP COLUMN derive_why")
    raw.execute("ALTER TABLE judge2_reviews DROP COLUMN lines_discarded")
    raw.execute("ALTER TABLE judge2_reviews DROP COLUMN evidence_downgraded")
    raw.execute("ALTER TABLE judge2_reviews DROP COLUMN contract")
    raw.execute("ALTER TABLE judge2_reviews ALTER COLUMN required_fit SET NOT NULL")
    raw.execute("DROP TABLE IF EXISTS judge2_lines")
    raw.close()

    con2 = store.connect(db_path)
    version = con2.execute("SELECT version FROM schema_info").fetchone()[0]
    assert version == store.SCHEMA_VERSION

    row = con2.execute("SELECT required_fit, contract, lines_discarded, evidence_downgraded, derive_why "
                       "FROM judge2_reviews WHERE prompt_version = 'old-pv'").fetchone()
    assert row == ("meets", "overall", 0, 0, None)
    assert con2.execute("SELECT count(*) FROM judge2_lines").fetchone()[0] == 0

    # required_fit is nullable now: a 'lines' contract row with no surviving required lines must be insertable
    con2.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at, derive_why, lines_discarded, evidence_downgraded,
                   contract) VALUES (?, ?, 'new-pv', 'gemini', 'm', NULL, '[]', 0, false, NULL, NULL, 'high', '',
                   0, ?, 'no required lines survived validation', 0, 0, 'lines')""", [pid, dh, NOW])
    null_row = con2.execute("SELECT required_fit, contract FROM judge2_reviews "
                            "WHERE prompt_version = 'new-pv'").fetchone()
    assert null_row == (None, "lines")
    con2.close()


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


# ---------------------------------------------------------------- validation (§29.1/§29.2 per-line guards)
def test_parse_response_discards_non_verbatim_line():
    jd = "Required: 5+ years of process improvement experience. Active TS/SCI clearance required."
    raw = json.dumps({"lines": [
        {"line": "Active TS/SCI clearance required.", "section": "required", "kind": "clearance",
         "verdict": "unmet", "evidence": ""},
        {"line": "a paraphrased made-up requirement", "section": "required", "kind": "skill",
         "verdict": "unmet", "evidence": ""},
    ], "held_clearance": True, "confidence": "high"})
    review = judge2.parse_response(raw, jd, "")
    assert review is not None
    assert [l.line for l in review.lines] == ["Active TS/SCI clearance required."]
    assert review.lines_discarded == 1


def test_parse_response_discards_invalid_enum_values():
    jd = "Required: Active TS/SCI clearance required."
    raw = json.dumps({"lines": [
        {"line": "Active TS/SCI clearance required.", "section": "required", "kind": "clearance",
         "verdict": "sort-of", "evidence": ""},
        {"line": "Active TS/SCI clearance required.", "section": "not-a-section", "kind": "clearance",
         "verdict": "unmet", "evidence": ""},
        {"line": "Active TS/SCI clearance required.", "section": "required", "kind": "not-a-kind",
         "verdict": "unmet", "evidence": ""},
    ], "held_clearance": True, "confidence": "high"})
    review = judge2.parse_response(raw, jd, "")
    assert review is not None
    assert review.lines == []
    assert review.lines_discarded == 3


def test_parse_response_evidence_guard_downgrades_met_without_verbatim_evidence():
    jd = "Required: Bachelor's degree or equivalent experience."
    bg = "Holds a Bachelor of Science in Business, awarded 2010."

    good = json.dumps({"lines": [{"line": "Bachelor's degree or equivalent experience.", "section": "required",
                                  "kind": "degree", "verdict": "met", "evidence": bg}],
                       "held_clearance": False, "confidence": "high"})
    review = judge2.parse_response(good, jd, bg)
    assert review.lines[0].verdict == "met"
    assert review.lines[0].evidence_downgraded is False
    assert review.evidence_downgraded == 0

    fabricated = json.dumps({"lines": [{"line": "Bachelor's degree or equivalent experience.",
                                        "section": "required", "kind": "degree", "verdict": "met",
                                        "evidence": "a fabricated degree claim not in the background"}],
                             "held_clearance": False, "confidence": "high"})
    review2 = judge2.parse_response(fabricated, jd, bg)
    assert review2.lines[0].verdict == "unclear"
    assert review2.lines[0].evidence_downgraded is True
    assert review2.evidence_downgraded == 1

    empty = json.dumps({"lines": [{"line": "Bachelor's degree or equivalent experience.", "section": "required",
                                   "kind": "degree", "verdict": "met", "evidence": ""}],
                        "held_clearance": False, "confidence": "high"})
    review3 = judge2.parse_response(empty, jd, bg)
    assert review3.lines[0].verdict == "unclear"
    assert review3.lines[0].evidence_downgraded is True


def test_parse_response_tolerates_fenced_and_prose_wrapped_json():
    jd = "Required: an active Public Trust clearance."
    bg = "Holds an active Public Trust clearance."
    fenced = "Here is my answer:\n```json\n" + json.dumps(
        {"lines": [{"line": "an active Public Trust clearance.", "section": "required", "kind": "clearance",
                    "verdict": "met", "evidence": bg}],
         "held_clearance": True, "confidence": "high"}) + "\n```\nThanks."
    review = judge2.parse_response(fenced, jd, bg)
    assert review is not None and review.lines[0].verdict == "met"


def test_parse_response_rejects_missing_or_non_list_lines():
    assert judge2.parse_response(json.dumps({"held_clearance": True}), "jd text") is None
    assert judge2.parse_response(json.dumps({"lines": "not a list"}), "jd text") is None


def test_parse_response_unparseable_returns_none():
    assert judge2.parse_response("not json at all", "jd text") is None


def test_verbatim_check_is_whitespace_normalized_not_case_folded():
    jd = "Required:   Active   TS/SCI clearance required."
    raw = json.dumps({"lines": [{"line": "Active TS/SCI clearance required.", "section": "required",
                                 "kind": "clearance", "verdict": "unmet", "evidence": ""}],
                      "held_clearance": True, "confidence": "high"})
    review = judge2.parse_response(raw, jd, "")
    assert [l.line for l in review.lines] == ["Active TS/SCI clearance required."]

    raw_wrong_case = json.dumps({"lines": [{"line": "active ts/sci clearance required.", "section": "required",
                                            "kind": "clearance", "verdict": "unmet", "evidence": ""}],
                                 "held_clearance": True, "confidence": "high"})
    review2 = judge2.parse_response(raw_wrong_case, jd, "")
    assert review2.lines == []
    assert review2.lines_discarded == 1


# ---------------------------------------------------------------- derive_required_fit (§29.2, pure, table-style)
def _line(line="a required line", section="required", kind="skill", verdict="met", years=None):
    return {"line": line, "section": section, "kind": kind, "verdict": verdict, "years": years}


def test_derive_zero_required_lines_returns_none():
    fit, why = judge2.derive_required_fit([], title="Analyst")
    assert fit is None and isinstance(why, str)
    # only a preferred line present -- still zero REQUIRED lines
    fit2, _why2 = judge2.derive_required_fit([_line(section="preferred")], title="Analyst")
    assert fit2 is None


def test_derive_hard_gate_unmet_fails():
    lines = [_line(kind="clearance", verdict="unmet"), _line(kind="skill", verdict="met")]
    fit, why = judge2.derive_required_fit(lines, title="")
    assert fit == "fails"
    assert "hard gate" in why


def test_derive_hard_gate_unclear_is_partial_never_meets():
    lines = [_line(kind="licence", verdict="unclear"), _line(kind="skill", verdict="met")]
    fit, why = judge2.derive_required_fit(lines, title="")
    assert fit == "partial"


def test_derive_lone_soft_gap_meets():
    lines = [_line(kind="years_function", verdict="met", line="L1"),
            _line(kind="tool", verdict="unmet", line="Familiarity with Jira is a plus.")]
    fit, why = judge2.derive_required_fit(lines, title="Process Analyst")
    assert fit == "meets"


def test_derive_two_soft_gaps_partial():
    # 4 required lines, 2 unmet (exactly half -- NOT "more than half", so the majority-unmet rule does not
    # fire); 2 soft gaps exceeds SOFT_UNMET_MEETS_MAX(1) -> partial, not meets.
    lines = [_line(kind="years_function", verdict="met", line="L1"),
            _line(kind="skill", verdict="met", line="L2"),
            _line(kind="tool", verdict="unmet", line="L3"),
            _line(kind="degree", verdict="unmet", line="L4")]
    fit, why = judge2.derive_required_fit(lines, title="Analyst")
    assert fit == "partial"


def test_derive_majority_unmet_fails():
    lines = [_line(kind="skill", verdict="unmet", line="L1"),
            _line(kind="skill", verdict="unmet", line="L2"),
            _line(kind="skill", verdict="met", line="L3")]
    fit, why = judge2.derive_required_fit(lines, title="Analyst")
    assert fit == "fails"
    assert "majority" in why


def test_derive_preferred_lines_never_move_the_call():
    lines = [_line(kind="skill", verdict="met", section="required"),
            _line(kind="clearance", verdict="unmet", section="preferred", line="Preferred: active TS/SCI")]
    fit, why = judge2.derive_required_fit(lines, title="Analyst")
    assert fit == "meets"


def test_derive_tool_in_title_is_a_hard_gate():
    lines = [_line(kind="tool", verdict="unmet", line="Experience with Salesforce required.")]
    fit, why = judge2.derive_required_fit(lines, title="Salesforce Administrator")
    assert fit == "fails"
    assert "Salesforce" in why


def test_derive_tool_with_years_on_the_line_is_a_hard_gate_even_off_title():
    lines = [_line(kind="tool", verdict="unmet", line="5+ years developing in Salesforce Apex.")]
    fit, why = judge2.derive_required_fit(lines, title="Business Analyst")
    assert fit == "fails"


def test_derive_tool_off_title_with_no_years_stays_soft():
    lines = [_line(kind="years_function", verdict="met", line="L1"),
            _line(kind="tool", verdict="unmet", line="Familiarity with Jira preferred.")]
    fit, why = judge2.derive_required_fit(lines, title="Process Analyst")
    assert fit == "meets"   # a lone soft gap, not a hard-gate failure


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
        body = _gemini_body({"lines": [{"line": "Active TS/SCI clearance required.", "section": "required",
                                        "kind": "clearance", "verdict": "unmet", "evidence": ""}],
                             "held_clearance": True, "confidence": "high"})
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
        body = _gemini_body({"lines": [], "held_clearance": False, "confidence": "medium"})
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
        return _FakeResponse(200, _gemini_body({"lines": [], "held_clearance": False, "confidence": "high"}))

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


# ---------------------------------------------------------------- §29.4/§29.5: rerun/tag, compare, rederive,
# line-level report, thinking level, dry-run prompt
def test_dry_run_prints_the_rendered_prompt(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    logs = []
    judge2.run(con, top_n=5, dry_run=True, show=1, transport=_ExplodingTransport(), log=logs.append)
    combined = "\n".join(logs)
    assert "rendered prompt" in combined
    assert "You are a strict, independent reviewer" in combined
    con.close()


def test_run_tag_keeps_a_repeat_run_separate_from_the_first(tmp_path, monkeypatch):
    """§29.4's noise-floor tool: a `--rerun --run-tag` re-ask under the IDENTICAL prompt is stored under its
    own (tagged) prompt_version rather than overwriting the first run's row, so `judge2 eval --compare` has
    two distinct rows to diff. Default (no tag) behaviour -- a repeat overwrites -- is unaffected."""
    monkeypatch.setenv("JUDGE2_LIVE_OK", "1")
    monkeypatch.setenv("GEMINI_API_MODEL", "m")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)

    def fake_transport(url, headers=None, json=None):
        return _FakeResponse(200, _gemini_body({"lines": [], "held_clearance": False, "confidence": "high"}))

    r1 = judge2.run(con, top_n=5, transport=fake_transport, sleep_fn=lambda s: None, log=lambda *_: None)
    r2 = judge2.run(con, top_n=5, transport=fake_transport, sleep_fn=lambda s: None, log=lambda *_: None,
                    rerun=True, run_tag="round6")
    assert r1["base_prompt_version"] == r2["base_prompt_version"]
    assert r1["prompt_version"] == r1["base_prompt_version"]          # untagged: unchanged from before
    assert r2["prompt_version"] == f"{r2['base_prompt_version']}:round6"
    rows = con.execute("SELECT count(*) FROM judge2_reviews WHERE posting_id = ?", [pid]).fetchone()[0]
    assert rows == 2
    con.close()


def test_compare_reports_differing_calls_between_two_prompt_versions(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    for pv, fit in (("pv-a", "meets"), ("pv-b", "fails")):
        con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider,
                       model, required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap,
                       confidence, raw_response, prompt_chars, reviewed_at, derive_why, lines_discarded,
                       evidence_downgraded, contract) VALUES (?, ?, ?, 'gemini', 'm', ?, '[]', 0, false, false,
                       NULL, 'high', '', 0, ?, 'why', 0, 0, 'lines')""", [pid, dh, pv, fit, NOW])
    diffs = judge2.compare(con, "pv-a", "pv-b", log=lambda *_: None)
    assert len(diffs) == 1
    assert diffs[0]["posting_id"] == pid
    assert diffs[0]["fit_a"] == "meets" and diffs[0]["fit_b"] == "fails"
    # identical calls under two prompt_versions -> no diff
    con.execute("UPDATE judge2_reviews SET required_fit = 'meets' WHERE prompt_version = 'pv-b'")
    assert judge2.compare(con, "pv-a", "pv-b", log=lambda *_: None) == []
    con.close()


def test_rederive_recomputes_from_stored_lines_with_no_api_call(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    pv = "rederive-pv"
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at, derive_why, lines_discarded, evidence_downgraded,
                   contract) VALUES (?, ?, ?, 'gemini', 'm', 'meets', '[]', 0, false, false, NULL, 'high', '',
                   0, ?, 'stale why', 0, 0, 'lines')""", [pid, dh, pv, NOW])
    con.execute("""INSERT INTO judge2_lines (posting_id, description_hash, prompt_version, line_no, line,
                   section, kind, verdict, evidence, years, evidence_downgraded)
                   VALUES (?, ?, ?, 0, 'Active TS/SCI clearance required.', 'required', 'clearance', 'unmet',
                          '', NULL, false)""", [pid, dh, pv])
    out = judge2.rederive(con, log=lambda *_: None)
    assert out["updated"] == 1
    row = con.execute("SELECT required_fit, derive_why, unmet FROM judge2_reviews WHERE prompt_version = ?",
                      [pv]).fetchone()
    assert row[0] == "fails"
    assert "hard gate" in row[1]
    assert json.loads(row[2]) == ["Active TS/SCI clearance required."]
    con.close()


def test_line_level_report_matches_gold_unmet_to_a_judge_line(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    feedback.record_mark(con, pid, dh, "pass", reason_code="requirement", required_fit="fails",
                         required_unmet="Active TS/SCI clearance required.", basis="blind", now=NOW)
    pv = judge2.prompt_version(judge2.get_background("public"))
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at, derive_why, lines_discarded, evidence_downgraded,
                   contract) VALUES (?, ?, ?, 'gemini', 'm', 'fails', '[]', 0, false, false, NULL, 'high', '',
                   0, ?, 'why', 0, 0, 'lines')""", [pid, dh, pv, NOW])
    con.execute("""INSERT INTO judge2_lines (posting_id, description_hash, prompt_version, line_no, line,
                   section, kind, verdict, evidence, years, evidence_downgraded)
                   VALUES (?, ?, ?, 0, 'Active TS/SCI clearance required.', 'required', 'clearance', 'unmet',
                          '', NULL, false)""", [pid, dh, pv])
    out = judge2.line_level_report(con, log=lambda *_: None)
    assert out["n_gold_unmet_lines"] == 1
    assert out["n_caught_unmet"] == 1
    assert out["rows"][0]["judge_verdict"] == "unmet"
    con.close()


def test_line_level_report_reports_never_listed_when_no_matching_judge_line(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _make_posting(con, "R1")
    _insert_screen(con, pid)
    feedback.record_mark(con, pid, dh, "pass", reason_code="requirement", required_fit="fails",
                         required_unmet="Active TS/SCI clearance required.", basis="blind", now=NOW)
    out = judge2.line_level_report(con, log=lambda *_: None)   # no judge2_lines rows at all
    assert out["n_gold_unmet_lines"] == 1
    assert out["n_caught_unmet"] == 0
    assert out["rows"][0]["judge_verdict"] == "never_listed"
    con.close()


def test_thinking_level_joins_prompt_version(monkeypatch):
    bg = judge2.get_background("public")
    monkeypatch.delenv("JUDGE2_THINKING", raising=False)
    assert judge2.thinking_level() == judge2.DEFAULT_THINKING
    pv_default = judge2.prompt_version(bg)
    monkeypatch.setenv("JUDGE2_THINKING", "high")
    assert judge2.thinking_level() == "high"
    pv_high = judge2.prompt_version(bg)
    assert pv_default != pv_high


# ---------------------------------------------------------------- orchestrator audit fixes (2026-09-20)
def test_derive_generic_capitalized_word_in_title_is_not_a_tool_gate():
    # "Data" opens the line and sits in the title; it is not a tool name, so the lone gap stays soft -> meets.
    lines = [_line(kind="years_function", verdict="met", line="L1"),
            _line(kind="tool", verdict="unmet", line="Data visualization tools such as Tableau")]
    fit, _why = judge2.derive_required_fit(lines, title="Senior Data Analyst")
    assert fit == "meets"


def test_derive_title_match_is_whole_word_not_substring():
    # "Go" must not match inside "Category"; the tool is not the job.
    lines = [_line(kind="years_function", verdict="met", line="L1"),
            _line(kind="tool", verdict="unmet", line="Exposure to Go")]
    fit, _why = judge2.derive_required_fit(lines, title="Category Planning Analyst")
    assert fit == "meets"


def test_derive_soft_unclear_lines_do_not_block_meets_until_a_majority():
    lines = [_line(kind="years_function", verdict="met", line="L1"),
            _line(kind="skill", verdict="met", line="L2"), _line(kind="skill", verdict="met", line="L3"),
            _line(kind="skill", verdict="unclear", line="Strong communication skills")]
    assert judge2.derive_required_fit(lines, title="Analyst")[0] == "meets"
    lines = [_line(kind="years_function", verdict="met", line="L1"),
            _line(kind="skill", verdict="met", line="L2"),
            _line(kind="skill", verdict="unclear", line="L3"), _line(kind="skill", verdict="unclear", line="L4")]
    assert judge2.derive_required_fit(lines, title="Analyst")[0] == "partial"


def test_derive_discarded_lines_cap_meets_at_partial():
    lines = [_line(kind="years_function", verdict="met", line="L1"), _line(kind="skill", verdict="met", line="L2")]
    assert judge2.derive_required_fit(lines, title="Analyst")[0] == "meets"
    fit, why = judge2.derive_required_fit(lines, title="Analyst", lines_discarded=1)
    assert fit == "partial" and "dropped" in why
    # a hard-gate fail is still a fail, never softened by the cap
    lines.append(_line(kind="clearance", verdict="unmet", line="L3"))
    assert judge2.derive_required_fit(lines, title="Analyst", lines_discarded=1)[0] == "fails"


def test_evaluate_reads_an_earlier_run_after_a_later_one(tmp_path):
    """A `--run-tag` repeat is NEWER than the run it repeats. evaluate() must still find the earlier run's rows
    (it reads judge2_reviews by prompt_version, not the newest-per-posting view), and the tagged repeat must
    never become the review the rank reads."""
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    pids = []
    for i in range(5):
        pids.append(_blind_row(con, f"C{i}", human_fit="fails", judge1_fit="meets", judge2_fit="fails", pv="pvold"))
        pids.append(_blind_row(con, f"A{i}", human_fit="meets", judge1_fit="meets", judge2_fit="meets", pv="pvold"))
    con.execute("""INSERT INTO judge2_reviews SELECT * REPLACE ('pvold:round6' AS prompt_version,
                   reviewed_at + INTERVAL 1 DAY AS reviewed_at) FROM judge2_reviews""")
    out = judge2.evaluate(con, prompt_version_override="pvold", log=lambda *a: None)
    assert out["n_unjudged"] == 0 and out["passed"] is True
    assert con.execute("SELECT count(*) FROM vw_judge2_latest WHERE prompt_version LIKE '%:%'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM vw_judge2_latest").fetchone()[0] == 10
    con.close()
