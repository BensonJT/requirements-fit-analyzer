"""JD-text rules for the finder, layered on the card-level engine in backend/screen.py.

`screen_row` runs `screen.screen()` on a postings row, then the JD rules below, and merges
the two. Each JD rule returns (reasons, flags, notes): a reason rejects, a flag sends the
posting to review, notes carry numbers for the report.

One deliberate substitution: the card-level discipline test in screen.py (section 4) was
written for ~500-character aggregator snippets and matches substrings, so on a full JD
"implant" counts as "plant". `discipline_rule` re-runs that test on the full JD with word
boundaries and a business-process counterweight, and its output replaces section 4's.
"""
import html
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from backend import profile as P
from backend import screen as S

RuleResult = tuple[list, list, dict]

# screen.py section-4 outputs that discipline_rule supersedes when the finder has the JD.
SUPERSEDED_PREFIXES = (
    "different discipline (plant/industrial",
    "plant/manufacturing vocabulary on listing",
    "engineer title -- apply the discipline test",
    "below target level (",   # superseded by level_rule: years + pay decide level, not "associate"
)


@dataclass
class ScreenRecord:
    posting_id: str
    verdict: str
    tier: Optional[int]
    rule_score: int
    reasons: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)


# ---------------------------------------------------------------- helpers
@lru_cache(maxsize=128)
def _terms_re(terms: tuple) -> Optional[re.Pattern]:
    """Case-insensitive alternation of `terms`, bounded so a term never matches inside a word
    ("plant" never matches "implant"); a trailing plural s/es is allowed."""
    cleaned = sorted({t.strip().lower() for t in terms if t and t.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    alt = "|".join(re.escape(t) for t in cleaned)
    return re.compile(rf"(?<![a-z0-9])(?:{alt})(?:e?s)?(?![a-z0-9])", re.I)


def find_terms(terms, text: str) -> list:
    """All word-bounded term occurrences in `text`, lowercased, in order of appearance."""
    rx = _terms_re(tuple(terms or ()))
    return [m.group(0).lower() for m in rx.finditer(text)] if rx and text else []


def _heading_lines(text: str):
    """(start, end, line) for short lines that read as a section heading in plain-text JDs."""
    pos = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and len(stripped) <= 60 and not stripped.endswith(".") and len(stripped.split()) <= 6:
            yield pos, pos + len(line), stripped
        pos += len(line)


def find_required_block(text: str) -> Optional[str]:
    """The Required section: from the first REQUIRED heading to the next PREFERRED heading or
    2,500 characters, whichever comes first. None when the JD has no Required heading."""
    if not text:
        return None
    req, pref = re.compile(P.REQUIRED_HEADINGS, re.I), re.compile(P.PREFERRED_HEADINGS, re.I)
    start = None
    for s, e, line in _heading_lines(text):
        if start is None:
            if req.search(line) and not pref.search(line):
                start = e
        elif pref.search(line):
            return text[start:min(s, start + 2500)]
    return None if start is None else text[start:start + 2500]


def required_block(text: str) -> str:
    """`find_required_block`, falling back to the whole text when no heading is found."""
    block = find_required_block(text)
    return (text or "") if block is None else block


# ---------------------------------------------------------------- JD rules
# \d{1,3} so "100%" matches too; the <= 100 guard on the parsed value keeps a stray "250%" typo
# (or an unrelated three-digit number caught in the travel window) from being read as a percentage.
_PCT = r"(?<!\d)(\d{1,3})\s*%"
_SPAN = re.compile(r"(?<!\d)(\d{1,3})\s*%?\s*(?:-|–|—|to)\s*(\d{1,3})\s*%")
_SINGLE = re.compile(_PCT)


def travel_rule(title: str, text: str) -> RuleResult:
    """Travel percentages within 40 characters of the word 'travel'."""
    limit = P.TRAVEL_MAX_PCT
    if limit is None or not text:
        return [], [], {}
    worst_span, worst_single = None, None
    for m in re.finditer(r"travel", text, re.I):
        window = text[max(0, m.start() - 40): m.end() + 40]
        span = _SPAN.search(window)
        if span:
            a, b = int(span.group(1)), int(span.group(2))
            if a <= 100 and b <= 100 and (worst_span is None or b > worst_span[1]):
                worst_span = (a, b)
            continue
        for n in (int(x) for x in _SINGLE.findall(window)):
            if n <= 100:
                worst_single = n if worst_single is None else max(worst_single, n)
    reasons, flags, notes = [], [], {}
    if worst_span:
        a, b = worst_span
        notes["travel_pct"] = b
        if b >= 2 * limit:
            reasons.append(f"travel span {a}-{b}% doubles the limit")
        elif b > limit:
            flags.append(f"travel span {a}-{b}% (limit {limit}%)")
    if worst_single is not None and (not worst_span or worst_single > worst_span[1]):
        n = worst_single
        notes["travel_pct"] = n
        # >= , not >: the span branch above rejects at exactly 2x the limit ("doubles the limit"), so a
        # flat "50% travel" against a 25% limit must reject too. With > , the identical burden written
        # two ways split the verdict -- "travel 0-50%" rejected while "up to 50% travel" only flagged
        # (Guidehouse 39017 reached review on the flag alone).
        if n >= 2 * limit:
            reasons.append(f"travel {n}% (limit {limit}%)")
        elif n > limit:
            flags.append(f"travel ceiling {n}% (limit {limit}%)")
    return reasons, flags, notes


# Only an explicit direct-report phrase can reject: "12 direct reports", "5 people managers",
# "manage 8 reports". "team of N" / "staff of N" / "organization of N" is program or org headcount
# (CACI's "team of 250+ professionals", "lead a team of 12 analysts") and never rejects on its own --
# see the flag-only branch below.
_REPORTS = re.compile(r"(?<!\d)(\d{1,3})\+?\s*(?:direct[- ]reports?|people managers?)"
                      r"|manage\s*(\d{1,3})\+?\s*(?:direct[- ])?reports?", re.I)
_TEAM_SIZE = re.compile(r"(?:team|staff|organization|org)\s+of\s*(\d{1,3})(?!\d)", re.I)
# Whether a "team of N" headcount is something the posting asks the candidate to LEAD, not merely
# support -- CACI's "support a team of 250+ professionals" is headcount, never scope (level_fit_rule, A1).
_TEAM_LED_RE = re.compile(r"\b(?:lead|leads|leading|manage|manages|managing|run|runs|running|"
                          r"own|owns|owning|direct|directs|directing|build|builds|building)\b", re.I)


def direct_reports_rule(title: str, text: str) -> RuleResult:
    """Team size stated in the JD, plus markers of building or running a large org.

    `notes["reports"]` stays the combined max (existing tests and the card-level flag/reason logic rely
    on it). `direct_reports` (explicit "N direct reports") and `team_headcount` (program/org "team of N")
    split the two for level_fit_rule (A1), which treats them differently; `team_headcount_led` records
    whether the headcount number sits within 30 characters of a lead/manage/run/own/direct/build verb, so
    "support a team of 250" never reads as scope the way "lead a team of 12" does.
    """
    limit = P.MAX_DIRECT_REPORTS
    if limit is None or not text:
        return [], [], {}
    reasons, flags, notes = [], [], {}
    counts = [int(a or b) for a, b in _REPORTS.findall(text)]
    team_matches = list(_TEAM_SIZE.finditer(text))
    if counts:
        n = max(counts)
        notes["reports"] = n
        notes["direct_reports"] = n
        if n > 2 * limit:
            reasons.append(f"large team ({n} direct reports)")
        elif n > limit:
            flags.append(f"team of {n} (limit {limit})")
    if team_matches:
        n = max(int(m.group(1)) for m in team_matches)
        notes["reports"] = max(notes.get("reports", 0), n)
        notes["team_headcount"] = n
        notes["team_headcount_led"] = any(
            _TEAM_LED_RE.search(text[max(0, m.start() - 30):m.start()])
            for m in team_matches if int(m.group(1)) == n)
        if n > limit:
            flags.append(f"team of {n} (limit {limit})")
    markers = find_terms(P.LARGE_TEAM_MARKERS, text)
    if markers:
        flags.append(f"large-team markers ({markers[0]})")
    return reasons, flags, notes


def domain_tenure_rule(title: str, text: str) -> RuleResult:
    """'N+ years ... <industry>' in the Required block is a gate to note, never a rejection."""
    terms = [t for t in (P.DOMAIN_TENURE_TERMS or []) if t.strip()]
    if not terms or not text:
        return [], [], {}
    alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    block = required_block(text)
    m = re.search(rf"(?<!\d)(\d{{1,2}})\+?\s*years?[^.\n]{{0,60}}?(?<![a-z0-9])({alt})(?![a-z0-9])",
                  block, re.I)
    if not m:
        return [], [], {}
    if _tenure_clause_admits_candidate(block, m):
        return [], [], {}
    return [], [f"domain-tenure gate ({m.group(2).lower()}, {m.group(1)} yrs)"], {"domain_tenure": m.group(2).lower()}


def _tenure_clause_admits_candidate(block: str, m) -> bool:
    """True when the years-requirement is a DISJUNCTIVE list naming a field the candidate has.

    "10+ years in Supply Chain, Operations, Logistics, Manufacturing, Consulting, or a related field" gates on
    none of them -- any one will do, and two are his. "10+ years in medical device operations" is a compound
    domain, not a list, so it still gates: the disjunction has to be there (a comma series or an explicit "or")
    before a candidate field counts.
    """
    after = block[m.end(2): m.end(2) + 240]
    stop = re.search(r"[.;\n]", after)
    tail = after[: stop.start()] if stop else after
    if not (" or " in tail.lower() or "," in tail):
        return False
    clause = block[m.start(): m.end(2)] + tail
    fields = [f for f in (getattr(P, "CANDIDATE_TENURE_FIELDS", None) or []) if f.strip()]
    return any(re.search(rf"(?<![a-z0-9]){re.escape(f)}(?![a-z0-9])", clause, re.I) for f in fields)


def discipline_rule(title: str, text: str) -> RuleResult:
    """Plant/industrial vocabulary against business-process vocabulary."""
    blob = f"{title}\n{text}"
    plant = find_terms(P.PLANT_DISCIPLINE_TERMS, blob)
    lane = find_terms(P.LANE_PROCESS_TERMS, blob)
    notes = {"plant_hits": len(plant), "lane_hits": len(lane)}
    engineer = "engineer" in title.lower()
    if engineer and plant and len(plant) > len(lane):
        return [f"different discipline (plant/industrial: {plant[0]})"], [], notes
    if len(plant) >= 3 and len(plant) > len(lane):
        return [f"plant-floor scope ({plant[0]})"], [], notes
    if plant:
        return [], [f"plant vocabulary ({plant[0]}) -- read REQUIRED quals"], notes
    if engineer and not text:
        return [], ["engineer title -- apply the discipline test on the live JD"], notes
    return [], [], notes


def corridor_rule(title: str, text: str, locations=()) -> RuleResult:
    """In a manufacturing-cluster place, a plant term in the Required block means a plant role.
    `locations` is one location string or a list of them; CORRIDOR_PLACES may pin a state."""
    if isinstance(locations, str):
        locations = [locations]
    if not P.CORRIDOR_PLACES or not text or not any(S.place_matches(loc, P.CORRIDOR_PLACES) for loc in locations if loc):
        return [], [], {}
    hits = find_terms(P.PLANT_DISCIPLINE_TERMS, required_block(text))
    if not hits:
        return [], [], {}
    return [f"corridor manufacturing (required: {hits[0]})"], [], {}


def assessment_gate_rule(title: str, text: str) -> RuleResult:
    hits = find_terms(P.ASSESSMENT_GATE_TERMS, f"{title}\n{text}")
    return ([f"assessment-gated ({hits[0]})"], [], {}) if hits else ([], [], {})


_HOURS = re.compile(r"(?<!\d)(\d{1,2})\s*(?:-|–|to)+\s*(\d{1,2})\s*hours?\s*(?:per|a|/)\s*week"
                    r"|\$\d[\d,]*\s*(?:per|/)\s*week", re.I)
# "part-time" alone is benefits boilerplate in most JDs ("full-time and part-time employees are
# eligible..."), so it counts only in the title, the ATS employment type, or a sentence about this role.
_PART_TIME_ROLE = re.compile(r"\b(?:this|the|is an?|a)\s+part[- ]time\s+(?:position|role|job|opportunity|contract)"
                             r"|\bpart[- ]time\s*\(\s*\d{1,2}", re.I)


def hours_cap_rule(title: str, text: str, employment_type: Optional[str] = None) -> RuleResult:
    """Part-time or capped weekly hours: an hourly rate must not be annualized."""
    part_time = (employment_type == "part_time" or re.search(r"part[- ]time", title, re.I)
                 or _PART_TIME_ROLE.search(text))
    if not (part_time or _HOURS.search(text)):
        return [], [], {}
    return [], ["hours capped -- do not annualize"], {"hours_capped": True}


def sales_ops_rule(title: str, text: str) -> RuleResult:
    """Sales / revenue operations scope. A reason only when the ROLE is sales ops: the title says so, the
    Required block carries a strong term (a quota) or two distinct terms, or a JD with no Required heading
    carries three. One passing mention ("partner with Finance and GTM", "familiarity with CRM, ERP, CPQ")
    is a flag: blind grading on 2026-09-19 found a process-excellence role rejected on a tool list."""
    if not text:
        return [], [], {}
    rx = re.compile(P.SALES_OPS_PATTERN)
    same = getattr(P, "SALES_OPS_SYNONYMS", {})
    strong = set(getattr(P, "SALES_OPS_STRONG_TERMS", []))
    tm = rx.search(title or "") or re.search(getattr(P, "SALES_OPS_TITLE_PATTERN", r"(?!x)x"), title or "")
    if tm:
        return [f"sales/revenue ops scope ({tm.group(0)})"], [], {}
    block = find_required_block(text)
    hits = [m.group(0) for m in rx.finditer(text if block is None else block)]
    terms = {same.get(h.lower(), h.lower()) for h in hits}
    need = 3 if block is None else 2   # no Required heading: the whole JD stands in, so ask for more
    if terms & strong or len(terms) >= need:
        first = next((h for h in hits if h.lower() in strong), hits[0])
        return [f"sales/revenue ops scope ({first})"], [], {}
    m = rx.search(text)
    return ([], [f"sales/revenue ops vocabulary ({m.group(0)})"], {}) if m else ([], [], {})


def coding_test_rule(title: str, text: str) -> RuleResult:
    """Data-lane only (tier 3): a coding test or algorithmic take-home closes the lane."""
    hits = find_terms(getattr(P, "CODING_TEST_TERMS", []), text)
    return ([f"coding-test signal ({hits[0]})"], [], {}) if hits else ([], [], {})


# ---------------------------------------------------------------- level, workplace, country
# Years of experience. Each "year(s)/yr(s)" word counts when a number phrase ends right before it and "experience"
# appears in the same sentence (after it, or before it as in "Experience: 12+ years"). Number phrases: "10+",
# "10 or more", "10-12" / "5 to 7" (lower bound), "ten (10)", "eight". So "within 2 years of hire" and
# "100 years serving clients" do not count.
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
              "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
_NUM = r"(?:(?<![\d.,$])\d{1,2}(?![\d.,])|\b(?:" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True)) + r")\b)"
_YEAR_WORD = re.compile(r"\b(?:years?|yrs?)\b", re.I)
_LEAD = re.compile(rf"({_NUM})\s*\)?\s*(?:\(\s*\d{{1,2}}\s*\)\s*)?(?:\+|plus)?\s*"
                   rf"(?:(?:-|–|—|to)\s*{_NUM}\s*\)?\s*(?:\(\s*\d{{1,2}}\s*\)\s*)?\+?\s*)?"
                   r"(?:or\s+(?:more|greater|longer)\s*)?\+?\s*$", re.I)
