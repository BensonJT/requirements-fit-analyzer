"""Splitter 2026-11-01.1 (docs/SPLITTER_STUDY_20261101.md): one test per pattern the corpus study found, each shaped
on the real posting named in its docstring."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.finder import judge2, requirements  # noqa: E402


def _sec(text):
    """{source line: section} for every unit."""
    return {(u.source or u.text): u.section for u in requirements.split_requirements(text)}


def _find(got, fragment):
    hits = [s for t, s in got.items() if fragment in t]
    assert hits, f"{fragment!r} not found in {list(got)}"
    return hits[0]


def test_first_preferred_item_naming_experience_is_not_a_required_heading():
    """Deloitte 2839e9cc: "Azure DevOps experience" re-opened Required for the whole Preferred list."""
    got = _sec("Required:\n- Bachelor's degree in computer science or a related field.\n"
               "Preferred:\nAzure DevOps experience\n"
               "Excellent analytical and problem-solving skills\n"
               "Strong verbal and written communication skills; strong listening skills\n")
    assert _find(got, "Excellent analytical and problem-solving") == "preferred"
    assert _find(got, "Bachelor's degree") == "required"


def test_topic_heading_after_preferred_stays_preferred_but_degree_lines_stay_required():
    """Cooley "Competencies" / Fannie Mae's keyword-dump "Qualifications" after a Preferred section."""
    got = _sec("Minimum Qualifications\n- 5+ years of experience in legal operations management.\n"
               "Preferred Qualifications\n- Experience with law firm billing systems and reporting.\n"
               "Competencies\n- Ability to work independently in a fast-paced environment for several supervisors.\n"
               "- Bachelor's degree in business administration or a related field.\n")
    assert _find(got, "Ability to work independently") == "preferred"
    assert _find(got, "Bachelor's degree in business") == "required"
    assert _find(got, "legal operations management") == "required"


def test_topic_heading_over_duties_stays_responsibilities():
    """ServiceNow 4c0d5079 "Content and knowledge amplification"; GitLab "Requirements Gathering"."""
    text = ("Key Responsibilities\n- Define the community operating model, charter, and success metrics.\n"
            "Content and Knowledge Amplification\n"
            "- Coordinate with the Content Standardization Lead to ensure content meets quality standards.\n"
            "- Contribute to the repository and discovery mechanisms for community-sourced learning assets.\n"
            "Requirements Gathering & Translation\n"
            "- Work directly with finance and operations teams to understand process requirements.\n"
            "- Translate business needs into clear user stories and acceptance criteria for delivery.\n"
            "Required Qualifications\n- 10+ years of experience in enablement or program management.\n")
    got = _sec(text)
    assert _find(got, "Coordinate with the Content Standardization Lead") == "responsibility"
    assert _find(got, "Translate business needs into clear user stories") == "responsibility"
    assert _find(got, "10+ years of experience in enablement") == "required"


def test_a_heading_that_is_itself_a_qualification_opens_required_over_duty_lines():
    """AHEAD 704604a3: "5+ years of relevant experience in:" followed by duty-shaped experience lines."""
    got = _sec("Roles and Responsibilities:\n- Lead customer workshops on platform engineering practices.\n"
               "5+ years of relevant experience in:\n"
               "- Own every engagement end to end as Engagement Lead, from kickoff through executive readout.\n"
               "- Own scope and SoW compliance: set success criteria and manage change control.\n")
    assert _find(got, "Own every engagement end to end") == "required"


def test_any_of_list_is_one_required_line():
    """Deloitte 2839e9cc: 22 accreditations, any one of which satisfies the gate, were 22 Required lines."""
    text = ("Required:\n- Bachelor's degree in computer science or a related field.\n"
            "One of the following active accreditations obtained:\n"
            "Licensed CPA in state of practice/primary office if eligible to sit for the CPA\n"
            "If not CPA eligible:\nLicensed attorney\nEnrolled Agent\nCertified SAFe® Agilist\n"
            "Project Management Professional (PMP)\n"
            "Preferred:\n- Experience with Azure DevOps boards and pipelines.\n")
    req = judge2.section_lines(text, "required")
    joined = [line for line in req if line.startswith("One of the following active accreditations obtained:")]
    assert len(joined) == 1 and "Licensed attorney" in joined[0] and "Project Management Professional" in joined[0]
    assert not any(line.startswith("Licensed attorney") for line in req)
    assert "If not CPA eligible" not in joined[0]


def test_bonus_benefit_headings_drop_but_bonus_points_is_preferred():
    """Benefit headings opened Preferred through "bonus" (221 postings)."""
    assert requirements._heading_kind("Peer Bonus Awards") == "drop"
    assert requirements._heading_kind("Annual Bonus Eligibility") == "drop"
    assert requirements._heading_kind("Bonus Points For:") == "preferred"
    assert requirements._heading_kind("Bonus:") == "preferred"


