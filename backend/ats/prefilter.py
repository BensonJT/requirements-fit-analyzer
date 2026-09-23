"""Title prefilter for the detail-fetch budget. NOT the screening engine.

Workday and Oracle boards hold ~60k postings between them and their list calls carry
no JD, so we can't fetch every JD every day. This regex decides which postings get a
detail request FIRST. Anything it misses still sits in `postings` with its title,
location and dates, and can be detail-fetched later (`--detail-all`, or a wider
pattern). Over-inclusion is cheap; tune freely.

Two invariants, both tested (tests/test_ats.py):
  * every title term the screen itself treats as a function match (profile.TITLE_FUNCTION_TERMS
    and PRECISE_TITLE_TERMS) is caught by DETAIL_TITLE_PATTERN -- otherwise a posting the screen
    WOULD pass on its title can sit forever without a JD (found 2026-09-23: "operating model",
    "business architect", "change manager", "dmaic", "idef0" were missing; three live postings);
  * DIRECTIONAL_TITLE_PATTERN is a superset of DETAIL_TITLE_PATTERN. It is the wider net
    `vw_jd_missing` uses to show which JD-less titles are in the general direction of the search
    (project managers, consultants, operations, data, HR/people, quality, supply chain, ML,
    product/solution owners) without being function matches. It fetches nothing by itself; it
    exists so the size of what the prefilter leaves behind can be counted and read, not guessed.
"""
import re

DETAIL_TITLE_PATTERN = (
    r"process|operational excellence|operations excellence|opex|continuous improvement|"
    r"business excellence|performance excellence|six sigma|lean|black belt|kaizen|dmaic|idef0|"
    r"transformation|change management|change manager|change enablement|organizational change|"
    r"operating model|business architect|"
    r"business analy|operations analy|operations manager|operations lead|"
    r"program manager|program director|portfolio|pmo|"
    r"data (engineer|analy|scien)|analytics|business intelligence|insights|reporting|"
    r"service delivery|service management|quality (manager|lead|director|assurance)|"
    r"strategy|planning|governance|workforce|productivity|efficiency|automation|"
    r"\bai\b|agentic|adoption|enablement|\basset|lifecycle|life cycle|capacity|improvement|knowledge management|"
    r"director|principal|senior manager|sr\.? manager"
)

# Wider than the prefilter on purpose; read the docstring. Deliberately NOT in it: bare "manager"
# and "engineer" (branch managers, thermal engineers) and anything sales-shaped.
DIRECTIONAL_TITLE_PATTERN = DETAIL_TITLE_PATTERN + (
    r"|project (manager|lead|director)|consult|\boperations?\b|\bdata\b|\bprogram\b|"
    r"\bhr\b|people operations|talent|\bquality\b|supply chain|procurement|"
    r"machine learning|\bml\b|product (manager|owner|operations)|solutions? (architect|manager)|"
    r"customer success|implementation|delivery"
)

# A title in DIRECTIONAL that also matches this is NOT directional (RE2 has no lookahead, so the
# view applies it as a second test): "Retail Sales Consultant" is not a consultant in our sense,
# a data-center technician is not a data role.
DIRECTIONAL_EXCLUDE_PATTERN = r"\bsales\b|\bretail\b|technician|coordinator|\bintern|internship|\bnurse|clinical|\bdriver\b|warehouse"


def function_title_pattern() -> str:
    """A regex equivalent to the screen's title function match (profile.TITLE_FUNCTION_TERMS +
    PRECISE_TITLE_TERMS, which are plain substrings tested against a space-padded title)."""
    from backend import profile as P  # local: profile pulls in the gitignored profile_local
    terms = sorted(set(P.TITLE_FUNCTION_TERMS) | set(P.PRECISE_TITLE_TERMS), key=len, reverse=True)
    return "|".join(re.escape(t) for t in terms)
