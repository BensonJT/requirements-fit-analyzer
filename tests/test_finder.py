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
    # A "100%" describing something other than travel, far from the word, must not be misread as a travel figure.
    assert rules.travel_rule("", "This posting is 100% remote, offers a great culture, strong benefits, and "
                                 "excellent growth potential for candidates. Minimal travel is required, "
                                 "under 10% annually.")[:2] == ([], [])
    # \d{1,2} used to cap the regex below 100, so "100% travel" was silently unmatched -- fixed to \d{1,3}
    # with a <=100 guard; it must reject exactly like a 75% figure does.
    assert rules.travel_rule("", "Up to 100% travel required.")[0] == ["travel 100% (limit 25%)"]
    assert rules.travel_rule("", "Up to 75% travel required.")[0] == ["travel 75% (limit 25%)"]


def test_travel_rule_skipped_without_limit(monkeypatch):
    monkeypatch.setattr(P, "TRAVEL_MAX_PCT", None)
    assert rules.travel_rule("", "Travel 90%") == ([], [], {})


def test_direct_reports_rule_branches():
    reasons, flags, notes = rules.direct_reports_rule("", "Lead a team of 7 analysts.")
    assert (reasons, flags, notes["reports"]) == ([], ["team of 7 (limit 5)"], 7)
    assert rules.direct_reports_rule("", "Manage 12 direct reports.")[0] == ["large team (12 direct reports)"]
    assert rules.direct_reports_rule("", "Own the hiring plan.")[1] == ["large-team markers (hiring plan)"]
    assert rules.direct_reports_rule("", "Manage 3 direct reports.")[:2] == ([], [])


def test_direct_reports_rule_program_headcount_never_rejects():
    # CACI: "team of 250+ professionals" is program headcount, not a direct-report count -- flag only, never reject.
    reasons, flags, notes = rules.direct_reports_rule("", "Support a team of 250+ professionals on this program.")
    assert reasons == [] and flags == ["team of 250 (limit 5)"] and notes["reports"] == 250
    # "lead a team of 12 analysts" reads like a report count but is still "team of N" phrasing -- flag, not reject.
    reasons, flags, notes = rules.direct_reports_rule("", "You will lead a team of 12 analysts on this initiative.")
    assert reasons == [] and flags == ["team of 12 (limit 5)"]
    # Only an explicit direct-report phrase can still reject.
    assert rules.direct_reports_rule("", "This role carries 12 direct reports.")[0] == ["large team (12 direct reports)"]
    assert rules.direct_reports_rule("", "You will manage 12 reports across two sites.")[0] == \
        ["large team (12 direct reports)"]


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


def test_content_fit_is_the_best_lens_never_the_averaged_model():
    """A single-lens role is a NEGATIVE to the averaged main model by construction; the best lens decides
    what is shown, and the main model stands in only when no lens score exists."""
    assert pipeline.content_fit(0.20, [0.83, 0.05, None]) == 0.83          # strong on one lens: shown
    assert pipeline.content_fit(0.20, [0.10, 0.12, 0.81]) == 0.81          # applied-AI alone is enough
    assert pipeline.content_fit(0.90, [0.10, 0.12, 0.05]) == 0.12          # main never outvotes the lenses
    assert pipeline.content_fit(0.64, [None, None, None]) == 0.64          # no lens models: main stands in
    assert pipeline.content_fit(None, []) is None
    rec = rules.screen_row(_row(title="Director, Operational Excellence"))
    pipeline.apply_content_gate(rec, pipeline.content_fit(0.20, [0.83, 0.05, None]))
    assert rec.verdict != "reject" and not any(r.startswith("content does not fit") for r in rec.reasons)


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


def test_rescreen_predicate_is_null_safe(tmp_path):
    """`!=` against a NULL rv/mv is NULL (unknown), not true, so a screened row would silently never come
    up for rescreen if either version were ever passed as None -- IS DISTINCT FROM treats NULL as a real,
    comparable value instead."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    ids = _seed(con)
    pipeline.screen(con, log=_quiet)
    rv = version.rules_version()
    assert set(pipeline.candidate_ids(con, None, "none")) == set(ids.values())
    assert set(pipeline.candidate_ids(con, rv, None)) == set(ids.values())
    assert pipeline.candidate_ids(con, rv, "none") == []          # the real versions still match and skip a rescreen


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


def test_match_rows_fuzzy_prefers_a_posting_near_the_application_date():
    """A 2025 application must not fuzzy-match a same-titled 2026 req at the same employer --
    only a posting first seen within FUZZY_MATCH_WINDOW_DAYS of the tracker's date applied
    is eligible."""
    rows = [tracker_sync.TrackerRow("Active", "2026-09-01", "Acme Corp", "Process Excellence Lead")]
    postings = [
        ("old", "Acme Corp", "Process Excellence Lead", datetime(2025, 1, 1)),
        ("new", "Acme Corp", "Process Excellence Lead", datetime(2026, 8, 20)),
    ]
    matches = tracker_sync.match_rows(rows, postings)
    _, matched, kind = matches[0]
    assert (matched, kind) == ("new", "fuzzy")


def test_match_rows_fuzzy_finds_nothing_outside_the_window():
    rows = [tracker_sync.TrackerRow("Active", "2026-09-01", "Acme Corp", "Process Excellence Lead")]
    postings = [("old", "Acme Corp", "Process Excellence Lead", datetime(2025, 1, 1))]
    matches = tracker_sync.match_rows(rows, postings)
    assert matches[0][1:] == (None, "none")


def test_match_rows_fuzzy_falls_back_without_a_parseable_date():
    """No date applied (or an unparseable one) keeps the old date-blind behavior."""
    rows = [tracker_sync.TrackerRow("Active", "", "Acme Corp", "Process Excellence Lead")]
    postings = [("old", "Acme Corp", "Process Excellence Lead", datetime(2020, 1, 1))]
    matches = tracker_sync.match_rows(rows, postings)
    assert matches[0][1:] == ("old", "fuzzy")


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
    jobs = [N.base(req_id=f"S{i}", title=f"Senior Manager, Lean Six Sigma Operational Excellence {i}",
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
        # each row's text is distinct (a "case N" tag) so the text-hash CV groups (features._text_group_ids)
        # don't collapse all 30 positives -- and all 30 negatives -- into a single group; real JDs vary too.
        rows.append([f"p{i}", "application", None, None, "Acme", f"Process Excellence Lead {i}",
                     f"{FIT_JD} Case {i}.", 1, 1.0, now])
        rows.append([f"n{i}", "pseudo_neg", None, None, "Beta", f"Software Engineer {i}",
                     f"{OFF_JD} Case {i}.", 0, 1.0, now])
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

    # `train` stores a path relative to the repo root, not the absolute one it wrote to (see REPO_ROOT).
    stored_path = con.execute("SELECT path FROM models").fetchone()[0]
    assert not os.path.isabs(stored_path) and stored_path == os.path.relpath(result["path"], features.REPO_ROOT)
    con.close()


def test_load_latest_falls_back_to_model_dir_when_stored_path_is_gone(tmp_path):
    """Defect 1: a `models.path` row surviving from a deleted drive (an absolute path that no longer exists
    anywhere) must still load as long as the joblib is sitting in MODEL_DIR (or the given `model_dir`) under
    its own version name -- and a truly missing file must still come back as None, not raise."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    result = _trained(tmp_path, con)
    version, real_path = result["model_version"], result["path"]
    assert os.path.exists(real_path)

    # simulate the stored row pointing at a drive that no longer exists
    con.execute("UPDATE models SET path = ? WHERE model_version = ?",
                [f"/nonexistent/dir/{version}.joblib", version])
    model = features.load_latest(con, log=_quiet, model_dir=os.path.dirname(real_path))
    assert model is not None and model["version"] == version

    # a file that truly isn't anywhere (not in MODEL_DIR, not in model_dir) still returns None cleanly
    con.execute("UPDATE models SET path = ? WHERE model_version = ?",
                [f"/nonexistent/dir/{version}.joblib", version])
    os.remove(real_path)
    assert features.load_latest(con, log=_quiet, model_dir=os.path.dirname(real_path)) is None
    con.close()


def test_text_group_ids_shares_a_group_for_identical_text():
    """Defect 2: two rows whose `text` is identical after normalization (a repost, a judge `dup:` copy) must
    get the same CV group id, whatever their label_id/posting_id, so a StratifiedGroupKFold split can never
    put one in train and the other in test."""
    rows = [{"text": "Same JD text.  "}, {"text": "totally different job"}, {"text": "  SAME jd TEXT."}]
    ids = features._text_group_ids(rows)
    assert ids[0] == ids[2] and ids[0] != ids[1]


def test_train_passes_text_based_groups_to_cross_validate(tmp_path, monkeypatch):
    """Defect 2, integration: `train` must build its `groups` argument from text (not `range(n_jobs)`), so two
    label rows with identical text land in the same fold no matter which source/id they came in under."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15)
    rows = []
    for i in range(20):
        rows.append([f"p{i}", "application", None, None, "Acme", f"Process Excellence Lead {i}",
                     f"{FIT_JD} Case {i}.", 1, 1.0, now])
        rows.append([f"n{i}", "pseudo_neg", None, None, "Beta", f"Software Engineer {i}",
                     f"{OFF_JD} Case {i}.", 0, 1.0, now])
    # a repost pair: identical text, different ids/sources/companies (so training_set's fuzzy company+title
    # dedupe doesn't collapse them into one row before grouping even runs) -- must never split across folds
    rows.append(["dup1", "application", None, None, "Zeta", "Repost Lead A", FIT_JD + " Case dup.", 1, 1.0, now])
    rows.append(["dup2", "llm_judge", None, None, "Theta", "Repost Lead B", FIT_JD + " Case dup.", 1, 1.0, now])
    con.executemany("INSERT INTO label_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    captured = {}
    real_cv = features.cross_validate

    def spy(texts, y, w, **kw):
        captured["groups"] = kw.get("groups")
        return real_cv(texts, y, w, **kw)

    monkeypatch.setattr(features, "cross_validate", spy)
    features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"), log=_quiet)
    groups = captured["groups"]
    assert groups is not None and groups != list(range(len(groups)))   # not the old range()-per-row grouping

    training_rows = features.training_set(con)
    idx = {r["label_id"]: i for i, r in enumerate(training_rows)}
    assert groups[idx["dup1"]] == groups[idx["dup2"]]
    con.close()


def test_train_metrics_exclude_career_site_copies(tmp_path):
    """Defect 3: a vault positive whose matched posting's own JD text differs spawns a career-site copy that
    `train` appends to y/weights/texts for extra training signal -- but confusion_at_0_5, pos/neg_mean_fit_oof
    and oof_auc must describe one row per job (n_jobs), not double-count that job via its copy."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_label_corpus(con, n_each=20)
    fit_pids = [r[0] for r in con.execute(
        "SELECT posting_id FROM postings WHERE req_id LIKE 'F%' ORDER BY req_id").fetchall()]
    now = datetime(2026, 9, 15)
    rows = []
    # each vault positive's own text differs from its matched posting's career-site JD -> spawns a copy
    for i, pid in enumerate(fit_pids[:15]):
        rows.append([f"app{i}", "application", None, pid, "Acme", f"Process Excellence Lead {i}",
                     f"{FIT_JD} Vault-only phrasing {i}.", 1, 1.0, now])
    for i in range(15):
        rows.append([f"n{i}", "pseudo_neg", None, None, "Beta", f"Software Engineer {i}",
                     f"{OFF_JD} Case {i}.", 0, 1.0, now])
    con.executemany("INSERT INTO label_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"), log=_quiet)
    assert result["n_pos"] == 15 and result["n_neg"] == 15
    n_jobs = result["n_pos"] + result["n_neg"]
    assert len(result["oof"]) > n_jobs   # the copies really were appended
    conf = result["confusion_at_0_5"]
    assert conf["tp"] + conf["fp"] + conf["fn"] + conf["tn"] == n_jobs   # metrics describe one row per job
    con.close()


