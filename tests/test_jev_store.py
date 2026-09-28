"""Tests for the Jev tier's storage (schema v23, backend/ats/store.py; docs/JEV_PLAN.md §3).

Every test builds its own tmp_path DuckDB through store.connect(); nothing here opens the live DB or makes a
network call. The key test is `test_rank_is_byte_identical_with_and_without_jev_rows`: in v23 Jev is REPORTED
only, so no rank column of vw_lens_fit may move when Jev rows (however extreme, bar passed) are present.
"""
import os
import sys
import uuid
from datetime import datetime

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import store  # noqa: E402
from backend.finder.jev_types import LineRecord, ReviewRecord  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0)
REVIEWED_AT = "2026-09-28T12:00:00+00:00"
JEV_TABLES = ("jev_reviews", "jev_lines", "jev_evals")
JEV_VIEWS = ("vw_jev_latest", "vw_jev_eval_latest", "vw_jev_bar")
J3_COLUMNS = ("j3_required_fit", "j3_lens_process_grade", "j3_lens_technical_grade", "j3_lens_ai_grade",
              "j3_injection_p", "j3_prompt_version", "jev_bar_passed")


def _connect(tmp_path, name="t.duckdb"):
    return store.connect(str(tmp_path / name))


def _line(line_no, *, section="required", verdict="met", probs=None, fact="f01"):
    return LineRecord(
        line_no=line_no, section=section, line_text=f"line {line_no} text", kind="skill", kind_conf=0.9,
        verdict=verdict, verdict_raw=verdict,
        verdict_probs=probs or {"met": 0.7, "adjacent": 0.1, "unmet": 0.1, "unclear": 0.1}, verdict_conf=0.6,
        evidence_fact_id=fact, evidence_p=0.8 if fact else None, evidence_text="a fact" if fact else None,
        evidence_downgraded=False)


def _review(pid, dh="h", *, pv="pv1", run_tag=None, drift=False, tokens=1000, reviewed_at=REVIEWED_AT,
            required_fit="meets", grade="bullseye", injection_p=0.01, lines=None):
    probs = {"wrong": 0.05, "stretch": 0.1, "adjacent": 0.25, "bullseye": 0.6}
    return ReviewRecord(
        posting_id=pid, description_hash=dh, prompt_version=pv, run_tag=run_tag, endpoint="typesafe",
        model_requested="jev-1.13.0", model_answered="jev-9.9.9" if drift else "jev-1.13.0",
        version_drift=drift, input_tokens=tokens,
        lens_process_score=2.4, lens_process_grade=grade, lens_process_conf=0.7, lens_process_probs=probs,
        lens_technical_score=1.1, lens_technical_grade=grade, lens_technical_conf=0.5,
        lens_technical_probs=dict(probs),
        lens_ai_score=0.2, lens_ai_grade=grade, lens_ai_conf=0.8, lens_ai_probs=dict(probs),
        gates={"clearance_required": 0.02, "cadence": {"choice": "remote", "confidence": 0.9,
                                                       "probabilities": {"remote": 0.9, "not_stated": 0.1}}},
        injection_p=injection_p, required_fit=required_fit, derive_why="all required met",
        lines_fit=required_fit, shape_fit="fits", shape_score=0.8,
        raw_response={"role": {"model": "jev-1.13.0"}, "lines": [{"model": "jev-1.13.0"}]},
        reviewed_at=reviewed_at, lines=lines if lines is not None else [])


