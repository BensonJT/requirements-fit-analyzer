"""2026-10-20.1: a candidate-portrait heading ("Who you are") is Required only when the posting has no other
Required heading (splitter, requirements.SOFT_PERSON_HEADINGS)."""
from backend.finder import requirements

CAPITAL_ONE_SHAPE = (
    "About the Team\n"
    "We run the operating rhythm of the HR leadership team.\n"
    "Who you are:\n"
    "A Strategic Problem Solver:\n"
    "You can take a messy, ambiguous problem and break it down into a structured workplan.\n"
    "Team Player:\n"
    "You enjoy working with diverse people and driving the team toward a common goal.\n"
    "You have 5+ years of experience running executive operating cadences.\n"
    "Basic Qualifications:\n"
    "Bachelor's Degree or Military experience\n"
    "At least 4 years of experience in Strategic Operations, Project Management or Management Consulting\n"
    "Preferred Qualifications:\n"
    "Exceptional writing and visual storytelling skills\n"
)

PORTRAIT_ONLY = (
    "What you'll do\n"
    "Lead the quarterly business review and own the KPI pack for the region.\n"
    "Who you are\n"
    "You have led process improvement programs across several business units.\n"
    "You are comfortable presenting to and influencing senior leaders.\n"
)

HARD_WORDED = (
    "Responsibilities\n"
    "Coordinate cross-functional workstreams for the program office.\n"
    "Top skills or competencies to be successful:\n"
    "Strong project management and organizational skills with attention to detail.\n"
    "Basic Qualifications:\n"
    "Bachelor's degree in business or a related field.\n"
)


def _sections(text):
    return {u.text: u.section for u in requirements.split_requirements(text) if u.klass == "work"}


def test_portrait_lines_are_preferred_beside_an_explicit_required_block():
    got = _sections(CAPITAL_ONE_SHAPE)
    assert got["You can take a messy, ambiguous problem and break it down into a structured workplan."] == "preferred"
    assert got["You enjoy working with diverse people and driving the team toward a common goal."] == "preferred"
    assert got["Bachelor's Degree or Military experience"] == "required"
    years = [t for t in got if "executive operating cadences" in t]
    assert years and got[years[0]] == "required"            # a years line under the portrait stays a gate


def test_a_portrait_heading_still_carries_the_requirements_when_it_is_the_only_one():
    got = _sections(PORTRAIT_ONLY)
    assert got["You have led process improvement programs across several business units."] == "required"


def test_a_heading_that_names_skills_or_needs_stays_hard():
    got = _sections(HARD_WORDED)
    assert got["Strong project management and organizational skills with attention to detail."] == "required"
    assert not requirements._soft_person_heading("Here is what you need to be successful:")
    assert not requirements._soft_person_heading("What do you need to be successful?")
    assert requirements._soft_person_heading("Who You Are:")
    assert requirements._soft_person_heading("What We're Looking For")
