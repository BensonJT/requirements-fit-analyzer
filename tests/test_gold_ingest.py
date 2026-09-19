"""Unit tests for backend/finder/gold_ingest.py: format detection, alias normalization, row rejection,
basis handling, precedence merging and the write path -- temp DuckDB files, invented postings only (no
personal names, employers or real CSV rows), no network."""
import csv
import os
import runpy
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import gold_ingest  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


def _quiet(*_a, **_k):
    pass


def _posting(con, pid, *, employer="Acme", title="Role", description_hash="h"):
    con.execute("""INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary,
        status, description_hash, first_seen_at, last_seen_at)
        VALUES (?, ?, 'greenhouse', ?, ?, ?, 'Remote - USA', 'active', ?, '2026-09-01', '2026-09-01')""",
        [pid, employer, pid, title, f"https://x/{pid}", description_hash])


def _write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


PID_A = "a" + "0" * 19
PID_B = "b" + "0" * 19


# ---------------------------------------------------------------- format detection

def test_detect_format_f1_f2_f3_f4():
    assert gold_ingest.detect_format(
        ["posting_id", "description_hash", "basis", "assessor", "confirmed_by_user", "verdict"]) == "f1"
    assert gold_ingest.detect_format(
        ["row", "posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"]) == "f2"
    assert gold_ingest.detect_format(
        ["posting_id", "employer", "title", "location", "url", "human_grade", "required_fit",
         "required_unmet", "level_fit", "note"]) == "f3"
    assert gold_ingest.detect_format(
        ["row", "posting_id", "employer", "title", "url", "posting_status", "human_grade_ai", "level_fit",
         "confidence", "note"]) == "f4"


def test_detect_format_f5_and_unknown():
    assert gold_ingest.detect_format(
        ["posting_id", "employer", "title", "human_grade", "half", "baseline_judge_grade"]) == "f5"
    with pytest.raises(gold_ingest.UnknownFormat):
        gold_ingest.detect_format(["some", "other", "columns"])


def test_detect_format_bom_case_whitespace_insensitive():
    assert gold_ingest.detect_format(["﻿Row", " Posting_ID ", "Employer", "Title", "URL",
                                      "Human_Grade", "Level_Fit", "Note"]) == "f2"


def test_ignored_columns_are_listed():
    cols = gold_ingest.ignored_columns(
        ["row", "posting_id", "employer", "title", "url", "human_grade", "level_fit", "note", "still_open"],
        "f2")
    assert cols == ["still_open"]


# ---------------------------------------------------------------- unknown format / F5 / proposed guard