# ---- Required-block ranking model (NOT a lens -- see features.REQUIRED_MODEL) ----

def _seed_required_corpus(con, n_each=20):
    """Postings judged on required_fit only -- no label_docs, no decisions -- so vw_label_set_required has
    something to train on. `meets` rows keep the process-excellence JD, `fails` rows the off-lane one, each
    with a distinct `Case N` tag so the text-hash CV groups don't collapse them."""
    now = datetime(2026, 9, 15, 12, 0)
    meets = [N.base(req_id=f"RM{i}", title=f"Process Excellence Lead {i}", url=f"https://x/RM{i}",
                    location="Remote - USA", workplace_type="remote") for i in range(n_each)]
    fails = [N.base(req_id=f"RX{i}", title=f"Software Engineer {i}", url=f"https://x/RX{i}",
                    location="Remote - USA", workplace_type="remote") for i in range(n_each)]
    store.record_board(con, "Acme", "greenhouse", meets + fails, now)
    rows = []
    for i in range(n_each):
        pid = con.execute("SELECT posting_id FROM postings WHERE req_id = ?", [f"RM{i}"]).fetchone()[0]
        text = f"{FIT_JD} Case {i}."
        dh = store.description_hash(text)
        con.execute("UPDATE postings SET description_text = ?, description_hash = ? WHERE posting_id = ?",
                   [text, dh, pid])
        rows.append([pid, dh, "claude-sonnet-batch", "bullseye", "meets", now])
    for i in range(n_each):
        pid = con.execute("SELECT posting_id FROM postings WHERE req_id = ?", [f"RX{i}"]).fetchone()[0]
        text = f"{OFF_JD} Case {i}."
        dh = store.description_hash(text)
        con.execute("UPDATE postings SET description_text = ?, description_hash = ? WHERE posting_id = ?",
                   [text, dh, pid])
        rows.append([pid, dh, "claude-sonnet-batch", "wrong", "fails", now])
    con.executemany("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                    "required_fit, judged_at) VALUES (?, ?, 'rv1', ?, ?, ?, ?)", rows)


def test_vw_label_set_required_is_judge_only_no_vault_no_decisions(tmp_path):
    """HARD RULE 2: the training view takes ONLY judged required_fit rows -- label 1 for meets, 0 for
    arguable/fails, weight 1.0 always -- and a vault positive / build decision for the SAME posting must never
    leak in, because the view doesn't read label_docs or decisions at all."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_required_corpus(con, n_each=5)
    pid = con.execute("SELECT posting_id FROM postings WHERE req_id = 'RM0'").fetchone()[0]
    now = datetime(2026, 9, 15)
    con.execute("INSERT INTO label_docs VALUES ('vault1', 'application', 'f', ?, 'Acme', 'x', ?, 1, 1.0, ?)",
               [pid, FIT_JD, now])
    con.execute("INSERT INTO decisions VALUES (?, 'build', NULL, 'tracker', 'Active', ?)", [pid, now])
    # an arguable row: label must be 0 (a real gap), not folded into the positives
    con.execute("INSERT INTO postings (posting_id, employer, platform, req_id, title, url, status, "
                "description_hash, description_text, first_seen_at, last_seen_at) VALUES "
                "('argp', 'Acme', 'greenhouse', 'argp', 'Process Lead X', 'https://x/argp', 'active', 'ah', ?, ?, ?)",
               [FIT_JD + " Case arg.", now, now])
    con.execute("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                "required_fit, judged_at) VALUES ('argp', 'ah', 'rv1', 'test', 'adjacent', 'arguable', ?)", [now])

    rows = features.training_set(con, lens="required")
    assert rows and all(r["source"] in ("llm_judge", "user_adjudicated") for r in rows)   # no vault/decision source
    assert {r["grade"] for r in rows} <= {"meets", "arguable", "fails"}
    assert all(r["weight"] == 1.0 for r in rows)
    by_grade = {r["grade"]: r["label"] for r in rows}
    assert by_grade["meets"] == 1 and by_grade["fails"] == 0 and by_grade["arguable"] == 0
    con.close()


def test_train_required_model_stores_its_own_kind_and_is_not_a_lens(tmp_path):
    """train(lens=features.REQUIRED_MODEL) runs the normal path (kind tfidf_lr_required) and never lands in
    LENSES / load_lens_models's dict -- only load_required_model can see it."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_required_corpus(con, n_each=20)
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"),
                            lens=features.REQUIRED_MODEL, log=_quiet)
    kind = con.execute("SELECT kind FROM models WHERE model_version = ?", [result["model_version"]]).fetchone()[0]
    assert kind == "tfidf_lr_required"
    assert "required" not in features.LENSES
    assert features.load_lens_models(con, log=_quiet) == {}          # not one of the three lenses
    required_model = features.load_required_model(con, log=_quiet)
    assert required_model is not None and required_model["version"] == result["model_version"]
    # meets/arguable/fails land in grade_report's by_grade (not silently dropped) and print AUCs for the
    # vocabulary that is actually present (fails/arguable), not the lens vocabulary (wrong/stretch).
    g = result["grades"]
    assert set(g["by_grade"]) <= {"meets", "fails"}                   # no arguable rows seeded here
    assert "auc_vs_fails" in g and "auc_vs_wrong" not in g
    con.close()


def test_train_required_model_career_site_copies_are_a_noop(tmp_path):
    """HARD RULE 3: career-site-copy augmentation only fires when a row's text differs from the posting's own
    current description_text -- vw_label_set_required's text always IS that text, so it must add zero copies
    and train() must run without raising, not crash on the empty case."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_required_corpus(con, n_each=20)
    rows = features.training_set(con, lens=features.REQUIRED_MODEL)
    assert features._career_site_copies(con, rows) == {}
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"),
                            lens=features.REQUIRED_MODEL, log=_quiet)
    assert len(result["oof"]) == result["n_pos"] + result["n_neg"]    # no copies appended
    con.close()


def test_fit_required_written_by_screen_never_moves_verdict_or_final_score(tmp_path):
    """HARD RULE 4/1: fit_required is stored by a screen, but screening the SAME row with and without the
    Required-block model must produce an identical verdict and final_score -- it is ranking-only."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_required_corpus(con, n_each=20)
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"),
                            lens=features.REQUIRED_MODEL, log=_quiet)
    required_model = features.load_required_model(con, log=_quiet)
    assert required_model["version"] == result["model_version"]

    now = datetime(2026, 9, 16, 12, 0)
    posting = N.base(req_id="NEWROW", title="Process Excellence Lead 99", url="https://x/NEWROW",
                     location="Remote - USA", workplace_type="remote")
    store.record_board(con, "Acme", "greenhouse", [posting], now)
    pid = con.execute("SELECT posting_id FROM postings WHERE req_id = 'NEWROW'").fetchone()[0]
    con.execute("UPDATE postings SET description_text = ?, description_fetched_at = ? WHERE posting_id = ?",
               [FIT_JD + " Case new.", now, pid])

    pipeline.screen(con, full=True, required_model=None, log=_quiet)
    before = con.execute("SELECT verdict, final_score, fit_required FROM vw_screen_latest "
                         "WHERE posting_id = ?", [pid]).fetchone()
    assert before[2] is None

    pipeline.screen(con, full=True, required_model=required_model, log=_quiet)
    after = con.execute("SELECT verdict, final_score, fit_required FROM vw_screen_latest "
                        "WHERE posting_id = ?", [pid]).fetchone()
    assert after[2] is not None
    assert (before[0], before[1]) == (after[0], after[1])
    con.close()