_SENTENCE_END = re.compile(r"[.;\n]")


def _number(token: str) -> int:
    token = token.lower()
    return int(token) if token.isdigit() else _NUM_WORDS[token]


def required_years(text: str) -> list:
    """Every years-of-experience number in the JD, in order (company-history sized numbers dropped)."""
    out, text = [], text or ""
    for m in _YEAR_WORD.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        lead = _LEAD.search(before)
        if not lead:
            continue
        after = _SENTENCE_END.split(text[m.end():m.end() + 100], 1)[0]
        prior = _SENTENCE_END.split(text[max(0, m.start() - 80):m.start()])[-1]
        if not re.search(r"experien", after + " " + prior, re.I) or re.match(r"\s*(?:old|of age)\b", after, re.I):
            continue
        n = _number(lead.group(1))
        if 0 < n <= P.LEVEL_YEARS_CAP:
            out.append(n)
    return out


def level_rule(title: str, text: str, annual_top: Optional[float] = None) -> RuleResult:
    """Level from the MOST years any line asks for (one '2+ years of X' line in a 10+ years JD is not the
    level) plus the pay band; a title counts only when unambiguous (Director, VP, Principal, Head of, Chief).

    junior (< LEVEL_YEARS_MID) = reason, unless a senior title or a band top at the floor says otherwise (flag);
    mid = flag unless a senior title or a band top at the ask; early-career title = reason.
    """
    years = required_years(text)
    most = max(years) if years else None
    senior_title = find_terms(P.SENIOR_LEVEL_TITLE_TERMS, title)
    early = find_terms(P.EARLY_CAREER_TITLE_TERMS, title)
    if not early:
        year_hit = _EARLY_CAREER_YEAR_RE.search(title or "")
        if year_hit:
            early = [year_hit.group(0).lower()]
    pay_floor = annual_top is not None and bool(P.COMP_FLOOR) and annual_top >= P.COMP_FLOOR
    pay_ask = annual_top is not None and bool(P.COMP_ASK) and annual_top >= P.COMP_ASK
    level = None if most is None else ("senior" if most >= P.LEVEL_YEARS_SENIOR
                                       else "mid" if most >= P.LEVEL_YEARS_MID else "junior")
    reasons, flags = [], []
    if early and not senior_title:
        reasons.append(f"early-career title ({early[0]})")
    if level == "junior":
        if senior_title or pay_floor:
            flags.append(f"few years asked (at most {most}) -- {'title' if senior_title else 'pay band'} says senior")
        else:
            reasons.append(f"junior level (at most {most} yrs required)")
    elif level == "mid" and not (senior_title or pay_ask):
        flags.append(f"mid level (at most {most} yrs required)")
    notes = {"max_years": most, "level": level, "senior": level == "senior" or bool(senior_title) or pay_ask}
    return reasons, flags, notes


