"""Unit tests for the finder layer (backend/finder/) — no network, temp DuckDB files.

Every test runs against the neutral example profile (backend/profile_local.example.py),
never the personal values in a local profile_local.py.
"""
import json
import os
import runpy
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend import screen as S  # noqa: E402
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import pipeline, report, rules, tracker_sync, version  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


def _quiet(*_a, **_k):
    pass


def _row(**kw):
    row = dict(posting_id="a" * 20, employer="Acme", platform="greenhouse", req_id="R1",
               title="Director, Operational Excellence", url="https://x/R1", location_primary="Remote - USA",
               locations=None, country=None, workplace_type="remote", employment_type="full_time", job_level=None,
               pay_min=None, pay_max=None, pay_interval=None, posted_at=None, description_text="",
               first_seen_at=None, description_fetched_at=None)
    row.update(kw)
    return row


# ---------------------------------------------------------------- row -> Listing
def test_listing_from_row():
    lst = rules.listing_from_row(_row(locations='["Springfield, IL", "Remote"]', pay_min=100000, pay_max=120000))
    assert (lst.source, lst.search_pass, lst.company) == ("ats", "greenhouse", "Acme")
    assert lst.locations == ["Springfield, IL", "Remote"] and lst.location == "Remote - USA"
    assert (lst.salary_min, lst.salary_max, lst.salary_predicted) == (100000, 120000, False)
    assert lst.extra["workplace_type"] == "remote" and lst.extra["posting_id"] == "a" * 20
    assert rules.listing_from_row(_row(locations="not json")).locations == []


# ---------------------------------------------------------------- JD rules
def test_travel_rule_flag_and_reason_branches():
    assert rules.travel_rule("", "Travel up to 30% required.")[:2] == ([], ["travel ceiling 30% (limit 25%)"])
    assert rules.travel_rule("", "Travel: 60% of the time.")[0] == ["travel 60% (limit 25%)"]
    assert rules.travel_rule("", "Expect 20-50% travel.")[0] == ["travel span 20-50% doubles the limit"]
    assert rules.travel_rule("", "Travel 10 to 30% annually.")[:2] == ([], ["travel span 10-30% (limit 25%)"])
    assert rules.travel_rule("", "100% remote. Minimal travel (under 10%).")[:2] == ([], [])


def test_travel_rule_skipped_without_limit(monkeypatch):
    monkeypatch.setattr(P, "TRAVEL_MAX_PCT", None)
    assert rules.travel_rule("", "Travel 90%") == ([], [], {})


def test_direct_reports_rule_branches():
    reasons, flags, notes = rules.direct_reports_rule("", "Lead a team of 7 analysts.")
    assert (reasons, flags, notes["reports"]) == ([], ["team of 7 (limit 5)"], 7)
    assert rules.direct_reports_rule("", "Manage 12 direct reports.")[0] == ["large team (12 direct reports)"]
    assert rules.direct_reports_rule("", "Own the hiring plan.")[1] == ["large-team markers (hiring plan)"]
    assert rules.direct_reports_rule("", "Manage 3 direct reports.")[:2] == ([], [])


def test_domain_tenure_rule_flags_required_block_only():
    jd = ("About the role\nWe transform operations.\n\nRequired Qualifications\n"
          "- 10+ years of experience in retail banking operations\n\nPreferred Qualifications\n- MBA\n")
    reasons, flags, _ = rules.domain_tenure_rule("", jd)
    assert reasons == [] and flags == ["domain-tenure gate (banking, 10 yrs)"]
    jd_pref = "Required Qualifications\n- Lean Six Sigma\n\nPreferred\n- 10+ years in banking\n"
    assert rules.domain_tenure_rule("", jd_pref) == ([], [], {})