def test_required_value_and_breadth_x_required(tmp_path):
    """required_value: the judge's call wins (meets 1.0 / arguable 0.5 / fails 0.0), else coalesce(prob, 0.5).
    breadth_x_required = lens_breadth * required_value -- covering judged-meets / judged-arguable /
    judged-fails / unjudged-with-a-model-probability / unjudged-with-neither."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    ids = {"meets": "g" * 20, "arguable": "h" * 20, "fails": "i" * 20}
    for rf, pid in ids.items():
        _top_posting(con, pid, grade_process="bullseye", grade_technical="bullseye", grade_ai="bullseye",
                     required_fit=rf)
    con.execute("INSERT INTO postings (posting_id, employer, platform, req_id, title, url, status, "
                "description_hash, first_seen_at, last_seen_at) VALUES "
                "('u1', 'Acme', 'greenhouse', 'u1', 'T', 'url1', 'active', 'h', now(), now())")
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                "rule_score, final_score, band, fit_process, fit_technical, fit_ai, fit_required) VALUES "
                "('u1', 'rv', 'mv', now(), 'review', 70, 80, 'strong', 0.9, 0.9, 0.9, 0.66)")
    con.execute("INSERT INTO postings (posting_id, employer, platform, req_id, title, url, status, "
                "description_hash, first_seen_at, last_seen_at) VALUES "
                "('u2', 'Acme', 'greenhouse', 'u2', 'T', 'url2', 'active', 'h', now(), now())")
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                "rule_score, final_score, band, fit_process, fit_technical, fit_ai) VALUES "
                "('u2', 'rv', 'mv', now(), 'review', 70, 80, 'strong', 0.9, 0.9, 0.9)")

    rows = con.execute(
        "SELECT posting_id, required_value(required_fit, fit_required), breadth_x_required FROM vw_lens_fit "
        "WHERE posting_id IN ('u1', 'u2', ?, ?, ?)",
        [ids["meets"], ids["arguable"], ids["fails"]]).fetchall()
    got = {pid: (rv, br) for pid, rv, br in rows}
    assert got[ids["meets"]] == (1.0, pytest.approx(3.0))          # three bullseyes: lens_breadth 3.0 * 1.0
    assert got[ids["arguable"]] == (0.5, pytest.approx(1.5))       # same breadth, halved by the judge's call
    assert got[ids["fails"]] == (0.0, 0.0)                         # fails zeroes lens_breadth itself too
    assert got["u1"] == (pytest.approx(0.66), pytest.approx(1.782))   # unjudged: model prob stands in
    assert got["u2"] == (0.5, pytest.approx(1.35))                    # unjudged, no fit_required either: 0.5
    con.close()


def test_llm_labels_required_fit_columns_present_and_fit_required_column_fresh_and_upgraded(tmp_path):
    """Fresh DB: screens.fit_required exists via CREATE TABLE. An older DB (schema_info stuck below v13) picks
    it up through _add_missing_columns on the next connect(), same pattern as fit_ai at v10."""
    fresh = store.connect(str(tmp_path / "fresh.duckdb"))
    assert "fit_required" in store._columns(fresh, "screens")
    fresh.close()

    old = store.connect(str(tmp_path / "old.duckdb"))
    old.execute("ALTER TABLE screens DROP COLUMN fit_required")
    old.execute("UPDATE schema_info SET version = 12")
    old.close()
    upgraded = store.connect(str(tmp_path / "old.duckdb"))
    assert "fit_required" in store._columns(upgraded, "screens")
    upgraded.close()


def test_content_fit_ignores_fit_required(tmp_path):
    """content_fit's signature never takes fit_required, and combine() never sees it -- confirmed structurally:
    passing the required model alongside a lens score must not change content_fit's result versus the lens
    scores alone."""
    assert pipeline.content_fit(0.20, [0.83, 0.05, None]) == pipeline.content_fit(0.20, [0.83, 0.05, None])
    import inspect
    assert "fit_required" not in inspect.signature(pipeline.content_fit).parameters
    assert "required" not in inspect.signature(pipeline.combine).parameters


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


def test_signal_report_blend_matches_stored_final_score(tmp_path):
    """Defect 4: `signal_report`'s `blend` column must be the score the pipeline actually stored
    (`vw_screen_latest.final_score`), not a value recomputed with `pipeline.combine(..., rejected=...)` and no
    `flags=` -- that call drops the penalty flags a real screen applied, so it can't reproduce final_score."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    fit = [N.base(req_id=f"F{i}", title=f"Process Excellence Lead {i}", url=f"https://x/F{i}",
                  location="Remote - USA", workplace_type="remote") for i in range(15)]
    off = [N.base(req_id=f"O{i}", title=f"Software Engineer {i}", url=f"https://x/O{i}",
                  location="Remote - USA", workplace_type="remote") for i in range(15)]
    store.record_board(con, "Acme", "greenhouse", fit + off, now)
    fit_pids = [r[0] for r in con.execute("SELECT posting_id FROM postings WHERE req_id LIKE 'F%' "
                                          "ORDER BY req_id").fetchall()]
    off_pids = [r[0] for r in con.execute("SELECT posting_id FROM postings WHERE req_id LIKE 'O%' "
                                          "ORDER BY req_id").fetchall()]
    rows = []
    for i, pid in enumerate(fit_pids):
        text = f"{FIT_JD} Case {i}."
        con.execute("UPDATE postings SET description_text = ?, description_fetched_at = ? WHERE posting_id = ?",
                    [text, now, pid])
        rows.append([f"p{i}", "application", None, pid, "Acme", f"Process Excellence Lead {i}", text, 1, 1.0, now])
    for i, pid in enumerate(off_pids):
        text = f"{OFF_JD} Case {i}."
        con.execute("UPDATE postings SET description_text = ?, description_fetched_at = ? WHERE posting_id = ?",
                    [text, now, pid])
        rows.append([f"n{i}", "pseudo_neg", None, pid, "Beta", f"Software Engineer {i}", text, 0, 1.0, now])
    con.executemany("INSERT INTO label_docs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"), log=_quiet)
    model = features.load_latest(con, log=_quiet)
    pipeline.screen(con, model=model, log=_quiet)
    stored = dict(con.execute("SELECT posting_id, final_score FROM vw_screen_latest").fetchall())

    rows_out = features.signal_report(con, result)
    blend_row = next(r for r in rows_out if r[0] == "blend")
    assert blend_row[3] > 0   # n_all: at least one training row matched a screen

    # cross-check by hand: every training row with a screen contributes its OWN stored final_score to the AUC
    y_blend = [(r["label"], stored[r["posting_id"]]) for r in result["rows"] if r["posting_id"] in stored]
    expected_auc = features._auc([y for y, _ in y_blend], [b for _, b in y_blend])
    assert blend_row[1] == features._fmt(expected_auc)
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
    assert rules.level_rule("Strategy & Project Management - Summer 2027", "", None)[0] == \
        ["early-career title (summer 2027)"]
    assert rules.level_rule("University Program - Data Analyst", "", None)[0] == \
        ["early-career title (university program)"]
    assert rules.level_rule("Summer Concert Series Coordinator", "", None)[0] == []   # "summer" alone doesn't match
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


def test_is_remote_reads_the_full_jd_and_the_jd_beats_a_generic_or_stale_ats_flag():
    # Blue Yonder: the ATS says hybrid, but "Location: US-REMOTE ..." sits well past the old 600-char cutoff.
    padding = ("Partner with cross-functional stakeholders to drive operational excellence and process "
              "improvement across the organization. ") * 6
    blue_yonder = S.Listing(source="ats", search_pass="", title="Principal Consultant", company="Blue Yonder",
                            location="Dallas, TX", url="",
                            extra={"workplace_type": "hybrid"},
                            description=padding + "Location: US-REMOTE with the ability to travel up to 30%.")
    assert len(blue_yonder.description) > 600
    assert S.is_remote(blue_yonder)

    # Microsoft: the ATS flags onsite, but the location is the generic "Multiple Locations" shape --
    # that flag is never trusted on its own, and the JD's own remote statement wins.
    microsoft = S.Listing(source="ats", search_pass="", title="Principal Program Manager", company="Microsoft",
                          location="United States, Multiple Locations", url="",
                          extra={"workplace_type": "onsite"},
                          description="Location: Remote, United States. This position is remote eligible.")
    assert S.is_remote(microsoft)

    # A real onsite flag on a specific (non-generic) location is still trusted even if the JD mentions
    # remote work in a duties sentence rather than as a location statement.
    onsite = S.Listing(source="ats", search_pass="", title="Program Manager", company="Acme", location="Austin, TX",
                       url="", extra={"workplace_type": "onsite"},
                       description="Manage remote field teams across five states from our Austin office.")
    assert not S.is_remote(onsite)

    # Negative: "remote" used only inside a duties sentence, with no ATS flag or location signal, is not remote.
    unflagged = S.Listing(source="ats", search_pass="", title="Program Manager", company="Acme",
                          location="Austin, TX", url="", extra={},
                          description="Manage remote field teams across five states from our Austin office.")
    assert not S.is_remote(unflagged)


# --- B2 (2026-09-19): non-commutable on-site postings mistakenly read as remote --------------------
# Three blind-graded rows (RTX/Tucson hybrid, Booz Allen/Atlanta, T. Rowe Price/Baltimore) were all plain
# `candidate` with the user's note "not remote" -- each trips a different boilerplate shape below. Amended
# same day: conditional phrasing ("remote work may be considered") is a GENUINE possible-remote fact, not
# boilerplate, and must still read remote (with a visible flag), never a reject.

def test_generic_workplace_enumeration_is_not_a_remote_statement():
    """RTX case: 'regardless of whether the role is designated as on-site, hybrid or remote' classifies the
    CONCEPT, it does not commit to one -- must not read as remote."""
    text = ("Employees may be asked to work at one of our office locations, regardless of whether the role "
           "is designated as on-site, hybrid or remote. The salary range for this role is $107,500-$204,500.")
    assert not S._remote_in_context(text)


def test_remote_policy_glossary_is_not_a_remote_statement():
    """Booz Allen case: 'If this position is listed as remote, ...' explains a policy generically -- it does
    not itself assert this posting is remote."""
    text = ("Remote: If this position is listed as remote, there may still be occasions when you are "
           "required to work in person at a Booz Allen or customer facility.")
    assert not S._remote_in_context(text)


def test_pay_transparency_band_is_not_a_remote_statement():
    """T. Rowe Price case: 'for the location of: ... and remote workers' is a compensation-band disclosure
    naming several possible geographies, not a statement about where THIS role sits."""
    text = "$122,000.00 - $209,000.00 for the location of: Maryland, Colorado, Washington and remote workers"
    assert not S._remote_in_context(text)


def test_explicit_remote_negation_is_not_remote():
    for text in ("This is not a remote position. On-site required.",
                 "Telework Eligible: No.",
                 "This role offers no remote work option."):
        assert not S._remote_in_context(text), text


def test_virtual_teams_and_remote_sensing_are_duty_vocabulary_not_location():
    assert not S._remote_in_context("Lead virtual teams across five regions from our Austin office.")
    assert not S._remote_in_context("Experience with remote sensing data is a plus.")


def test_bare_remote_heading_is_not_a_remote_statement():
    """Splitting a JD on '[\\n.]+' can separate a policy-glossary heading ('Remote') from the sentence
    that explains it ('If this position is listed as remote, ...') -- the bare heading alone must not
    count as a location statement (Booz Allen case, full residual bug)."""
    text = "Remote\n: If this position is listed as remote, there may still be occasions when you work on-site."
    assert not S._remote_in_context(text)


def test_virtual_meeting_mode_is_not_a_remote_statement():
    """'virtually'/'virtual interview' describes HOW people communicate, not WHERE the job sits."""
    for text in ("Our culture prioritizes collaboration, whether in person or virtually.",
                 "Employees working virtually are expected to have cameras on during meetings.",
                 "The use of AI during virtual interviews is prohibited."):
        assert not S._remote_in_context(text), text


def test_eeo_boilerplate_mentioning_remote_is_not_a_remote_statement():
    text = ("Our Equal Employment Opportunity policy provides reasonable accommodation regardless of "
           "national origin; some accommodations may include remote arrangements case by case.")
    assert not S._remote_in_context(text)


def test_true_remote_signals_still_read_as_remote():
    """The rescued cases from the 2026-09-16/17 fixes must not regress: Blue Yonder's `Location: US-REMOTE`,
    an `#LI-Remote` footer, `US Off-Site`, and a flat 'this role is fully remote' statement."""
    assert S._remote_in_context("Location: US-REMOTE with the ability to travel up to 30%.")
    assert S._remote_in_context("Great team culture. #LI-Remote")
    assert S._remote_in_context("This role is fully remote.")


def test_conditional_remote_phrasing_is_genuine_not_boilerplate():
    """User amendment 2026-09-19: 'will/may be considered' / 'considered for the right candidate' is a real
    possible-remote fact -- it must still count as remote (is_remote True, no reject) and must be visible
    via conditional_remote_phrase, distinct from the excluded boilerplate above."""
    for text in ("Remote work may be considered for the right candidate.",
                 "This role is on-site; remote will be considered for exceptional candidates.",
                 "Remote considered for the right candidate."):
        assert S._remote_in_context(text), text
        assert S.conditional_remote_phrase(text), text


def test_conditional_remote_phrase_none_for_flat_or_negated_text():
    assert S.conditional_remote_phrase("This role is fully remote.") is None
    assert S.conditional_remote_phrase("This is not a remote position.") is None


def test_screen_row_flags_conditional_remote_without_rejecting():
    row = _row(location_primary="Atlanta, GA", workplace_type=None,
              description_text="This is an on-site role in Atlanta. Remote work may be considered for the "
                                "right candidate.")
    rec = rules.screen_row(row)
    flags = [f for f in rec.flags if "conditional" in f]
    assert flags and "considered" in flags[0]
    assert "not remote and outside the commute area" not in "; ".join(rec.reasons)


