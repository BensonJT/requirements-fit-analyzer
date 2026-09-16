"""Unit tests for the finder layer (backend/finder/) — no network, temp DuckDB files.

Every test runs against the neutral example profile (backend/profile_local.example.py),
never the personal values in a local profile_local.py.
"""
import json
import os
import runpy
import subprocess
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend import screen as S  # noqa: E402
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import features, labels, pipeline, report, rules, tracker_sync, version  # noqa: E402

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
    optional = ["chatham, il?"]                                            # state optional: that state or none
    for loc in ("Chatham", "Chatham (Hybrid)", "Chatham, IL", "US-IL-Chatham"):
        assert S.place_matches(loc, optional) == optional, loc
    for loc in ("Chatham, NJ", "NJ - Chatham", "Chatham, New Jersey; Peoria, IL"):
        assert S.place_matches(loc, optional) == [], loc
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
    assert rec.rule_score == 92   # level senior 100 (title), remote 100, pay not posted 60, tier 1 100: (20+15+6+5)/0.5


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
def test_combine_content_first_caps_penalties_bands_and_rejects():
    # The blend arithmetic is derived from the weight rather than hardcoded, so retuning `content` (0.50 -> 0.90
    # on 2026-09-15) does not break this test. What is asserted explicitly is the behaviour that must not move:
    # the no-content cap, the tier cap, the flag penalty and its ceiling, the band edges, reject, and the LLM blend.
    cw = P.SCORE_COMPONENT_WEIGHTS["content"]
    blend = lambda content, rule: int(cw * content + (1 - cw) * rule + 0.5)  # noqa: E731

    assert pipeline.combine(80, 0.9, None, None) == (blend(90, 80), "very_strong")
    assert pipeline.combine(80, None, None, None) == (60, "partial")             # no content: capped
    fw = (0.15 * 90 + (1 - cw) * 80) / (0.15 + (1 - cw))                         # low-data fit weight overrides cw
    assert pipeline.combine(80, 0.9, None, None, {"fit_weight": 0.15}) == (int(fw + 0.5), pipeline.band_for(int(fw + 0.5)))
    # content is the mean of fit and the calibrated embedding: (90 + 50) / 2
    assert pipeline.combine(60, 0.9, 0.5, None, {"embed_lo": 0.3, "embed_hi": 0.7}) == (blend(70, 60), "partial")
    assert pipeline.combine(60, None, 0.5, None) == (60, "partial")              # no calibration: embed ignored
    for score, band in ((100, "very_strong"), (85, "very_strong"), (84, "strong"), (70, "strong"),
                        (69, "partial"), (50, "partial"), (49, "weak"), (30, "weak"), (29, "none"), (0, "none")):
        assert pipeline.combine(score, score / 100, None, None) == (score, band)
    assert pipeline.combine(95, 0.95, None, None, tier=3) == (80, "strong")
    assert pipeline.combine(95, 0.95, None, None, tier=1) == (95, "very_strong")
    assert pipeline.combine(80, 0.9, None, None, flags=2) == (blend(90, 80) - 10, "strong")
    assert pipeline.combine(80, 0.9, None, None, flags=9) == (blend(90, 80) - 25, "partial")   # penalty capped at 25
    assert pipeline.combine(95, 0.95, None, None, rejected=True) == (0, "none")
    assert pipeline.combine(60, 0.6, None, 100) == (80, "strong")                # LLM blend: 0.5 * 60 + 0.5 * 100


def test_content_gate_replaces_the_title_gate():
    rec = rules.screen_row(_row(title="Director of Business Operations, Client Optimization"))
    assert "off-function title" in rec.reasons
    pipeline.apply_content_gate(rec, 0.94)
    assert "off-function title" not in rec.reasons and rec.verdict != "reject"
    rec = rules.screen_row(_row(title="Director, Operational Excellence"))
    pipeline.apply_content_gate(rec, 0.20)
    assert rec.reasons[-1] == "content does not fit (fit 0.20)" and rec.verdict == "reject"
    rec = rules.screen_row(_row(title="Director, Operational Excellence"))
    pipeline.apply_content_gate(rec, 0.42)
    assert "content fit borderline (fit 0.42)" in rec.flags and rec.verdict == "review"
    rec = rules.screen_row(_row(title="Pharmacy Technician"))
    pipeline.apply_content_gate(rec, None)                                        # no content: title gate stands
    assert "off-function title" in rec.reasons and rec.verdict == "reject"


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

    # Re-recording the board adds C and re-sends A's ORIGINAL JD, which overwrites the edit above. That is a
    # real text change, so A is due a re-screen as well -- exactly what the Lever `lists` fix depends on, and
    # why the upsert stamps description_fetched_at whenever the text actually differs. B is unchanged and is
    # correctly left alone.
    ids = _seed(con, when=later, extra=[N.base(req_id="C", title="Lean Six Sigma Lead")])
    assert set(pipeline.candidate_ids(con, rv, "none")) == {ids["C"], ids["A"]}
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


# ---------------------------------------------------------------- Phase 2: labels + fit model
FIT_JD = ("Lead process excellence and continuous improvement across operations. Map value streams, remove handoffs, "
          "own the operating model and lean six sigma deployment with cross-functional stakeholders. ") * 12
OFF_JD = ("Write production Java microservices, own Kubernetes deployments, on-call rotation, code reviews, "
          "distributed systems design and CI pipelines for the platform engineering team. ") * 12


def _vault(tmp_path):
    js = tmp_path / "Professional" / "Areas" / "Job_Search"
    (js / "Applications").mkdir(parents=True)
    (js / "Search_Results").mkdir()
    return tmp_path, js


def _app(js, folder, status, company, role, jd):
    d = js / "Applications" / folder
    d.mkdir()
    (d / "index.md").write_text(f"---\nstatus: {status}\nrole: {role}\ncompany: {company}\n---\n\n# x\n\n"
                                f"## Job Description\n\nApply: https://jobs.example/{folder}\nRemote · Full-time\n\n"
                                f"{jd}\n\n## Notes\nnot part of the JD\n", encoding="utf-8")


def test_strip_boilerplate_and_jd_section():
    text = "Lead CI.\nWe are an Equal Opportunity Employer and value everyone.\nBenefits: medical\nOwn the model."
    assert labels.strip_boilerplate(text) == "Lead CI.\nWe are an\nOwn the model."
    raw = "---\nstatus: applied\n---\n## Job Description\nApply: https://x\nRemote · FT\n\nBody line\n## Notes\nno"
    assert labels.jd_section(raw) == "Body line"
    assert labels.jd_section("## Other\nno") == ""


def test_classify_passed_reason():
    assert labels.classify_passed_reason("Posting closed November 18") == "stale"
    assert labels.classify_passed_reason("Altamonte Springs FL, on-site, not commutable") == "logistics"
    assert labels.classify_passed_reason("Below comp floor") == "logistics"
    assert labels.classify_passed_reason("Remote, but the scope is sales operations") == "fit"
    assert labels.classify_passed_reason("Plant-floor CI role") == "fit"


