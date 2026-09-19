"""Unit tests for backend/finder/feedback.py (report_feedback load / agreement / export) and the schema v10
write path in backend/ats/store.py + backend/finder/pipeline.py -- temp DuckDB files only, no network.
"""
import csv
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import feedback, features, pipeline, rules  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "report_feedback_sample.csv")


def _quiet(*_a, **_k):
    pass


def _seed_posting(con, req_id, *, now, title="Role", description_hash="h", workplace_type="remote"):
    store.record_board(con, "Acme", "greenhouse",
                       [N.base(req_id=req_id, title=title, url=f"https://x/{req_id}",
                               location_primary="Remote - USA", workplace_type=workplace_type)], now)
    pid = store.posting_id("Acme", "greenhouse", req_id)
    con.execute("UPDATE postings SET description_hash = ? WHERE posting_id = ?", [description_hash, pid])
    return pid


# ---------------------------------------------------------------- load_csv
def test_load_csv_counts_and_view_precedence(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 17)
    p1 = _seed_posting(con, "P1", now=now, description_hash="hash1")
    p2 = _seed_posting(con, "P2", now=now, description_hash="hash2")
    # P3 deliberately NOT seeded: the CSV row for it has no posting_id and a url that matches nothing.
    p4 = _seed_posting(con, "P4", now=now, description_hash="hash4-current")  # CSV says stale-hash-zzz
    p5 = _seed_posting(con, "P5", now=now, description_hash="hash5")
    p6 = _seed_posting(con, "P6", now=now, description_hash="hash6")

    counts = feedback.load_csv(con, FIXTURE, log=_quiet)
    assert counts == {"loaded": 6, "skipped_no_posting": 1, "matched_by_url": 1}

    # every loaded row actually landed
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 6

    latest = {r[0]: r[1:] for r in con.execute(
        "SELECT posting_id, level_fit, assessor, confirmed_by_user FROM vw_report_feedback_latest").fetchall()}
    # P1, P2, P6: current hash matches -> present. P4: stale hash -> expired, absent.
    assert p1 in latest and latest[p1] == ("in_range", "user", True)
    assert p2 in latest and latest[p2] == ("out_of_reach", "claude-sonnet-review", True)
    assert p4 not in latest
    assert p6 in latest and latest[p6][2] is False  # not confirmed
    # P5: two rows for the same posting/hash; human-override outranks the later review row.
    assert p5 in latest
    assert latest[p5] == ("too_low", "human-override", True)
    con.close()


def test_load_csv_blank_posting_id_no_match_is_skipped(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 17)
    _seed_posting(con, "P1", now=now, description_hash="hash1")
    _seed_posting(con, "P2", now=now, description_hash="hash2")
    _seed_posting(con, "P4", now=now, description_hash="hash4-current")
    _seed_posting(con, "P5", now=now, description_hash="hash5")
    _seed_posting(con, "P6", now=now, description_hash="hash6")
    feedback.load_csv(con, FIXTURE, log=_quiet)
    # The "Ghost Role" row (P3) has posting_id blank and a url nothing matches: never invented a row for it.
    assert con.execute("SELECT count(*) FROM report_feedback WHERE report_files IS NULL "
                       "AND human_grade IS NULL AND reason_code = 'function' AND verdict = 'pass'"
                       ).fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- vw_level_agreement
def _insert_screen(con, pid, level_fit, when):
    con.execute("""INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier,
                                        rule_score, final_score, band, level_fit)
                   VALUES (?, 'rv1', 'none', ?, 'candidate', 1, 50, 50, 'partial', ?)""", [pid, when, level_fit])


def test_vw_level_agreement_steps_and_exclusions(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 17)
    p1 = _seed_posting(con, "P1", now=now, description_hash="hash1")
    p2 = _seed_posting(con, "P2", now=now, description_hash="hash2")
    p5 = _seed_posting(con, "P5", now=now, description_hash="hash5")
    p6 = _seed_posting(con, "P6", now=now, description_hash="hash6")
    _seed_posting(con, "P4", now=now, description_hash="hash4-current")
    feedback.load_csv(con, FIXTURE, log=_quiet)

    _insert_screen(con, p1, "in_range", now)       # human in_range -> agree, 0 steps
    _insert_screen(con, p2, "stretch_up", now)      # human out_of_reach -> disagree, 1 step
    _insert_screen(con, p5, "out_of_reach", now)    # human too_low (human-override wins) -> disagree, 3 steps
    _insert_screen(con, p6, "in_range", now)        # P6 not confirmed_by_user -> excluded entirely

    rows = {r[0]: r[1:] for r in con.execute(
        "SELECT posting_id, human_level_fit, rule_level_fit, agree, steps_apart FROM vw_level_agreement").fetchall()}
    assert rows[p1] == ("in_range", "in_range", True, 0)
    assert rows[p2] == ("out_of_reach", "stretch_up", False, 1)
    assert rows[p5] == ("too_low", "out_of_reach", False, 3)
    assert p6 not in rows
    con.close()