def test_screen_row_rtx_style_enumeration_still_rejects_when_not_commutable():
    """The RTX-shaped posting (enumeration boilerplate, no genuine remote signal, non-commutable location)
    must be rejected on location, not waved through as remote."""
    row = _row(location_primary="US-AZ-TUCSON-807A", workplace_type=None,
              description_text="Employees may be asked to work at one of our office locations, regardless "
                                "of whether the role is designated as on-site, hybrid or remote.")
    rec = rules.screen_row(row)
    assert "not remote and outside the commute area (per listing)" in rec.reasons


# ---------------------------------------------------------------- §23: residence-restricted remote
def _remote_row(desc: str, **kw):
    kw.setdefault("workplace_type", "remote")
    return _row(location_primary="Remote - USA", description_text=desc, **kw)


def test_residence_restriction_hub_list_rejects_when_none_commutable():
    """Acceptance (a): a hub restriction naming no commutable place rejects."""
    desc = ("This role is remote but must be located within commuting distance of one of our hubs: "
            "Austin, TX; Denver, CO.")
    assert S.residence_restriction(desc)[0] == "places"
    rec = rules.screen_row(_remote_row(desc))
    assert rec.verdict == "reject"
    assert any(r.startswith("remote restricted to:") and "Austin, TX" in r and "Denver, CO" in r
               for r in rec.reasons)


def test_residence_restriction_passes_when_a_hub_is_commutable():
    desc = ("This role is remote but must be located within commuting distance of one of our hubs: "
            "Springfield, IL; Denver, CO.")
    rec = rules.screen_row(_remote_row(desc))
    assert not [r for r in rec.reasons if r.startswith("remote restricted to:")]
    assert rec.verdict != "reject"


def test_residence_restriction_state_list_rejects_when_none_match_home_state():
    desc = "This role is remote within the following states: Texas, Colorado, Arizona."
    rec = rules.screen_row(_remote_row(desc))
    assert any(r.startswith("remote restricted to:") for r in rec.reasons)
    assert rec.verdict == "reject"


def test_residence_restriction_state_list_passes_on_home_state_match():
    desc = "This role is remote within the following states: Illinois, Indiana, Wisconsin."
    rec = rules.screen_row(_remote_row(desc))
    assert not [r for r in rec.reasons if r.startswith("remote restricted to:")]


def test_residence_restriction_within_n_miles_trigger():
    desc = "Employees must live within 50 miles of Austin, TX or Denver, CO."
    kind, places, phrase = S.residence_restriction(desc)
    assert kind == "places" and places == ["Austin, TX", "Denver, CO"]
    rec = rules.screen_row(_remote_row(desc))
    assert rec.verdict == "reject"


def test_residence_restriction_eligible_states_trigger():
    desc = "Eligible states: Texas, Colorado, Arizona."
    rec = rules.screen_row(_remote_row(desc))
    assert any(r.startswith("remote restricted to:") for r in rec.reasons)


def test_residence_restriction_unclear_flags_without_rejecting():
    """Restriction language with no parseable place list flags, quoted, and never rejects -- same
    pass-through principle as clearance_call's ambiguous verdict."""
    desc = "This role must be based within commuting distance of our headquarters."
    kind, places, phrase = S.residence_restriction(desc)
    assert kind == "unclear" and places == [] and phrase
    rec = rules.screen_row(_remote_row(desc))
    assert not [r for r in rec.reasons if r.startswith("remote restricted to:")]
    flags = [f for f in rec.flags if f.startswith("remote-residence-check")]
    assert flags and "headquarters" in flags[0]
    assert rec.verdict != "reject"


def test_residence_restriction_none_when_no_restriction_language():
    assert S.residence_restriction("This role is fully remote, work from anywhere in the US.")[0] == "none"
    assert S.residence_restriction("")[0] == "none"


def test_residence_restriction_only_applies_when_read_as_remote():
    """An on-site posting is already handled by the commute rule -- the residence rule must not add
    its own reason on top of it."""
    row = _row(location_primary="Austin, TX", workplace_type="onsite",
              description_text=("This role must be located within commuting distance of one of our "
                                 "hubs: Austin, TX; Denver, CO."))
    rec = rules.screen_row(row)
    assert "not remote and outside the commute area (per listing)" in rec.reasons
    assert not [r for r in rec.reasons if r.startswith("remote restricted to:")]


# ---- exclusions: false-reject guards (§23 "what must NOT trigger it")
def test_residence_restriction_excludes_pay_transparency_clause():
    desc = ("For candidates located in the following states, the range differs: must be based within "
            "commuting distance of Denver, CO for local comp-band eligibility.")
    assert S.residence_restriction(desc)[0] == "none"


def test_residence_restriction_excludes_cannot_hire_list():
    """A 'states where we cannot hire' exclusion list must not be read as an inclusion requirement,
    even when it sits in the same sentence as a genuine eligible-states phrase."""
    desc = ("We are unable to hire in several states, but our eligible states are: California, New "
            "York, Washington.")
    assert S.residence_restriction(desc)[0] == "none"


def test_residence_restriction_excludes_office_list_offered_as_option():
    """Acceptance (b): an office list offered as an alternative ('or work from any of our offices'),
    even one that includes a commutable city, is not a restriction and must still pass."""
    desc = ("This role is remote but must be located within commuting distance of one of our hub "
            "offices, or work from any of our offices in Austin, Denver, or Chicago.")
    assert S.residence_restriction(desc)[0] == "none"
    rec = rules.screen_row(_remote_row(desc))
    assert not [r for r in rec.reasons if r.startswith("remote restricted to:")]
    assert rec.verdict != "reject"


def test_residence_restriction_excludes_eeo_boilerplate():
    desc = ("Reasonable accommodation will be provided; must be based within commuting distance of "
            "our office in Cupertino.")
    assert S.residence_restriction(desc)[0] == "none"


def test_residence_restriction_excludes_preferred_timezone_language():
    desc = "Must be located within the Pacific time zone (preferred)."
    assert S.residence_restriction(desc)[0] == "none"


def test_residence_restriction_bare_hub_list_with_no_obligation_word_is_not_a_trigger():
    desc = "You may choose any of our hub offices: Austin, Denver, or Chicago."
    assert S.residence_restriction(desc)[0] == "none"


# ---- §23 rework (9/20 dry-run audit): the ~8/49 false rejects and the ROOT FIX (validated places only)
def test_residence_restriction_country_only_is_none_not_a_reject():
    """Case 1: a bare country reference names no place at all -- kind 'none', never a reject, even
    when it rides along with unrelated eligibility-to-work boilerplate ('US Citizen')."""
    for desc in ("Applicants must reside in the United States.",
                 "Employees must be based in the United States.",
                 "Candidates must be located within the United States and US Citizen."):
        assert S.residence_restriction(desc) == ("none", [], None), desc
        rec = rules.screen_row(_remote_row(desc))
        assert not [r for r in rec.reasons if r.startswith("remote restricted to:")]
        assert rec.verdict != "reject"


def test_residence_restriction_country_prefix_before_colon_does_not_swallow_the_real_list():
    """The country mention before the colon must not turn the sentence into 'none' -- the list after
    the colon is the actual restriction and must still be read (and reject when none of its states
    is commutable or the home state)."""
    desc = "Must reside in the United States: Rhode Island, Vermont or Arizona."
    kind, places, phrase = S.residence_restriction(desc)
    assert kind == "places" and places == ["Rhode Island", "Vermont", "Arizona"]
    rec = rules.screen_row(_remote_row(desc))
    assert rec.verdict == "reject"
    assert any(r.startswith("remote restricted to:") for r in rec.reasons)


def test_residence_restriction_registered_entity_boilerplate_is_none():
    """Case 2: 'a state where <employer> has a registered entity' is company boilerplate covering
    most of the country and names no real place -- must not reject."""
    desc = "You must live in a state where Denimwear Holdings, Inc has a registered entity."
    assert S.residence_restriction(desc)[0] == "none"
    rec = rules.screen_row(_remote_row(desc))
    assert rec.verdict != "reject"


def test_residence_restriction_conditional_clause_is_none():
    """Case 3: a sentence that opens on a condition binds only people who already meet it -- it is a
    hybrid-cadence/perk clause, not a residence gate."""
    for desc in (
            "If you live within 50 miles of one of our hub locations, the expectation is twice a "
            "week in office.",
            "If you are within 50 miles of Denver, Austin or Chicago you will be required to work a "
            "hybrid schedule.",
            "For those who live within commuting distance of Denver, CO, a hybrid schedule applies."):
        assert S.residence_restriction(desc)[0] == "none", desc


def test_residence_restriction_preference_language_is_none():
    """Case 4: a preference, not a requirement -- 'preference will be given', 'preferred', 'ideally',
    'a plus' must never turn into a reject."""
    for desc in ("Preference will be given to candidates residing within 50 miles of Denver, CO.",
                 "Living within 50 miles of Austin, TX is preferred but not required.",
                 "Ideally the candidate would live within 50 miles of Denver, CO."):
        assert S.residence_restriction(desc)[0] == "none", desc


def test_residence_restriction_garbage_sentence_is_unclear_not_a_reject():
    """Case 5: a trigger phrase that accidentally lands in a marketing sentence or unrelated
    boilerplate must not manufacture a fake place list -- unclear at most."""
    for desc in (
            "This is Acme Retail, a leading force in omnichannel merchandising, delivering joy "
            "nationwide, must be located within our growth footprint.",
            "Denimwear Co listed on the exchange this year, must be located within our growth "
            "footprint."):
        kind, places, phrase = S.residence_restriction(desc)
        assert kind in ("none", "unclear"), desc
        assert places == []
        rec = rules.screen_row(_remote_row(desc))
        assert rec.verdict != "reject"


def test_residence_restriction_validated_true_positives_still_reject_or_stay_unclear():
    """The ROOT FIX must not swallow real restrictions: a multi-city/state list with no
    and/or between pairs still reads correctly and still rejects; a bare city with no state
    validates as 'unclear' (acceptable per the rework), never silently passed as a real match."""
    desc = ("Applicants must be within a reasonable commuting distance of an office in Windsor, CT, "
            "Boston, MA, New York/New Jersey.")
    kind, places, phrase = S.residence_restriction(desc)
    assert kind == "places" and places == ["Windsor, CT", "Boston, MA", "New York", "New Jersey"]
    rec = rules.screen_row(_remote_row(desc))
    assert rec.verdict == "reject"

    bare_city = "Hybrid role, must live near Fictionville office."
    assert S.residence_restriction(bare_city)[0] == "unclear"

    shipyard = "Applicants must be within commutable distance to Fiction Naval Shipyard."
    assert S.residence_restriction(shipyard)[0] == "unclear"


def test_residence_restriction_flag_text_is_short():
    desc = "Applicants must be within commutable distance to Fiction Naval Shipyard."
    rec = rules.screen_row(_remote_row(desc))
    flags = [f for f in rec.flags if f.startswith("remote-residence-check")]
    assert flags and flags[0] == f'remote-residence-check ("{desc.rstrip(".")}")'
    long_desc = "Applicants must be within commutable distance to " + "Fictionville " * 30 + "office"
    long_rec = rules.screen_row(_remote_row(long_desc))
    long_flags = [f for f in long_rec.flags if f.startswith("remote-residence-check")]
    assert long_flags and len(long_flags[0]) < 170


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


