"""2026-09-30.1: USAJobs paragraph-dump announcements (splitter Layer 1)."""
from backend.finder import requirements, judge2

USAJOBS_JD = (
    "Duties\n"
    "Independently conducts studies of complex problems. Selects and applies statistics appropriate for solving "
    "operations research problems.\n"
    "Requirements\n"
    "Conditions of Employment\n"
    "Must be a U.S. Citizen or National. Federal experience is not required.\n"
    "Qualifications\n"
    "To qualify for this position, applicants must meet all requirements by the closing date of this announcement. "
    "Time-In-Grade Requirement: Applicants who are current Federal employees and have held a GS grade any time in the "
    "past 52 weeks must also meet time-in-grade requirements. An SF-50 that shows your time-in-grade eligibility must "
    "be submitted with your application materials. SPECIALIZED EXPERIENCE GS-14: In addition to basic requirements, "
    "to be eligible for this position, you must have one (1) year of specialized experience at a level of difficulty "
    "and responsibility equivalent to the GS-13 grade level in the Federal service.Specialized experience includes: "
    "Experience conducting complex operations research studies to evaluate operational problems and recommend "
    "solutions. Experience applying mathematical and statistical methods to analyze operational data and evaluate "
    "program performance. Specialized experience would be demonstrated by 1) Directing enterprise IT and network "
    "operations programs across an organization. 2) Leading process improvement initiatives that changed how "
    "several teams work. You must show proof the education credentials have been deemed to be at least equivalent "
    "to that gained in conventional U.S. education program. STANDARD POSITION DESCRIPTIONS (SPD): PD95938 Visit the "
    "IRS SPD Library to access the position descriptions.\n"
)


def test_usajobs_specialized_experience_lands_in_required():
    units = requirements.split_requirements(USAJOBS_JD)
    required = [u.source for u in units if u.section == "required"]
    assert any(t.startswith("Experience conducting complex operations research") for t in required)
    assert any(t.startswith("Experience applying mathematical and statistical") for t in required)
    # inline "1) ... 2) ..." items become their own Required units, numbering stripped
    assert any(t.startswith("Directing enterprise IT and network operations") for t in required)
    assert any(t.startswith("Leading process improvement initiatives") for t in required)
    # the opener sentence itself is an eligibility statement, not a claim
    assert not any("level of difficulty and responsibility" in t for t in required)


def test_usajobs_application_mechanics_are_dropped_piece_by_piece():
    units = requirements.split_requirements(USAJOBS_JD)
    texts = [u.source for u in units]
    for noise in ("Time-In-Grade", "SF-50", "education credentials have been deemed", "STANDARD POSITION DESCRIPTIONS",
                  "closing date of this announcement"):
        assert not any(noise.lower() in t.lower() for t in texts), noise
    # the duties paragraph is untouched
    assert any(u.section == "responsibility" and u.source.startswith("Independently conducts") for u in units)


def test_judge_required_lines_follow_the_splitter():
    lines = judge2.required_lines(USAJOBS_JD)
    assert any(l.startswith("Experience conducting complex operations research") for l in lines)
    assert not any("education credentials" in l for l in lines)
    assert judge2.DERIVE_NOISE.search("You must show proof the education credentials have been deemed to be at least equivalent")
    assert judge2.DERIVE_NOISE.search("STANDARD POSITION DESCRIPTIONS (SPD): PD95938")


def test_short_lines_keep_line_level_notice_behaviour():
    # a normal-length line carrying a NOTICE phrase is still dropped whole (unchanged behaviour)
    jd = "Requirements\n5+ years of process improvement experience.\nBeware of scams asking for money transfers.\n"
    units = requirements.split_requirements(jd)
    assert not any("scams" in u.source for u in units)
    assert any(u.section == "required" and "process improvement" in u.source.lower() for u in units)
