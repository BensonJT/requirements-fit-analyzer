"""Unit tests for level_fit_rule (sec 20.2) and the sec 20.3 remote tags -- Agent A's files only.

Every test runs against the neutral example profile (backend/profile_local.example.py), never the
personal values in a local profile_local.py. Follows the fixture conventions in tests/test_finder.py
(EXAMPLE dict + autouse neutral_profile fixture, the _row() builder) without importing that module,
since tests/ is not a package.
"""
import os
import runpy
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend import screen as S  # noqa: E402
from backend.finder import rules  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


def _row(**kw):
    row = dict(posting_id="a" * 20, employer="Acme", platform="greenhouse", req_id="R1",
               title="Director, Operational Excellence", url="https://x/R1", location_primary="Remote - USA",
               locations=None, country=None, workplace_type="remote", employment_type="full_time", job_level=None,
               pay_min=None, pay_max=None, pay_interval=None, posted_at=None, description_text="",
               first_seen_at=None, description_fetched_at=None)
    row.update(kw)
    return row


# ---------------------------------------------------------------- level_fit_rule, one test per branch
def test_out_of_reach_title_term():
    _, _, notes = rules.level_fit_rule("Senior Director, Process Excellence", "", None)
    assert notes["level_fit"] == "out_of_reach"
    assert any(h.startswith("title: senior director") for h in notes["level_fit_hits"])


def test_director_plus_pnl_is_out_of_reach_two_scope_hits():
    text = "You will own the P&L for the business unit and drive results."
    _, _, notes = rules.level_fit_rule("Director, Operations", text, None)
    assert notes["level_fit"] == "out_of_reach"
    assert "p&l" in notes["level_fit_hits"]


def test_director_alone_is_stretch_up():
    _, _, notes = rules.level_fit_rule("Director, Process Excellence", "", None)
    assert notes["level_fit"] == "stretch_up"


def test_managerial_years_over_limit_is_stretch_up():
    # Example profile: MAX 3, OUT 5; 4 sits between them -- one scope hit, no title stretch.
    text = "Requires 4+ years of people management experience leading a team."
    _, _, notes = rules.level_fit_rule("Senior Manager, Operations", text, None)
    assert notes["level_fit"] == "stretch_up"
    assert any(h.startswith("managerial years 5 > 3") for h in notes["level_fit_hits"])


def test_principal_with_nothing_else_is_in_range():
    _, _, notes = rules.level_fit_rule("Principal Consultant", "", None)
    assert notes["level_fit"] == "in_range"


def test_low_years_analyst_with_band_at_floor_is_in_range():
    text = "Requires 3+ years of experience in data analysis."
    _, _, notes = rules.level_fit_rule("Process Analyst", text, 95_000)   # COMP_FLOOR is 90,000
    assert notes["level_fit"] == "in_range"


def test_low_years_analyst_with_no_band_is_too_low():
    text = "Requires 3+ years of experience in data analysis."
    _, _, notes = rules.level_fit_rule("Process Analyst", text, None)
    assert notes["level_fit"] == "too_low"


def test_intern_is_too_low():
    _, _, notes = rules.level_fit_rule("Operations Intern", "", None)
    assert notes["level_fit"] == "too_low"


def test_bare_title_no_signals_is_unknown():
    _, _, notes = rules.level_fit_rule("Operations Coordinator", "", None)
    assert notes["level_fit"] == "unknown"


def test_caci_shape_support_a_team_is_no_scope_hit():
    text = "Support a team of 250+ professionals on this program."
    _, _, notes = rules.level_fit_rule("Business Process Consultant", text, None,
                                       rules.direct_reports_rule("", text)[2])
    assert not any("team headcount" in h or "direct reports" in h for h in notes["level_fit_hits"])
    assert notes["level_fit"] != "out_of_reach" and notes["level_fit"] != "stretch_up"


def test_twelve_direct_reports_is_out_of_reach():
    text = "This role carries 12 direct reports."
    reports_notes = rules.direct_reports_rule("", text)[2]
    _, _, notes = rules.level_fit_rule("Operations Manager", text, None, reports_notes)
    assert notes["level_fit"] == "out_of_reach"
    assert any(h.startswith("direct reports 12") for h in notes["level_fit_hits"])


def test_level_fit_rule_never_a_reason_or_a_flag():
    reasons, flags, notes = rules.level_fit_rule("Senior Director, Process Excellence", "", None)
    assert reasons == [] and flags == []
    assert notes["level_fit"] == "out_of_reach"


# ---------------------------------------------------------------- direct_reports_rule split (A2)
def test_direct_reports_rule_splits_notes_and_keeps_reports_unchanged():
    reasons, flags, notes = rules.direct_reports_rule("", "This role carries 12 direct reports.")
    assert notes["reports"] == 12 and notes["direct_reports"] == 12 and "team_headcount" not in notes

    reasons, flags, notes = rules.direct_reports_rule("", "Support a team of 250+ professionals on this program.")
    assert notes["reports"] == 250 and notes["team_headcount"] == 250 and notes["team_headcount_led"] is False

    reasons, flags, notes = rules.direct_reports_rule("", "You will lead a team of 12 analysts on this initiative.")
    assert notes["team_headcount"] == 12 and notes["team_headcount_led"] is True