def test_discipline_rule_branches():
    r, f, _ = rules.discipline_rule("Process Engineer", "Support the manufacturing plant and production line yield.")
    assert r == ["different discipline (plant/industrial: manufacturing)"] and f == []
    r, f, _ = rules.discipline_rule("Continuous Improvement Lead", "Drive the shop floor of our manufacturing plant.")
    assert r == ["plant-floor scope (shop floor)"]
    r, f, _ = rules.discipline_rule("Process Excellence Director",
                                    "Improve approval workflows and cycle time with stakeholders in manufacturing.")
    assert r == [] and f == ["plant vocabulary (manufacturing) -- read REQUIRED quals"]
    assert rules.discipline_rule("Process Excellence Manager", "Dental implant distribution approvals.")[:2] == ([], [])


def test_corridor_rule_needs_place_and_required_plant_term():
    jd = "Required Qualifications\n- Experience in GMP manufacturing\n"
    assert rules.corridor_rule("", jd, "Springfield, IL")[0] == ["corridor manufacturing (required: gmp)"]
    assert rules.corridor_rule("", jd, "Chicago, IL") == ([], [], {})


def test_place_matching_pins_the_state():
    places = ["springfield, il", "chatham"]
    for loc in ("Springfield, IL", "US-IL-Springfield", "Springfield, Illinois, United States", "IL - Springfield (Hybrid)"):
        assert S.place_matches(loc, places) == ["springfield, il"], loc
    for loc in ("Springfield, MO", "US-MO-Springfield", "Springfield, Ontario", "West Springfield, MA", "Springfieldia",
                "Indianapolis-8351 W Springfield", "Springfield, MO; Peoria, IL", "Springfield"):
        assert S.place_matches(loc, places) == [], loc
    assert S.place_matches("Chatham, NJ", places) == ["chatham"]          # no state on the entry: anywhere
    assert S.place_matches("Bluefield, Virginia", ["bluefield, va"]) == ["bluefield, va"]
    assert S.place_matches("Bluefield, West Virginia", ["bluefield, va"]) == []   # "west virginia" is not VA
    dc = ["washington, dc"]
    assert S.place_matches("Washington, DC", dc) == dc and S.place_matches("Washington, D.C.", dc) == dc
    assert S.place_matches("Seattle, Washington", dc) == [] and S.place_matches("Remote - Washington", dc) == []


def test_commutable_and_corridor_check_each_location_with_its_state():
    other = rules.listing_from_row(_row(location_primary="Springfield, MO", workplace_type="hybrid",
                                        locations='["Springfield, MO", "Chicago, IL"]'))
    assert not S.is_commutable(other)                                     # IL elsewhere never vouches for MO
    both = rules.listing_from_row(_row(location_primary="Springfield, MO", locations='["Springfield, IL"]'))
    assert S.is_commutable(both)
    jd = "Required Qualifications\n- Experience in GMP manufacturing\n"
    assert rules.corridor_rule("", jd, ["US-MO-Springfield"]) == ([], [], {})
    assert rules.corridor_rule("", jd, ["US-IL-Springfield"])[0] == ["corridor manufacturing (required: gmp)"]


def test_assessment_gate_rule():
    assert rules.assessment_gate_rule("", "All applicants complete the CCAT.")[0] == ["assessment-gated (ccat)"]
    assert rules.assessment_gate_rule("", "No tests.") == ([], [], {})


def test_hours_cap_suppresses_hourly_annualization():
    base = dict(title="Process Improvement Consultant", pay_min=30, pay_max=40, pay_interval="hour")
    plain = rules.screen_row(_row(**base, description_text="Improve workflows for stakeholders."))
    assert any(r.startswith("comp:") for r in plain.reasons)
    capped = rules.screen_row(_row(**base, description_text="This is a part-time role, 20-25 hours per week."))
    assert "hours capped -- do not annualize" in capped.flags
    assert not any(r.startswith("comp:") for r in capped.reasons)
    boiler = "Improve workflows. Full-time and part-time employees are eligible for benefits."
    assert rules.hours_cap_rule("", boiler) == ([], [], {})
    assert rules.hours_cap_rule("", boiler, "part_time")[2] == {"hours_capped": True}