# ---------------------------------------------------------------- feedback.agreement()
def test_agreement_uses_the_live_rule_not_the_stored_screen(tmp_path, monkeypatch):
    """agreement() must work before any rescreen -- it computes rules.screen_row live, not vw_screen_latest."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 17)
    p1 = _seed_posting(con, "P1", now=now, description_hash="hash1")
    p2 = _seed_posting(con, "P2", now=now, description_hash="hash2")
    p5 = _seed_posting(con, "P5", now=now, description_hash="hash5")
    _seed_posting(con, "P4", now=now, description_hash="hash4-current")
    _seed_posting(con, "P6", now=now, description_hash="hash6")
    feedback.load_csv(con, FIXTURE, log=_quiet)
    # No screens rows exist at all -- rule_level_fit falls back to rules.screen_row directly.
    assert con.execute("SELECT count(*) FROM screens").fetchone()[0] == 0

    live = {p1: "in_range", p2: "out_of_reach", p5: "too_low"}
    monkeypatch.setattr(feedback, "rule_level_fit",
                        lambda con, ids: {pid: {"level_fit": live.get(pid), "level_fit_hits": ["fake"]}
                                          for pid in ids})
    out = feedback.agreement(con, log=_quiet)
    assert out["n"] == 3          # P1, P2, P5(human-override) are confirmed with a level_fit; P6 is not confirmed
    assert out["exact"] == 3
    assert out["bar_n"] == 2      # in_range/out_of_reach only: P1 + P2 (P5's human value is too_low)
    assert out["bar_exact"] == 2
    con.close()


# ---------------------------------------------------------------- pipeline.screen write path
def test_screen_writes_level_fit_and_fit_ai(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 16)
    store.record_board(con, "Acme", "greenhouse",
                       [N.base(req_id="S1", title="Principal Analyst", url="https://x/S1",
                               location_primary="Remote - USA", workplace_type="remote")], now)
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h', description_fetched_at = ?",
               ["Principal analyst role text about process excellence and operating models.", now])

    original = rules.screen_row

    def fake_screen_row(row):
        rec = original(row)
        rec.notes["level_fit"] = "in_range"
        rec.notes["level_fit_hits"] = ["title: principal"]
        return rec

    monkeypatch.setattr(rules, "screen_row", fake_screen_row)
    monkeypatch.setattr(features, "predict", lambda m, texts: [0.77] * len(texts))
    fake_ai_model = {"version": "ai-v1"}
    pipeline.screen(con, model=None, lens_models={"ai": fake_ai_model}, log=_quiet)

    row = con.execute("SELECT level_fit, fit_ai FROM vw_screen_latest").fetchone()
    assert row == ("in_range", 0.77)
    con.close()


def test_screen_defaults_level_fit_to_none_when_the_note_is_absent(tmp_path, monkeypatch):
    """Defensive default: pipeline.screen reads rec.notes.get("level_fit"), so a ScreenRecord with no such
    note (as if Agent A's rule did not exist) still writes a clean NULL rather than raising. fit_ai stays
    NULL here too: no lens_models were passed."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 16)
    store.record_board(con, "Acme", "greenhouse",
                       [N.base(req_id="S2", title="Principal Analyst", url="https://x/S2",
                               location_primary="Remote - USA", workplace_type="remote")], now)

    original = rules.screen_row

    def strip_level_fit(row):
        rec = original(row)
        rec.notes.pop("level_fit", None)
        rec.notes.pop("level_fit_hits", None)
        return rec

    monkeypatch.setattr(rules, "screen_row", strip_level_fit)
    pipeline.screen(con, model=None, log=_quiet)
    assert con.execute("SELECT level_fit, fit_ai FROM vw_screen_latest").fetchone() == (None, None)
    con.close()


# ---------------------------------------------------------------- feedback.export()
def test_export_needs_you_branches(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 17)
    reqs = ["N1", "N2", "N3", "N4", "N5", "N6"]
    pids = {r: _seed_posting(con, r, now=now, description_hash=f"h{r}") for r in reqs}

    rows = [
        # req, level_fit, human_grade, verdict, confidence, needs_confirm
        ("N1", "in_range", "bullseye", "build", "low", False),      # low confidence
        ("N2", "in_range", "bullseye", "build", "high", False),     # rule out_of_reach: 2 steps apart
        ("N3", None, "bullseye", "build", "high", False),           # no human level_fit; rule too_low; verdict build
        ("N4", "in_range", "bullseye", "build", "high", False),     # judge grade differs > 1 step
        ("N5", "in_range", "bullseye", "build", "high", True),      # needs_confirm
        ("N6", "in_range", "bullseye", "build", "high", False),     # clean row: no branch fires
    ]
    for req, level_fit, human_grade, verdict, confidence, needs_confirm in rows:
        pid = pids[req]
        con.execute("""INSERT INTO report_feedback
                        (posting_id, description_hash, verdict, basis, assessor, confirmed_by_user,
                         human_grade, level_fit, confidence, needs_confirm, assessed_at, loaded_at)
                       SELECT ?, description_hash, ?, 'jd_read', 'user', TRUE, ?, ?, ?, ?, ?, ?
                       FROM postings WHERE posting_id = ?""",
                   [pid, verdict, human_grade, level_fit, confidence, needs_confirm, now, now, pid])

    # A judge grade for N4 (far from its human grade) and N6 (matching, so N6 stays clean).
    for req, grade in (("N4", "wrong"), ("N6", "bullseye")):
        pid = pids[req]
        con.execute("""INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, judged_at)
                       SELECT ?, description_hash, 'rv1', 'claude-sonnet-batch', ?, ? FROM postings
                       WHERE posting_id = ?""", [pid, grade, now, pid])

    rule_map = {pids["N1"]: "in_range", pids["N2"]: "out_of_reach", pids["N3"]: "too_low",
               pids["N4"]: "in_range", pids["N5"]: "in_range", pids["N6"]: "in_range"}
    monkeypatch.setattr(feedback, "rule_level_fit",
                        lambda con, ids: {pid: {"level_fit": rule_map.get(pid), "level_fit_hits": []}
                                          for pid in ids})

    out_path = tmp_path / "export.csv"
    feedback.export(con, out_path, log=_quiet)
    with out_path.open(newline="", encoding="utf-8") as f:
        by_req = {}
        for row in csv.DictReader(f):
            for req, pid in pids.items():
                if row["posting_id"] == pid:
                    by_req[req] = row

    def needs_you(req):
        return by_req[req]["needs_you"] in ("True", "true", "1")

    assert needs_you("N1"), "low confidence must set needs_you"
    assert needs_you("N2"), "level_fit > 1 step from the rule must set needs_you"
    assert needs_you("N3"), "no human level_fit but rule out_of_reach/too_low on a pursued verdict must set needs_you"
    assert needs_you("N4"), "human_grade > 1 step from judge_grade must set needs_you"
    assert needs_you("N5"), "needs_confirm must set needs_you"
    assert not needs_you("N6"), "a row with nothing wrong must not be flagged"
    assert by_req["N6"]["expired"] in ("False", "false", "0")
    con.close()


def test_export_surfaces_human_lens_grades_beside_judge_grades(tmp_path):
    """v19: a human_lens_grades row for a posting's current hash shows up in export() as human_grade_<lens>,
    beside judge_grade_<lens> -- report-only, never written by export() itself."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 19)
    pid = _seed_posting(con, "H1", now=now, description_hash="h1")
    con.execute("""INSERT INTO report_feedback
                    (posting_id, description_hash, verdict, basis, assessor, confirmed_by_user,
                     human_grade, assessed_at, loaded_at)
                   SELECT ?, description_hash, 'build', 'jd_read', 'user', TRUE, 'bullseye', ?, ?
                   FROM postings WHERE posting_id = ?""", [pid, now, now, pid])
    con.execute("""INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade,
                   grade_ai, judged_at)
                   SELECT ?, description_hash, 'rv1', 'claude-sonnet-batch', 'adjacent', 'wrong', ?
                   FROM postings WHERE posting_id = ?""", [pid, now, pid])
    con.execute("""INSERT INTO human_lens_grades (posting_id, description_hash, lens, grade, basis,
                   source_file, graded_at)
                   SELECT ?, description_hash, 'ai', 'bullseye', 'seen', 'f4.csv', ?
                   FROM postings WHERE posting_id = ?""", [pid, now, pid])

    out_path = tmp_path / "export.csv"
    feedback.export(con, out_path, log=_quiet)
    with out_path.open(newline="", encoding="utf-8") as f:
        row = next(r for r in csv.DictReader(f) if r["posting_id"] == pid)
    assert row["judge_grade_ai"] == "wrong"
    assert row["human_grade_ai"] == "bullseye"
    assert row["human_grade_process"] == ""
    con.close()


def test_ts_accepts_the_formats_excel_rewrites_a_timestamp_into():
    assert feedback._ts("2026-09-16T10:30:00") == datetime(2026, 9, 16, 10, 30)
    assert feedback._ts("9/16/2026 10:30") == datetime(2026, 9, 16, 10, 30)
    assert feedback._ts("9/16/2026 10:30:05") == datetime(2026, 9, 16, 10, 30, 5)
    assert feedback._ts("9/16/2026") == datetime(2026, 9, 16)
    assert feedback._ts("") is None
    with pytest.raises(ValueError):
        feedback._ts("not a date")