def test_unknown_header_refuses_file_but_continues(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    bad = tmp_path / "bad.csv"
    _write(bad, ["totally", "unrecognized"], [("x", "y")])
    good = tmp_path / "good.csv"
    _write(good, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    result = gold_ingest.ingest(con, [(bad, "seen"), (good, "seen")], log=_quiet)
    assert result["ok"] is False
    assert result["files"][0]["refused"] is not None
    assert "unrecognized header" in result["files"][0]["refused"]
    assert result["files"][1]["refused"] is None
    assert con.execute("SELECT human_grade FROM report_feedback WHERE posting_id = ?",
                       [PID_A]).fetchone() == ("bullseye",)
    con.close()


def test_f5_derived_split_is_always_refused(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    path = tmp_path / "split.csv"
    _write(path, ["posting_id", "employer", "title", "human_grade", "half", "baseline_judge_grade",
                 "baseline_rubric_version"],
          [(PID_A, "Acme", "Role", "adjacent", "frozen", "adjacent", "r1")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is False
    assert result["files"][0]["format"] == "f5"
    assert "derived train/frozen split" in result["files"][0]["refused"]
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 0
    con.close()


def test_proposed_file_refused_without_flag_accepted_with_it(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "proposed.csv"
    _write(path, ["posting_id", "employer", "title", "location", "url", "human_grade", "required_fit",
                 "required_unmet", "level_fit", "note", "source_sheet", "proposal_confidence",
                 "proposal_reason"],
          [(PID_A, "Acme", "Role", "Remote", f"https://x/{PID_A}", "adjacent", "fails", "5 years required",
            "in_range", "", "wave1", "0.8", "title match")])

    refused = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert refused["ok"] is False
    assert "proposal_confidence" in refused["files"][0]["refused"]
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 0

    accepted = gold_ingest.ingest(con, [(path, "seen")], accept_proposed=True, log=_quiet)
    assert accepted["ok"] is True
    row = con.execute("SELECT human_grade, required_fit, basis FROM report_feedback WHERE posting_id = ?",
                      [PID_A]).fetchone()
    assert row == ("adjacent", "fails", "seen")
    assert set(accepted["files"][0]["ignored_columns"]) == {"source_sheet", "proposal_confidence",
                                                             "proposal_reason"}
    con.close()


# ---------------------------------------------------------------- basis required for narrow sheets

def test_f2_without_basis_is_refused(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    result = gold_ingest.ingest(con, [(path, None)], log=_quiet)
    assert result["ok"] is False
    assert "no basis declared" in result["files"][0]["refused"]
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- normalization / aliasing / rejection

def test_level_fit_aliases_applied_and_logged(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "wrong", "to_low", "")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is True
    assert con.execute("SELECT level_fit FROM report_feedback WHERE posting_id = ?",
                       [PID_A]).fetchone() == ("too_low",)
    aliases = result["files"][0]["aliases_applied"]
    assert aliases == [{"line": 2, "posting_id": PID_A, "field": "level_fit", "from": "to_low",
                        "to": "too_low"}]
    con.close()


def test_invalid_enum_value_rejects_row_others_proceed(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    _posting(con, PID_B, employer="Beta")
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "wrong", "not_a_level", ""),
           (PID_B, "Beta", "Role", f"https://x/{PID_B}", "adjacent", "in_range", "")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is False
    rejected = result["files"][0]["rejected"]
    assert rejected == [{"line": 2, "posting_id": PID_A, "field": "level_fit", "value": "not_a_level"}]
    # the good row still landed
    assert con.execute("SELECT human_grade FROM report_feedback WHERE posting_id = ?",
                       [PID_B]).fetchone() == ("adjacent",)
    # the bad row did not
    assert con.execute("SELECT count(*) FROM report_feedback WHERE posting_id = ?", [PID_A]).fetchone()[0] == 0
    con.close()


def test_blank_human_grade_skipped_never_guessed(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "", "in_range", "")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is True
    assert result["files"][0]["skipped_blank_grade"] == 1
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 0
    con.close()


def test_posting_id_not_in_postings_skipped_and_counted(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    path = tmp_path / "sheet.csv"
    ghost = "z" + "9" * 19
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(ghost, "Acme", "Role", "https://x/ghost", "bullseye", "in_range", "")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is True
    assert result["files"][0]["skipped_no_posting"] == 1
    con.close()


def test_blank_posting_id_matched_by_url(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [("", "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is True
    assert result["files"][0]["matched_by_url"] == 1
    assert con.execute("SELECT human_grade FROM report_feedback WHERE posting_id = ?",
                       [PID_A]).fetchone() == ("bullseye",)
    con.close()


def test_f3_writes_required_fit_null_for_f2(tmp_path):
    """F2 has no required_fit column at all -- the written row's required_fit stays NULL."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert con.execute("SELECT required_fit FROM report_feedback WHERE posting_id = ?",
                       [PID_A]).fetchone() == (None,)
    con.close()


def test_f3_required_fit_meets_bridges_to_llm_labels(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "location", "url", "human_grade", "required_fit",
                 "required_unmet", "level_fit", "note"],
          [(PID_A, "Acme", "Role", "Remote", f"https://x/{PID_A}", "adjacent", "fails", "5+ years required",
            "in_range", "")])
    result = gold_ingest.ingest(con, [(path, "blind")], log=_quiet)
    assert result["ok"] is True
    row = con.execute("SELECT required_fit, required_unmet, lens_grade_source, scorer FROM llm_labels "
                      "WHERE posting_id = ?", [PID_A]).fetchone()
    assert row == ("fails", "5+ years required", "human", "user-adjudicated")
    con.close()


# ---------------------------------------------------------------- precedence / merge rules

def test_later_file_wins_grade_but_keeps_earlier_required_fit(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    f3 = tmp_path / "required.csv"
    _write(f3, ["posting_id", "employer", "title", "location", "url", "human_grade", "required_fit",
               "required_unmet", "level_fit", "note"],
          [(PID_A, "Acme", "Role", "Remote", f"https://x/{PID_A}", "wrong", "fails", "clearance required",
            "in_range", "first pass")])
    f2 = tmp_path / "spot.csv"
    _write(f2, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "adjacent", "stretch_up", "second look")])

    result = gold_ingest.ingest(con, [(f3, "blind"), (f2, "seen")], log=_quiet)
    assert result["ok"] is True
    row = con.execute("SELECT human_grade, level_fit, note, required_fit, required_unmet, basis "
                      "FROM report_feedback WHERE posting_id = ?", [PID_A]).fetchone()
    # grade/level/note come from the LATER file; required_fit/required_unmet from the EARLIER file, which
    # is the only one that carried a required_fit at all; basis is protected -- 'blind' never downgrades.
    assert row == ("adjacent", "stretch_up", "second look", "fails", "clearance required", "blind")
    con.close()


def test_basis_never_downgrades_blind_to_seen(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    blind_file = tmp_path / "blind.csv"
    _write(blind_file, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    seen_file = tmp_path / "seen.csv"
    _write(seen_file, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "adjacent", "in_range", "")])

    result = gold_ingest.ingest(con, [(blind_file, "blind"), (seen_file, "seen")], log=_quiet)
    assert con.execute("SELECT basis FROM report_feedback WHERE posting_id = ?", [PID_A]).fetchone() == \
        ("blind",)
    assert len(result["files"][1]["conflicts"]) == 1


def test_basis_never_upgrades_seen_to_blind_across_runs(tmp_path):
    """The DB's existing basis wins even when the CONFLICTING file runs in a later, separate `ingest` call."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    seen_file = tmp_path / "seen.csv"
    _write(seen_file, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "adjacent", "in_range", "")])
    gold_ingest.ingest(con, [(seen_file, "seen")], log=_quiet)

    blind_file = tmp_path / "blind.csv"
    _write(blind_file, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    result = gold_ingest.ingest(con, [(blind_file, "blind")], log=_quiet)
    assert con.execute("SELECT basis, human_grade FROM report_feedback WHERE posting_id = ?",
                       [PID_A]).fetchone() == ("seen", "bullseye")
    assert len(result["files"][0]["conflicts"]) == 1
    con.close()


# ---------------------------------------------------------------- dry run

def test_dry_run_writes_nothing(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("report_feedback", "llm_labels")}
    result = gold_ingest.ingest(con, [(path, "seen")], dry_run=True, log=_quiet)
    after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
             for t in ("report_feedback", "llm_labels")}
    assert before == after
    assert result["files"][0]["would_write"] == 1
    con.close()


def test_ingest_is_idempotent(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "sheet.csv"
    _write(path, ["posting_id", "employer", "title", "location", "url", "human_grade", "required_fit",
                 "required_unmet", "level_fit", "note"],
          [(PID_A, "Acme", "Role", "Remote", f"https://x/{PID_A}", "adjacent", "fails", "5+ years",
            "in_range", "note one")])
    gold_ingest.ingest(con, [(path, "blind")], log=_quiet)
    first = con.execute("SELECT * FROM report_feedback WHERE posting_id = ?", [PID_A]).fetchall()
    first_labels = con.execute("SELECT * FROM llm_labels WHERE posting_id = ?", [PID_A]).fetchall()
    gold_ingest.ingest(con, [(path, "blind")], log=_quiet)
    second = con.execute("SELECT * FROM report_feedback WHERE posting_id = ?", [PID_A]).fetchall()
    second_labels = con.execute("SELECT * FROM llm_labels WHERE posting_id = ?", [PID_A]).fetchall()
    assert len(first) == 1 and len(second) == 1
    assert first[0][:-2] == second[0][:-2]  # every column but assessed_at / loaded_at (both re-stamped)
    assert len(first_labels) == 1 and len(second_labels) == 1
    con.close()


# ---------------------------------------------------------------- F1 delegation

def test_f1_delegates_to_load_csv(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A, description_hash="hash1")
    path = tmp_path / "golden.csv"
    _write(path, list(__import__("backend.finder.feedback", fromlist=["FEEDBACK_CSV_COLUMNS"])
                      .FEEDBACK_CSV_COLUMNS),
          [(PID_A, "", "Acme", "Role", f"https://x/{PID_A}", "hash1", "", "", "", "", "bullseye", "in_range",
            "build", "", "", "", "", "high", "jd_read", "human-override", "2026-09-19", "TRUE", "", "", "",
            "", "", "")])
    result = gold_ingest.ingest(con, [(path, None)], log=_quiet)
    assert result["ok"] is True
    assert result["files"][0]["format"] == "f1"
    row = con.execute("SELECT human_grade, basis, assessor FROM report_feedback WHERE posting_id = ?",
                      [PID_A]).fetchone()
    assert row == ("bullseye", "jd_read", "human-override")
    con.close()


def test_f1_dry_run_counts_without_writing(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A, description_hash="hash1")
    path = tmp_path / "golden.csv"
    _write(path, list(__import__("backend.finder.feedback", fromlist=["FEEDBACK_CSV_COLUMNS"])
                      .FEEDBACK_CSV_COLUMNS),
          [(PID_A, "", "Acme", "Role", f"https://x/{PID_A}", "hash1", "", "", "", "", "bullseye", "in_range",
            "build", "", "", "", "", "high", "jd_read", "human-override", "2026-09-19", "TRUE", "", "", "",
            "", "", "")])
    result = gold_ingest.ingest(con, [(path, None)], dry_run=True, log=_quiet)
    assert result["files"][0]["would_write"] == 1
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 0
    con.close()


# ---------------------------------------------------------------- F4 (single-lens AI grade)

def _f4_write(path, rows):
    _write(path, ["row", "posting_id", "employer", "title", "url", "posting_status", "human_grade_ai",
                 "level_fit", "confidence", "note"], rows)


def test_f4_writes_human_lens_grades_never_touches_llm_labels(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "ai_lens.csv"
    _f4_write(path, [(1, PID_A, "Acme", "Role", f"https://x/{PID_A}", "active", "wrong", "out_of_reach",
                      "high", "a note")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is True
    assert result["files"][0]["format"] == "f4"
    assert result["files"][0]["refused"] is None
    assert result["files"][0]["would_write"] == 1
    assert result["lens_grades_written"] == 1
    row = con.execute("SELECT posting_id, description_hash, lens, grade, basis, level_fit, note, source_file "
                      "FROM human_lens_grades").fetchone()
    assert row == (PID_A, "h", "ai", "wrong", "seen", "out_of_reach", "a note", "ai_lens.csv")
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM llm_labels").fetchone()[0] == 0
    con.close()


def test_f4_dry_run(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "ai_lens.csv"
    _f4_write(path, [(1, PID_A, "Acme", "Role", f"https://x/{PID_A}", "active", "bullseye", "in_range",
                      "high", "")])
    result = gold_ingest.ingest(con, [(path, "seen")], dry_run=True, log=_quiet)
    assert result["ok"] is True
    assert result["files"][0]["would_write"] == 1
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0
    con.close()


def test_f4_without_basis_is_refused(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "ai_lens.csv"
    _f4_write(path, [(1, PID_A, "Acme", "Role", f"https://x/{PID_A}", "active", "bullseye", "", "", "")])
    result = gold_ingest.ingest(con, [(path, None)], log=_quiet)
    assert result["ok"] is False
    assert "no basis declared" in result["files"][0]["refused"]
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0
    con.close()


def test_f4_invalid_lens_grade_rejected(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "ai_lens.csv"
    _f4_write(path, [(1, PID_A, "Acme", "Role", f"https://x/{PID_A}", "active", "not-a-grade", "", "", "")])
    result = gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert result["ok"] is False
    assert result["files"][0]["rejected"][0]["field"] == "human_grade_ai"
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 0
    con.close()


def test_f4_reingest_is_idempotent(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    path = tmp_path / "ai_lens.csv"
    _f4_write(path, [(1, PID_A, "Acme", "Role", f"https://x/{PID_A}", "active", "adjacent", "in_range",
                      "high", "")])
    gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    gold_ingest.ingest(con, [(path, "seen")], log=_quiet)
    assert con.execute("SELECT count(*) FROM human_lens_grades").fetchone()[0] == 1
    assert con.execute("SELECT grade FROM human_lens_grades").fetchone() == ("adjacent",)
    con.close()


def test_f4_blind_not_overwritten_by_seen(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    blind_path = tmp_path / "blind.csv"
    _f4_write(blind_path, [(1, PID_A, "Acme", "Role", f"https://x/{PID_A}", "active", "bullseye",
                            "in_range", "high", "")])
    gold_ingest.ingest(con, [(blind_path, "blind")], log=_quiet)
    seen_path = tmp_path / "seen.csv"
    _f4_write(seen_path, [(1, PID_A, "Acme", "Role", f"https://x/{PID_A}", "active", "wrong",
                           "out_of_reach", "high", "")])
    result = gold_ingest.ingest(con, [(seen_path, "seen")], log=_quiet)
    assert len(result["files"][0]["conflicts"]) == 1
    basis, grade = con.execute("SELECT basis, grade FROM human_lens_grades WHERE posting_id = ?",
                               [PID_A]).fetchone()
    assert basis == "blind"
    assert grade == "bullseye"  # the blind row is kept whole: a seen grade never lands under a blind basis
    con.close()


# ---------------------------------------------------------------- manifest

def test_manifest_resolves_relative_paths_and_per_file_basis(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    _posting(con, PID_B, employer="Beta")
    sub = tmp_path / "sheets"
    sub.mkdir()
    f2 = sub / "spot.csv"
    _write(f2, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", "")])
    f3 = sub / "blind.csv"
    _write(f3, ["posting_id", "employer", "title", "location", "url", "human_grade", "required_fit",
               "required_unmet", "level_fit", "note"],
          [(PID_B, "Beta", "Role", "Remote", f"https://x/{PID_B}", "adjacent", "meets", "", "in_range", "")])
    manifest = tmp_path / "manifest.csv"
    _write(manifest, ["path", "basis", "notes"],
          [("sheets/spot.csv", "seen", "spot check"), ("sheets/blind.csv", "blind", "monthly blind")])

    targets = gold_ingest.load_manifest(manifest)
    assert [str(p) for p, _b, _n in targets] == [str(f2), str(f3)]
    result = gold_ingest.ingest(con, [(p, b) for p, b, _n in targets], log=_quiet)
    assert result["ok"] is True
    assert con.execute("SELECT basis FROM report_feedback WHERE posting_id = ?", [PID_A]).fetchone() == \
        ("seen",)
    assert con.execute("SELECT basis FROM report_feedback WHERE posting_id = ?", [PID_B]).fetchone() == \
        ("blind",)
    con.close()


def test_prior_golden_csv_grade_makes_a_later_blind_sheet_row_seen(tmp_path):
    """A posting the user already graded through the golden CSV ('human-override', basis jd_read) is a second
    look when it turns up on a blind sheet: the new row lands as 'seen' and the conflict is reported."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, PID_A)
    _posting(con, PID_B)
    con.execute("""INSERT INTO report_feedback (posting_id, description_hash, human_grade, verdict, basis, assessor,
                   confirmed_by_user, assessed_at, loaded_at)
                   VALUES (?, 'older-hash', 'adjacent', 'consider', 'jd_read', 'human-override', TRUE, now(), now())""", [PID_A])
    sheet = tmp_path / "blind.csv"
    _write(sheet, ["posting_id", "employer", "title", "url", "human_grade", "level_fit", "note"],
          [(PID_A, "Acme", "Role", f"https://x/{PID_A}", "bullseye", "in_range", ""),
           (PID_B, "Acme", "Role", f"https://x/{PID_B}", "bullseye", "in_range", "")])
    result = gold_ingest.ingest(con, [(sheet, "blind")], log=_quiet)
    assert len(result["files"][0]["conflicts"]) == 1
    got = dict(con.execute("SELECT posting_id, basis FROM report_feedback WHERE assessor = 'user'").fetchall())
    assert got == {PID_A: "seen", PID_B: "blind"}
    assert [r[0] for r in con.execute("SELECT posting_id FROM vw_report_feedback_blind").fetchall()] == [PID_B]