# "own the P&L" / "profit and loss responsibility": one scope hit for level_fit_rule when the mention
# sits within 80 characters of an ownership/accountability word, so a P&L mentioned only as a reporting
# artifact ("P&L review each quarter") does not count.
_PNL_RE = re.compile(r"\bP\s*&\s*L\b|\bprofit\s+(?:and|&)\s+loss\b", re.I)
_PNL_OWNERSHIP_RE = re.compile(r"own|ownership|responsib|accountab|manag|deliver", re.I)
# "N years managerial / people leadership" -- the same number grammar as required_years, but only when
# the sentence itself reads as people-management scope, not a generic years-of-experience line.
# PEOPLE management only: "project management", "process management", "change management" and every other
# "<noun> management" line is function, not scope, and the target lane is full of them. A bare "manag" here
# would have made "10+ years of project management experience" a scope hit on most of the corpus.
_MGR_SENTENCE_RE = re.compile(
    r"people[- ](?:management|leadership)|managerial|supervis(?:or|ory|ing|ion)"
    r"|(?:manag(?:e|ing|ed|ement)|lead(?:ing|ership)?|led)\s+(?:of\s+)?(?:an?\s+|the\s+)?"
    r"(?:teams?|staff|people|direct reports|employees|analysts|managers|engineers|organi[sz]ations?|groups?)\b"
    r"|team management|direct reports", re.I)