def test_sales_ops_rule_reason_in_required_flag_elsewhere():
    req = "Responsibilities\n- Improve processes\n\nRequired Qualifications\n- Own quota attainment\n"
    assert rules.sales_ops_rule("", req)[0] == ["sales/revenue ops scope (quota)"]
    other = "Responsibilities\n- Partner with the go-to-market team\n\nRequired Qualifications\n- Lean Six Sigma\n"
    assert rules.sales_ops_rule("", other)[:2] == ([], ["sales/revenue ops vocabulary (go-to-market)"])
    assert rules.sales_ops_rule("", "Required Qualifications\n- Build data pipelines\n") == ([], [], {})


def test_required_block_slices_and_falls_back():
    jd = "Intro text here.\nRequired Qualifications\nA\nB\nPreferred Qualifications\nC\n"
    assert rules.required_block(jd) == "A\nB\n"
    assert rules.required_block("No headings at all.") == "No headings at all."
    long = "Requirements\n" + "x" * 5000
    assert len(rules.required_block(long)) == 2500


def test_contract_employment_never_produces_a_reason():
    rec = rules.screen_row(_row(employment_type="contract",
                                description_text="Lead operational excellence across workflow and stakeholder teams."))
    assert rec.verdict == "candidate" and rec.reasons == [] and rec.tier == 1
    assert rec.rule_score == 30 + 5 + 10  # tier 1 + senior title + remote


def test_tier3_data_lane_drops_off_function_and_rejects_coding_test():
    rec = rules.screen_row(_row(title="Senior Business Intelligence Manager", description_text="Own reporting."))
    assert rec.tier == 3 and "off-function title" not in rec.reasons
    rec = rules.screen_row(_row(title="Senior Business Intelligence Manager",
                                description_text="Candidates complete a HackerRank assessment."))
    assert rec.verdict == "reject" and rec.reasons == ["coding-test signal (hackerrank)"]


def test_rules_version_is_stable_and_changes(monkeypatch):
    v1 = version.rules_version()
    assert v1 == version.rules_version() and len(v1) == 12
    monkeypatch.setattr(P, "TRAVEL_MAX_PCT", 30)
    v2 = version.rules_version()
    assert v2 != v1
    monkeypatch.setattr(version, "RULES_CODE_VERSION", "test")
    assert version.rules_version() != v2


# ---------------------------------------------------------------- scoring
def test_combine_renormalizes_caps_bands_and_rejects():
    assert pipeline.combine(60, None, None, None) == (60, "partial")
    assert pipeline.combine(60, 0.8, None, None) == (69, "partial")        # (0.4*60 + 0.35*80) / 0.75
    assert pipeline.combine(60, 0.9, 0.5, None, {"embed_lo": 0.3, "embed_hi": 0.7}) == (68, "partial")
    assert pipeline.combine(60, None, 0.5, None) == (60, "partial")        # no calibration: embed ignored
    for score, band in ((100, "very_strong"), (85, "very_strong"), (84, "strong"), (70, "strong"),
                        (69, "partial"), (50, "partial"), (49, "weak"), (30, "weak"), (29, "none"), (0, "none")):
        assert pipeline.combine(score, None, None, None) == (score, band)
    assert pipeline.combine(95, None, None, None, tier=3) == (80, "strong")
    assert pipeline.combine(95, None, None, None, tier=1) == (95, "very_strong")
    assert pipeline.combine(95, None, None, None, rejected=True) == (0, "none")
    assert pipeline.combine(60, None, None, 100) == (80, "strong")


