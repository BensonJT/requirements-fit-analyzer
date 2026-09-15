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
_PCT = r"(?<!\d)(\d{1,2})\s*%"
_SPAN = re.compile(r"(?<!\d)(\d{1,2})\s*%?\s*(?:-|–|—|to)\s*(\d{1,2})\s*%")
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
            if worst_span is None or b > worst_span[1]:
                worst_span = (a, b)
            continue
        for n in (int(x) for x in _SINGLE.findall(window)):
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
        if n > 2 * limit:
            reasons.append(f"travel {n}% (limit {limit}%)")
        elif n > limit:
            flags.append(f"travel ceiling {n}% (limit {limit}%)")
    return reasons, flags, notes


_REPORTS = re.compile(r"(?<!\d)(\d{1,3})\+?\s*(?:direct[- ]reports|people managers?)|(?:team|staff) of\s*(\d{1,3})(?!\d)", re.I)


def direct_reports_rule(title: str, text: str) -> RuleResult:
    """Team size stated in the JD, plus markers of building or running a large org."""
    limit = P.MAX_DIRECT_REPORTS
    if limit is None or not text:
        return [], [], {}
    reasons, flags, notes = [], [], {}
    counts = [int(a or b) for a, b in _REPORTS.findall(text)]
    if counts:
        n = max(counts)
        notes["reports"] = n
        if n > 2 * limit:
            reasons.append(f"large team ({n} direct reports)")
        elif n > limit:
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
    m = re.search(rf"(?<!\d)(\d{{1,2}})\+?\s*years?[^.\n]{{0,60}}?(?<![a-z0-9])({alt})(?![a-z0-9])",
                  required_block(text), re.I)
    if not m:
        return [], [], {}
    return [], [f"domain-tenure gate ({m.group(2).lower()}, {m.group(1)} yrs)"], {"domain_tenure": m.group(2).lower()}


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
    """Sales / revenue operations scope: a reason in the Required block, a flag anywhere else."""
    if not text:
        return [], [], {}
    rx = re.compile(P.SALES_OPS_PATTERN)
    block = find_required_block(text)
    m = rx.search(text if block is None else block)
    if m:  # no Required heading: the whole JD stands in for the block
        return [f"sales/revenue ops scope ({m.group(0)})"], [], {}
    m = rx.search(text)
    return ([], [f"sales/revenue ops vocabulary ({m.group(0)})"], {}) if m else ([], [], {})


def coding_test_rule(title: str, text: str) -> RuleResult:
    """Data-lane only (tier 3): a coding test or algorithmic take-home closes the lane."""
    hits = find_terms(getattr(P, "CODING_TEST_TERMS", []), text)
    return ([f"coding-test signal ({hits[0]})"], [], {}) if hits else ([], [], {})


# ---------------------------------------------------------------- level, workplace, country
# "10+ years of experience", "5-7 years' experience", "(8) years ... experience", "experience: 10+ years".
# A range counts by its lower bound. Only numbers tied to "experience" count, so "within 2 years of hire" does not.
_YEARS = re.compile(
    r"(?<![\d.])\(?(\d{1,2})\)?\s*(?:\+|plus)?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*\+?)?\s*(?:years?|yrs?)\b"
    r"(?=[^.\n;]{0,80}?\bexperien)"
    r"|\bexperien\w*[^.\n;\d]{0,30}?(?<![\d.])(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)


def required_years(text: str) -> list:
    """Every years-of-experience number in the JD, in order (company-history sized numbers dropped)."""
    out = []
    for m in _YEARS.finditer(text or ""):
        n = int(m.group(1) or m.group(2))
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
    return S.Listing(
        source="ats", search_pass=row.get("platform") or "", title=row.get("title") or "",
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


def rule_max() -> int:
    """The most rule points a posting can earn: tier 1 + extra hits + senior + remote + comp at the ask."""
    pts = P.RULE_POINTS
    return (pts["tier1"] + pts["extra_hit_cap"] + pts["senior"] + max(pts["remote"], pts["commutable_hybrid"])
            + pts["comp_ask"])


def rule_points(listing: S.Listing, tier: Optional[int], flags: list, senior: bool = False) -> int:
    """Raw rule points (before rescaling): tier, extra function hits, level, location, comp, flags.
    `senior` comes from level_rule (years, pay, or an unambiguous title)."""
    pts = P.RULE_POINTS
    title = f" {listing.title.lower()} "
    score = {1: pts["tier1"], 2: pts["tier2"], 3: pts["tier3"]}.get(tier, 0)
    hits = len(S._has(title, P.TITLE_FUNCTION_TERMS))
    score += min(max(hits - 1, 0) * pts["extra_hit"], pts["extra_hit_cap"])
    if senior:
        score += pts["senior"]
    if S.is_remote(listing):
        score += pts["remote"]
    elif listing.extra.get("workplace_type") in (None, "hybrid") and S.is_commutable(listing):
        score += pts["commutable_hybrid"]
    top = listing.annual_top
    if top is not None:
        if P.COMP_ASK and top >= P.COMP_ASK:
            score += pts["comp_ask"]
        elif P.COMP_FLOOR and top >= P.COMP_FLOOR:
            score += pts["comp_floor"]
        else:
            score += pts["comp_posted"]
    score += max(len(flags) * pts["flag"], pts["flag_floor"])
    return score


def screen_row(row: dict, rv: Optional[str] = None) -> ScreenRecord:
    """Card-level screen + JD rules + tier + rule points for one postings row.
    rule_score is rescaled to 0-100 against rule_max(), so it blends with fit on the same scale."""
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

    verdict = "reject" if reasons else ("review" if flags else "candidate")
    raw = rule_points(listing, tier, flags, senior=bool(notes.get("senior")))
    top = rule_max()
    score = int(100 * max(0, min(raw, top)) / top + 0.5)
    return ScreenRecord(posting_id=row.get("posting_id") or "", verdict=verdict, tier=tier,
                        rule_score=score, reasons=reasons, flags=flags, notes=notes)