# "Chief of Staff" is a director-band deputy seat (a stretch term), not a C-suite title: strip it before the
# "chief" term is looked up so it never reads out_of_reach.
_CHIEF_OF_STAFF_RE = re.compile(r"chief\s+of\s+staff", re.I)
# EARLY_CAREER_TITLE_TERMS is a literal-word list (see find_terms/_terms_re) and can't express "any year" --
# a seasonal-intern title names the specific summer ("Summer 2027"), so that one case is a regex here instead.
# "Summer" alone (a concert series, a sale) must not match; the four-digit year is what makes it early-career.
_EARLY_CAREER_YEAR_RE = re.compile(r"\bsummer\s+20\d{2}\b", re.I)


def _managerial_years(block: str) -> Optional[int]:
    """The largest years-of-experience number whose sentence also reads as people-management scope."""
    block = block or ""
    best = None
    for m in _YEAR_WORD.finditer(block):
        before = block[max(0, m.start() - 40):m.start()]
        lead = _LEAD.search(before)
        if not lead:
            continue
        after = _SENTENCE_END.split(block[m.end():m.end() + 100], 1)[0]
        prior = _SENTENCE_END.split(block[max(0, m.start() - 120):m.start()])[-1]
        sentence = prior + " " + after
        if not re.search(r"experien", after + " " + prior, re.I):
            continue
        if not _MGR_SENTENCE_RE.search(sentence):
            continue
        n = _number(lead.group(1))
        if 0 < n <= P.LEVEL_YEARS_CAP:
            best = n if best is None else max(best, n)
    return best