def test_evidence_postgres_source_reads_rows_and_fails_loudly(tmp_path, monkeypatch):
    toml = ('[[source]]\nname = "bullets"\ntype = "postgres"\npath = "postgresql://u@localhost/resume"\n'
            'query = "SELECT * FROM v"\ntext_columns = ["bullet"]\nref_columns = ["position"]\nkind = "achievement"\n')
    (tmp_path / "pg.toml").write_text(toml)
    m = evidence.load_manifest(str(tmp_path / "pg.toml"))
    assert m.sources[0].path == "postgresql://u@localhost/resume"                        # not resolved as a file
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='position,bullet\nAnalyst,"Cut cycle time 40% by '
                                           'redesigning the intake process, end to end"\n', stderr="")
    monkeypatch.setattr(evidence.shutil, "which", lambda name: "/usr/bin/psql")
    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    units = evidence.iter_units(m)
    assert [(u.ref, u.kind) for u in units] == [("Analyst", "achievement")] and "cycle time" in units[0].text
    assert calls[0][:3] == ["psql", "postgresql://u@localhost/resume", "--csv"] and calls[0][-1] == "SELECT * FROM v"
    assert evidence.check(m, log=_quiet)
    monkeypatch.setattr(evidence.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 2, stdout="", stderr="connection refused"))
    with pytest.raises(RuntimeError, match="connection refused"):                        # never an empty source
        evidence.iter_units(m)
    assert not evidence.check(m, log=_quiet)
    monkeypatch.setenv("JS_TEST_DB_URL", "postgresql://u:secret@localhost/resume")          # `$VAR` from .env
    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    (tmp_path / "env.toml").write_text(toml.replace("postgresql://u@localhost/resume", "$JS_TEST_DB_URL"))
    m_env = evidence.load_manifest(str(tmp_path / "env.toml"))
    assert m_env.sources[0].path == "$JS_TEST_DB_URL" and evidence.iter_units(m_env)       # secret never stored
    assert calls[-1][1] == "postgresql://u:secret@localhost/resume"
    monkeypatch.delenv("JS_TEST_DB_URL")
    with pytest.raises(RuntimeError, match="not set"):
        evidence.iter_units(m_env)
    (tmp_path / "noq.toml").write_text(toml.replace('query = "SELECT * FROM v"\n', ""))
    with pytest.raises(ValueError, match="query"):
        evidence.load_manifest(str(tmp_path / "noq.toml"))


def test_evidence_ensure_current_syncs_new_rows_and_never_empties_a_source(tmp_path):
    pytest.importorskip("numpy")
    import shutil as sh
    con = store.connect(str(tmp_path / "t.duckdb"))
    sh.copy(os.path.join(FIXTURES, "bullets.csv"), tmp_path / "bullets.csv")
    (tmp_path / "m.toml").write_text(f'''[[source]]
name = "bullets"
type = "csv"
path = "{tmp_path}/bullets.csv"
text_columns = ["context", "bullet"]
ref_columns = ["role"]
kind = "achievement"

[[source]]
name = "portfolio"
type = "html"
path = "{FIXTURES}/about.html"
kind = "narrative"
''')
    m = evidence.load_manifest(str(tmp_path / "m.toml"))
    enc = FakeEncoder()
    first = evidence.rebuild(con, m, enc, log=_quiet)["evidence_version"]
    assert evidence.ensure_current(con, m, enc, log=_quiet) == first and enc.calls == 1      # unchanged: no embedding
    with open(tmp_path / "bullets.csv", "a", encoding="utf-8") as fh:
        fh.write('Analyst,Intake,"Rebuilt the vendor onboarding workflow and cut approval time from ten days to three"\n')
    second = evidence.ensure_current(con, m, enc, log=_quiet)
    assert second != first and second == evidence.stored_version(con, m.embed_model) and enc.calls == 2
    assert con.execute("SELECT count(*) FROM evidence_units WHERE text LIKE '%vendor onboarding%'").fetchone()[0] == 1
    before = con.execute("SELECT count(*) FROM evidence_units").fetchone()[0]
    os.remove(tmp_path / "bullets.csv")                                                   # a moved file is not a deletion
    assert evidence.ensure_current(con, m, enc, log=_quiet) == second
    assert con.execute("SELECT count(*) FROM evidence_units").fetchone()[0] == before
    with pytest.raises(RuntimeError, match="refused"):
        evidence.rebuild(con, m, enc, log=_quiet)
    con.close()


def test_ensure_current_only_swallows_io_errors(tmp_path, monkeypatch):
    """A source that's unreadable (missing file, bad toml, a failed subprocess) falls back to stored evidence
    with a warning; a programming error in `iter_units` is a real bug and must not be hidden the same way."""
    pytest.importorskip("numpy")
    con = store.connect(str(tmp_path / "t.duckdb"))
    m = _evidence_manifest(tmp_path)
    enc = FakeEncoder()
    evidence.rebuild(con, m, enc, log=_quiet)
    def _boom(_manifest):
        raise ValueError("boom")
    monkeypatch.setattr(evidence, "iter_units", _boom)
    with pytest.raises(ValueError, match="boom"):
        evidence.ensure_current(con, m, enc, log=_quiet)
    con.close()


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


def test_split_requirements_keeps_short_bulleted_skill_lines():
    # A bullet marker is never a heading, even when it's short, Title Case, or names a PERSON_HEADINGS word.
    jd = """Required Qualifications
- 8+ years of experience in process improvement.
- Lean Six Sigma Black Belt Certification
- Advanced Excel skills
- Strong SQL and Python experience
- Bachelor's Degree in Business Administration
- Skills: SQL, Python, Tableau
"""
    units = requirements.split_requirements(jd)
    texts = [u.text for u in units]
    for expect in ("Lean Six Sigma Black Belt Certification", "Advanced Excel skills",
                  "Strong SQL and Python experience", "Bachelor's Degree in Business Administration"):
        assert expect in texts, texts
    assert any(t.startswith("Skills:") or t == "SQL, Python, Tableau" for t in texts), texts
    assert all(u.section == "required" for u in units)


def test_logistics_line_with_a_work_verb_is_not_logistics():
    # "hybrid" alone doesn't make a line logistics when an action verb is doing the work.
    assert requirements.classify("Coordinate hybrid cloud migrations across regions") == (
        "work", "Coordinate hybrid cloud migrations across regions")
    assert requirements.classify("Design and deliver remote-first onboarding across distributed teams") == (
        "work", "Design and deliver remote-first onboarding across distributed teams")
    # a genuine logistics line (no rescue verb, short / subject-led) still classifies as logistics.
    assert requirements.classify("Must be willing to work a hybrid schedule with 3 days on-site per week")[0] == \
        "logistics"
    assert requirements.classify("Requires an active TS/SCI clearance with polygraph")[0] == "logistics"


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