def test_vault_label_loaders(tmp_path):
    vault, js = _vault(tmp_path)
    _app(js, "Acme_DirOpEx", "applied", "Acme", "Director, Operational Excellence", FIT_JD)
    _app(js, "Beta_Pass", "not-pursuing", "Beta", "Software Engineer", OFF_JD)
    _app(js, "Gamma_Short", "evaluating", "Gamma", "Lead", "too short")
    (js / "Search_Results" / "Jobs_Found_20260101_0900.md").write_text(
        "# Jobs Found — by hand\n\n# Company: Acme\n## Title: Director, Operational Excellence\nApply: https://x/1\n"
        f"{FIT_JD}\n\n# Company: Delta\n## Title: Process Excellence Lead\nApply: https://x/2\n## Description:\n{FIT_JD}\n"
        "---\n\n**Fit: ~80%.**\n\n---\n\n## Passed / Filtered Out\n\n| Company | Role | Reason |\n|---|---|---|\n"
        "| **Epsilon** | [Plant CI Manager](https://x/3) | Plant-floor manufacturing scope |\n"
        "| Zeta | Ops Lead, R-1 | Posting closed |\n| Eta | Analyst | On-site in Tampa |\n"
        "| Company | Role | Reason |\n", encoding="utf-8")
    (js / "Search_Results" / "Jobs_Found_20260102_0900.md").write_text(
        "# Jobs Found — ATS pipeline (`jobsearch/finder.py`)\n\n# Company: Omega\n## Title: Pipeline Pick\n"
        f"{FIT_JD}\n", encoding="utf-8")

    apps = labels.load_applications(str(vault))
    # a not-pursuing folder is a near-miss positive, never a negative
    assert sorted((d.source_ref, d.label, d.weight) for d in apps) == [("Acme_DirOpEx", 1, 1.0), ("Beta_Pass", 1, 0.5)]
    acme = next(d for d in apps if d.company == "Acme")
    assert acme.text.startswith("Lead process excellence") and "not part of the JD" not in acme.text
    assert acme.url == "https://jobs.example/Acme_DirOpEx"

    esc = labels.load_jobs_found_escalated(str(vault), apps)
    assert [(d.company, d.title, d.label, d.weight) for d in esc] == [("Delta", "Process Excellence Lead", 1, 0.7)]
    assert "## Description:" in esc[0].text and "Fit:" not in esc[0].text

    passed, skipped = labels.load_jobs_found_passed(str(vault))
    assert [(d.company, d.title, d.url, d.text) for d in passed] == [("Epsilon", "Plant CI Manager", "https://x/3", None)]
    assert skipped == {"stale": 1, "logistics": 1}


def _seed_label_corpus(con, n_each=40):
    now = datetime(2026, 9, 15, 12, 0)
    fit = [N.base(req_id=f"F{i}", title=f"Process Excellence Lead {i}", url=f"https://x/F{i}",
                  location="Remote - USA", workplace_type="remote") for i in range(n_each)]
    off = [N.base(req_id=f"O{i}", title=f"Software Engineer {i}", url=f"https://x/O{i}",
                  location="Remote - USA", workplace_type="remote") for i in range(n_each)]
    store.record_board(con, "Acme", "greenhouse", fit + off, now)
    con.execute("UPDATE postings SET description_text = CASE WHEN req_id LIKE 'F%' THEN ? ELSE ? END, "
                "description_fetched_at = ?", [FIT_JD, OFF_JD, now])


def test_pseudo_negatives_and_sync_labels(tmp_path):
    vault, js = _vault(tmp_path)
    _app(js, "Acme_Lead", "applied", "Acme", "Process Excellence Lead 3", FIT_JD)
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_label_corpus(con, n_each=10)
    pseudo = labels.pseudo_negatives(con, n=5, seed=7)
    assert len(pseudo) == 5 and all(d.label == 0 and d.weight == 0.5 for d in pseudo)
    assert all(d.title.startswith("Software Engineer") for d in pseudo)
    assert [d.posting_id for d in pseudo] == [d.posting_id for d in labels.pseudo_negatives(con, n=5, seed=7)]

    counts = labels.sync_labels(con, str(vault), n_pseudo=5, log=_quiet)
    assert counts["by_source"][("application", 1)] == {"docs": 1, "with_text": 1, "matched": 1}
    assert counts["pseudo_text"] == 5
    labels.sync_labels(con, str(vault), n_pseudo=5, log=_quiet)   # rebuild, not append
    assert con.execute("SELECT count(*) FROM label_docs").fetchone()[0] == 6
    con.close()


def test_training_set_dedupes_by_posting_and_company_title(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_label_corpus(con, n_each=2)
    pid = con.execute("SELECT posting_id FROM postings WHERE req_id = 'F0'").fetchone()[0]
    now = datetime(2026, 9, 15)
    con.execute("INSERT INTO label_docs VALUES ('app1', 'application', 'f', NULL, 'Acme Inc', "
                "'Process Excellence Lead 0', ?, 1, 1.0, ?)", [FIT_JD, now])
    con.execute("INSERT INTO label_docs VALUES ('pn1', 'pseudo_neg', NULL, ?, 'Acme', 'x', ?, 0, 0.5, ?)",
                [pid, OFF_JD, now])
    con.execute("INSERT INTO decisions VALUES (?, 'build', NULL, 'tracker', 'Active', ?)", [pid, now])
    rows = features.training_set(con)
    assert [r["label_id"] for r in rows] == ["app1"]   # decision deduped to app1; pn1 may not relabel that posting
    con.close()


def _trained(tmp_path, con):
    now = datetime(2026, 9, 15)
    rows = []
    for i in range(30):
        rows.append([f"p{i}", "application", None, None, "Acme", f"Process Excellence Lead {i}", FIT_JD, 1, 1.0, now])
        rows.append([f"n{i}", "pseudo_neg", None, None, "Beta", f"Software Engineer {i}", OFF_JD, 0, 1.0, now])
    rows.append(["ctx", "jobs_found_passed", "f.md: sales ops", None, "Gamma", "Process Lead", FIT_JD, 0, 1.0, now])
    con.executemany("INSERT INTO label_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"), log=_quiet)


def test_train_predict_top_terms_and_load_latest(tmp_path):
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    assert features.load_latest(con, log=_quiet) is None
    result = _trained(tmp_path, con)
    assert result["cv_auc"] == 1.0 and result["n_pos"] == 30 and result["n_neg"] == 30   # the passed row is not trained
    assert result["source_gap"]["vault text"]["n"] == 30 and result["coefficients"]["positive"]
    assert not any(t in ("lead", "senior", "associate") for t, _ in result["coefficients"]["positive"]
                   + result["coefficients"]["negative"])
    assert result["fit_weight"] == features.LOW_DATA_FIT_WEIGHT and len(result["warnings"]) == 2
    kind, n_pos, notes = con.execute("SELECT kind, n_pos, notes FROM models").fetchone()
    assert (kind, n_pos, json.loads(notes)["fit_weight"]) == ("tfidf_lr", 30, features.LOW_DATA_FIT_WEIGHT)
    assert len(features.hard_negatives(result, k=5)) == 5

    model = features.load_latest(con, log=_quiet)
    assert model["version"] == result["model_version"]
    fit_p, off_p = features.predict(model, [features.doc_text("Process Excellence Lead", FIT_JD),
                                            features.doc_text("Software Engineer", OFF_JD)])
    assert fit_p > 0.5 > off_p
    terms = features.top_terms(model, features.doc_text("Process Excellence Lead", FIT_JD))
    assert terms and all(c > 0 for _, c in terms) and len(terms) <= 6
    assert any("excellence" in t or "process" in t for t, _ in terms)
    con.close()