def level_fit_rule(title: str, text: str, annual_top: Optional[float] = None,
                   reports_notes: Optional[dict] = None) -> RuleResult:
    """Second, independent level signal (sprint plan sec 20.2): title and Required-block scope against the
    author's target level, ordered too_low < in_range < stretch_up < out_of_reach < unknown (outside the
    order). Never a reason or a flag -- level_rule above still decides verdict/rule_score; this rule only
    writes notes["level_fit"] (and notes["level_fit_hits"], short strings the report shows) for the human
    vs. rule agreement check in report_feedback. `reports_notes` is direct_reports_rule's notes dict (or
    the screen_row-merged notes, a superset of it) so team size does not have to be recomputed here.

    Decision order, first match wins: early-career title with no senior/in-range/stretch/out-of-reach
    signal -> too_low; an out-of-reach title term -> out_of_reach; 2+ scope hits (a Director-type stretch
    title term counts as one of them, so "Director" alone is one hit and "Director" + a P&L mention is
    two) -> out_of_reach, exactly 1 -> stretch_up; a posted band top under COMP_FLOOR -> too_low; years
    below LEVEL_YEARS_MID -> in_range when the band is at/above the floor or the title/years already read
    senior (level_rule's own senior note), else too_low; otherwise in_range when an in-range title term,
    level_rule's senior note, a band at/above the floor, or years at/above LEVEL_YEARS_MID fired; else
    unknown.
    """
    title = title or ""
    block = required_block(text)
    reports_notes = reports_notes or {}
    hits: list = []

    out_terms = find_terms(P.LEVEL_OUT_OF_REACH_TITLE_TERMS, _CHIEF_OF_STAFF_RE.sub("", title))
    stretch_terms = find_terms(P.LEVEL_STRETCH_TITLE_TERMS, title)
    in_range_terms = find_terms(P.LEVEL_IN_RANGE_TITLE_TERMS, title)
    early_terms = find_terms(P.EARLY_CAREER_TITLE_TERMS, title)
    if out_terms:
        hits.append(f"title: {out_terms[0]}")
    elif stretch_terms:
        hits.append(f"title: {stretch_terms[0]}")
    if in_range_terms:
        hits.append(f"title: {in_range_terms[0]}")
    if early_terms:
        hits.append(f"title: {early_terms[0]}")

    # Scope: org-building phrases (capped at 2, each distinct term once), P&L ownership, managerial
    # years over the profile limit, team size over the profile limit -- plus the stretch title term
    # itself, so "Director" alone is one hit (stretch_up) and "Director" + one more scope signal is
    # two (out_of_reach).
    org_universe = list(P.ORG_BUILDING_TERMS) + list(P.LARGE_TEAM_MARKERS)
    distinct_org = list(dict.fromkeys(find_terms(org_universe, block)))
    org_scope = min(len(distinct_org), 2)
    for t in distinct_org[:2]:
        hits.append(f"scope: {t}")

    pnl_scope = 0
    for m in _PNL_RE.finditer(block):
        window = block[max(0, m.start() - 80):m.start()]
        if _PNL_OWNERSHIP_RE.search(window):
            pnl_scope = 1
            hits.append("p&l")
            break

    mgr_years = _managerial_years(block)
    mgr_scope = 0
    out_years = getattr(P, "LEVEL_MANAGERIAL_YEARS_OUT", None)
    if mgr_years is not None and out_years is not None and mgr_years >= out_years:
        mgr_scope = 2   # "5+ years managing people" is a level above the record on its own (user, 2026-09-17)
        hits.append(f"managerial years {mgr_years} >= {out_years}")
    elif mgr_years is not None and mgr_years > P.LEVEL_MANAGERIAL_YEARS_MAX:
        mgr_scope = 1
        hits.append(f"managerial years {mgr_years} > {P.LEVEL_MANAGERIAL_YEARS_MAX}")

    reports_scope = 0
    max_reports = reports_notes.get("direct_reports")
    max_headcount = reports_notes.get("team_headcount")
    headcount_led = reports_notes.get("team_headcount_led")
    if P.MAX_DIRECT_REPORTS is not None:
        limit = P.MAX_DIRECT_REPORTS
        if max_reports is not None and max_reports > 2 * limit:
            reports_scope += 2
            hits.append(f"direct reports {max_reports}")
        elif max_reports is not None and max_reports > limit:
            reports_scope += 1
            hits.append(f"direct reports {max_reports}")
        if headcount_led and max_headcount is not None and max_headcount > 2 * limit:
            reports_scope += 1
            hits.append(f"team headcount {max_headcount}")

    stretch_scope = 1 if stretch_terms else 0
    scope_hits = org_scope + pnl_scope + mgr_scope + reports_scope + stretch_scope

    years = required_years(block)
    most_years = max(years) if years else None
    _, _, level_notes = level_rule(title, text, annual_top)
    senior_note = bool(level_notes.get("senior"))

    pay_posted = annual_top is not None
    pay_below_floor = pay_posted and bool(P.COMP_FLOOR) and annual_top < P.COMP_FLOOR
    pay_at_floor = pay_posted and bool(P.COMP_FLOOR) and annual_top >= P.COMP_FLOOR
    years_below_mid = most_years is not None and most_years < P.LEVEL_YEARS_MID
    years_at_mid = most_years is not None and most_years >= P.LEVEL_YEARS_MID

    if early_terms and not (senior_note or in_range_terms or stretch_terms or out_terms):
        value = "too_low"
    elif out_terms:
        value = "out_of_reach"
    elif scope_hits >= 2:
        value = "out_of_reach"
    elif scope_hits == 1:
        value = "stretch_up"
    elif pay_below_floor:
        value = "too_low"
    elif years_below_mid:
        # Pay in range overrides low years (the user's rule); with no band posted, only a title/years
        # that already reads senior (level_rule's own senior note) rescues it -- a bare IC title like
        # "Analyst" or "Engineer" alone does not, or a 3-year Analyst JD with no band would always
        # read in_range off its own title term.
        value = "in_range" if (pay_at_floor or senior_note) else "too_low"
    elif in_range_terms or senior_note or pay_at_floor or years_at_mid:
        value = "in_range"
    else:
        value = "unknown"

    return [], [], {"level_fit": value, "level_fit_hits": hits}