def _posting(con, pid, *, dh="h", verdict="review", fit_process=None, fit_technical=None, fit_ai=None,
             fit_required=None, grade_process=None, grade_technical=None, grade_ai=None, required_fit=None,
             scorer="test", lens_grade_source=None, level_fit="in_range"):
    """A screened, active fit-track posting (the shape tests/test_bullseye.py uses to feed vw_lens_fit)."""
    con.execute(
        "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary, "
        "status, description_hash, first_seen_at, last_seen_at) VALUES "
        "(?, 'Acme', 'greenhouse', ?, 'T', ?, 'Remote - USA', 'active', ?, ?, ?)",
        [pid, pid, f"https://x/{pid}", dh, NOW, NOW])
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
        "final_score, band, fit_process, fit_technical, fit_ai, fit_required, level_fit) VALUES "
        "(?, 'rv', 'mv', ?, ?, 70, 80, 'strong', ?, ?, ?, ?, ?)",
        [pid, NOW, verdict, fit_process, fit_technical, fit_ai, fit_required, level_fit])
    if grade_process or grade_technical or grade_ai or required_fit:
        con.execute(
            "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
            "grade_technical, grade_ai, required_fit, required_unmet, lens_grade_source, judged_at) "
            "VALUES (?, ?, 'rv1', ?, 'bullseye', ?, ?, ?, ?, ?, ?, ?)",
            [pid, dh, scorer, grade_process, grade_technical, grade_ai, required_fit,
             "Active clearance required." if required_fit == "fails" else None, lens_grade_source, NOW])


