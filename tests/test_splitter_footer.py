"""Splitter 2026-10-30.1: employer footers drop by content wherever they sit, and intro "You will" sentences are
responsibilities. The JD below has the shape that failed: nothing closes the last qualification heading, so every
footer line was a Preferred requirement, and the duties lived in the intro paragraph."""
from backend.finder.requirements import FOOTER_LINE, section_lines, split_requirements

JD = """Sr. Manager, Project Manager - Small Business Bank

Acme Bank, a Fortune 500 company and one of the nation's top 10 banks, offers a broad spectrum of financial products.

As a Senior Manager, Project Manager on our Small Business Bank team, you will drive strategy and execute on high priority projects for the company. You will help define process improvements including tool creation and evolution, achieve buy-in, and drive execution across our business. You will monitor data trends to measure effectiveness and install necessary benchmarks or thresholds to ensure data integrity.

General Responsibilities:
- Leading moderate to large complex projects and programs by providing strategic direction to projects, peers, and direct reports
- Managing and leading a team of associates

Basic Qualifications:
- At least 7 years of Project Management experience
- At least 3 years of Process Management experience

Preferred Qualifications:
- PMP, Lean, Agile or Six Sigma certification
- At least 2 years of experience in Compliance or Risk Management

At this time, Acme Bank will not sponsor a new applicant for employment authorization for this position.

This role is hybrid meaning associates spend 3 days per week in-person at one of our offices listed on this job posting. Fridays are enterprise- wide virtual work days. Monday, Tuesday, Wednesday and Thursday are in-person days for associates to spend time together.

The minimum and maximum full-time annual salaries for this role are listed below, by location. Please note that this salary information is solely for candidates hired to perform work within one of these locations. Salaries for part-time roles will be prorated based upon the agreed upon number of hours to be regularly worked.

Candidates hired to work in other locations will be subject to the pay range associated with that location.

This role is also eligible to earn performance based incentive compensation, which may include cash bonus(es) and/or long term incentives (LTI). Incentives could be discretionary or non discretionary depending on the plan.

This role is expected to accept applications for a minimum of 5 business days.

No agencies please. Acme Bank is an equal opportunity employer committed to non-discrimination. Acme Bank promotes a drug-free workplace. Acme Bank will consider for employment qualified applicants with a criminal history in a manner consistent with applicable laws regarding criminal background inquiries.

If you have visited our website in search of information on employment opportunities or to apply for a position, and you require an accommodation, please contact Acme Recruiting.

For technical support or questions about Acme Bank's recruiting process, please send an email to careers@example.com

Acme Bank does not provide, endorse nor guarantee and is not liable for third-party products, services, educational tools or other information available through this site.

Acme Bank Financial is made up of several different entities. Please note that any position posted in Canada is for Acme Bank Canada.
"""

FOOTER_WORDS = ("sponsor", "hybrid", "salar", "prorated", "incentive", "agencies", "equal opportunity", "criminal",
                "accommodation", "recruiting process", "endorse", "entities", "virtual work days", "in-person days",
                "pay range", "minimum of 5")


def _units():
    return split_requirements(JD)


def test_footer_after_last_heading_is_not_a_requirement():
    for u in _units():
        assert not any(w in u.source.lower() for w in FOOTER_WORDS), u.source


def test_real_qualifications_keep_their_sections():
    by_source = {u.source: u.section for u in _units()}
    assert by_source["At least 7 years of Project Management experience"] == "required"
    assert by_source["At least 3 years of Process Management experience"] == "required"
    assert by_source["At least 2 years of experience in Compliance or Risk Management"] == "preferred"
    assert by_source["PMP, Lean, Agile or Six Sigma certification"] == "preferred"


def test_intro_you_will_sentences_are_responsibilities():
    sections = {u.source: u.section for u in _units()}
    duty = next(s for s in sections if s.startswith("You will help define process improvements"))
    assert sections[duty] == "responsibility"
    assert sections[next(s for s in sections if s.startswith("As a Senior Manager"))] == "responsibility"
    blurb = next((s for s in sections if s.startswith("Acme Bank, a Fortune 500")), None)
    assert blurb is None or sections[blurb] == "intro"


def test_section_lines_drop_footer_and_keep_intro_duties():
    lines = section_lines(JD)
    text = "\n".join(l for _, l, _ in lines).lower()
    assert not any(w in text for w in ("drug-free", "several different entities", "salaries for part-time",
                                       "not liable for third-party"))
    duty = next(s for s, l, _ in lines if "tool creation and evolution" in l)
    assert duty == "responsibility"


def test_statement_forms_only_real_duties_survive():
    kept = [
        "Experience managing reasonable accommodation requests and leave programs",
        "Design incentive compensation programs for the sales organization",
        "5+ years of experience administering pay range structures and salary surveys",
        "Partner with Legal on equal employment opportunity reporting and affirmative action plans",
        "Improve the recruiting process for hourly hiring",
    ]
    # The footer rule must not match a duty. (Two of these lines are cut upstream by labels.strip_boilerplate's
    # BOILERPLATE_PATTERNS before the splitter sees them: a known, separate gap, unchanged by 2026-10-30.1.)
    for line in kept:
        assert not FOOTER_LINE.search(line), line
    dropped = [
        "Microsoft is an equal opportunity employer.",
        "Business Management IC4 - The typical base pay range for this role across the U.S. is USD $97,600 - $188,400 per year.",
        "If you need assistance or an accommodation due to a disability, you may request an accommodation.",
    ]
    for line in dropped:
        assert FOOTER_LINE.search(line), line


def test_you_will_join_and_report_stay_intro():
    jd = ("You will join a collaborative team of analysts. You'll report to the Director of Operations.\n\n"
          "Responsibilities:\n- Own the operating cadence for the program office and its reporting\n")
    sections = {u.source: u.section for u in split_requirements(jd)}
    assert all(sec != "responsibility" for src, sec in sections.items() if src.startswith(("You will join", "You'll report")))


def test_pay_paragraph_glued_to_a_requirement_is_cut_not_the_requirement():
    jd = ("Preferred Qualifications:\n- Certification in relevant technologies or disciplines: PL-600 Power Platform "
          "Solution Architect Expert, or AI-102 Azure AI Engineer Associate, or\n"
          "Solution Engineering IC5 - The typical base pay range for this role across the U.S. is USD $130,900 - "
          "$251,900 per year.\n")
    sources = [u.source for u in split_requirements(jd)]
    assert any(s.startswith("Certification in relevant technologies") and "AI-102" in s and "pay" not in s
               for s in sources), sources


def test_a_clearance_sponsorship_notice_is_kept():
    for line in ("Please note, this position will not sponsor a clearance or upgrade",
                 "GE will not sponsor this requirement."):
        assert not FOOTER_LINE.search(line), line