_DAYS = r"(?:\d|one|two|three|four|five)(?:\s*(?:-|–|to|or)\s*(?:\d|one|two|three|four|five))?\s*(?:\(\d\)\s*)?days?"
_WEEK = r"\s*(?:a|per|each|every|/)\s*week"
_HYBRID_TEXT = re.compile(
    rf"\b(?:hybrid|in-office|in office|on-?site|in-person|in person)\b[^.\n]{{0,100}}?\b{_DAYS}{_WEEK}\b"
    rf"|\b{_DAYS}{_WEEK}\b[^.\n]{{0,80}}?\b(?:office|on-?site|in[- ]person)\b", re.I)
_ONSITE_TEXT = re.compile(r"\b(?:(?:this|the)\s+(?:role|position|job)\s+is\s+(?:fully\s+|100%\s+)?on-?site"
                          r"|(?:fully|100%)\s+(?:on-?site|in[- ]office))\b", re.I)


def workplace_from_text(text: str) -> Optional[str]:
    """'onsite' or 'hybrid' when the JD states an in-office requirement in days per week; else None."""
    if _ONSITE_TEXT.search(text or ""):
        return "onsite"
    if _HYBRID_TEXT.search(text or ""):
        return "hybrid"
    return None


_US_TEXT = re.compile(r"(?<![a-z])(?:united states|u\.s\.a?\.?|usa|us)(?![a-z])", re.I)