def test_team_company_and_tag_headings_drop():
    for heading in ("The Team", "Company Overview", "What We Believe", "Work Shift", "Employee Referral Program",
                    "Parental Leave", "#LI-Remote", "Educational Assistance/Tuition Reimbursement",
                    "Physical Demand Requirements"):
        assert requirements._heading_kind(heading) == "drop", heading


def test_job_description_heading_resets_a_metadata_required_block():
    """GDIT d3559ae9: "Public Trust/Other Required:" opened Required and the whole description stayed there."""
    text = ("Clearance Level Must Currently Possess:\nNone\nPublic Trust/Other Required:\nNone\n"
            "Job Description:\nGDIT is your place to make meaningful contributions to challenging projects.\n"
            "The Business Intelligence (BI) Architect will execute the following responsibilities:\n"
            "- Design the end-to-end BI and data architecture and integrate configurable dashboards.\n"
            "Required Skills:\n- Advanced SQL skills and experience with analytical data models.\n")
    got = _sec(text)
    assert _find(got, "meaningful contributions") in ("intro", "body")
    assert _find(got, "Design the end-to-end BI") == "responsibility"
    assert _find(got, "Advanced SQL skills") == "required"


def test_new_duty_openers():
    for heading in ("Work you'll do", "Your Mission", "Essential Job Functions", "What you'll work on",
                    "How You'll Make a Difference"):
        assert requirements._heading_kind(heading) == "responsibility", heading
    assert requirements._heading_kind("The successful candidate will have:") == "required"


def test_new_required_and_preferred_openers():
    assert requirements._heading_kind("Minimum Requirement") == "required"
    assert requirements._heading_kind("License/Certifications") == "required"
    assert requirements._heading_kind("What will make you stand out:") == "preferred"
    assert requirements._heading_kind("What will help you stand out (Nonessential Skills/Nice to Haves)") == "preferred"
    assert requirements._heading_kind("While not required, we would be especially excited if you bring:") == "preferred"


def test_degree_line_is_never_a_heading():
    """Fannie Mae 1a1fb6dc: "Bachelor's Level Degree (Required)" was read as a heading."""
    assert requirements._heading_kind("Bachelor's Level Degree (Required)") is None


def test_line_states_preferred_at_the_start_or_mid_line():
    s = requirements.inline_section
    assert s("Desired experience with C, C++, and/or Rust.") == "preferred"
    assert s("Hospitality experience is a plus, in particular for technology platforms.") == "preferred"
    assert s("JD degree desirable, but not required.") == "preferred"
    assert s("Familiarity with Anaplan or other EPM technologies (desirable)") == "preferred"
    assert s("5+ years of pre-sales experience, ideally with Open Source technologies.") is None   # soft qualifier
    assert s("Familiarity with HRIS platforms (ADP is preferred, but not required).") is None       # parenthetical
    assert s("Must hold an active Secret clearance.") == "required"


def test_as_required_is_not_a_status():
    """McKesson 7816d218: "...support other strategic projects as required." was filed as Required."""
    assert requirements.inline_section("In addition, this role will support other strategic projects as required.") is None
    got = _sec("Responsibilities\n- Support other strategic projects across the finance team as required.\n")
    assert _find(got, "strategic projects") == "responsibility"


def test_required_and_preferred_in_one_line_split():
    got = _sec("Education\n- Bachelor's degree required, Master's degree in finance preferred\n")
    assert _find(got, "Bachelor's degree required") == "required"
    assert _find(got, "Master's degree in finance preferred") == "preferred"


def test_sentence_case_labels_in_duties_are_not_lines():
    """ServiceNow 4c0d5079: "Measurement and continuous improvement" counted as a duty line."""
    text = ("Key Responsibilities\nMeasurement and continuous improvement\n"
            "Define and track metrics across learning, engagement and business outcome dimensions every quarter.\n"
            "Run quarterly capability reviews with skills gap analysis to keep programming ahead of role changes.\n")
    resp = judge2.section_lines(text, "responsibility")
    assert not any(line.startswith("Measurement and continuous") for line in resp)
    assert len(resp) == 2


def test_rescue_runs_when_no_heading_opened_required():
    """Perficient b64e4713: no Required heading; one inline "Must be open to travel." blocked the years rescue."""
    text = ("Responsibilities\n- Lead projects within architecture and deliver platform MVPs with small teams.\n"
            "- 10+ years software development, engineering, or technology consulting experience\n"
            "- Must be open to travel.\n")
    got = _sec(text)
    assert _find(got, "10+ years software development") == "required"