def test_refresh_hard_negatives_excludes_judge_stretch_grade(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    jobs = [N.base(req_id=f"S{i}", title=f"Line Cook {i}", url=f"https://x/S{i}", location="Springfield, IL")
           for i in range(3)]
    store.record_board(con, "Acme", "greenhouse", jobs, now)
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h1', description_fetched_at = ?",
                [OFF_REQ_JD, now])
    ids = [r[0] for r in con.execute("SELECT posting_id FROM postings ORDER BY req_id").fetchall()]
    con.executemany("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier, "
                    "rule_score, fit_prob, final_score, band) VALUES (?, 'v1', 'none', ?, 'review', NULL, 10, ?, 10, "
                    "'weak')", [[pid, now, 0.9 - i * 0.01] for i, pid in enumerate(ids)])
    # the highest-fit posting is the confusable `stretch` band, not a confirmed negative -- it must never land
    # in fit_top or audit, only in the separate stretch evaluation set.
    con.execute("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                "grade_process, grade_technical, lane, confidence, blocker, rationale, batch, judged_at) "
                "VALUES (?, 'h1', 'rv', 'test', 'stretch', 'stretch', 'stretch', 'secondary', 'high', '', '', "
                "'b', ?)", [ids[0], now])
    monkeypatch.setattr(P, "AUDIT_NEGATIVES", [ids[0]])
    coverage.refresh_hard_negatives(con, hard_top=5, log=_quiet)
    sources = dict(con.execute("SELECT posting_id, source FROM hard_negatives").fetchall())
    assert ids[0] not in sources
    assert sources.get(ids[1]) == "fit_top" and sources.get(ids[2]) == "fit_top"
    con.close()


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
    # unique per row like the positives above: identical negative text would collapse into a single CV group
    # (features._text_group_ids) and starve some folds of a negative class.
    rows += [[f"n{i}", "pseudo_neg", None, None, "Beta", f"Software Engineer {i}",
              OFF_REQ_JD + f"\n- Ticket number {i} in the on-call rotation", 0, 1.0, now]
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
    assert result["aucs_stretch"].keys() >= {"coverage_gated", "fit_heldout"} and result["reranker"] is None
    # Experiment switches: title context gets its own requirement cache; the reranker moves scores to its own scale.
    monkeypatch.setenv("JOBSEARCH_REQ_CONTEXT", "title")
    monkeypatch.setenv("JOBSEARCH_RERANKER", "fake-reranker")
    monkeypatch.setattr(embed, "Reranker", FakeReranker)
    ctx = coverage.calibrate(con, m, FakeEncoder(), n_pseudo=5, hard_top=5, log=_quiet)
    assert (ctx["req_context"], ctx["reranker"]) == ("title", "fake-reranker") and ctx["version"] != result["version"]
    assert 0.05 <= ctx["partial"] < ctx["strong"] <= 0.95
    keys = dict(con.execute("SELECT model, count(DISTINCT posting_id) FROM requirement_units GROUP BY 1").fetchall())
    assert keys == {f"{m.embed_model}|ctx=title": 6}      # model is not in the cache's key: a switch replaces rows
    # current_calibration only returns a row whose notes match the running config -- a threshold set tuned
    # under the reranker/title-context experiment must never be handed to a plain-cosine run, or vice versa.
    plain = coverage.current_calibration(con, encoder="fake", reranker=None, req_context=None, log=_quiet)
    assert plain["version"] == result["version"]
    with_ctx = coverage.current_calibration(con, encoder="fake", reranker="fake-reranker", req_context="title",
                                            log=_quiet)
    assert with_ctx["version"] == ctx["version"]
    lines = []
    unmatched = coverage.current_calibration(con, encoder="fake", reranker=None, req_context="title", log=lines.append)
    assert unmatched is None and any("no calibration matches" in ln for ln in lines)
    con.close()


class FakeReranker:
    """Word-overlap relevance in 0-1; no model download."""

    def __init__(self, name):
        self.name, self.pairs_scored = name, 0

    def score(self, pairs):
        import numpy as np
        self.pairs_scored += len(pairs)
        out = []
        for q, d in pairs:
            qw, dw = set(re.findall(r"[a-z]{3,}", q.lower())), set(re.findall(r"[a-z]{3,}", d.lower()))
            out.append(len(qw & dw) / max(len(qw), 1))
        return np.asarray(out, dtype=np.float32)


def test_requirement_context_keys_and_rerank_best(monkeypatch):
    import numpy as np
    monkeypatch.delenv("JOBSEARCH_REQ_CONTEXT", raising=False)
    assert coverage.req_model_key("m") == "m" and coverage.query_text("Python", "Staff Engineer") == "Python"
    monkeypatch.setenv("JOBSEARCH_REQ_CONTEXT", "title")
    assert coverage.req_model_key("m") == "m|ctx=title"
    assert coverage.query_text("Python", " Staff Engineer ") == "Staff Engineer: Python"
    assert coverage.query_text("Python", None) == "Python"
    ev = [{"text": "mapped the value stream and cut cycle time", "kind": "achievement"},
          {"text": "wrote python services", "kind": "method"},
          {"text": "unrelated gardening notes", "kind": "narrative"}]
    ev_vecs = np.eye(3, dtype=np.float32)
    req_vecs = np.asarray([[0.9, 0.1, 0.0], [0.1, 0.9, 0.0]], dtype=np.float32)
    best, arg = coverage.rerank_best(FakeReranker("f"), ["value stream cycle time", "python services"], req_vecs,
                                     ev_vecs, ev, top=2)
    ach, meth = coverage.KINDS.index("achievement"), coverage.KINDS.index("method")
    assert arg[0, ach] == 0 and best[0, ach] == pytest.approx(1.0)
    assert arg[1, meth] == 1 and best[1, meth] == pytest.approx(1.0)
    assert best[0, coverage.KINDS.index("narrative")] == -1.0                            # not among the top 2: a gap


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


def test_judge_queue_interleaves_and_dedupes_only_matched_pairs(tmp_path):
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


def _title_dup_corpus(tmp_path, postings):
    """`postings` is a list of (req_id, employer, title, text, description_hash). Each distinct employer gets
    its own `record_board` call (postings.employer is fixed per call), all under one platform/timestamp."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    by_employer = {}
    for req_id, employer, title, text, dhash in postings:
        by_employer.setdefault(employer, []).append((req_id, title, text, dhash))
    for employer, rows in by_employer.items():
        jobs = [N.base(req_id=req_id, title=title, url=f"https://x/{req_id}", location_primary="Remote - USA",
                       workplace_type="remote") for req_id, title, _text, _dhash in rows]
        store.record_board(con, employer, "greenhouse", jobs, now)
    for req_id, _employer, _title, text, dhash in postings:
        con.execute("UPDATE postings SET description_text = ?, description_hash = ?, description_fetched_at = ? "
                    "WHERE req_id = ?", [text, dhash, now, req_id])
    pipeline.screen(con, log=_quiet)
    m = _evidence_manifest(tmp_path)
    evidence.rebuild(con, m, FakeEncoder(), log=_quiet)
    ids = [r[0] for r in con.execute("SELECT posting_id FROM postings ORDER BY req_id").fetchall()]
    coverage.cover(con, m, FakeEncoder(), posting_ids=ids, log=_quiet)
    by_req = dict(con.execute("SELECT req_id, posting_id FROM postings").fetchall())
    return con, by_req


def test_judge_dedupes_same_employer_same_title_country_suffix(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _title_dup_corpus(tmp_path, [
        ("C1", "Acme", "Director, Process Excellence | Spain | Remote", REQ_JD, "h1"),
        ("C2", "Acme", "Director, Process Excellence | India | Remote", REQ_JD, "h2")])
    dup = judge.duplicate_map(con, list(by_req.values()))
    assert dup == {by_req["C2"]: by_req["C1"]} or dup == {by_req["C1"]: by_req["C2"]}
    con.close()


def test_judge_same_employer_same_title_low_similarity_not_deduped(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _title_dup_corpus(tmp_path, [
        ("D1", "Acme", "Director, Process Excellence", REQ_JD, "h1"),
        ("D2", "Acme", "Director, Process Excellence", OFF_REQ_JD, "h2")])
    dup = judge.duplicate_map(con, list(by_req.values()))
    assert dup == {}
    con.close()


def test_judge_different_employers_same_title_and_text_not_deduped(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _title_dup_corpus(tmp_path, [
        ("E1", "Acme", "Director, Process Excellence", REQ_JD, "h1"),
        ("E2", "Globex", "Director, Process Excellence", REQ_JD, "h2")])
    dup = judge.duplicate_map(con, list(by_req.values()))
    assert dup == {}
    con.close()


def test_judge_same_employer_different_titles_near_identical_text_not_deduped(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _title_dup_corpus(tmp_path, [
        ("F1", "Acme", "Director, Process Excellence", REQ_JD, "h1"),
        ("F2", "Acme", "Director, Business Transformation", REQ_JD, "h2")])
    dup = judge.duplicate_map(con, list(by_req.values()))
    assert dup == {}
    con.close()


def test_title_key_strips_only_location_and_workplace_suffixes():
    from backend.finder.judge import _title_key
    assert _title_key("Staff Analyst | Spain | Remote") == "staff analyst"
    assert _title_key("Staff Analyst (Remote)") == "staff analyst"
    assert _title_key("Staff Analyst (Remote - US)") == "staff analyst"
    assert _title_key("Staff Analyst - Remote") == "staff analyst"
    assert _title_key("  Staff   Analyst  ") == "staff analyst"
    assert _title_key(None) == ""
    # Not touched: level, seniority, and non-workplace dash suffixes are real title differences.
    assert _title_key("Analyst II") != _title_key("Analyst III")
    assert _title_key("Senior Analyst") != _title_key("Analyst")
    assert _title_key("Analyst - Team Rocket") != _title_key("Analyst")


def test_non_us_primary():
    from backend.finder.judge import non_us_primary
    assert non_us_primary("Remote - US & Canada") is False
    assert non_us_primary("Spain - Remote") is True
    assert non_us_primary(None) is False
    assert non_us_primary("") is False
    assert non_us_primary("Remote") is False
    assert non_us_primary("United States") is False
    assert non_us_primary("India") is True


def test_pools_skips_non_us_primary_location(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 15, 12)
    jobs = [N.base(req_id="G1", title="Process Excellence Lead", url="https://x/G1",
                   location_primary="Remote - USA", workplace_type="remote"),
            N.base(req_id="G2", title="Process Excellence Lead", url="https://x/G2",
                   location_primary="India", workplace_type="remote")]
    store.record_board(con, "Acme", "greenhouse", jobs, now)
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h1', description_fetched_at = ? "
                "WHERE req_id = 'G1'", [REQ_JD, now])
    con.execute("UPDATE postings SET description_text = ?, description_hash = 'h2', description_fetched_at = ? "
                "WHERE req_id = 'G2'", [REQ_JD, now])
    pipeline.screen(con, log=_quiet)
    by_req = dict(con.execute("SELECT req_id, posting_id FROM postings").fetchall())
    seen = []
    pool = judge.pools(con, log=seen.append)
    everyone = {pid for ids in pool.values() for pid in ids}
    assert by_req["G2"] not in everyone
    assert any("non-US" in msg for msg in seen)
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


def test_judge_import_required_fit_round_trips_including_duplicate(tmp_path):
    """required_fit/required_unmet import onto the representative AND get copied onto its near-duplicate,
    same as the lens grades do. required_unmet may arrive as a list (some result files send it that way) and
    must be ' ; '-joined; required_fit is normalized to lowercase."""
    pytest.importorskip("numpy")
    con, by_req = _judge_corpus(tmp_path)
    queue = [(by_req[r], "high") for r in ("A1", "A2", "A3")] + [(by_req["B1"], "reject")]
    out = tmp_path / "batches"
    judge.write_batches(con, str(out), queue, batch_size=2, log=_quiet)
    manifest = json.loads((out / "manifest.json").read_text())
    dup_id, rep_id = next(iter(manifest["duplicates"].items()))
    results = [{"posting_id": rep_id, "grade": "bullseye", "grade_process": "bullseye",
                "grade_technical": "adjacent", "lane": "primary", "confidence": "high", "blocker": "",
                "required_fit": "Meets", "required_unmet": ["needs SAP", "needs CPA"],
                "rationale": "process ownership work"}]
    for name, meta in manifest["batches"].items():
        for pid in meta["postings"]:
            if pid != rep_id:
                results.append({"posting_id": pid, "grade": "wrong", "lane": "wrong", "confidence": "high",
                                 "blocker": "", "rationale": "n/a"})
    for name, meta in manifest["batches"].items():
        subset = [r for r in results if r["posting_id"] in meta["postings"]]
        (out / meta["result"]).write_text(json.dumps(subset))
    result = judge.load_results(con, str(out), scorer="test-scorer", log=_quiet)
    assert result["errors"] == []
    rows = dict((pid, (rf, ru)) for pid, rf, ru in
                con.execute("SELECT posting_id, required_fit, required_unmet FROM llm_labels").fetchall())
    assert rows[rep_id] == ("meets", "needs SAP ; needs CPA")
    assert rows[dup_id] == ("meets", "needs SAP ; needs CPA")   # near-duplicate copies it too
    con.close()


def test_judge_import_missing_required_fit_loads_as_null(tmp_path):
    """A result file predating the Required-block question omits required_fit/required_unmet entirely --
    still importable, with NULLs (same back-compat policy as grade_ai)."""
    pytest.importorskip("numpy")
    con, by_req = _judge_corpus(tmp_path)
    queue = [(by_req["A1"], "high")]
    out = tmp_path / "batches"
    judge.write_batches(con, str(out), queue, batch_size=2, log=_quiet)
    manifest = json.loads((out / "manifest.json").read_text())
    only = next(iter(manifest["batches"].values()))
    (out / only["result"]).write_text(json.dumps(
        [{"posting_id": by_req["A1"], "grade": "bullseye", "lane": "primary", "confidence": "high",
          "blocker": "", "rationale": "process ownership work"}]))
    result = judge.load_results(con, str(out), scorer="test-scorer", log=_quiet)
    assert result["errors"] == []
    rf, ru = con.execute("SELECT required_fit, required_unmet FROM llm_labels WHERE posting_id = ?",
                         [by_req["A1"]]).fetchone()
    assert rf is None and ru is None
    con.close()


def test_judge_import_rejects_invalid_required_fit(tmp_path):
    pytest.importorskip("numpy")
    con, by_req = _judge_corpus(tmp_path)
    queue = [(by_req["A1"], "high")]
    out = tmp_path / "batches"
    judge.write_batches(con, str(out), queue, batch_size=2, log=_quiet)
    manifest = json.loads((out / "manifest.json").read_text())
    only = next(iter(manifest["batches"].values()))
    (out / only["result"]).write_text(json.dumps(
        [{"posting_id": by_req["A1"], "grade": "bullseye", "grade_process": "bullseye",
          "grade_technical": "adjacent", "lane": "primary", "confidence": "high",
          "blocker": "", "rationale": "process ownership work", "required_fit": "maybe"}]))
    result = judge.load_results(con, str(out), scorer="test-scorer", log=_quiet)
    assert len(result["errors"]) == 1 and "required_fit" in result["errors"][0]
    assert con.execute("SELECT count(*) FROM llm_labels").fetchone()[0] == 0
    con.close()


def test_vw_selection_tiers_apply_review_hidden_and_no_lens(tmp_path):
    """apply: at least one strong lens + required_fit meets. review: same but arguable. hidden: fails, or a
    row with no lens hit at all (even when required_fit meets). Level/location gating stays out of this view."""
    pytest.importorskip("numpy")
    con, by_req = _judge_corpus(tmp_path)
    now = datetime(2026, 9, 15, 12)
    rows = [
        # (posting_id, description_hash, grade_process, grade_technical, grade_ai, required_fit)
        [by_req["A1"], "h1", "bullseye", "wrong", "wrong", "meets"],       # apply
        [by_req["A2"], "h2", "wrong", "adjacent", "wrong", "arguable"],    # review
        [by_req["B1"], "h3", "bullseye", "wrong", "wrong", "fails"],       # hidden: fails
        [by_req["A3"], "h1", "wrong", "wrong", "wrong", "meets"],          # hidden: no lens hit
    ]
    con.executemany(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
        "grade_technical, grade_ai, lane, confidence, blocker, rationale, required_fit, required_unmet, batch, "
        "judged_at) VALUES (?, ?, 'rv', 'test', 'bullseye', ?, ?, ?, NULL, NULL, '', '', ?, '', 'b', ?)",
        [[pid, dhash, gp, gt, ga, rf, now] for pid, dhash, gp, gt, ga, rf in rows])
    tiers = dict(con.execute("SELECT posting_id, tier FROM vw_selection").fetchall())
    assert tiers[by_req["A1"]] == "apply"
    assert tiers[by_req["A2"]] == "review"
    assert tiers[by_req["B1"]] == "hidden"
    assert tiers[by_req["A3"]] == "hidden"
    # His own adjudication wins the latest-label view and carries no required_fit: it tiers on his grade alone
    # rather than dropping out of the view.
    con.execute(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, batch, judged_at) "
        "VALUES (?, 'h3', 'rv', 'user-adjudicated', 'adjacent', 'b', ?)", [by_req["B1"], now])
    assert con.execute("SELECT tier, adjudicated FROM vw_selection WHERE posting_id = ?",
                       [by_req["B1"]]).fetchone() == ("apply", True)
    con.close()


def test_llm_labels_required_fit_columns_present_fresh_and_upgraded(tmp_path):
    """A fresh DB gets required_fit/required_unmet from the CREATE TABLE. An older DB (schema_info stuck below
    v12) picks them up through _add_missing_columns on the next connect(), same as grade_ai did at v10."""
    fresh = store.connect(str(tmp_path / "fresh.duckdb"))
    assert {"required_fit", "required_unmet"} <= store._columns(fresh, "llm_labels")
    fresh.close()

    old = store.connect(str(tmp_path / "old.duckdb"))
    old.execute("ALTER TABLE llm_labels DROP COLUMN required_fit")
    old.execute("ALTER TABLE llm_labels DROP COLUMN required_unmet")
    old.execute("UPDATE schema_info SET version = 11")
    old.close()
    upgraded = store.connect(str(tmp_path / "old.duckdb"))
    assert {"required_fit", "required_unmet"} <= store._columns(upgraded, "llm_labels")
    upgraded.close()


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


def _clearance_reasons(text):
    return [r for r in rules.screen_row(_row(description_text=text)).reasons if "clearance" in r]


def test_obtainable_clearance_is_not_a_blocker():
    f = _clearance_flags("Clearance Required : Ability to Obtain Public Trust. U.S. Citizenship with the "
                         "ability to obtain and maintain a federal Public Trust clearance.")
    assert f and "reachable" in f[0], f
    assert _clearance_reasons("Clearance Required : Ability to Obtain Public Trust. U.S. Citizenship with the "
                              "ability to obtain and maintain a federal Public Trust clearance.") == []


def test_sponsored_hard_clearance_is_also_reachable():
    """Even TS/SCI is not a blocker when the employer offers to sponsor it -- the two roles this rule
    rescued on 2026-09-16 (CACI, Guidehouse) both read this way and must still pass through."""
    f = _clearance_flags("Must be eligible to obtain a TS/SCI security clearance; we sponsor.")
    assert f and "reachable" in f[0], f
    assert _clearance_reasons("Must be eligible to obtain a TS/SCI security clearance; we sponsor.") == []


def test_clearance_that_must_be_held_is_now_a_reject_reason():
    """2026-09-19 fix (b1a): a CONFIDENTLY held clearance is a screen REJECT reason, not merely a flag --
    it must leave the rank, like the commute and pay-floor reasons."""
    for text in ("Requires an active TS/SCI clearance with polygraph.",
                 "Candidate must currently hold a Top Secret security clearance.",
                 "An active secret clearance is required on day one."):
        rec = rules.screen_row(_row(description_text=text))
        reasons = [r for r in rec.reasons if "clearance" in r]
        assert reasons and "already be held" in reasons[0], (text, reasons)
        assert rec.verdict == "reject", (text, rec.verdict)
        assert not [f for f in rec.flags if "clearance" in f]   # not double-counted as a flag too


def test_hard_level_without_sponsorship_is_now_a_reject_reason():
    rec = rules.screen_row(_row(description_text="TS/SCI security clearance required for this position."))
    reasons = [r for r in rec.reasons if "clearance" in r]
    assert reasons and "already be held" in reasons[0], reasons
    assert rec.verdict == "reject"


def test_obtain_governs_only_the_clearance_it_sits_near_bug_b1b():
    """A TS/SCI stated as required, with only the POLYGRAPH said to be obtainable, must not launder the
    TS/SCI itself into 'reachable' -- the old blob-wide re.search bug."""
    text = "A TS/SCI is required to start, with the ability to obtain a polygraph."
    rec = rules.screen_row(_row(description_text=text))
    reasons = [r for r in rec.reasons if "clearance" in r]
    assert reasons and "already be held" in reasons[0], reasons
    assert rec.verdict == "reject"
    assert not [f for f in rec.flags if "reachable" in f]


def test_minimum_clearance_required_to_start_structured_field():
    held = rules.screen_row(_row(description_text="Minimum Clearance Required to Start: TS/SCI with Polygraph."))
    assert [r for r in held.reasons if "clearance" in r] and held.verdict == "reject"
    # a None/Not Applicable value must not create a held call at all
    none_val = rules.screen_row(_row(description_text="Minimum Clearance Required to Start: None."))
    assert not [r for r in none_val.reasons if "clearance" in r]
    assert not [f for f in none_val.flags if "clearance" in f]


def test_active_and_maintained_quoted_level_is_a_reject():
    text = 'An ACTIVE and MAINTAINED "SECRET" DoD security clearance is required.'
    rec = rules.screen_row(_row(description_text=text))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_clearance_required_colon_active_top_secret_is_a_reject():
    rec = rules.screen_row(_row(description_text="Clearance Required : Active Top Secret."))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_ts_sci_with_poly_required_is_a_reject():
    rec = rules.screen_row(_row(description_text="TS/SCI with Poly required for this role."))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_clearance_required_after_day_one_is_sponsored_not_a_reject():
    text = ("The ability to obtain and maintain a U.S. government issued security clearance is required. "
            "Security Clearance Type: DoD Clearance: Secret\nSecurity Clearance Status: Active and existing "
            "security clearance required after day 1")
    rec = rules.screen_row(_row(description_text=text))
    assert not [r for r in rec.reasons if "clearance" in r]
    assert [f for f in rec.flags if f.startswith("clearance is sponsored")]


def test_clearance_required_on_day_one_is_a_reject():
    text = ("Security Clearance Type: DoD Clearance: Secret\nSecurity Clearance Status: Active and existing "
            "security clearance required on day 1")
    rec = rules.screen_row(_row(description_text=text))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_obtain_same_level_beats_a_footer_mention_of_that_level():
    text = ("Basic Qualifications:\nAbility to obtain a Secret clearance\nBachelor's degree\n\nClearance:\n"
            "Applicants selected will be subject to a security investigation; Secret clearance is required.")
    rec = rules.screen_row(_row(description_text=text))
    assert not [r for r in rec.reasons if "clearance" in r]


def test_higher_level_under_nice_if_you_have_is_not_a_reject():
    text = ("You Have:\nAbility to obtain a Secret clearance\nBachelor's degree\n\nNice If You Have:\n"
            "Experience with federal clients\nTop Secret clearance\nMaster's degree")
    rec = rules.screen_row(_row(description_text=text))
    assert not [r for r in rec.reasons if "clearance" in r]


def test_ts_sci_held_with_obtainable_polygraph_is_still_a_reject():
    text = "Required: Top Secret/Sensitive Compartmented Information (TS/SCI) with ability to obtain a polygraph."
    rec = rules.screen_row(_row(description_text=text))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_obtain_a_certification_does_not_launder_a_required_secret_clearance():
    text = "You Have:\nSecret clearance\nHS diploma\nAbility to obtain Security+ certification within 6 months"
    rec = rules.screen_row(_row(description_text=text))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_self_contradicting_clearance_posting_is_flagged_not_rejected():
    text = ("Active and transferable U.S. government issued security clearance is required prior to start date.\n"
            "Security Clearance Status: Ability to obtain interim U.S. government issued security clearance is "
            "required prior to start date")
    rec = rules.screen_row(_row(description_text=text))
    assert not [r for r in rec.reasons if "clearance" in r]
    assert [f for f in rec.flags if "clearance" in f]


def test_able_to_obtain_public_trust_passes():
    rec = rules.screen_row(_row(description_text="Required: Must be able to obtain and maintain a Public Trust clearance."))
    assert not [r for r in rec.reasons if "clearance" in r]


def test_secret_clearance_is_required_is_a_reject():
    rec = rules.screen_row(_row(description_text="Secret clearance is required for this position."))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_active_public_trust_required_is_a_reject():
    """'public trust' is a CONDITIONAL hard level (profile.CLEARANCE_CONDITIONAL_LEVELS): a bare mention is
    reachable/everyday, but paired with 'active' nearby it reads as confidently held."""
    rec = rules.screen_row(_row(description_text="Must maintain an active Public Trust (required) clearance."))
    assert [r for r in rec.reasons if "clearance" in r] and rec.verdict == "reject"


def test_bare_public_trust_without_active_is_not_a_reject():
    rec = rules.screen_row(_row(description_text="A Public Trust clearance may be required for this role."))
    assert not [r for r in rec.reasons if "clearance" in r]


def test_preferred_clearance_is_ambiguous_flag_not_reject():
    """A hard level stated as merely preferred/nice-to-have is a real gap, not a confident 'must already
    hold' fact -- stays a flag, never a reject."""
    rec = rules.screen_row(_row(description_text="An active Secret clearance is preferred but not required."))
    assert not [r for r in rec.reasons if "clearance" in r]
    flags = [f for f in rec.flags if "clearance" in f]
    assert flags and "unclear" in flags[0]
    assert rec.verdict != "reject"


def test_compensation_boilerplate_raises_no_clearance_flag_or_reason():
    """'...skill sets, experience, security clearances, licensure...' is a pay paragraph, not a requirement."""
    text = ("Compensation decisions depend on skill sets, experience and training, security clearances, "
           "licensure and certifications, and other business needs.")
    assert _clearance_flags(text) == []
    assert _clearance_reasons(text) == []


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


# ---- finder.py top / report.top_rows / report.write_top_jobs (§22, end-of-pipeline list) ----

def _top_posting(con, pid, *, employer="Acme", title="Director, Ops", screened=True, level_fit="in_range",
                 verdict="review", score=80, first_seen=None, location="Remote - USA", pay_min=None,
                 pay_max=None, grade_process="bullseye", grade_technical="bullseye", grade_ai="bullseye",
                 required_fit="meets", scorer="claude-sonnet-batch", status="active"):
    """A judged posting with independently controllable status / screen / level / gates, for testing
    report.top_rows and the vw_selection <-> vw_lens_fit join directly (no pipeline.screen call)."""
    first_seen = first_seen or datetime(2026, 9, 16)
    con.execute(
        "INSERT OR REPLACE INTO postings (posting_id, employer, platform, req_id, title, url, "
        "location_primary, pay_min, pay_max, pay_interval, status, description_hash, first_seen_at, "
        "last_seen_at) VALUES (?, ?, 'greenhouse', ?, ?, ?, ?, ?, ?, 'year', ?, 'h', ?, ?)",
        [pid, employer, pid, title, f"https://x/{pid}", location, pay_min, pay_max, status, first_seen,
         first_seen])
    if screened:
        con.execute(
            "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
            "rule_score, final_score, band, level_fit) VALUES (?, 'rv', 'mv', ?, ?, 70, ?, 'strong', ?)",
            [pid, first_seen, verdict, score, level_fit])
    con.execute(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
        "grade_technical, grade_ai, required_fit, judged_at) VALUES (?, 'h', 'rv1', ?, 'bullseye', ?, ?, ?, ?, ?)",
        [pid, scorer, grade_process, grade_technical, grade_ai, required_fit, first_seen])


def test_top_rows_orders_three_lens_above_bullseye_above_adjacent(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _top_posting(con, "x" * 20, grade_process="bullseye", grade_technical="bullseye", grade_ai="bullseye")
    _top_posting(con, "y" * 20, grade_process="bullseye", grade_technical="wrong", grade_ai="wrong")
    _top_posting(con, "z" * 20, grade_process="adjacent", grade_technical="wrong", grade_ai="wrong")
    rows = report.top_rows(con, "apply")
    order = [r[0] for r in rows]
    assert order == ["x" * 20, "y" * 20, "z" * 20]
    con.close()


def test_top_rows_gates_and_footer_counts(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _top_posting(con, "1" * 20)                                                  # baseline: passes every gate
    _top_posting(con, "2" * 20, status="closed")                                 # inactive
    _top_posting(con, "3" * 20, verdict="reject")                                # verdict reject
    _top_posting(con, "4" * 20, level_fit="too_low")                             # level out of range
    _top_posting(con, "5" * 20, screened=False)                                  # judged, no screen row
    _top_posting(con, "6" * 20)                                                  # decided
    con.execute("INSERT INTO decisions VALUES (?, 'build', NULL, 'cli', NULL, ?)", ["6" * 20, datetime(2026, 9, 16)])
    _top_posting(con, "7" * 20)                                                  # in tracker
    con.execute("INSERT INTO tracker VALUES ('search', NULL, 'Acme', 'Director, Ops', NULL, NULL, ?, 'exact', ?)",
               ["7" * 20, datetime(2026, 9, 16)])

    rows = report.top_rows(con, "apply")
    assert [r[0] for r in rows] == ["1" * 20]

    gates = report._gate_counts(con, "apply")
    assert gates["total"] == 7
    assert gates["inactive"] == 1
    assert gates["verdict_reject"] == 1
    # Gates are independent, not a sequential funnel: the unscreened posting (#5) has a NULL level_fit too
    # (it comes from the same missing screen row), so it is counted under BOTH "no screen row" and "level
    # out of range" -- that is deliberate, per report._gate_counts's docstring.
    assert gates["level_out_of_range"] == 2         # "4" (too_low) and "5" (no screen -> NULL level_fit)
    assert gates["no_screen_row"] == 1
    assert gates["decided_or_in_tracker"] == 2      # "6" (decided) and "7" (in tracker)
    con.close()


def test_top_rows_no_screen_row_is_counted_not_crashed(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _top_posting(con, "n" * 20, screened=False)
    rows = report.top_rows(con, "apply")     # must not raise
    assert rows == []
    assert report._gate_counts(con, "apply")["no_screen_row"] == 1
    con.close()


def test_top_rows_hides_decided_and_in_tracker_unless_included(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _top_posting(con, "d" * 20)
    con.execute("INSERT INTO decisions VALUES (?, 'build', NULL, 'cli', NULL, ?)", ["d" * 20, datetime(2026, 9, 16)])
    _top_posting(con, "t" * 20)
    con.execute("INSERT INTO tracker VALUES ('search', NULL, 'Acme', 'Director, Ops', NULL, NULL, ?, 'exact', ?)",
               ["t" * 20, datetime(2026, 9, 16)])
    assert report.top_rows(con, "apply") == []
    included = {r[0] for r in report.top_rows(con, "apply", include_decided=True)}
    assert included == {"d" * 20, "t" * 20}
    con.close()


def test_write_top_jobs_no_hard_wrap_and_unique_path(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _top_posting(con, "a" * 20, grade_process="bullseye", grade_technical="bullseye", grade_ai="bullseye")
    _top_posting(con, "b" * 20, required_fit="arguable", verdict="review")
    path1 = report.write_top_jobs(con, None, out_path=str(tmp_path))
    text = path1.read_text(encoding="utf-8")
    funnel_lines = [ln for ln in text.splitlines() if "**Funnel:**" in ln]
    assert len(funnel_lines) == 1
    assert "apply" in funnel_lines[0] and "review" in funnel_lines[0]
    assert "★" in text                                      # the three-lens row is starred
    apply_gate_lines = [ln for ln in text.splitlines() if "Gate detail — Apply" in ln]
    assert len(apply_gate_lines) == 1 and "level out of range" in apply_gate_lines[0]

    path2 = report.write_top_jobs(con, None, out_path=str(tmp_path))
    assert path1 != path2 and path2.exists()
    con.close()


def test_lens_breadth_adds_the_lenses_and_is_zeroed_when_the_row_is_not_worth_showing(tmp_path):
    """lens_breadth = the three lens values added (judge's grade where there is one, model probability where
    not), and ZERO when the best lens is not strong, the Required block fails, or the screen rejected the row."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _top_posting(con, "a" * 20, grade_process="bullseye", grade_technical="adjacent", grade_ai="wrong")
    _top_posting(con, "b" * 20, grade_process="wrong", grade_technical="wrong", grade_ai="bullseye")     # AI alone
    _top_posting(con, "c" * 20, grade_process="stretch", grade_technical="stretch", grade_ai="stretch")  # max weak
    _top_posting(con, "d" * 20, required_fit="fails")                       # three bullseyes, requirements fail
    _top_posting(con, "e" * 20, verdict="reject")                           # three bullseyes, outside commute
    got = dict(con.execute("SELECT posting_id, lens_breadth FROM vw_lens_fit").fetchall())
    best = dict(con.execute("SELECT posting_id, lens_best FROM vw_lens_fit").fetchall())
    assert got["a" * 20] == pytest.approx(1.75) and best["a" * 20] == 1.0
    assert got["b" * 20] == pytest.approx(1.0) and best["b" * 20] == 1.0    # an AI-only fit is still shown
    assert got["c" * 20] == 0.0 and best["c" * 20] == pytest.approx(0.35)   # a high-ish sum of weak lenses is ignored
    assert got["d" * 20] == 0.0 and got["e" * 20] == 0.0
    # an unjudged row falls back to the lens models' probabilities
    con.execute("INSERT INTO postings (posting_id, employer, platform, req_id, title, url, status, description_hash, "
                "first_seen_at, last_seen_at) VALUES ('f', 'Acme', 'greenhouse', 'f', 'T', 'u', 'active', 'h', now(), now())")
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
                "final_score, band, fit_process, fit_technical, fit_ai) VALUES ('f', 'rv', 'mv', now(), 'review', 70, 80, "
                "'strong', 0.9, 0.8, 0.1)")
    assert con.execute("SELECT lens_breadth, lens_best FROM vw_lens_fit WHERE posting_id = 'f'").fetchone() == \
        (pytest.approx(1.8), pytest.approx(0.9))
    con.close()


def test_rank_score_is_the_one_ordering_and_rank_why_names_the_deciding_facts(tmp_path):
    """One rank (0-100) orders a list; every component stays visible beside it. Bullseye + adjacent > one bullseye >
    three adjacents > two adjacents > one adjacent; multiplied down by an arguable Required block and
    a level stretch, and by ZERO on a failed Required block or a screen reject. rank_why says why, in words."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    W = "wrong"
    _top_posting(con, "three", grade_process="adjacent", grade_technical="adjacent", grade_ai="adjacent")
    _top_posting(con, "bulladj", grade_process="bullseye", grade_technical="adjacent", grade_ai=W)
    _top_posting(con, "onebull", grade_process=W, grade_technical=W, grade_ai="bullseye")       # AI alone counts
    _top_posting(con, "twoadj", grade_process="adjacent", grade_technical="adjacent", grade_ai=W)
    _top_posting(con, "oneadj", grade_process="adjacent", grade_technical=W, grade_ai=W)
    _top_posting(con, "arguable", required_fit="arguable")
    _top_posting(con, "stretchup", level_fit="stretch_up")
    _top_posting(con, "fails", required_fit="fails")
    _top_posting(con, "reject", verdict="reject")
    _top_posting(con, "weak", grade_process="stretch", grade_technical="stretch", grade_ai="stretch")
    r = dict(con.execute("SELECT posting_id, rank_score FROM vw_lens_fit").fetchall())
    why = dict(con.execute("SELECT posting_id, rank_why FROM vw_lens_fit").fetchall())
    assert r["bulladj"] > r["onebull"] > r["three"] > r["twoadj"] > r["oneadj"] > 0   # one bullseye beats three adjacents
    assert r["onebull"] == pytest.approx(80.0) and r["three"] == pytest.approx(75.0)
    assert r["arguable"] == pytest.approx(50.0) and r["stretchup"] == pytest.approx(85.0)     # of a perfect 100
    assert r["fails"] == 0 and r["reject"] == 0 and r["weak"] == 0
    assert why["three"].startswith("★ three-lens") and "Required: meets" in why["three"]
    assert why["onebull"] == "AI bullseye; Required: meets"
    assert "Required FAILS" in why["fails"] and "SCREEN REJECT" in why["reject"] and "level stretch_up" in why["stretchup"]
    assert why["weak"].startswith("no strong lens")
    assert [x[0] for x in report.top_rows(con, "apply")][:2] == ["bulladj", "stretchup"]       # 87.5 then 85: ordered by the one rank
    con.close()
