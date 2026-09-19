"""Unit tests for the monthly blind sheet with decoys (backend/finder/blind_sheet.py, sprint plan §26) --
no network, temp DuckDB files, no personal profile data (only synthetic postings/screens/llm_labels rows)."""
import csv
import json
import os
import runpy
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import blind_sheet  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


def _quiet(*_a, **_k):
    pass


def _posting(con, pid, *, employer="Acme", title="Role", verdict="candidate", reasons=None,
             grade_process=None, required_fit=None, first_seen=None):
    """A minimal postings + screens (+ optional llm_labels) row, direct SQL, no rule engine involved --
    the bucket regexes are tested against hand-picked `reasons` text instead."""
    first_seen = first_seen or datetime(2026, 9, 1)
    con.execute("""INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary,
        status, description_hash, first_seen_at, last_seen_at)
        VALUES (?, ?, 'greenhouse', ?, ?, ?, 'Remote - USA', 'active', 'h', ?, ?)""",
        [pid, employer, pid, title, f"https://x/{pid}", first_seen, first_seen])
    con.execute("""INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict,
        rule_score, final_score, band, reasons)
        VALUES (?, 'rv', 'mv', ?, ?, 50, 50, 'weak', ?)""",
        [pid, first_seen, verdict, json.dumps(reasons) if reasons is not None else None])
    if grade_process:
        con.execute("""INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade,
            grade_process, grade_technical, grade_ai, required_fit, judged_at)
            VALUES (?, 'h', 'rv1', 'claude-sonnet-batch', 'bullseye', ?, 'wrong', 'wrong', ?, ?)""",
            [pid, grade_process, required_fit or "meets", first_seen])


def _seed_universe(con, *, n_top=11, n_mid=10, dup=False):
    """`n_top` ranked-1..n_top active candidates (grade_process='bullseye', strong rank), `n_mid` more
    immediately below them (grade_process='adjacent', a lower but still-active rank), and 2 rejects per
    bucket. Returns the top posting_ids in rank order for convenience."""
    for i in range(n_top):
        pid = f"t{i:02d}" + "0" * 18
        _posting(con, pid, employer=f"Emp{i}", title=f"Title {i}", grade_process="bullseye")
    for i in range(n_mid):
        pid = f"m{i:02d}" + "0" * 18
        _posting(con, pid, employer=f"MidEmp{i}", title=f"Mid Title {i}", grade_process="adjacent")
    if dup:
        _posting(con, "dup0" + "0" * 16, employer="Emp0", title="Title 0", grade_process="bullseye")
    for i in range(2):
        _posting(con, f"rloc{i}" + "0" * 16, employer=f"LocEmp{i}", title="Loc Role", verdict="reject",
                 reasons=["not remote and outside the commute area (per listing)"])
        _posting(con, f"rclr{i}" + "0" * 16, employer=f"ClrEmp{i}", title="Clr Role", verdict="reject",
                 reasons=["clearance must already be held -- likely unreachable (Secret)"])
        _posting(con, f"rttl{i}" + "0" * 16, employer=f"TtlEmp{i}", title="Ttl Role", verdict="reject",
                 reasons=["off-function title"])


# ---------------------------------------------------------------- quota / split (pure functions)
def test_quota_matches_the_8_6_6_of_20_mix():
    assert blind_sheet.quota(20) == (8, 6, 6)


@pytest.mark.parametrize("n", [1, 2, 5, 7, 13, 37])
def test_quota_always_sums_to_n(n):
    assert sum(blind_sheet.quota(n)) == n


def test_split_counts_puts_the_remainder_on_the_first_buckets():
    assert blind_sheet._split_counts(6, 3) == [2, 2, 2]
    assert blind_sheet._split_counts(7, 3) == [3, 2, 2]
    assert blind_sheet._split_counts(0, 3) == [0, 0, 0]


# ---------------------------------------------------------------- reject bucket recognition
@pytest.mark.parametrize("reasons, bucket", [
    (["not remote and outside the commute area (per listing)"], "location"),
    (["remote restricted to: Austin, Denver"], "location"),
    (["clearance must already be held -- likely unreachable (Secret)"], "clearance"),
    (["off-function title"], "title"),
    (["off-lane title (Marketing Coordinator)"], "title"),
    (["hard-avoid industry"], None),
    (["comp: band top under the pay floor"], None),
])
def test_bucket_for_reasons(reasons, bucket):
    assert blind_sheet.bucket_for_reasons(reasons) == bucket
    assert blind_sheet.bucket_for_reasons(json.dumps(reasons)) == bucket   # the JSON-text shape DuckDB returns