# ---------------------------------------------------------------- pipeline + store
def _seed(con, when=None, extra=()):
    when = when or pipeline._now()
    jobs = [N.base(req_id="A", title="Director of Operational Excellence", location_primary="Remote - USA",
                   workplace_type="remote", description_text="Lead operational excellence with stakeholders."),
            N.base(req_id="B", title="Pharmacy Technician", location_primary="Tulsa, OK"),
            *extra]
    store.record_board(con, "Acme", "greenhouse", jobs, when)
    return {j["req_id"]: store.posting_id("Acme", "greenhouse", j["req_id"]) for j in jobs}


def test_screen_writes_screens_and_postings_columns(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    ids = _seed(con)
    stats = pipeline.screen(con, log=_quiet)
    assert stats["screened"] == 2 and stats["verdict"] == {"candidate": 1, "reject": 1}
    rows = dict(con.execute("SELECT posting_id, verdict FROM screens").fetchall())
    assert rows == {ids["A"]: "candidate", ids["B"]: "reject"}
    pv = {r[0]: r[1:] for r in con.execute(
        "SELECT posting_id, screen_verdict, screen_score, screen_reasons, screened_at FROM postings").fetchall()}
    assert pv[ids["A"]][0] == "candidate" and pv[ids["A"]][1] > 0 and pv[ids["A"]][3] is not None
    assert pv[ids["B"]][1] == 0 and "off-function title" in pv[ids["B"]][2]
    assert json.loads(con.execute("SELECT reasons FROM screens WHERE posting_id = ?", [ids["B"]]).fetchone()[0])


def test_rescreen_predicate_selects_only_new_changed_or_version_changed(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    ids = _seed(con)
    pipeline.screen(con, log=_quiet)
    rv = version.rules_version()
    assert pipeline.candidate_ids(con, rv, "none") == []

    later = pipeline._now()  # after the first screen, before the next one
    store.apply_detail(con, ids["A"], {"description_text": "A changed JD about process excellence."}, later)
    assert pipeline.candidate_ids(con, rv, "none") == [ids["A"]]
    pipeline.screen(con, log=_quiet)

    ids = _seed(con, when=later, extra=[N.base(req_id="C", title="Lean Six Sigma Lead")])
    assert pipeline.candidate_ids(con, rv, "none") == [ids["C"]]
    pipeline.screen(con, log=_quiet)

    monkeypatch.setattr(version, "RULES_CODE_VERSION", "bumped")
    assert set(pipeline.candidate_ids(con, version.rules_version(), "none")) == set(ids.values())
    assert pipeline.candidate_ids(con, rv, "other-model") != []


def test_shortlist_excludes_decided_and_tracked(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    extra = [N.base(req_id=r, title="Director of Operational Excellence", workplace_type="remote") for r in ("D", "E")]
    ids = _seed(con, extra=extra)
    pipeline.screen(con, log=_quiet)
    listed = {r[0] for r in con.execute("SELECT posting_id FROM vw_shortlist").fetchall()}
    assert listed == {ids["A"], ids["D"], ids["E"]}
    now = pipeline._now()
    con.execute("INSERT INTO decisions VALUES (?, 'pass', 'no', 'cli', NULL, ?)", [ids["D"], now])
    con.execute("INSERT INTO tracker VALUES ('Active', '2026-09-01', 'Acme', 'x', 'Applied', NULL, ?, 'fuzzy', ?)",
                [ids["E"], now])
    assert [r[0] for r in con.execute("SELECT posting_id FROM vw_shortlist").fetchall()] == [ids["A"]]
    assert con.execute("SELECT count(*) FROM vw_scored_new(1)").fetchone()[0] == 1


# ---------------------------------------------------------------- tracker
TRACKER_MD = """# Application Tracker

## Active

| Date Applied | Company | Role | Status | Status Date | Recruiter | Next Action | Follow-Up Due | Posting ID |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-09-01 | Acme Corp | Director of Operational Excellence | Applied | 2026-09-01 | — | wait | | {pid_a} |
| 2026-09-02 | Globex (Initech) | Change Manager | Applied | 2026-09-02 | | |

## Consultant Networks

| Date Accepted | Platform | Status | Profile |
| --- | --- | --- | --- |
| 2026-03-04 | Catalant | Active | [Profile](https://x) |

## Closed

| Date Applied | Company | Role | Status | Status Date | Recruiter | Close Reason | | | |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-01 | Initech | Senior Process Improvement Lead | Rejected | 2026-08-10 | | no | | | | | |
| 2026-07-01 | Hooli | Ops Manager |
| 2026-06-01 | Umbrella | Program Lead | Closed | | | dup of 0123456789abcdef0123 |
"""


def test_parse_tracker_shapes(tmp_path):
    path = tmp_path / "Application_Tracker.md"
    path.write_text(TRACKER_MD.format(pid_a="a" * 20))
    rows = tracker_sync.parse_tracker(path)
    assert [(r.section, r.company) for r in rows] == [
        ("Active", "Acme Corp"), ("Active", "Globex (Initech)"), ("Closed", "Initech"), ("Closed", "Hooli"),
        ("Closed", "Umbrella")]
    assert rows[0].posting_id == "a" * 20 and rows[1].posting_id is None      # Posting ID column, short row
    assert rows[3].status is None                                               # ragged Closed row
    assert rows[4].posting_id == "0123456789abcdef0123"                         # id in a trailing cell


def test_tracker_sync_exact_fuzzy_none_and_decisions(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    ids = _seed(con)
    store.record_board(con, "Initech", "workday", [N.base(req_id="I1", title="Senior Process Improvement Lead - Remote")],
                       pipeline._now())
    (tmp_path / "Application_Tracker.md").write_text(TRACKER_MD.format(pid_a=ids["A"]))
    counts = tracker_sync.sync(con, str(tmp_path), log=_quiet)
    assert (counts["rows"], counts["exact"], counts["fuzzy"], counts["none"]) == (5, 1, 1, 3)
    assert counts["decisions_added"] == 2
    kinds = dict(con.execute("SELECT company, match_kind FROM tracker").fetchall())
    assert kinds["Acme Corp"] == "exact" and kinds["Initech"] == "fuzzy" and kinds["Hooli"] == "none"
    again = tracker_sync.sync(con, str(tmp_path), log=_quiet)
    assert again["decisions_added"] == 0 and con.execute("SELECT count(*) FROM tracker").fetchone()[0] == 5


# ---------------------------------------------------------------- report
JD_WITH_HEADINGS = """## About the role
Lead operational excellence with stakeholders.
Responsibilities
---
# Improve workflows
Outcomes
===
"""


def _report_fixture(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    jobs = [N.base(req_id=f"S{i}", title=f"Senior Director, Lean Six Sigma Operational Excellence {i}",
                   url=f"https://x/S{i}", workplace_type="remote", pay_min=120000, pay_max=150000,
                   pay_interval="year", description_text=JD_WITH_HEADINGS) for i in range(2)]
    jobs.append(N.base(req_id="R1", title="Operational Excellence Engineer", url="https://x/R1",
                       workplace_type="remote", description_text="Own the manufacturing plant production line yield."))
    store.record_board(con, "Acme", "greenhouse", jobs, pipeline._now())
    stats = pipeline.screen(con, log=_quiet)
    return con, {"since": None, "screen": stats, "stages": {}}


def test_report_writer_output_reparses(tmp_path):
    con, meta = _report_fixture(tmp_path)
    vault = tmp_path / "vault"
    path = report.write_jobs_found(con, str(vault), meta, block_min_band="partial")
    assert path.parent == vault / "Professional" / "Areas" / "Job_Search" / "Search_Results"
    text = path.read_text()
    lines = text.splitlines()
    assert text.count("\n# Company:") == 2 and text.count("\n## Title:") == 2 and text.count("**Fit: ~") == 2
    body_start = lines.index("---", 1) + 1                 # after the frontmatter
    for i, line in enumerate(lines[body_start:], start=body_start):
        if line == "---":
            assert lines[i - 1] == "" and lines[i + 1] == "", f"fence without blank lines at line {i + 1}"
    allowed = ("# Company:", "## Title:", "# Jobs Found", "## Coverage Log", "## Summary", "## Escalated Roles",
               "## Passed / Filtered Out")
    assert [ln for ln in lines[body_start:] if ln.startswith("#") and not ln.startswith(allowed)] == []
    assert "About the role" in text and "Improve workflows" in text
    assert "| Acme | [Operational Excellence Engineer](https://x/R1) | different discipline" in text
    assert con.execute("SELECT count(*) FROM surfaced").fetchone()[0] == 2
    second = report.write_jobs_found(con, str(vault), meta, block_min_band="partial")
    assert second.name.endswith("_2.md") and second.read_text().count("\n# Company:") == 0
    assert "(shown " in second.read_text()


def test_parse_decisions_round_trip_and_read_back_idempotent(tmp_path):
    con, meta = _report_fixture(tmp_path)
    vault = tmp_path / "vault"
    path = report.write_jobs_found(con, str(vault), meta, block_min_band="partial")
    assert report.parse_decisions(path) == []
    pids = [r[0] for r in con.execute("SELECT posting_id FROM vw_shortlist ORDER BY posting_id").fetchall()]
    text = path.read_text()
    out = []
    for line in text.splitlines():
        if line.startswith(f"| {pids[0]} |"):
            line = line[: line.rindex("|  |  |")] + "| build |  |"
        elif line.startswith(f"| {pids[1]} |"):
            line = line[: line.rindex("|  |  |")] + "| Pass | too junior |"
        out.append(line)
    path.write_text("\n".join(out) + "\n")
    got = report.parse_decisions(path)
    assert sorted(got, key=lambda d: d.posting_id) == [report.Decision(pids[0], "build", None),
                                                       report.Decision(pids[1], "pass", "too junior")]
    assert report.read_back(con, str(vault)) == 2
    assert report.read_back(con, str(vault)) == 0                   # unchanged file is skipped
    os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 10))
    assert report.read_back(con, str(vault)) == 0                   # changed mtime, same decisions: no duplicates
    assert con.execute("SELECT count(*) FROM vw_shortlist").fetchone()[0] == 0


def test_snapshots_write_files(tmp_path):
    con, _ = _report_fixture(tmp_path)
    paths = report.snapshots(con, out_dir=str(tmp_path / "snap"))
    assert len(paths) == 6 and all(p.exists() for p in paths)


# ---------------------------------------------------------------- sweep integration
def test_finder_import_pulls_no_optional_dependencies():
    code = ("import sys; sys.path.insert(0, '.'); import backend.finder.pipeline, backend.ats.sweep; "
            "bad = [m for m in sys.modules if m.split('.')[0] in "
            "('sklearn', 'numpy', 'scipy', 'torch', 'sentence_transformers', 'fastembed', 'joblib')]; "
            "print(bad); sys.exit(1 if bad else 0)")
    res = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_sweep_run_screen_flag(tmp_path, monkeypatch):
    from backend.ats import sweep as ats_sweep
    monkeypatch.setattr(ats_sweep, "load_registry", lambda: [])
    monkeypatch.delenv("JOBSEARCH_VAULT_DIR", raising=False)
    monkeypatch.setattr(report, "SNAPSHOT_DIR", str(tmp_path / "snap"))
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    _seed(con)
    con.close()
    ats_sweep.run(db_path=db, skip_sweep=True, detail_budget=0, screen=False, log=_quiet)
    con = store.connect(db)
    assert con.execute("SELECT count(*) FROM screens").fetchone()[0] == 0
    con.close()
    ats_sweep.run(db_path=db, skip_sweep=True, detail_budget=0, log=_quiet)
    con = store.connect(db)
    assert con.execute("SELECT count(*) FROM screens").fetchone()[0] == 2
    assert (tmp_path / "snap" / "shortlist.csv").exists()