# ---------------------------------------------------------------- screen_row integration
def test_screen_row_writes_level_fit_and_leaves_verdict_and_score_untouched():
    row_a = _row(title="Senior Director, Process Excellence", description_text="")
    row_b = _row(title="Process Excellence Consultant", description_text="")
    rec_a = rules.screen_row(row_a)
    rec_b = rules.screen_row(row_b)
    assert rec_a.notes["level_fit"] == "out_of_reach"
    assert rec_b.notes["level_fit"] == "in_range"
    # level_fit_rule always returns ([], [], ...) -- it never contributes a reason or a flag, so
    # rule_score must still come out exactly as profile_score(components) computes it independently,
    # with nothing about level_fit folded in, whichever level_fit value landed.
    for rec in (rec_a, rec_b):
        assert not any("level_fit" in r for r in rec.reasons) and not any("level_fit" in f for f in rec.flags)
        assert rec.rule_score == rules.profile_score(rec.notes["components"])
        assert rec.verdict == ("reject" if rec.reasons else ("review" if rec.flags else "candidate"))


# ---------------------------------------------------------------- remote tags (sec 20.3)
def test_us_offsite_location_is_remote():
    lst = S.Listing(source="ats", search_pass="", title="Process Excellence Consultant", company="Acme",
                    location="US Off-Site", url="", extra={})
    assert S.is_remote(lst)
    # hyphen optional, any case, and also honored from the locations list rather than the primary location
    lst2 = S.Listing(source="ats", search_pass="", title="Process Excellence Consultant", company="Acme",
                     location="Austin, TX", url="", extra={"workplace_type": "onsite"},
                     locations=["US OffSite"])
    assert S.is_remote(lst2)


def test_li_remote_tag_in_jd_footer_is_remote():
    lst = S.Listing(source="ats", search_pass="", title="Process Excellence Consultant", company="Acme",
                    location="Austin, TX", url="", extra={},
                    description="Own process design end to end.\n\n#LI-Remote")
    assert S.is_remote(lst)


def test_li_hybrid_and_li_remote_together_is_remote():
    lst = S.Listing(source="ats", search_pass="", title="Process Excellence Consultant", company="Acme",
                    location="Austin, TX", url="", extra={"workplace_type": "hybrid"},
                    description="Own process design end to end.\n\n#LI-Hybrid #LI-Remote")
    assert S.is_remote(lst)


def test_li_hybrid_alone_with_ats_hybrid_flag_and_specific_city_is_not_remote():
    lst = S.Listing(source="ats", search_pass="", title="Process Excellence Consultant", company="Acme",
                    location="Austin, TX", url="", extra={"workplace_type": "hybrid"},
                    description="Own process design end to end.\n\n#LI-Hybrid")
    assert not S.is_remote(lst)


# ---------------------------------------------------------------- Fable audit additions (2026-09-17)
def test_project_management_years_are_function_not_people_scope():
    """'<noun> management experience' is the target lane's normal phrasing and never a managerial-years hit."""
    text = ("Required\n- 10+ years of project management and process improvement experience.\n"
            "- 8+ years of change management experience.\n")
    assert rules._managerial_years(text) is None
    _, _, notes = rules.level_fit_rule("Senior Manager, Process Excellence", text, None, {})
    assert notes["level_fit"] == "in_range", notes


def test_people_management_years_still_count():
    text = "Required\n- 5+ years of experience managing a team of analysts.\n"
    assert rules._managerial_years(text) == 5
    text2 = "Required\n- 6+ years of people management experience.\n"
    assert rules._managerial_years(text2) == 6


def test_chief_of_staff_is_not_c_suite():
    _, _, notes = rules.level_fit_rule("Chief of Staff, Enterprise Operations", "Required\n- 8+ years of experience.", None, {})
    assert notes["level_fit"] == "stretch_up", notes   # a deputy/director-band seat, confirmed by the user
    _, _, notes = rules.level_fit_rule("Chief Operating Officer", "Required\n- 8+ years of experience.", None, {})
    assert notes["level_fit"] == "out_of_reach"


def test_managerial_years_at_or_over_the_out_limit_is_out_of_reach_alone():
    text = "Required\n- 5+ years of experience managing a team of analysts.\n"
    _, _, notes = rules.level_fit_rule("Senior Manager, Operations", text, None, {})
    assert notes["level_fit"] == "out_of_reach" and any("managerial years 5 >=" in h for h in notes["level_fit_hits"])
    text = "Required\n- 4 years of people management experience.\n"
    assert rules.level_fit_rule("Senior Manager, Operations", text, None, {})[2]["level_fit"] == "stretch_up"