def _judge2(con, pid, dh="h", *, pv, required_fit, passed=None):
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at)
                   VALUES (?, ?, ?, 'gemini', 'm', ?, '["Active clearance required."]', 0, false, false, NULL,
                           'high', '', 0, ?)""", [pid, dh, pv, required_fit, NOW])
    if passed is not None:
        con.execute("""INSERT INTO judge2_evals (run_id, prompt_version, evaluated_at, n_catch, n_catch_hits,
                       catch_rate, n_agree, n_agree_hits, agree_rate, passed, reason)
                       VALUES (?, ?, ?, 10, 8, 0.8, 10, 9, 0.9, ?, 'test')""",
                   [uuid.uuid4().hex, pv, NOW, passed])


def _pass_bar(con, pv, families=("required", "lens", "repeatability")):
    run_id = uuid.uuid4().hex
    for family in families:
        store.insert_jev_eval(con, run_id=run_id, prompt_version=pv, family=family, n=112,
                              metrics={"rate": 0.9}, passed=True, reason="test")


def _objects(con, kind):
    return {r[0] for r in con.execute(f"SELECT {kind}_name FROM duckdb_{kind}s() WHERE NOT internal").fetchall()}


# ---------------------------------------------------------------- schema / migration
def test_fresh_db_is_v23_with_jev_tables_and_views(tmp_path):
    con = _connect(tmp_path)
    assert store.SCHEMA_VERSION == 23
    assert con.execute("SELECT version FROM schema_info").fetchone()[0] == 23
    assert set(JEV_TABLES) <= _objects(con, "table")
    assert set(JEV_VIEWS) <= _objects(con, "view")
    lens_cols = store._columns(con, "vw_lens_fit")
    assert set(J3_COLUMNS) <= lens_cols
    con.close()


def test_jev_reviews_has_one_column_per_review_record_field_except_lines(tmp_path):
    con = _connect(tmp_path)
    fields = set(ReviewRecord.__dataclass_fields__) - {"lines"}
    assert store._columns(con, "jev_reviews") == fields
    line_fields = set(LineRecord.__dataclass_fields__)
    assert store._columns(con, "jev_lines") == line_fields | {"posting_id", "description_hash",
                                                              "prompt_version", "run_tag"}
    con.close()


def test_v23_migrates_cleanly_from_v22_keeping_existing_data(tmp_path):
    """A v22 DB (no jev tables or views) reconnects as v23: the new tables exist and are empty, the new
    views exist, and every existing row is untouched."""
    fresh = _connect(tmp_path, "fresh.duckdb")
    fresh_cols = {t: sorted(store._columns(fresh, t)) for t in JEV_TABLES}
    fresh.close()

    db_path = str(tmp_path / "v22.duckdb")
    con = store.connect(db_path)
    _posting(con, "p1", fit_process=0.9, grade_process="bullseye", required_fit="meets")
    _judge2(con, "p1", pv="j2pv", required_fit="meets", passed=True)
    dump = {t: con.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
            for t in ("postings", "screens", "llm_labels", "judge2_reviews", "judge2_evals")}
    rank_before = con.execute("SELECT rank_score, effective_required_source FROM vw_lens_fit").fetchall()
    con.close()

    raw = duckdb.connect(db_path)
    raw.execute("UPDATE schema_info SET version = 22")
    for view in ("vw_lens_fit", "vw_jev_bar", "vw_jev_eval_latest", "vw_jev_latest"):
        raw.execute(f"DROP VIEW {view}")
    for table in JEV_TABLES:
        raw.execute(f"DROP TABLE {table}")
    raw.close()

    con2 = store.connect(db_path)
    assert con2.execute("SELECT version FROM schema_info").fetchone()[0] == store.SCHEMA_VERSION == 23
    assert {t: sorted(store._columns(con2, t)) for t in JEV_TABLES} == fresh_cols
    for table in JEV_TABLES:
        assert con2.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert set(JEV_VIEWS) <= _objects(con2, "view")
    for table, rows in dump.items():
        assert con2.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall() == rows
    assert con2.execute("SELECT rank_score, effective_required_source FROM vw_lens_fit").fetchall() == rank_before
    assert con2.execute("SELECT j3_required_fit, jev_bar_passed FROM vw_lens_fit").fetchall() == [(None, False)]
    con2.close()


# ---------------------------------------------------------------- insert / load
def test_insert_load_round_trip_with_lines(tmp_path):
    con = _connect(tmp_path)
    lines = [_line(0), _line(1, section="responsibility", verdict="adjacent", fact="f07"),
             _line(2, verdict="unclear", fact=None,
                   probs={"met": 0.2, "adjacent": 0.1, "unmet": 0.3, "unclear": 0.4})]
    lines[2].verdict_raw = "met"
    lines[2].evidence_downgraded = True
    record = _review("p1", lines=lines)
    store.insert_jev_review(con, record)

    loaded = store.load_jev_review(con, "p1", "h", "pv1")
    assert loaded == record                      # every field, dicts and lines included
    assert loaded.run_tag is None
    assert con.execute("SELECT run_tag FROM jev_reviews").fetchone()[0] == ""
    assert loaded.lines[2].evidence_fact_id is None and loaded.lines[2].evidence_downgraded is True
    assert loaded.gates["cadence"]["probabilities"]["remote"] == 0.9
    assert loaded.lines[0].line == "line 0 text"   # the judge2 derivation's read names still work
    assert store.load_jev_review(con, "p1", "h", "pv1", run_tag="r2") is None
    assert store.load_jev_review(con, "p1", "other-hash", "pv1") is None
    con.close()


def test_run_tag_rows_are_separate_and_round_trip(tmp_path):
    con = _connect(tmp_path)
    store.insert_jev_review(con, _review("p1", lines=[_line(0)]))
    store.insert_jev_review(con, _review("p1", run_tag="r2", required_fit="fails", lines=[_line(0), _line(1)]))
    canonical = store.load_jev_review(con, "p1", "h", "pv1")
    repeat = store.load_jev_review(con, "p1", "h", "pv1", run_tag="r2")
    assert canonical.required_fit == "meets" and len(canonical.lines) == 1
    assert repeat.run_tag == "r2" and repeat.required_fit == "fails" and len(repeat.lines) == 2
    con.close()


def test_update_jev_derivation_touches_only_the_derived_columns_of_one_key(tmp_path):
    con = _connect(tmp_path)
    store.insert_jev_review(con, _review("p1", lines=[_line(0)]))
    store.insert_jev_review(con, _review("p1", run_tag="r2", lines=[_line(0)]))
    n = store.update_jev_derivation(con, "p1", "h", "pv1", None, required_fit="fails", derive_why="new why",
                                    lines_fit="fails", shape_fit=None, shape_score=None)
    assert n == 1
    loaded = store.load_jev_review(con, "p1", "h", "pv1")
    expected = _review("p1", lines=[_line(0)])
    expected.required_fit, expected.derive_why, expected.lines_fit = "fails", "new why", "fails"
    expected.shape_fit, expected.shape_score = None, None
    assert loaded == expected                                       # every Jev answer unchanged
    assert store.load_jev_review(con, "p1", "h", "pv1", "r2").required_fit == "meets"   # the rerun untouched
    assert store.update_jev_derivation(con, "nope", "h", "pv1", None, required_fit=None, derive_why=None,
                                       lines_fit=None, shape_fit=None, shape_score=None) == 0
    con.close()


def test_reinsert_replaces_review_and_lines_rather_than_appending(tmp_path):
    con = _connect(tmp_path)
    store.insert_jev_review(con, _review("p1", lines=[_line(0), _line(1), _line(2)]))
    store.insert_jev_review(con, _review("p1", required_fit="partial", tokens=5, lines=[_line(0)]))
    assert con.execute("SELECT count(*) FROM jev_reviews").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM jev_lines").fetchone()[0] == 1
    loaded = store.load_jev_review(con, "p1", "h", "pv1")
    assert loaded.required_fit == "partial" and loaded.input_tokens == 5 and len(loaded.lines) == 1
    con.close()


def test_optional_dicts_store_null_and_load_none(tmp_path):
    con = _connect(tmp_path)
    record = _review("p1")
    record.lens_ai_probs = None
    record.lens_ai_score = record.lens_ai_grade = record.lens_ai_conf = None
    record.required_fit = None
    store.insert_jev_review(con, record)
    assert con.execute("SELECT lens_ai_probs FROM jev_reviews").fetchone()[0] is None
    assert store.load_jev_review(con, "p1", "h", "pv1") == record
    con.close()


def test_reviewed_at_offsets_normalize_to_utc(tmp_path):
    con = _connect(tmp_path)
    store.insert_jev_review(con, _review("p1", reviewed_at="2026-09-28T08:00:00-04:00"))
    store.insert_jev_review(con, _review("p2", reviewed_at="2026-09-28T12:00:00Z"))
    stored = con.execute("SELECT reviewed_at FROM jev_reviews ORDER BY posting_id").fetchall()
    assert stored == [(NOW,), (NOW,)]
    assert store.load_jev_review(con, "p1", "h", "pv1").reviewed_at == REVIEWED_AT
    con.close()


def test_jev_already_reviewed(tmp_path):
    con = _connect(tmp_path)
    assert store.jev_already_reviewed(con, "p1", "h", "pv1", None) is False
    store.insert_jev_review(con, _review("p1"))
    assert store.jev_already_reviewed(con, "p1", "h", "pv1", None) is True
    assert store.jev_already_reviewed(con, "p1", "h", "pv1", "") is True
    assert store.jev_already_reviewed(con, "p1", "h", "pv1", "r2") is False
    assert store.jev_already_reviewed(con, "p1", "h", "pv2", None) is False
    assert store.jev_already_reviewed(con, "p1", "h2", "pv1", None) is False
    store.insert_jev_review(con, _review("p1", run_tag="r2"))
    assert store.jev_already_reviewed(con, "p1", "h", "pv1", "r2") is True
    con.close()


def test_jev_tokens_since_counts_every_run_tag_from_the_boundary(tmp_path):
    con = _connect(tmp_path)
    store.insert_jev_review(con, _review("p1", tokens=100, reviewed_at="2026-09-27T23:59:59+00:00"))
    store.insert_jev_review(con, _review("p2", tokens=200, reviewed_at="2026-09-28T00:00:00+00:00"))
    store.insert_jev_review(con, _review("p2", run_tag="r2", tokens=300, reviewed_at="2026-09-28T09:00:00+00:00"))
    store.insert_jev_review(con, _review("p3", drift=True, tokens=400, reviewed_at="2026-09-28T10:00:00+00:00"))
    assert store.jev_tokens_since(con, "2026-09-28T00:00:00+00:00") == 900
    assert store.jev_tokens_since(con, "2026-09-28T00:00:00Z") == 900
    assert store.jev_tokens_since(con, "2026-09-28T00:00:00") == 900
    assert store.jev_tokens_since(con, "2026-09-27T00:00:00+00:00") == 1000
    assert store.jev_tokens_since(con, "2026-09-29T00:00:00+00:00") == 0
    assert isinstance(store.jev_tokens_since(con, "2026-09-29T00:00:00+00:00"), int)
    con.close()


def test_insert_jev_eval_rejects_an_unknown_family(tmp_path):
    con = _connect(tmp_path)
    with pytest.raises(ValueError):
        store.insert_jev_eval(con, run_id="r", prompt_version="pv1", family="vibes", n=1, metrics={},
                              passed=True, reason=None)
    store.insert_jev_eval(con, run_id="r", prompt_version="pv1", family="calibration", n=112,
                          metrics={"ece": 0.04, "brier": 0.12}, passed=True, reason="ok")
    row = con.execute("SELECT family, n, metrics::VARCHAR, passed, reason FROM jev_evals").fetchone()
    assert row[0] == "calibration" and row[1] == 112 and row[3] is True and row[4] == "ok"
    assert '"ece": 0.04' in row[2]
    con.close()


# ---------------------------------------------------------------- views
def test_vw_jev_latest_excludes_run_tag_drift_and_stale_hash_rows(tmp_path):
    con = _connect(tmp_path)
    for pid in ("p1", "p2", "p3", "p4"):
        _posting(con, pid, dh="cur")
    # p1: canonical row wins over a newer repeat run
    store.insert_jev_review(con, _review("p1", dh="cur", required_fit="meets"))
    store.insert_jev_review(con, _review("p1", dh="cur", run_tag="r2", required_fit="fails",
                                         reviewed_at="2026-09-28T13:00:00+00:00"))
    # p2: only a drifted row -> absent
    store.insert_jev_review(con, _review("p2", dh="cur", drift=True))
    # p3: only a stale-hash row -> absent
    store.insert_jev_review(con, _review("p3", dh="old"))
    # p4: two canonical prompt_versions -> the newest reviewed_at
    store.insert_jev_review(con, _review("p4", dh="cur", pv="pv-old", required_fit="fails",
                                         reviewed_at="2026-09-27T12:00:00+00:00"))
    store.insert_jev_review(con, _review("p4", dh="cur", pv="pv-new", required_fit="partial"))
    got = con.execute("SELECT posting_id, prompt_version, required_fit FROM vw_jev_latest "
                      "ORDER BY posting_id").fetchall()
    assert got == [("p1", "pv1", "meets"), ("p4", "pv-new", "partial")]
    con.close()


def test_vw_jev_eval_latest_is_the_newest_per_prompt_version_and_family(tmp_path):
    con = _connect(tmp_path)
    for run_id, when, passed in (("a", datetime(2026, 9, 1), False), ("b", datetime(2026, 9, 2), True)):
        con.execute("INSERT INTO jev_evals VALUES (?, 'pv1', 'lens', 5, '{}', ?, NULL, ?)", [run_id, passed, when])
    con.execute("INSERT INTO jev_evals VALUES ('a', 'pv1', 'required', 5, '{}', FALSE, NULL, ?)",
                [datetime(2026, 9, 1)])
    got = con.execute("SELECT family, run_id, passed FROM vw_jev_eval_latest ORDER BY family").fetchall()
    assert got == [("lens", "b", True), ("required", "a", False)]
    con.close()


def _bar(con, pv):
    row = con.execute("SELECT jev_bar_passed FROM vw_jev_bar WHERE prompt_version = ?", [pv]).fetchone()
    return None if row is None else row[0]


def test_jev_bar_passed_only_when_required_lens_and_repeatability_all_passed(tmp_path):
    con = _connect(tmp_path)
    assert _bar(con, "pv1") is None                                   # never evaluated
    _pass_bar(con, "pv1", families=("required", "lens"))
    assert _bar(con, "pv1") is False                                  # repeatability missing
    _pass_bar(con, "pv1", families=("calibration", "injection", "sentinel", "compare"))
    assert _bar(con, "pv1") is False                                  # other families never stand in
    store.insert_jev_eval(con, run_id="rep", prompt_version="pv1", family="repeatability", n=112,
                          metrics={}, passed=False, reason="flip rate 0.05")
    assert _bar(con, "pv1") is False                                  # repeatability failed
    con.execute("INSERT INTO jev_evals VALUES ('rep2', 'pv1', 'repeatability', 112, '{}', TRUE, NULL, ?)",
                [datetime(2099, 1, 1)])
    assert _bar(con, "pv1") is True                                   # the newest repeatability passed
    con.execute("INSERT INTO jev_evals VALUES ('lens2', 'pv1', 'lens', 112, '{}', FALSE, NULL, ?)",
                [datetime(2099, 1, 2)])
    assert _bar(con, "pv1") is False                                  # a newer failed lens eval revokes it
    assert _bar(con, "pv2") is None                                   # per prompt_version
    assert con.execute("SELECT count(*) FROM vw_jev_bar").fetchone()[0] == 1
    con.close()


# ---------------------------------------------------------------- vw_lens_fit: reported only
def _population(con):
    """A spread of postings reaching every effective_required_source and rank branch."""
    _posting(con, "a-judged-meets", fit_process=0.9, grade_process="bullseye", required_fit="meets")
    _posting(con, "b-judge2-fails", fit_process=0.9, grade_process="bullseye", required_fit="meets")
    _judge2(con, "b-judge2-fails", pv="j2-passed", required_fit="fails", passed=True)
    _posting(con, "c-model-only", fit_process=0.92, fit_technical=0.8, fit_ai=0.75, fit_required=0.6)
    _posting(con, "d-judged-fails", grade_process="adjacent", grade_technical="bullseye", required_fit="fails")
    _posting(con, "e-human", grade_process="bullseye", grade_ai="adjacent", required_fit="meets",
             scorer="user-adjudicated", lens_grade_source="human")
    _posting(con, "f-screen-reject", verdict="reject", fit_process=0.95, fit_required=0.9)
    _posting(con, "g-judge2-unevaluated", grade_technical="adjacent", required_fit="meets",
             level_fit="stretch_up")
    _judge2(con, "g-judge2-unevaluated", pv="j2-never-evaluated", required_fit="fails")
    _posting(con, "h-weak", fit_process=0.1, fit_technical=0.2, fit_ai=0.05, fit_required=0.3,
             level_fit="too_low")
    _posting(con, "i-no-jev", fit_technical=0.85, fit_required=0.7)


def _rank_snapshot(con):
    cols = [c for c in store._columns(con, "vw_lens_fit") if c not in J3_COLUMNS]
    cols = sorted(cols)
    rows = con.execute(f"SELECT {', '.join(cols)} FROM vw_lens_fit ORDER BY posting_id").fetchall()
    return cols, rows


def test_rank_is_byte_identical_with_and_without_jev_rows(tmp_path):
    """THE v23 guarantee (docs/JEV_PLAN.md §3): Jev is reported only. Extreme Jev reviews under a PASSED bar
    -- every good row called `fails` / `wrong` with the canary firing, every bad row called `meets` /
    `bullseye` -- plus repeat-run, drifted, stale-hash and second-prompt_version rows, must leave every
    non-j3 column of vw_lens_fit (rank_score, effective_required_*, rank_why, lens_best, ...) identical and
    the row count unchanged, while the j3_* columns show the Jev values."""
    con = _connect(tmp_path)
    _population(con)
    cols, before = _rank_snapshot(con)
    assert {"rank_score", "effective_required_value", "effective_required_source", "rank_why",
            "rank_lens_best", "lens_best"} <= set(cols)
    assert len(before) == 9
    sources = {r[cols.index("effective_required_source")] for r in before}
    assert sources == {"judge", "judge2", "human", "model"}
    assert any(r[cols.index("rank_score")] > 0 for r in before)   # the snapshot is not trivially all zeros

    jev_pids = [r[cols.index("posting_id")] for r in before if r[cols.index("posting_id")] != "i-no-jev"]
    for pid in jev_pids:
        good = pid.startswith(("a-", "c-", "e-"))
        store.insert_jev_review(con, _review(
            pid, pv="jev-passed", required_fit="fails" if good else "meets",
            grade="wrong" if good else "bullseye", injection_p=0.99 if good else 0.0,
            lines=[_line(0, verdict="unmet" if good else "met"), _line(1)]))
        # rows that must never reach vw_lens_fit, and must never duplicate it
        store.insert_jev_review(con, _review(pid, pv="jev-passed", run_tag="r2", required_fit="partial"))
        store.insert_jev_review(con, _review(pid, pv="jev-drifted", drift=True, required_fit="partial",
                                             reviewed_at="2026-09-29T12:00:00+00:00"))
        store.insert_jev_review(con, _review(pid, dh="stale", pv="jev-passed", required_fit="partial",
                                             reviewed_at="2026-09-29T12:00:00+00:00"))
        store.insert_jev_review(con, _review(pid, pv="jev-older", required_fit="partial",
                                             reviewed_at="2026-09-01T12:00:00+00:00"))
    _pass_bar(con, "jev-passed")
    _pass_bar(con, "jev-older")
    _pass_bar(con, "jev-passed")          # a second passing run: still one bar row per prompt_version
    _pass_bar(con, "jev-drifted")

    cols_after, after = _rank_snapshot(con)
    assert cols_after == cols
    assert after == before                # every rank column, every row, byte-identical
    assert con.execute("SELECT count(*) FROM vw_lens_fit").fetchone()[0] == len(before)

    j3 = {r[0]: r[1:] for r in con.execute(
        f"SELECT posting_id, {', '.join(J3_COLUMNS)} FROM vw_lens_fit ORDER BY posting_id").fetchall()}
    assert j3["a-judged-meets"] == ("fails", "wrong", "wrong", "wrong", 0.99, "jev-passed", True)
    assert j3["h-weak"] == ("meets", "bullseye", "bullseye", "bullseye", 0.0, "jev-passed", True)
    assert j3["i-no-jev"] == (None, None, None, None, None, None, False)
    con.close()


def test_jev_bar_passed_is_false_in_vw_lens_fit_when_the_bar_has_not_passed(tmp_path):
    con = _connect(tmp_path)
    _posting(con, "p1", fit_process=0.9, grade_process="bullseye", required_fit="meets")
    store.insert_jev_review(con, _review("p1", pv="jev-unevaluated"))
    row = con.execute("SELECT j3_required_fit, j3_prompt_version, jev_bar_passed FROM vw_lens_fit").fetchone()
    assert row == ("meets", "jev-unevaluated", False)
    con.close()


def test_rank_expressions_never_name_a_jev_column():
    """A static guard beside the behavioral one: after the base CTE, nothing in vw_lens_fit's definition
    (placed + the final SELECT, where every rank column is computed) mentions j3 or jev."""
    start = store.VIEWS.index("CREATE OR REPLACE VIEW vw_lens_fit AS")
    end = store.VIEWS.index("CREATE OR REPLACE VIEW", start + 1)
    definition = store.VIEWS[start:end]
    after_base = definition[definition.index("), placed AS ("):]
    assert "j3" not in after_base.lower()
    assert "jev" not in after_base.lower()
