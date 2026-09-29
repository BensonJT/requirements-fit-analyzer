"""What the judges (judge2, Jev) receive from a JD: the 2026-09-29.1 extraction fixes.

Each case is a shape seen in the gold set: years kept for the judge, digits the career-site HTML split apart,
a heading glued to its first line, a bare qualification line, a line that states its own status, notice text.
"""
from types import SimpleNamespace

from backend.finder import jev, judge2
from backend.finder import jev_questions as Q
from backend.finder import requirements as R


def _required(jd):
    return judge2.section_lines(jd, "required")


def test_judge_line_keeps_years_while_coverage_text_strips_them():
    jd = "Requirements:\n- 10+ years of experience in HR operations or HR service delivery\n"
    unit = [u for u in R.split_requirements(jd) if u.section == "required"][0]
    assert "10+" not in unit.text                      # coverage form, unchanged
    assert unit.source.startswith("10+ years")         # the JD's own words
    assert _required(jd) == ["10+ years of experience in HR operations or HR service delivery"]


def test_years_phrase_does_not_cut_inside_a_word():
    assert R.strip_years("4 years of directly related experience in BI") .startswith("directly related")


def test_digits_split_by_html_are_glued_back():
    jd = "Critical Skills\n1\n2\n+ years in supply chain, demand planning, or customer operations roles with at least\n4\nyears in a leadership role\n"
    assert _required(jd) == ["12+ years in supply chain, demand planning, or customer operations roles with at least "
                             "4 years in a leadership role"]


def test_heading_ending_on_a_joining_word_is_not_glued_to_the_next_line():
    jd = "What We're Looking For\nProven, senior-level experience leading a contact center or operations function.\n"
    assert _required(jd) == ["Proven, senior-level experience leading a contact center or operations function."]


def test_lowercase_known_heading_opens_required():
    jd = "What you'll do\nDesign product analytics.\nYou have\nExperience partnering across product and engineering organizations\n"
    assert _required(jd) == ["Experience partnering across product and engineering organizations"]


def test_bare_qualification_line_is_a_requirement_not_a_subheading():
    jd = "You Have:\n5+ years of experience building business systems\nPublic Trust\nBachelor's degree\n"
    assert _required(jd) == ["5+ years of experience building business systems", "Public Trust", "Bachelor's degree"]


def test_short_preferred_qualification_is_content_and_does_not_swallow_the_list():
    jd = "Requirements:\n- 3+ years of process improvement experience\nAdvanced degree preferred\n- Experience with Lean tools\n"
    assert "Advanced degree preferred" in judge2.section_lines(jd, "preferred")
    assert "Experience with Lean tools" in _required(jd)


def test_line_states_its_own_status():
    jd = ("Qualifications, Knowledge, Skills and Abilities:\n"
          "Experience with working capital funds, preferred\n"
          "License(s)/Certification(s)\n"
          "Active TS/SCI with Polygraph security clearance, required\n")
    assert "Active TS/SCI with Polygraph security clearance, required" in _required(jd)
    assert "Experience with working capital funds, preferred" in judge2.section_lines(jd, "preferred")


def test_leading_must_or_minimum_is_required_even_under_desired():
    jd = ("Responsibilities\nCoach Green Belts.\nNice to have:\nMinimum 7 years of experience in process improvement.\n"
          "Experience with Minitab or JMP for analysis.\n")
    assert _required(jd) == ["Minimum 7 years of experience in process improvement."]
    assert judge2.section_lines(jd, "preferred") == ["Experience with Minitab or JMP for analysis."]


def test_linkedin_desired_skills_label_is_the_requirements_list():
    jd = "Desired skills and experience:\nCertified Six Sigma Black Belt issued by IASSC or ASQ.\n"
    assert _required(jd) == ["Certified Six Sigma Black Belt issued by IASSC or ASQ."]


def test_physical_requirements_heading_is_dropped():
    jd = "Requirements:\n- 5+ years of analytics experience\nPhysical Requirements\n- Able to sit for long periods of time at a desk\n"
    assert _required(jd) == ["5+ years of analytics experience"]


def test_judge_noise_drops_notices_and_keeps_real_gates():
    noise = ["At CACI, we place character and innovation at the center of everything we do.",
             "Our pay ranges are determined by role, level, and location.",
             "Have a question or do you require any special accommodations?",
             "We offer competitive, meaningful benefits in every country where we operate.",
             "The salary range for this role is 107,500 USD - 204,500 USD."]
    keep = ["Active TS/SCI with Polygraph security clearance, required", "Must be a US Citizen",
            "Expertise in portfolio governance, benefits realization, and KPI design",
            "Progressive leadership experience in pharmacy benefit management",
            "Experience in equity research or retirement plan operations"]
    assert all(judge2.judge_noise(t) for t in noise)
    assert not any(judge2.judge_noise(t) for t in keep)


def test_jev_cap_keeps_qualification_lines_first(monkeypatch):
    monkeypatch.setattr(Q, "MAX_REQUIRED_LINES", 3)
    jd = ("Requirements:\n- Strong communication skills with executives\n- Comfort with ambiguity and change\n"
          "- Curiosity and a bias for action every day\n- Excellent organizational skills and follow-through\n"
          "- 5+ years leading communities of practice at scale\n")
    posting = SimpleNamespace(posting_id="p1", description_text=jd)
    required = [t for s, t in jev.select_lines(posting, log=lambda *a: None) if s == "required"]
    assert len(required) == 3 and required[-1] == "5+ years leading communities of practice at scale"


# ---------------------------------------------------------------- 2026-09-29.2
def test_bare_clearance_label_joins_the_requirement_after_it():
    jd = ("Requirements:\n- 5+ years of experience in organizational design\nClearance Required\n\n:\n"
          "Must be able to OBTAIN and MAINTAIN a Federal or DoD PUBLIC TRUST\n")
    assert _required(jd) == ["5+ years of experience in organizational design",
                             "Clearance Required: Must be able to OBTAIN and MAINTAIN a Federal or DoD PUBLIC TRUST"]


def test_clearance_word_continuing_a_level_is_not_a_label():
    jd = "Required:\n- Active Top-Secret/SCI\nSecurity Clearance required\n- 5+ years of program management experience\n"
    assert _required(jd)[0] == "Active Top-Secret/SCI Security Clearance required"


def test_logistics_lines_never_reach_a_judge_but_eligibility_does():
    jd = ("Requirements:\n- Ability to work at client site in Washington, DC at least 3 days/week\n"
          "- Must be based in the United States.\n- Ability to travel up to 20%.\n"
          "- Must be a US Citizen with the ability to obtain a Secret clearance\n"
          "- Experience leading globally distributed remote teams on process improvement\n")
    assert _required(jd) == ["Must be a US Citizen with the ability to obtain a Secret clearance",
                             "Experience leading globally distributed remote teams on process improvement"]


def test_never_bridge_domain_blocks_the_bridge(monkeypatch):
    from backend import profile as P
    monkeypatch.setattr(P, "NEVER_BRIDGE_DOMAIN_TERMS", ["human resources", "HR"], raising=False)
    line = {"section": "required", "kind": "years_function", "verdict": "adjacent",
            "line": "10+ years of experience in HR operations or HR technology", "evidence": "x"}
    fit, why = judge2.derive_required_fit([line])
    assert fit == "partial" and why.startswith("years line names a domain not worked in")
    ok = dict(line, line="10+ years of experience in business transformation, with HR experience preferred")
    assert judge2.derive_required_fit([ok])[0] == "meets"