def test_screen_with_model_writes_fit_prob_terms_and_version(tmp_path):
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_label_corpus(con, n_each=3)
    result = _trained(tmp_path, con)
    model = features.load_latest(con, log=_quiet)
    pipeline.screen(con, model=model, log=_quiet)
    rows = con.execute("SELECT model_version, fit_prob, top_terms, rule_score, final_score, verdict, flags FROM screens "
                       "WHERE posting_id IN (SELECT posting_id FROM postings WHERE req_id = 'F0')").fetchall()
    mv, fit_prob, top, rule_score, final, verdict, flags = rows[0]
    assert mv == result["model_version"] and fit_prob > 0.5 and json.loads(top)
    if verdict != "reject":
        expected = pipeline.combine(rule_score, fit_prob, None, None, {"fit_weight": features.LOW_DATA_FIT_WEIGHT},
                                    flags=pipeline.penalized_flags(json.loads(flags)))[0]
        assert final == expected
    # a new model version makes every row due again; rules-only screens stay 'none'
    assert len(pipeline.candidate_ids(con, version.rules_version(), mv)) == 0
    assert len(pipeline.candidate_ids(con, version.rules_version(), "none")) == 6
    con.close()


# ---------------------------------------------------------------- 2026-09-15 amendments: level, workplace, country
def test_required_years_and_level_rule():
    jd = ("Required:\n- 10+ years of experience in operations\n- 8 years' experience leading programs\n"
          "- 2+ years of experience with dashboards\n")
    assert rules.required_years(jd) == [10, 8, 2]
    reasons, flags, notes = rules.level_rule("Process Excellence Lead", jd, None)
    assert (reasons, flags, notes) == ([], [], {"max_years": 10, "level": "senior", "senior": True})   # the 2+ line is not the level
    mid = "Minimum 5-7 years of experience in process improvement."
    assert rules.level_rule("Process Improvement Lead", mid, None)[1] == ["mid level (at most 5 yrs required)"]
    assert rules.level_rule("Director, Process Improvement", mid, None)[:2] == ([], [])
    assert rules.level_rule("Process Improvement Lead", mid, 120_000)[:2] == ([], [])        # band top at the ask
    junior = "3+ years of experience in data analysis."
    assert rules.level_rule("Process Analyst", junior, None)[0] == ["junior level (at most 3 yrs required)"]
    assert rules.level_rule("Process Analyst", junior, 95_000)[:2] == ([], ["few years asked (at most 3) -- pay band says senior"])
    assert rules.level_rule("Principal Process Analyst", junior, None)[1] == ["few years asked (at most 3) -- title says senior"]
    assert rules.level_rule("Summer Associate Internship", "", None)[0] == ["early-career title (summer associate)"]
    assert rules.level_rule("Associate Director, Operational Excellence", "", None) == \
        ([], [], {"max_years": None, "level": None, "senior": True})
    assert rules.level_rule("Associate, Operations", "", None)[:2] == ([], [])               # "associate" says nothing
    assert rules.required_years("Founded 120 years ago; 100 years of experience serving clients. Within 2 years of hire.") == []


def test_associate_title_is_not_a_level_reason():
    rec = rules.screen_row(_row(title="Associate Director, Operational Excellence"))
    assert not any("level" in r for r in rec.reasons)
    rec = rules.screen_row(_row(title="Associate, Operational Excellence"))
    assert not any("level" in r for r in rec.reasons)


def test_workplace_from_text_beats_the_multi_state_remote_guess():
    assert rules.workplace_from_text("This hybrid role includes an in-office presence requirement of 2–4 days per week.") == "hybrid"
    assert rules.workplace_from_text("Work 3 days a week in the office.") == "hybrid"
    assert rules.workplace_from_text("This role is fully on-site in Tampa.") == "onsite"
    assert rules.workplace_from_text("Remote; visit the office 2 days per month.") is None
    row = _row(workplace_type=None, location_primary="Quincy, Massachusetts",
               locations='["Quincy, Massachusetts", "Austin, Texas", "Atlanta, Georgia"]',
               description_text="This hybrid role includes an in-office presence requirement of 2–4 days per week.")
    rec = rules.screen_row(row)
    assert rec.notes["workplace_inferred"] == "hybrid"
    assert "not remote and outside the commute area (per listing)" in rec.reasons


def test_non_us_rule():
    assert rules.non_us_rule("IN", ["Bengaluru"])[0] == ["outside the US (country IN)"]
    assert rules.non_us_rule(None, ["India - Hyderabad"])[0] == ["outside the US (india)"]
    assert rules.non_us_rule(None, ["Toronto, ON", "Mississauga"])[0] == ["outside the US (toronto)"]
    assert rules.non_us_rule("IN", ["Hyderabad, India", "Austin, TX"]) == ([], [], {})       # one US location keeps it
    assert rules.non_us_rule(None, ["Albuquerque, New Mexico"]) == ([], [], {})
    assert rules.non_us_rule(None, ["Remote"]) == ([], [], {})
    assert rules.non_us_rule(None, ["Bengaluru", "Greenville"]) == ([], [], {})                # unknown place: keep
    assert rules.non_us_rule("US", ["Remote - United States"]) == ([], [], {})
    rec = rules.screen_row(_row(location_primary="China - Shanghai", workplace_type=None, country="CN"))
    assert rec.verdict == "reject" and "outside the US (country CN)" in rec.reasons


def test_profile_components_and_score(monkeypatch):
    monkeypatch.setattr(P, "COMMUTABLE_PLACES", ["springfield, il"])
    rec = rules.screen_row(_row(title="Director, Operational Excellence", pay_min=100_000, pay_max=120_000,
                                pay_interval="year"))
    assert rec.notes["components"] == {"level": 100, "location": 100, "pay": 100, "title": 100} and rec.rule_score == 100
    w = P.SCORE_COMPONENT_WEIGHTS
    for workplace, loc_pts in (("hybrid", 80), ("onsite", 70), (None, 70)):
        rec = rules.screen_row(_row(title="Operational Excellence Consultant", location_primary="Springfield, IL",
                                    workplace_type=workplace, pay_min=80_000, pay_max=95_000, pay_interval="year"))
        comp = rec.notes["components"]
        assert (comp["level"], comp["location"], comp["pay"], comp["title"]) == (60, loc_pts, 75, P.TITLE_POINTS[rec.tier])
        expected = (w["level"] * 60 + w["location"] * loc_pts + w["pay"] * 75 + w["title"] * comp["title"]) / 0.5
        assert rec.rule_score == int(expected + 0.5)
    assert rules.pay_points(None) == 60 and rules.level_points({"level": "mid"}) == 50
    assert rules.title_points(None, ["off-lane title (sales)"]) == 0 and rules.title_points(None, []) == 30



def test_normalize_and_boilerplate_lines_for_the_model():
    assert labels.normalize_text("Lead&#xa0;teams &amp; programs\\xa0now\u00a0here") == "Lead teams & programs now here"
    jd = ("Own the operating model and value streams.\n"
          "We are an equal opportunity employer; all qualified applicants receive consideration without regard to race, "
          "color, religion or disability.\n"
          "Benefits include dental coverage and paid time off.\n"
          "Partner with finance on process improvement.")
    assert labels.strip_boilerplate(jd) == "Own the operating model and value streams.\nPartner with finance on process improvement."
    text = features.doc_text("Lead, Process Excellence", "Acme Corp values lean. Acme builds.", "Acme Corp")
    assert "acme" not in text.lower() and "lean" in text


def test_screen_row_decodes_entity_line_breaks():
    row = _row(description_text="About&#xa;&#xa;Required Qualifications&#xa;- 3+ years of experience in reporting&#xa;"
                                "Preferred Qualifications&#xa;- 10+ years of experience in banking")
    rec = rules.screen_row(row)
    assert rec.notes["max_years"] == 10 and "&#xa;" not in report.jd_body(row["description_text"])