def non_us_rule(country: Optional[str], locations) -> RuleResult:
    """Hard reject when no location is in the US: the ATS country code says so, or every location segment
    names a non-US place. One US segment anywhere keeps the posting; a bare "Remote" never rejects."""
    segments = [seg.strip() for loc in locations if loc for seg in re.split(r"[;|]", str(loc)) if seg.strip()]
    if any(S.states_in(seg) or _US_TEXT.search(seg) for seg in segments):
        return [], [], {}
    code = (country or "").strip().upper()
    if code and code not in ("US", "USA", "UNITED STATES"):
        return [f"outside the US (country {code})"], [], {}
    named = [find_terms(P.NON_US_TERMS, seg) for seg in segments]
    if segments and all(named):
        return [f"outside the US ({named[0][0]})"], [], {}
    return [], [], {}


JD_RULES = (travel_rule, direct_reports_rule, domain_tenure_rule, discipline_rule,
            assessment_gate_rule, sales_ops_rule)


# ---------------------------------------------------------------- row -> record
def listing_from_row(row: dict) -> S.Listing:
    """Maps a `postings` row (dict) to the Listing the card-level engine takes."""
    try:
        locs = json.loads(row.get("locations") or "[]")
    except (TypeError, ValueError):
        locs = []
    if not isinstance(locs, list):
        locs = []
    posted = row.get("posted_at")
    # screen.py's federal-title rule (section 2) only fires when job.source == "USAJobs" -- true for
    # sweep.py's older aggregator Listings, never for an ATS-direct row otherwise (source was a flat
    # "ats" regardless of platform). The USAJobs ATS adapter (backend/ats/adapters.py) needs that rule
    # reachable, so this is the one narrow exception.
    source = "USAJobs" if row.get("platform") == "usajobs" else "ats"
    return S.Listing(
        source=source, search_pass=row.get("platform") or "", title=row.get("title") or "",
        company=row.get("employer") or "", location=row.get("location_primary") or "", url=row.get("url") or "",
        posted_at=str(posted) if posted else None, description=row.get("description_text") or "",
        salary_min=row.get("pay_min"), salary_max=row.get("pay_max"), salary_predicted=False,
        locations=[str(x) for x in locs if x],
        extra={k: row.get(k) for k in ("posting_id", "platform", "workplace_type", "employment_type", "job_level")},
    )


def _merge(into: list, items) -> None:
    for item in items:
        if item not in into:
            into.append(item)


def tier_for(title: str) -> Optional[int]:
    """1 precise function term, 2 broad function term, 3 data lane, None otherwise."""
    padded = f" {title.lower()} "
    if S._has(padded, P.PRECISE_TITLE_TERMS):
        return 1
    if S._has(padded, P.TITLE_FUNCTION_TERMS):
        return 2
    if P.DATA_LANE_ENABLED and S._has(padded, P.DATA_LANE_TERMS):
        return 3
    return None