def test_bucket_for_reasons_handles_none_and_empty():
    assert blind_sheet.bucket_for_reasons(None) is None
    assert blind_sheet.bucket_for_reasons([]) is None


# ---------------------------------------------------------------- generate_sheet
def _read_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_sheet_has_no_score_like_columns(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_universe(con)
    out = blind_sheet.generate_sheet(con, n=20, seed=1, out_path=str(tmp_path / "sheet.csv"),
                                     top_n=11, mid_start=12, mid_end=21, log=_quiet)
    rows = _read_rows(out["csv"])
    assert list(rows[0].keys()) == list(blind_sheet.SHEET_COLUMNS)
    banned = {"rank", "rank_score", "verdict", "band", "final_score", "reasons", "flags", "grade",
              "grade_process", "grade_technical", "why", "rank_why", "score"}
    assert not (banned & set(rows[0].keys()))
    for r in rows:
        assert r["human_grade"] == "" and r["required_fit"] == "" and r["note"] == ""
    con.close()


def test_sheet_mix_is_8_6_6_and_reject_split_2_2_2(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_universe(con)
    out = blind_sheet.generate_sheet(con, n=20, seed=1, out_path=str(tmp_path / "sheet.csv"),
                                     top_n=11, mid_start=12, mid_end=21, log=_quiet)
    counts = out["counts"]
    assert counts.get("top50") == 8
    assert counts.get("mid200_600") == 6
    assert counts.get("reject_location") == 2
    assert counts.get("reject_clearance") == 2
    assert counts.get("reject_title") == 2
    assert sum(counts.values()) == 20
    con.close()


def test_sheet_excludes_already_decided_and_already_graded(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_universe(con, n_top=11)
    excluded_pid = "t000" + "0" * 18
    con.execute("INSERT INTO decisions VALUES (?, 'pass', 'nuance: x', 'cli', NULL, ?)",
               [excluded_pid, datetime(2026, 9, 2)])
    out = blind_sheet.generate_sheet(con, n=8, seed=1, out_path=str(tmp_path / "sheet.csv"),
                                     top_n=11, mid_start=12, mid_end=21, log=_quiet)
    ids = {r["posting_id"] for r in _read_rows(out["csv"])}
    assert excluded_pid not in ids
    con.close()


def test_sheet_dedups_requisition_duplicates_by_employer_and_title(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_universe(con, n_top=11, dup=True)
    out = blind_sheet.generate_sheet(con, n=8, seed=1, out_path=str(tmp_path / "sheet.csv"),
                                     top_n=12, mid_start=13, mid_end=22, log=_quiet)
    ids = {r["posting_id"] for r in _read_rows(out["csv"])}
    # "Emp0 | Title 0" exists as both t00... and dup0... -- at most one may appear.
    assert len({"t00" + "0" * 18, "dup0" + "0" * 16} & ids) <= 1
    con.close()


def test_sheet_falls_back_to_any_reject_when_a_bucket_is_short(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_universe(con, n_top=11)
    # Remove one of the two location rejects, so the location bucket is short by one, and add a spare
    # reject the rule buckets don't recognize -- the only thing the fallback can draw on once the three
    # named buckets are otherwise fully consumed.
    con.execute("DELETE FROM postings WHERE posting_id = ?", ["rloc1" + "0" * 16])
    _posting(con, "spare" + "0" * 15, employer="SpareEmp", title="Spare Role", verdict="reject",
             reasons=["hard-avoid industry"])
    out = blind_sheet.generate_sheet(con, n=20, seed=1, out_path=str(tmp_path / "sheet.csv"),
                                     top_n=11, mid_start=12, mid_end=21, log=_quiet)
    counts = out["counts"]
    assert counts.get("reject_location") == 1
    assert counts.get("reject_any", 0) >= 1
    assert sum(counts.values()) == 20
    con.close()


def test_sheet_is_deterministic_for_the_same_seed_and_varies_across_seeds(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_universe(con)
    out1 = blind_sheet.generate_sheet(con, n=20, seed=42, out_path=str(tmp_path / "s1.csv"),
                                      top_n=11, mid_start=12, mid_end=21, log=_quiet)
    out2 = blind_sheet.generate_sheet(con, n=20, seed=42, out_path=str(tmp_path / "s2.csv"),
                                      top_n=11, mid_start=12, mid_end=21, log=_quiet)
    rows1 = [r["posting_id"] for r in _read_rows(out1["csv"])]
    rows2 = [r["posting_id"] for r in _read_rows(out2["csv"])]
    assert rows1 == rows2   # same seed -> same picks AND same shuffled order

    out3 = blind_sheet.generate_sheet(con, n=20, seed=7, out_path=str(tmp_path / "s3.csv"),
                                      top_n=11, mid_start=12, mid_end=21, log=_quiet)
    rows3 = [r["posting_id"] for r in _read_rows(out3["csv"])]
    assert rows3 != rows1
    con.close()


def test_sidecar_records_stratum_and_generation_time_rank_and_verdict(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_universe(con)
    out = blind_sheet.generate_sheet(con, n=20, seed=1, out_path=str(tmp_path / "sheet.csv"),
                                     top_n=11, mid_start=12, mid_end=21, log=_quiet)
    sidecar = json.loads(out["sidecar"].read_text(encoding="utf-8"))
    csv_ids = {r["posting_id"] for r in _read_rows(out["csv"])}
    assert set(sidecar.keys()) == csv_ids
    strata = {info["stratum"] for info in sidecar.values()}
    assert strata <= {"top50", "mid200_600", "reject_location", "reject_clearance", "reject_title", "reject_any"}
    for info in sidecar.values():
        assert "rank_score" in info and "verdict" in info
    con.close()


# ---------------------------------------------------------------- import_sheet
def _write_sheet(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=blind_sheet.SHEET_COLUMNS)
        w.writeheader()
        w.writerows(rows)


def _write_sidecar(path, sidecar):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f)


def test_import_writes_basis_blind_assessor_user_confirmed(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "p0" + "0" * 18, employer="Acme", title="Role")
    csv_path = tmp_path / "graded.csv"
    _write_sheet(csv_path, [{"posting_id": "p0" + "0" * 18, "employer": "Acme", "title": "Role",
                            "location": "Remote", "url": "https://x/p0", "human_grade": "bullseye",
                            "required_fit": "meets", "required_unmet": "", "level_fit": "in_range",
                            "note": "looks great"}])
    _write_sidecar(blind_sheet.sidecar_path_for(csv_path),
                  {"p0" + "0" * 18: {"stratum": "top50", "rank_score": 80.0, "verdict": "candidate"}})
    blind_sheet.import_sheet(con, csv_path, history_path=str(tmp_path / "hist.jsonl"), log=_quiet)
    row = con.execute("SELECT human_grade, basis, assessor, confirmed_by_user, level_fit, note "
                      "FROM report_feedback WHERE posting_id = ?", ["p0" + "0" * 18]).fetchone()
    assert row == ("bullseye", "blind", "user", True, "in_range", "looks great")
    con.close()


def test_import_bridges_required_fit_as_human_lens_grade_source(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "p1" + "0" * 18, employer="Acme", title="Role")
    csv_path = tmp_path / "graded.csv"
    _write_sheet(csv_path, [{"posting_id": "p1" + "0" * 18, "employer": "Acme", "title": "Role",
                            "location": "Remote", "url": "https://x/p1", "human_grade": "adjacent",
                            "required_fit": "fails", "required_unmet": "Active TS/SCI", "level_fit": "",
                            "note": ""}])
    _write_sidecar(blind_sheet.sidecar_path_for(csv_path),
                  {"p1" + "0" * 18: {"stratum": "top50", "rank_score": 60.0, "verdict": "candidate"}})
    blind_sheet.import_sheet(con, csv_path, history_path=str(tmp_path / "hist.jsonl"), log=_quiet)
    ll = con.execute("SELECT scorer, grade, required_fit, required_unmet, lens_grade_source "
                     "FROM llm_labels WHERE posting_id = ?", ["p1" + "0" * 18]).fetchone()
    assert ll == ("user-adjudicated", "adjacent", "fails", "Active TS/SCI", "human")
    con.close()


def test_import_skips_ungraded_rows_and_counts_them(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "p2" + "0" * 18, employer="Acme", title="Graded")
    _posting(con, "p3" + "0" * 18, employer="Acme", title="Ungraded")
    csv_path = tmp_path / "graded.csv"
    _write_sheet(csv_path, [
        {"posting_id": "p2" + "0" * 18, "employer": "Acme", "title": "Graded", "location": "Remote",
         "url": "https://x/p2", "human_grade": "wrong", "required_fit": "", "required_unmet": "",
         "level_fit": "", "note": ""},
        {"posting_id": "p3" + "0" * 18, "employer": "Acme", "title": "Ungraded", "location": "Remote",
         "url": "https://x/p3", "human_grade": "", "required_fit": "", "required_unmet": "",
         "level_fit": "", "note": ""},
    ])
    _write_sidecar(blind_sheet.sidecar_path_for(csv_path), {
        "p2" + "0" * 18: {"stratum": "top50", "rank_score": 10.0, "verdict": "candidate"},
        "p3" + "0" * 18: {"stratum": "top50", "rank_score": 5.0, "verdict": "candidate"},
    })
    entry = blind_sheet.import_sheet(con, csv_path, history_path=str(tmp_path / "hist.jsonl"), log=_quiet)
    assert entry["n_graded"] == 1 and entry["n_skipped_ungraded"] == 1
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 1
    con.close()


def test_import_computes_the_three_numbers_on_a_constructed_example(tmp_path):
    """4 top-stratum rows (bullseye, adjacent, stretch, wrong -> 2 good of 4 = precision 50%); 4 mid rows
    (1 bullseye -> miss rate 25%); 2 location rejects (1 bullseye -> false-reject 50%), 2 clearance
    rejects (0 good -> 0%), and a reject_any fallback row excluded from every per-bucket rate."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    ids = {}
    for i, (grade, req) in enumerate([("bullseye", "meets"), ("adjacent", "meets"), ("stretch", "meets"),
                                      ("wrong", "meets")]):
        pid = f"top{i}" + "0" * 17
        ids[pid] = ("top50", grade, req)
        _posting(con, pid, employer=f"T{i}", title=f"T{i}")
    for i, grade in enumerate(["bullseye", "adjacent", "stretch", "wrong"]):
        pid = f"mid{i}" + "0" * 17
        ids[pid] = ("mid200_600", grade, "")
        _posting(con, pid, employer=f"M{i}", title=f"M{i}")
    for i, grade in enumerate(["bullseye", "wrong"]):
        pid = f"loc{i}" + "0" * 17
        ids[pid] = ("reject_location", grade, "")
        _posting(con, pid, employer=f"L{i}", title=f"L{i}")
    for i, grade in enumerate(["wrong", "stretch"]):
        pid = f"clr{i}" + "0" * 17
        ids[pid] = ("reject_clearance", grade, "")
        _posting(con, pid, employer=f"C{i}", title=f"C{i}")
    any_pid = "anyx" + "0" * 16
    ids[any_pid] = ("reject_any", "bullseye", "")
    _posting(con, any_pid, employer="AnyEmp", title="AnyTitle")

    csv_path = tmp_path / "graded.csv"
    rows = [{"posting_id": pid, "employer": pid, "title": pid, "location": "Remote", "url": f"https://x/{pid}",
            "human_grade": grade, "required_fit": req, "required_unmet": "", "level_fit": "", "note": ""}
           for pid, (_, grade, req) in ids.items()]
    _write_sheet(csv_path, rows)
    sidecar = {pid: {"stratum": stratum, "rank_score": 0.0, "verdict": "candidate"}
              for pid, (stratum, _, _) in ids.items()}
    _write_sidecar(blind_sheet.sidecar_path_for(csv_path), sidecar)

    entry = blind_sheet.import_sheet(con, csv_path, history_path=str(tmp_path / "hist.jsonl"), log=_quiet)
    assert entry["n_top"] == 4 and entry["precision_top"] == pytest.approx(0.5)
    assert entry["n_mid"] == 4 and entry["miss_rate_mid"] == pytest.approx(0.25)
    fr = entry["false_reject_by_bucket"]
    assert fr["location"] == pytest.approx(0.5) and entry["n_by_bucket"]["location"] == 2
    assert fr["clearance"] == pytest.approx(0.0) and entry["n_by_bucket"]["clearance"] == 2
    assert fr["title"] is None and entry["n_by_bucket"]["title"] == 0   # no title-bucket rows in this example

    history = [json.loads(line) for line in (tmp_path / "hist.jsonl").read_text().splitlines()]
    assert len(history) == 1 and history[0]["n_graded"] == len(rows)
    con.close()


def test_import_rejects_an_invalid_grade_before_writing_anything(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "p9" + "0" * 18, employer="Acme", title="Role")
    csv_path = tmp_path / "graded.csv"
    _write_sheet(csv_path, [{"posting_id": "p9" + "0" * 18, "employer": "Acme", "title": "Role",
                            "location": "Remote", "url": "https://x/p9", "human_grade": "amazing",
                            "required_fit": "", "required_unmet": "", "level_fit": "", "note": ""}])
    _write_sidecar(blind_sheet.sidecar_path_for(csv_path),
                  {"p9" + "0" * 18: {"stratum": "top50", "rank_score": 1.0, "verdict": "candidate"}})
    with pytest.raises(ValueError):
        blind_sheet.import_sheet(con, csv_path, history_path=str(tmp_path / "hist.jsonl"), log=_quiet)
    assert con.execute("SELECT count(*) FROM report_feedback").fetchone()[0] == 0
    con.close()