def test_required_years_phrasings():
    cases = {
        "Typically 10 or more years of progressive experience in process excellence.": [10],
        "A minimum of ten (10) years of experience.": [10],
        "At least eight years' experience leading teams.": [8],
        "10-12 years of relevant experience": [10],
        "five to seven years of experience": [5],
        "(8) years of experience": [8],
        "Experience: 12+ years": [12],
        "15 years or more of experience in operations": [15],
        "Must be 18 years old. Benefits vest after 3 years.": [],
    }
    for text, expected in cases.items():
        assert rules.required_years(text) == expected, text
    assert rules.level_rule("Senior Manager, AI Transformation & Process Excellence",
                            "MINIMUM WORK EXPERIENCE:\nTypically 10 or more years of progressive experience in process "
                            "excellence, consulting, business transformation.", 178_067)[2]["level"] == "senior"


def test_component_priced_flags_cost_no_points():
    flags = ["$110K ask sits above the $100,000 top", "local/hybrid -- judge on route, not radius",
             "mid level (at most 6 yrs required)", "content fit borderline (fit 0.45)", "travel ceiling 30% (limit 25%)"]
    assert pipeline.penalized_flags(flags) == 1
    cw = P.SCORE_COMPONENT_WEIGHTS["content"]
    expected = int(cw * 90 + (1 - cw) * 80 + 0.5) - 5      # one penalized flag
    assert pipeline.combine(80, 0.9, None, None, flags=pipeline.penalized_flags(flags)) == (expected, "strong")


# ---------------------------------------------------------------- Phase 3a: evidence, requirements, coverage
import hashlib  # noqa: E402
import re  # noqa: E402
from argparse import Namespace  # noqa: E402

from backend.finder import coverage, embed, evidence, requirements, setup_check  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "evidence")
REQ_JD = """About the role
Lead process excellence for operations.

Responsibilities
- Map value streams across teams and remove handoffs to cut cycle time.
- Own the operating model for intake and approvals with stakeholders.

Required Qualifications
- 8+ years of experience in process improvement leading cross-functional teams
- Lean six sigma black belt certification and deployment of kaizen events
- Advanced dashboards built in Power BI for executive reporting
- Hands-on experience deploying workloads on AWS cloud infrastructure
- 10+ years of experience.
- 7+ years in retail banking operations
- Ability to travel up to 25% of the time

Preferred Qualifications
- Experience with SQL and Python automation of reporting pipelines

Benefits
- Medical, dental and vision coverage for you and your family
"""
OFF_REQ_JD = """Responsibilities
- Write production Java microservices and own Kubernetes deployments.
- Design distributed systems with message queues and caching layers.
- Review pull requests and mentor engineers on the platform team.
Requirements
- Strong Java and Go programming for backend services
- Experience operating Kubernetes clusters in production
- Deep knowledge of CI pipelines and infrastructure as code tooling
"""


class FakeEncoder:
    """Deterministic bag-of-words vectors: shared words mean a higher cosine. No model download."""
    name, dim, backend = "fake", 384, "fake"

    def __init__(self):
        self.calls = 0

    def encode(self, texts, query=False, batch_size=64):
        import numpy as np
        self.calls += 1
        out = np.zeros((len(texts), 384), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"[a-z]{3,}", text.lower()):
                out[i, int(hashlib.md5(word.encode()).hexdigest(), 16) % 384] += 1.0
        return embed.normalize(out)


def _evidence_manifest(tmp_path, extra: str = ""):
    path = tmp_path / "evidence.toml"
    path.write_text(f'''not_in_record = ["Power BI"]
light_in_record = ["AWS"]
{extra}
[[source]]
name = "bullets"
type = "csv"
path = "{FIXTURES}/bullets.csv"
text_columns = ["context", "bullet"]
ref_columns = ["role"]
kind = "achievement"

[[source]]
name = "articles"
type = "markdown"
path = "{FIXTURES}/articles"
exclude = ["BACKLOG.md"]
kind = "method"

[[source]]
name = "portfolio"
type = "html"
path = "{FIXTURES}/about.html"
kind = "narrative"

[[guard]]
name = "guards"
path = "{FIXTURES}/guards.csv"
text_columns = ["do_not_claim"]
''')
    return evidence.load_manifest(str(path))


def test_manifest_sources_units_guards_and_example(tmp_path):
    m = _evidence_manifest(tmp_path)
    assert (m.not_in_record, m.light_in_record, [s.kind for s in m.sources]) == (
        ["Power BI"], ["AWS"], ["achievement", "method", "narrative"])
    units = evidence.iter_units(m)
    texts = " | ".join(u.text for u in units)
    assert all(15 <= len(u.text) <= evidence.MAX_UNIT for u in units)
    assert "value stream" in texts and "Short row" not in texts                       # csv; a stub row is dropped
    assert "handoff between two teams" in texts and "title: Example" not in texts     # markdown minus frontmatter
    assert "Ideas that are not evidence" not in texts                                 # excluded file
    assert "forty green belts" in texts and "never counts" not in texts               # html minus nav/script/footer
    assert any(u.ref == "about › About" for u in units)
    assert "Never claim" not in texts and evidence.guard_texts(m) == ["Never claim a certification in Tool X."]
    assert evidence.check(m, log=_quiet)
    example = evidence.load_manifest(os.path.join(ROOT, "evidence.example.toml"))    # the committed example works
    assert evidence.check(example, log=_quiet) and evidence.iter_units(example)


def test_evidence_long_split_duplicates_skips_and_broken_manifests(tmp_path):
    long_text = " ".join(f"Sentence number {i} describes a measurable process improvement result." for i in range(40))
    (tmp_path / "a.txt").write_text(long_text + "\n\nShared paragraph about value stream mapping across teams.\n")
    (tmp_path / "b.txt").write_text("Shared paragraph about value stream mapping across teams.\n\nAI USAGE: load this "
                                    "file only when drafting marketing copy for the practice.\n")
    (tmp_path / "m.toml").write_text(f'''[[source]]
name = "a"
type = "text"
path = "{tmp_path}/a.txt"
kind = "narrative"
[[source]]
name = "b"
type = "text"
path = "{tmp_path}/b.txt"
kind = "achievement"
skip_patterns = ["(?i)^ai usage"]
''')
    units = evidence.iter_units(evidence.load_manifest(str(tmp_path / "m.toml")))
    shared = [u for u in units if u.text.startswith("Shared paragraph")]
    assert len(shared) == 1 and (shared[0].source, shared[0].weight) == ("b", 1.0)       # kept once, highest weight
    assert not any("AI USAGE" in u.text for u in units)
    assert sum(1 for u in units if u.source == "a") >= 3 and all(len(u.text) <= 600 for u in units)
    (tmp_path / "missing.toml").write_text('[[source]]\nname = "x"\ntype = "csv"\npath = "/nonexistent/x.csv"\n'
                                           'kind = "achievement"\n')
    assert not evidence.check(evidence.load_manifest(str(tmp_path / "missing.toml")), log=_quiet)
    (tmp_path / "bad.toml").write_text('[[source]]\nname = "x"\ntype = "csv"\npath = "x.csv"\nkind = "hobby"\n')
    with pytest.raises(ValueError):
        evidence.load_manifest(str(tmp_path / "bad.toml"))


def test_evidence_rebuild_float384_and_cosine(tmp_path):
    pytest.importorskip("numpy")
    con = store.connect(str(tmp_path / "t.duckdb"))
    m = _evidence_manifest(tmp_path)
    enc = FakeEncoder()
    stats = evidence.rebuild(con, m, enc, log=_quiet)
    assert stats["embedded"] == stats["units"] > 0 and stats["evidence_version"] == evidence.stored_version(con, m.embed_model)
    assert con.execute("SELECT typeof(vector) FROM evidence_units LIMIT 1").fetchone()[0] == "FLOAT[384]"
    units, matrix = evidence.load_matrix(con, m.embed_model)
    assert matrix.shape == (len(units), 384)
    cos = con.execute("SELECT max(array_cosine_similarity(vector, ?::FLOAT[384])) FROM evidence_units",
                      [matrix[0].tolist()]).fetchone()[0]
    assert cos == pytest.approx(1.0, abs=1e-4)
    assert evidence.rebuild(con, m, enc, log=_quiet)["embedded"] == 0                   # nothing new to embed
    con.close()