def location_points(listing: S.Listing) -> int:
    """Remote 100; nationwide listing with remote unverified; commutable hybrid; commutable on-site or unstated."""
    pts = P.LOCATION_POINTS
    if S.is_remote(listing):
        return pts["remote"]
    if listing.location.strip().lower() in P.NATIONWIDE_LOCATIONS:
        return pts["nationwide_unverified"]
    if listing.is_local_pass or S.is_commutable(listing):
        return pts["commutable_hybrid"] if listing.extra.get("workplace_type") == "hybrid" else pts["commutable_onsite"]
    return 0   # rejected on location anyway


def pay_points(annual_top: Optional[float]) -> int:
    """Band top at the ask; at the floor; not posted (unknown until a screening conversation, so mild)."""
    pts = P.PAY_POINTS
    if annual_top is None:
        return pts["not_posted"]
    if P.COMP_ASK and annual_top >= P.COMP_ASK:
        return pts["at_ask"]
    if not P.COMP_FLOOR or annual_top >= P.COMP_FLOOR:
        return pts["at_floor"]
    return 0   # rejected on comp anyway


def level_points(notes: dict) -> int:
    """Senior (years >= 8, band top at the ask, or an unambiguous title); mid; not stated."""
    if notes.get("senior"):
        return P.LEVEL_POINTS["senior"]
    if notes.get("level") in ("mid", "junior"):   # a junior count survives only with a pay-band override
        return P.LEVEL_POINTS["mid"]
    return P.LEVEL_POINTS["unknown"]


def title_points(tier: Optional[int], reasons: list) -> int:
    if any(r.startswith("off-lane title") for r in reasons):
        return P.TITLE_POINTS["off_lane"]
    return P.TITLE_POINTS.get(tier, P.TITLE_POINTS[None])


def profile_score(components: dict) -> int:
    """The non-content components (level, location, pay, title) weighted on 0-100."""
    weights = {k: w for k, w in P.SCORE_COMPONENT_WEIGHTS.items() if k != "content"}
    return int(sum(weights[k] * components[k] for k in weights) / sum(weights.values()) + 0.5)


def screen_row(row: dict) -> ScreenRecord:
    """Card-level screen + JD rules + tier + profile score for one postings row.
    rule_score is the profile score (level, location, pay, title on 0-100); content fit is blended in the pipeline."""
    title = row.get("title") or ""
    # Some boards store line breaks as the entity "&#xa;"; decode so headings and sentences split correctly.
    text = html.unescape(row.get("description_text") or "")
    listing = listing_from_row({**row, "description_text": text})
    reasons, flags, notes = [], [], {}
    if not listing.extra.get("workplace_type"):
        inferred = workplace_from_text(text)
        if inferred:
            listing.extra["workplace_type"] = inferred
            notes["workplace_inferred"] = inferred

    # Capped hours: an hourly rate is not an annual salary, so treat comp as not posted.
    h_reasons, h_flags, h_notes = hours_cap_rule(title, text, row.get("employment_type"))
    top = listing.salary_max or listing.salary_min
    if h_notes.get("hours_capped") and (row.get("pay_interval") == "hour" or (top and top < 500)):
        listing.salary_min = listing.salary_max = None

    S.screen(listing, skip_tracker=True)
    _merge(reasons, (r for r in listing.reasons if not r.startswith(SUPERSEDED_PREFIXES)))
    _merge(flags, (f for f in listing.flags if not f.startswith(SUPERSEDED_PREFIXES)))

    results = [rule(title, text) for rule in JD_RULES]
    results.append(corridor_rule(title, text, [listing.location, *listing.locations]))
    results.append(non_us_rule(row.get("country"), [listing.location, *listing.locations]))
    results.append(level_rule(title, text, listing.annual_top))
    results.append((h_reasons, h_flags, h_notes))
    tier = tier_for(title)
    if tier == 3:
        reasons = [r for r in reasons if r != "off-function title"]
        results.append(coding_test_rule(title, text))
    for r, f, n in results:
        _merge(reasons, r)
        _merge(flags, f)
        notes.update(n)

    # A second, independent level signal (sec 20.2): runs after the other rules so it can read
    # notes["direct_reports"] / notes["team_headcount"] / notes["team_headcount_led"] from
    # direct_reports_rule above. Never a reason or a flag -- verdict and rule_score are untouched.
    _, _, level_fit_notes = level_fit_rule(title, text, listing.annual_top, notes)
    notes.update(level_fit_notes)

    verdict = "reject" if reasons else ("review" if flags else "candidate")
    notes["components"] = {"level": level_points(notes), "location": location_points(listing),
                           "pay": pay_points(listing.annual_top), "title": title_points(tier, reasons)}
    score = profile_score(notes["components"])
    return ScreenRecord(posting_id=row.get("posting_id") or "", verdict=verdict, tier=tier,
                        rule_score=score, reasons=reasons, flags=flags, notes=notes)