def test_split_requirements_sections_weights_and_classes():
    units = requirements.split_requirements(REQ_JD)
    by_text = {u.text: u for u in units}
    assert by_text["Lead process excellence for operations."].section == "responsibility"
    value = next(u for u in units if u.text.startswith("Map value streams"))
    assert (value.section, value.group, value.weight, value.klass) == ("responsibility", "role", 0.8, "work")
    years = by_text["Process improvement leading cross-functional teams"]                # §16.4: skill content kept
    assert (years.section, years.group, years.weight, years.klass) == ("required", "required", 1.0, "work")
    assert by_text["10+ years of experience."].klass == "level"
    assert by_text["7+ years in retail banking operations"].klass == "domain"
    assert by_text["Ability to travel up to 25% of the time"].klass == "logistics"
    sql = next(u for u in units if "SQL" in u.text)
    assert (sql.section, sql.group, sql.weight) == ("preferred", "required", 0.4)
    assert not any("dental" in u.text for u in units)                                    # benefits section dropped
    body = requirements.split_requirements("We need someone to map processes across finance teams. "
                                           "You will build dashboards for weekly operating reviews.")
    assert [(u.section, u.weight) for u in body] == [("body", 0.7), ("body", 0.7)]
    assert requirements.classify("10+ years in process improvement leading cross-functional teams") == (
        "work", "Process improvement leading cross-functional teams")
    capped = requirements.split_requirements(REQ_JD, max_units=3)
    assert len(capped) == 3 and all(u.weight == 1.0 for u in capped)


def test_rejoin_lines_glues_split_sentences_and_drops_labels():
    lines = requirements.rejoin_lines("Aetna is seeking a\nVP\n,\n Chief Operating Officer with\ndeep experience")
    assert lines == ["Aetna is seeking a VP, Chief Operating Officer with deep experience"]
    units = requirements.split_requirements("Responsibilities\nEnterprise Operational Leadership\n"
                                            "Serves as the strategic operations advisor to the medical officers.\n")
    assert [u.text for u in units] == ["Serves as the strategic operations advisor to the medical officers."]


def test_credit_row_kind_weight_scales_credit_not_similarity():
    np = pytest.importorskip("numpy")
    arg = np.array([0, 1, 2, 3])
    assert coverage.KINDS == ("achievement", "duty", "narrative", "method")
    band, credit, idx, cos = coverage.credit_row(np.array([0.5, 0.5, 0.5, 0.85]), arg, 0.72, 0.62, None)
    assert (band, round(credit, 2), idx) == ("strong", 0.7, 3)                           # method unit still strong
    band, credit, idx, _ = coverage.credit_row(np.array([0.65, 0.5, 0.5, 0.85]), arg, 0.72, 0.62, None)
    assert (band, round(credit, 2), idx) == ("strong", 0.7, 3)                           # 0.7 beats partial 0.5
    band, credit, idx, _ = coverage.credit_row(np.array([0.80, 0.5, 0.5, 0.5]), arg, 0.72, 0.62, None)
    assert (band, credit, idx) == ("strong", 1.0, 0)
    assert coverage.credit_row(np.array([0.9, 0.5, 0.5, 0.5]), arg, 0.72, 0.62, "light_in_record")[:2] == ("partial", 0.5)
    assert coverage.credit_row(np.array([0.9, 0.9, 0.9, 0.9]), arg, 0.72, 0.62, "not_in_record")[:2] == ("gap", 0.0)
    missing = np.array([-1, -1, -1, 3])                                                  # only method evidence exists
    assert coverage.credit_row(np.array([-1, -1, -1, 0.7]), missing, 0.72, 0.62, None)[:3] == ("partial", 0.35, 3)
    assert coverage.term_cap("Dashboards in Power BI and AWS", ["Power BI"], ["AWS"]) == ("not_in_record", "power bi")
    assert coverage.term_cap("Deploy on AWS", ["Power BI"], ["AWS"]) == ("light_in_record", "aws")


def test_score_doc_math_null_under_three_and_gaps():
    ev = [{"ref": "Role A", "source": "bullets", "kind": "achievement"}]
    units = [{"text": t, "grp": g, "weight": 1.0, "spec": None, "klass": "work"}
             for t, g in (("req one", "required"), ("req two", "required"), ("req three", "required"),
                          ("role one", "role"), ("role two", "role"))]
    credits = [("strong", 1.0, 0, 0.8), ("partial", 0.5, 0, 0.65), ("gap", 0.0, 0, 0.3),
               ("strong", 1.0, 0, 0.9), ("gap", 0.0, 0, 0.1)]
    out = coverage.score_doc(units, credits, ev)
    assert out["coverage_required"] == 50.0 and out["coverage_role"] is None             # role has 2 work units
    assert (out["n_required"], out["n_required_strong"], out["n_required_partial"]) == (3, 1, 1)
    assert out["gaps"] == ["req three", "role two"]                                      # Required gaps first
    assert [mt[0] for mt in out["matches"]] == ["req one", "role one", "req two"]            # credit x weight order
    assert out["notes"]["sources"] == {"bullets": 3}
    units[0]["spec"] = 0.2                                                               # a generic strong line counts less
    assert coverage.score_doc(units, credits, ev)["coverage_required"] == pytest.approx(100 * (0.2 + 0.5) / 2.2, abs=0.1)
    assert coverage.primary({"coverage_required": None, "coverage_role": 40.0}) == 40.0


def test_specificity_and_doc_frequencies():
    np = pytest.importorskip("numpy")
    assert coverage.specificity(1, 100) == 1.0 and coverage.specificity(100, 100) == 0.2
    assert coverage.specificity(10, 100) == pytest.approx(0.5)
    same = np.tile(embed.normalize(np.ones((1, 384), np.float32)), (3, 1))
    other = embed.normalize(np.eye(1, 384, 5, dtype=np.float32))
    ref = np.vstack([same, other])
    owners = ["A", "B", "C", "D"]
    assert coverage.doc_frequencies(same[:1], ["A"], ref, owners) == [3]                 # B and C, not its own posting
    assert coverage.doc_frequencies(other, ["Z"], ref, owners) == [2]


def _covered_posting(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    store.record_board(con, "Acme", "greenhouse", [N.base(req_id="C1", title="Process Excellence Lead", url="https://x/C1",
                                                          location="Remote - USA", workplace_type="remote")], now)
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h1', description_fetched_at = ?",
                [REQ_JD, now])
    return con, con.execute("SELECT posting_id FROM postings").fetchone()[0]


def test_cover_writes_rows_caches_units_and_leaves_scores_alone(tmp_path):
    pytest.importorskip("numpy")
    con, pid = _covered_posting(tmp_path)
    pipeline.screen(con, log=_quiet)
    before = con.execute("SELECT final_score, band, verdict FROM vw_screen_latest").fetchall()
    m = _evidence_manifest(tmp_path)
    evidence.rebuild(con, m, FakeEncoder(), log=_quiet)
    stats = coverage.cover(con, m, FakeEncoder(), posting_ids=[pid], log=_quiet)
    assert stats["covered"] == 1
    c_req, c_role, n_req, n_role, notes, gaps, matches = con.execute(
        "SELECT coverage_required, coverage_role, n_required, n_role, notes, gaps, matches FROM vw_coverage_latest "
        "WHERE posting_id = ?", [pid]).fetchone()
    assert (n_req, n_role) == (5, 3) and all(v is None or 0 <= v <= 100 for v in (c_req, c_role))
    notes, matches = json.loads(notes), json.loads(matches)
    assert notes["domain"] == ["7+ years in retail banking operations"]
    assert (notes["level_lines"], notes["logistics_lines"]) == (1, 1)
    assert {c[1]: c[2] for c in notes["capped"]} == {"not_in_record": "power bi", "light_in_record": "aws"}
    assert not any("Power BI" in mt[0] for mt in matches)
    assert not any("AWS" in mt[0] and mt[5] == "strong" for mt in matches)
    n_units = con.execute("SELECT count(*) FROM requirement_units").fetchone()[0]
    counting = FakeEncoder()
    coverage.cover(con, m, counting, posting_ids=[pid], log=_quiet)
    assert counting.calls == 0 and con.execute("SELECT count(*) FROM requirement_units").fetchone()[0] == n_units
    assert con.execute("SELECT final_score, band, verdict FROM vw_screen_latest").fetchall() == before   # 3a: weight 0
    shortlist = con.execute("SELECT coverage_required, coverage_role FROM vw_shortlist WHERE posting_id = ?", [pid]).fetchall()
    assert shortlist in ([], [(c_req, c_role)])
    # survivors without coverage under the current versions: none left for this posting
    ev_version = evidence.stored_version(con, m.embed_model)
    assert pid not in coverage.survivor_ids(con, evidence_version=ev_version, model=m.embed_model, calibration="default")
    con.close()


def test_hard_negative_view_and_pass_reason_codes(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    con.executemany("INSERT INTO decisions VALUES (?, 'pass', ?, 'cli', NULL, ?)",
                    [["a" * 20, "function: clinical operations", now], ["b" * 20, "nuance", now],
                     ["c" * 20, "travel is too much", now]])
    con.execute("INSERT INTO decisions VALUES (?, 'build', NULL, 'cli', NULL, ?)", ["d" * 20, now])
    con.execute("INSERT INTO hard_negatives VALUES (?, 'audit', NULL, ?)", ["e" * 20, now])
    codes = dict(con.execute("SELECT posting_id, reason_code FROM vw_decisions").fetchall())
    assert codes == {"a" * 20: "function", "b" * 20: "nuance", "c" * 20: "other", "d" * 20: None}
    assert sorted(con.execute("SELECT posting_id, source FROM vw_hard_negatives").fetchall()) == [
        ("a" * 20, "decision:function"), ("e" * 20, "audit")]
    con.close()


def test_mark_pass_requires_a_reason_code(tmp_path):
    import finder
    con, pid = _covered_posting(tmp_path)
    with pytest.raises(SystemExit):
        finder.cmd_mark(con, Namespace(target=pid, decision="pass", reason="travel is too much"))
    finder.cmd_mark(con, Namespace(target=pid, decision="pass", reason="logistics: travel"))
    finder.cmd_mark(con, Namespace(target=pid, decision="hold", reason=None))
    assert con.execute("SELECT count(*) FROM decisions").fetchone()[0] == 2
    con.close()


def test_setup_check_passes_on_example_and_fails_on_broken_manifest(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    assert setup_check.run(con, manifest=os.path.join(ROOT, "evidence.example.toml"), log=_quiet)
    (tmp_path / "broken.toml").write_text('[[source]]\nname = "x"\ntype = "pdf"\npath = "/nonexistent/x.pdf"\n'
                                          'kind = "narrative"\n')
    lines = []
    assert not setup_check.run(con, manifest=str(tmp_path / "broken.toml"), log=lines.append)
    assert any(line.startswith("BLOCK") for line in lines)
    con.close()


def test_coverage_stage_skips_cleanly_without_manifest(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    monkeypatch.setenv("JOBSEARCH_EVIDENCE", str(tmp_path / "none.toml"))
    lines = []
    assert pipeline.coverage_stage(con, log=lines.append) is None
    assert lines and "skipped" in lines[0]
    con.close()


def test_fit_stanza_coverage_parts():
    parts = report.coverage_parts(50.0, 62.4, 5, 2, 1, 3, 1, 1, '["gap a", "gap | b"]',
                                  '[["req", "Role A", 0.81, "bullets", "achievement", "strong"]]')
    assert parts == ["coverage required 50 (2 of 5 strong, 1 partial)", "role 62 (1 of 3 strong, 1 partial)"]
    detail = report.coverage_detail(50.0, 62.4, 5, 2, 1, 3, 1, 1, '["gap a", "gap | b"]',
                                    '[["req", "Role A", 0.81, "bullets", "achievement", "strong"]]')
    assert detail[0].startswith("gaps: gap a; gap") and "|" not in detail[0]
    assert detail[1] == "matched: req ← Role A (bullets, 0.81)"
    assert report.coverage_parts() == [] and report.coverage_detail() == []


def test_calibrate_against_hard_negatives_stores_thresholds(tmp_path, monkeypatch):
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    off = [N.base(req_id=f"O{i}", title=f"Software Engineer {i}", url=f"https://x/O{i}", location="Remote - USA",
                  workplace_type="remote") for i in range(6)]
    store.record_board(con, "Acme", "greenhouse", off, now)
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'off', description_fetched_at = ?",
                [OFF_REQ_JD, now])
    rows = [[f"p{i}", "application", None, None, "Acme", f"Process Excellence Lead {i}",
             REQ_JD + f"\n- Run operating reviews for region number {i} with finance partners", 1, 1.0, now]
            for i in range(6)]
    rows += [[f"n{i}", "pseudo_neg", None, None, "Beta", f"Software Engineer {i}", OFF_REQ_JD, 0, 1.0, now]
             for i in range(6)]
    con.executemany("INSERT INTO label_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"), log=_quiet)
    pipeline.screen(con, model=features.load_latest(con, log=_quiet), log=_quiet)
    monkeypatch.setattr(P, "AUDIT_NEGATIVES", [con.execute("SELECT posting_id FROM postings LIMIT 1").fetchone()[0],
                                               "Acme|not an id"])
    m = _evidence_manifest(tmp_path)
    evidence.rebuild(con, m, FakeEncoder(), log=_quiet)
    result = coverage.calibrate(con, m, FakeEncoder(), n_pseudo=5, hard_top=5, log=_quiet)
    assert (result["n_pos"], result["n_hard"]) == (6, 6)
    assert 0.54 <= result["partial"] < result["strong"] <= 0.90
    assert {"coverage_required", "coverage_role", "fit_heldout", "blend_0.75"} <= set(result["aucs"])
    assert dict(con.execute("SELECT source, count(*) FROM hard_negatives GROUP BY 1").fetchall()) == {"audit": 1, "fit_top": 5}
    calib = coverage.current_calibration(con)
    assert calib["version"] == result["version"] and calib["strong"] == result["strong"]
    con.close()


# ---------------------------------------------------------------- the labeling run (judge)
from backend.finder import judge, rubric  # noqa: E402


def _judge_corpus(tmp_path):
    """Two survivors with different JDs, one exact repost, and one rejected posting."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    jobs = [N.base(req_id="A1", title="Director, Process Excellence", url="https://x/A1", location="Remote - USA",
                   workplace_type="remote"),
            N.base(req_id="A2", title="Director, Business Transformation", url="https://x/A2",
                   location="Remote - USA", workplace_type="remote"),
            N.base(req_id="A3", title="Director, Process Excellence (Repost)", url="https://x/A3",
                   location="Remote - USA", workplace_type="remote"),
            N.base(req_id="B1", title="Line Cook", url="https://x/B1", location="Springfield, IL")]
    store.record_board(con, "Acme", "greenhouse", jobs, now)
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h1', description_fetched_at = ? "
                "WHERE req_id IN ('A1', 'A3')", [REQ_JD, now])
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h2', description_fetched_at = ? "
                "WHERE req_id = 'A2'", [REQ_JD.replace("process excellence", "transformation"), now])
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h3', description_fetched_at = ? "
                "WHERE req_id = 'B1'", [OFF_REQ_JD, now])
    pipeline.screen(con, log=_quiet)
    m = _evidence_manifest(tmp_path)
    evidence.rebuild(con, m, FakeEncoder(), log=_quiet)
    ids = [r[0] for r in con.execute("SELECT posting_id FROM postings ORDER BY req_id").fetchall()]
    coverage.cover(con, m, FakeEncoder(), posting_ids=ids, log=_quiet)
    return con, dict(con.execute("SELECT req_id, posting_id FROM postings").fetchall())


def test_judge_queue_interleaves_and_dedupes_only_identical_text(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _judge_corpus(tmp_path)
    pool = {"high": ["a", "b", "c"], "low": ["x", "y"], "reject": ["r"]}
    mixed = judge.interleave(pool)
    assert sorted(mixed) == sorted([("a", "high"), ("b", "high"), ("c", "high"), ("x", "low"), ("y", "low"),
                                    ("r", "reject")])
    assert [t for _, t in mixed][:3] == ["high", "high", "high"]      # the confusable band leads each wave
    ids = list(by_req.values())
    dup = judge.duplicate_map(con, ids)
    assert dup == {by_req["A3"]: by_req["A1"]} or dup == {by_req["A1"]: by_req["A3"]}   # the repost only
    assert by_req["A2"] not in dup and by_req["A2"] not in dup.values()                 # a sibling role survives
    con.close()


def test_judge_export_import_round_trip_and_bad_results(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _judge_corpus(tmp_path)
    queue = [(by_req[r], "high") for r in ("A1", "A2", "A3")] + [(by_req["B1"], "reject")]
    out = tmp_path / "batches"
    stats = judge.write_batches(con, str(out), queue, batch_size=2, log=_quiet)
    assert stats["duplicates"] == 1 and stats["postings"] == 3
    manifest = json.loads((out / "manifest.json").read_text())
    text = (out / "batch_001.md").read_text()
    assert rubric.RUBRIC_PUBLIC.strip()[:40] in text and "CANDIDATE RECORD" in text        # rubric travels with it
    assert "Mapped the order-to-delivery value stream" not in text                         # never the evidence text
    judged = [p for b in manifest["batches"].values() for p in b["postings"]]
    (out / "batch_001.result.json").write_text(json.dumps(
        [{"posting_id": judged[0], "grade": "bullseye", "lane": "primary", "confidence": "high", "blocker": "",
          "rationale": "process ownership work"},
         {"posting_id": judged[1], "grade": "wrong", "lane": "wrong", "confidence": "high", "blocker": "kitchen",
          "rationale": "line cooking"}]))
    (out / "batch_002.result.json").write_text("```json\n" + json.dumps(
        [{"posting_id": judged[2], "grade": "adjacent", "lane": "secondary", "confidence": "low", "blocker": "",
          "rationale": "adjacent work"},
         {"posting_id": "f" * 20, "grade": "bullseye"},                    # not exported: refused
         {"posting_id": judged[2], "grade": "amazing"}]) + "\n```")        # invalid grade: refused
    result = judge.load_results(con, str(out), scorer="test-scorer", log=_quiet)
    assert len(result["errors"]) == 2 and result["copied"] == 1            # the repost copied its representative
    grades = dict(con.execute("SELECT posting_id, grade FROM vw_llm_labels_latest").fetchall())
    assert grades[by_req["A3"]] == grades[by_req["A1"]]
    assert set(grades.values()) <= set(rubric.GRADES) and len(grades) == 4
    pending = judge.status(str(out), log=_quiet)
    assert pending["pending"] == [] and len(pending["done"]) == 2
    (out / "batch_002.result.json").unlink()
    assert judge.status(str(out), log=_quiet)["pending"] == ["batch_002"]  # the resume point
    csv_path = tmp_path / "labels.csv"
    judge.to_csv(con, str(csv_path), log=_quiet)
    assert "bullseye" in csv_path.read_text()
    con.close()


def test_judge_agreement_reads_the_users_own_decisions(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _judge_corpus(tmp_path)
    now = datetime(2026, 9, 15, 12)
    con.execute("INSERT INTO decisions VALUES (?, 'build', NULL, 'cli', NULL, ?)", [by_req["A1"], now])
    con.execute("INSERT INTO decisions VALUES (?, 'pass', 'function: wrong lane', 'cli', NULL, ?)",
                [by_req["A2"], now])
    rows = [[by_req["A1"], "h1", "rv", "test", "wrong", "wrong", "wrong", "wrong", "high", "", "", "b", now],
            [by_req["A2"], "h2", "rv", "test", "bullseye", "bullseye", "stretch", "primary", "high", "", "", "b", now]]
    con.executemany("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                    "grade_process, grade_technical, lane, confidence, blocker, rationale, batch, judged_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    out = judge.agreement(con, log=_quiet)
    assert out["pursued"] == {"wrong": 1} and out["passed_function"] == {"bullseye": 1}   # both are disagreements
    con.close()


def test_import_accepts_two_lens_and_pre_split_result_files():
    from backend.finder import judge, rubric
    allowed = {"p1": "h1"}
    assert judge._validate({"posting_id": "p1", "grade_process": "bullseye",
                            "grade_technical": "wrong"}, allowed) is None
    assert judge._validate({"posting_id": "p1", "grade": "adjacent"}, allowed) is None   # pre-split file
    assert judge._validate({"posting_id": "p1", "grade_process": "nope",
                            "grade_technical": "wrong"}, allowed) is not None
    assert judge._validate({"posting_id": "nope", "grade": "adjacent"}, allowed) is not None
    assert set(rubric.GRADES) == {"bullseye", "adjacent", "stretch", "wrong"}


def test_overall_averages_the_lenses_rather_than_taking_the_best():
    from backend.finder import judge
    assert judge.overall("bullseye", "bullseye") == "bullseye"
    assert judge.overall("wrong", "wrong") == "wrong"
    assert judge.overall("bullseye", "adjacent") == "bullseye"      # ties round toward the better grade
    assert judge.overall("adjacent", "adjacent") == "adjacent"
    assert judge.overall("bullseye", "wrong") == "adjacent"         # excellent on one lens still surfaces
    assert judge.overall("adjacent", "wrong") == "stretch"
    assert judge.overall("stretch", "wrong") == "stretch"


def test_model_version_changes_when_a_grade_changes_not_just_the_id_set():
    """Re-grading changes labels and weights but no label_id. If the version ignored that, `rescreen` would
    skip the corpus because model_version matched, and every score would silently stay on the old model."""
    from backend.finder import features
    params = {"C": 4.0}
    a = features.model_version([["llm:x", 1, 1.0], ["llm:y", 0, 1.0]], params)
    b = features.model_version([["llm:x", 0, 1.0], ["llm:y", 0, 1.0]], params)   # grade flipped
    c = features.model_version([["llm:x", 1, 0.6], ["llm:y", 0, 1.0]], params)   # weight changed
    assert a != b and a != c and b != c
    assert a == features.model_version([["llm:y", 0, 1.0], ["llm:x", 1, 1.0]], params)   # order-independent


# --- Clearance: held vs obtainable (user ruling 2026-09-16) -----------------------------------------
# "Ability to obtain" means the employer sponsors and funds it, so it is not a blocker; only a clearance
# that must ALREADY be held is. The old rule flagged any clearance word and cost real roles -- CACI
# "Business Process Consultant" (93) and Guidehouse "Senior Business Process Analyst" (91) among them.

def _clearance_flags(text):
    return [f for f in rules.screen_row(_row(description_text=text)).flags if "clearance" in f]


def test_obtainable_clearance_is_not_a_blocker():
    f = _clearance_flags("Clearance Required : Ability to Obtain Public Trust. U.S. Citizenship with the "
                         "ability to obtain and maintain a federal Public Trust clearance.")
    assert f and "reachable" in f[0], f


def test_sponsored_hard_clearance_is_also_reachable():
    """Even TS/SCI is not a blocker when the employer offers to sponsor it."""
    f = _clearance_flags("Must be eligible to obtain a TS/SCI security clearance; we sponsor.")
    assert f and "reachable" in f[0], f


def test_clearance_that_must_be_held_is_a_blocker():
    for text in ("Requires an active TS/SCI clearance with polygraph.",
                 "Candidate must currently hold a Top Secret security clearance.",
                 "An active secret clearance is required on day one."):
        f = _clearance_flags(text)
        assert f and "already be held" in f[0], (text, f)


def test_hard_level_without_sponsorship_is_a_blocker():
    f = _clearance_flags("TS/SCI security clearance required for this position.")
    assert f and "already be held" in f[0], f


def test_compensation_boilerplate_raises_no_clearance_flag():
    """'...skill sets, experience, security clearances, licensure...' is a pay paragraph, not a requirement."""
    assert _clearance_flags(
        "Compensation decisions depend on skill sets, experience and training, security clearances, "
        "licensure and certifications, and other business needs.") == []


# --- Domain tenure: a disjunctive list is not a gate (user catch 2026-09-16) ------------------------
# "10+ years in Supply Chain, Operations, Logistics, Manufacturing, Consulting, or a related field"
# gates on none of them -- any one will do, and Operations and Consulting are his.

def test_disjunctive_tenure_list_is_not_a_gate():
    req = ("Required Qualifications\n10+ years of progressive experience in Banking, Operations, "
           "Logistics, Manufacturing, Consulting, or a related field.")
    assert rules.domain_tenure_rule("", req)[1] == []


def test_compound_domain_still_gates():
    """'10+ years in banking operations' is one compound domain, not a disjunctive list."""
    req = "Required Qualifications\n10+ years of experience in banking operations."
    assert rules.domain_tenure_rule("", req)[1] == ["domain-tenure gate (banking, 10 yrs)"]


def test_single_domain_with_no_alternatives_still_gates():
    req = "Required Qualifications\n8+ years of experience in banking."
    assert rules.domain_tenure_rule("", req)[1] == ["domain-tenure gate (banking, 8 yrs)"]


def test_list_without_a_candidate_field_still_gates():
    """A list is only harmless when one of the alternatives is actually his."""
    req = "Required Qualifications\n10+ years in pharma, biotech, or managed care."
    assert rules.domain_tenure_rule("", req)[1] != []


# ---- per-lens fit columns and vw_lens_fit (sprint plan 18.8) ----

def _lens_row(con, pid, *, verdict="review", fp=None, ft=None, score=80):
    now = datetime(2026, 9, 16)
    con.execute("INSERT OR REPLACE INTO postings (posting_id, employer, platform, req_id, title, url, status, "
                "first_seen_at, last_seen_at) VALUES (?, 'Acme', 'greenhouse', ?, ?, ?, 'active', ?, ?)",
                [pid, pid, f"Role {pid}", f"https://x/{pid}", now, now])
    con.execute("INSERT OR REPLACE INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                "rule_score, fit_prob, fit_process, fit_technical, final_score, band) "
                "VALUES (?, 'rv', 'mv', ?, ?, 70, 0.8, ?, ?, ?, 'strong')",
                [pid, now, verdict, fp, ft, score])


def _grade(con, pid, process, technical, scorer="claude-sonnet-batch"):
    con.execute("UPDATE postings SET description_hash = 'h' WHERE posting_id = ?", [pid])
    con.execute("INSERT OR REPLACE INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                "grade_process, grade_technical, judged_at) VALUES (?, 'h', 'r1', ?, 'adjacent', ?, ?, ?)",
                [pid, scorer, process, technical, datetime(2026, 9, 16)])


def test_lens_fit_prefers_a_grade_over_a_prediction(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _lens_row(con, "a" * 20, fp=0.95, ft=0.95)          # the model would call both lenses strong
    _grade(con, "a" * 20, "wrong", "wrong")             # the judge says otherwise
    row = con.execute("SELECT lens_bucket, lens_source, process_strong FROM vw_lens_fit").fetchone()
    assert row == ("neither", "judge", False)
    con.close()


def test_lens_fit_falls_back_to_the_model_per_lens(tmp_path):
    """A row graded before the second lens existed keeps its process grade and gets a technical prediction."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _lens_row(con, "b" * 20, fp=0.10, ft=0.95)
    _grade(con, "b" * 20, "bullseye", None)
    bucket, source, ps, ts = con.execute(
        "SELECT lens_bucket, lens_source, process_strong, technical_strong FROM vw_lens_fit").fetchone()
    assert (bucket, source, ps, ts) == ("both", "judge+model", True, True)
    con.close()


def test_lens_fit_marks_the_users_own_ruling(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _lens_row(con, "c" * 20, fp=0.1, ft=0.1)
    _grade(con, "c" * 20, "adjacent", "stretch", scorer="user-adjudicated")
    assert con.execute("SELECT lens_source, lens_bucket FROM vw_lens_fit").fetchone() == ("user", "process")
    con.close()


def test_model_rows_need_the_standout_bar_for_both(tmp_path):
    """Strong on each lens but standout on neither is a generalist, not a both-lens role."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _lens_row(con, "d" * 20, fp=0.75, ft=0.75)
    _lens_row(con, "e" * 20, fp=0.85, ft=0.75)
    got = dict(con.execute("SELECT posting_id, lens_bucket FROM vw_lens_fit").fetchall())
    assert got == {"d" * 20: "process", "e" * 20: "both"}
    assert con.execute("SELECT DISTINCT lens_source FROM vw_lens_fit").fetchone() == ("model",)
    con.close()


def test_screen_writes_both_lens_probabilities(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 16)
    jobs = [N.base(req_id=f"L{i}", title=f"Process Excellence Lead {i}", url=f"https://x/L{i}",
                   location="Remote - USA", workplace_type="remote") for i in range(4)]
    store.record_board(con, "Acme", "greenhouse", jobs, now)
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h', description_fetched_at = ?",
                [REQ_JD, now])
    fake = {"version": "lens-v1"}
    monkeypatch.setattr(features, "predict", lambda m, texts: [0.91] * len(texts))
    pipeline.screen(con, model=None, lens_models={"process": fake, "technical": fake}, log=_quiet)
    rows = con.execute("SELECT fit_process, fit_technical FROM vw_screen_latest").fetchall()
    assert len(rows) == 4 and all(r == (0.91, 0.91) for r in rows)
    con.close()


def test_screen_without_lens_models_leaves_the_columns_null(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 16)
    store.record_board(con, "Acme", "greenhouse", [N.base(req_id="L1", title="Process Excellence Lead",
                                                          url="https://x/L1", location="Remote - USA")], now)
    pipeline.screen(con, model=None, log=_quiet)
    assert con.execute("SELECT fit_process, fit_technical FROM vw_screen_latest").fetchone() == (None, None)
    con.close()
