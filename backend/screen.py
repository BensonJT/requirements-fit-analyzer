"""Deliberate pre-screen for harvested listings, built from the search profile in backend/profile.py.

This is a CARD-LEVEL filter. It exists to shrink hundreds of API rows to the few worth
opening -- it never replaces reading the live posting. Every row keeps its reasons so a
screened-out listing can be reopened when a rule changes.

Verdicts:
    candidate -- function match in title, nothing disqualifying on the listing
    review    -- worth a human look, but carries a flag (discipline, clearance, tracker, ...)
    reject    -- fails a hard rule; reason recorded
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from backend import profile as P


@dataclass
class Listing:
    source: str
    search_pass: str          # e.g. "adzuna:remote:Operational Excellence"
    title: str
    company: str
    location: str
    url: str
    posted_at: Optional[str] = None
    description: str = ""
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_text: str = ""
    salary_predicted: bool = False
    is_local_pass: bool = False
    locations: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    # filled by screen()
    verdict: str = ""
    score: int = 0
    reasons: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    annual_top: Optional[float] = None


def _has(text: str, terms) -> list:
    return [t for t in terms if t in text]


def parse_salary_text(text: str):
    """Parse Jooble-style '$110k - $135k' or '$21.75 - $24.88 per hour' -> (min, max, hourly)."""
    if not text:
        return None, None, False
    t = text.lower().replace(",", "")
    nums = []
    for m in re.finditer(r"\$?\s*(\d+(?:\.\d+)?)\s*(k)?", t):
        v = float(m.group(1))
        if m.group(2):
            v *= 1000
        nums.append(v)
    if not nums:
        return None, None, False
    hourly = "hour" in t or "/hr" in t or max(nums) < 500
    return min(nums), max(nums), hourly


def annual_top(job: Listing) -> Optional[float]:
    """Top of the posted band, annualized. Predicted (estimated) salaries are not comp data."""
    if job.salary_predicted:
        return None
    top = job.salary_max or job.salary_min
    if not top or top < 15:  # junk values like "1 - 6"
        return None
    if top < 500:  # hourly
        return top * P.HOURLY_ANNUALIZE
    return top


# A REMOTE_TERMS hit that reads as a duties sentence, not a location/workplace statement --
# "manage remote field teams", "supports remote sites", "virtual teams" -- never counts as the posting's
# own location. "virtual" added 2026-09-19 (B2 amendment): "virtual teams" is duties vocabulary, not a
# workplace statement, same as "remote teams".
# 2026-09-22: assistant/agent/chatbot/recruiter added. McKesson JR0153473 (hybrid, four non-commutable
# cities) was read as remote off one line of recruiting boilerplate -- "McKesson does rely on a virtual
# assistant (Gia) for certain recruiting-related communications with candidates" -- so the commute rule
# never ran. A virtual assistant is a piece of software, not a workplace.
_REMOTE_DUTY_RE = re.compile(
    r"\b(?:remote|virtual)\s+(?:\w+\s+){0,2}(?:team|teams|site|sites|staff|workforce|employee|employees|"
    r"office|offices|customer|customers|client|clients|user|users|support|assistant|assistants|agent|"
    r"agents|chatbot|chatbots|recruiter|recruiters)\b|\bremote\s+sensing\b", re.I)
# Locations too generic for an ATS on-site/hybrid flag to be trusted at all (Microsoft's
# "United States, Multiple Locations" onsite tag being the case that surfaced this).
_GENERIC_LOCATION_RE = re.compile(r"\b(?:multiple locations?|united states|nationwide)\b", re.I)

# "US Off-Site" is one employer's location-segment label for remote (sec 20.3), hyphen optional, any
# case. It is checked separately from REMOTE_TERMS/_remote_in_context because it can appear in
# job.locations (the plural list), which _remote_in_context never reads.
_US_OFFSITE_RE = re.compile(r"\bus\s+off-?site\b", re.I)

# 2026-09-19 (B2): boilerplate that mentions a REMOTE_TERMS word on a line but is not a statement about
# THIS posting's own workplace. Found via three blind-graded rows the old rule read as remote (an RTX
# hybrid role, a Booz Allen Atlanta req, a T. Rowe Price Baltimore req) -- none of the three JDs contain
# an actual remote/telework offer; each trips one of these four boilerplate shapes instead.
#
# 1. Explicit negation: "not a remote position", "no remote", "telework eligible: no".
_REMOTE_NEGATION_RE = re.compile(
    r"\bnot\s+(?:a\s+)?remote\b|\bno\s+remote\b|\btelework\s+eligible\s*:?\s*no\b", re.I)
# 2. A generic three-way workplace-type enumeration ("designated as on-site, hybrid or remote") that
#    classifies the CONCEPT without committing to which one this posting is -- the RTX case: "...regardless
#    of whether the role is designated as on-site, hybrid or remote."
_REMOTE_ENUMERATION_RE = re.compile(
    r"\b(?:on-?site|hybrid|remote)\b(?:\s*,\s*|\s+(?:or|and)\s+)\b(?:on-?site|hybrid|remote)\b"
    r"(?:\s*,\s*|\s+(?:or|and)\s+)\b(?:on-?site|hybrid|remote)\b", re.I)
# 3. A policy glossary explaining what "remote" WOULD mean if this posting were tagged that way -- the Booz
#    Allen case: "Remote: If this position is listed as remote, there may still be occasions..." (standard
#    boilerplate run on every req, not a per-posting fact).
_REMOTE_GLOSSARY_RE = re.compile(
    r"\bif\s+this\s+(?:position|role|job)\s+is\s+(?:listed|designated|classified)\s+as\s+remote\b", re.I)
# 4. A pay-transparency paragraph listing "remote workers" as one of several geographic compensation bands
#    -- the T. Rowe Price case: "$122,000 - $209,000 for the location of: Maryland, Colorado, Washington
#    and remote workers" -- a disclosure clause, not a statement of where THIS role sits.
_REMOTE_PAY_BAND_RE = re.compile(r"\bfor the location of\b", re.I)
# 5. Benefits / EEO paragraphs that happen to mention a remote term in passing (accommodation language,
#    equal-opportunity boilerplate) rather than describing the job's own workplace.
_REMOTE_EEO_RE = re.compile(
    r"equal employment opportunit|reasonable accommodation|protected veteran|gender identity|"
    r"sexual orientation|national origin|affirmative action", re.I)
# 6. A bare section heading ("Remote" / "Remote:") that introduces a policy glossary paragraph on its own
# line -- splitting the JD on "[\n.]+" (below) separates the heading from the sentence that actually
# explains it (the Booz Allen case: "Remote\n: If this position is listed as remote, ..." splits into the
# lone word "Remote" as its own line, which would otherwise match REMOTE_TERMS with zero context).
_REMOTE_BARE_HEADING_RE = re.compile(r"^\s*(?:remote|hybrid|on-?site|telework)\s*:?\s*$", re.I)
# 7. "virtually"/"virtual interview or meeting" describes HOW people communicate (camera-on policy,
# interview format), not WHERE the job sits -- distinct from _REMOTE_DUTY_RE's "virtual teams" (a duties
# noun phrase); this is the adverb/communication-mode usage.
_REMOTE_VIRTUAL_MODE_RE = re.compile(
    r"\bvirtually\b|\b(?:in.person or virtual|virtual or in.person)\b|\bvirtual\b(?:\s+\w+){0,2}\s+"
    r"(?:interview|interviews|meeting|meetings)\b", re.I)
# 8. 2026-09-22: a remote mention scoped to a NAMED POPULATION, or hedged across the employer's reqs in
#    general, is not a statement about THIS posting's workplace. Found on USAA R0120810 (HR Integration &
#    Planning Principal), which reached `candidate` with zero reasons: the only "remote" in its 8,199
#    characters is the military-spouse clause USAA runs on every req -- "We are proud to support
#    active-duty military spouses. USAA roles may offer remote or hybrid flexibility for active-duty
#    military spouses consistent with applicable policy and business needs." The posting's own workplace
#    sentence says the opposite ("requires an individual to be in the office 4 days per week", four named
#    cities, none commutable). It slipped past exclusions 1-7: only two of the three workplace words
#    appear, so the enumeration rule needs a third; and the sentence carries none of the EEO rule's
#    vocabulary. Two shapes are matched, both deliberately narrow:
#      (a) an offer scoped to a named group ("for active-duty military spouses"), and
#      (b) a hedged plural-subject sentence ("USAA roles may offer ...") -- about the req population, not
#          this req. Plural only: a singular "this position may offer remote work" is a real per-posting
#          fact and is left to _REMOTE_CONDITIONAL_RE below, which counts it as remote and flags it.
_REMOTE_POPULATION_SCOPED_RE = re.compile(
    r"\bfor\s+(?:active[-\s]?duty\s+)?military\s+spouses?\b|"
    r"\b(?:roles|positions|jobs|opportunities)\s+may\s+(?:offer|include|provide)\b", re.I)
_REMOTE_BOILERPLATE_RES = (_REMOTE_NEGATION_RE, _REMOTE_ENUMERATION_RE, _REMOTE_GLOSSARY_RE,
                           _REMOTE_PAY_BAND_RE, _REMOTE_EEO_RE, _REMOTE_BARE_HEADING_RE,
                           _REMOTE_VIRTUAL_MODE_RE, _REMOTE_POPULATION_SCOPED_RE)

# 2026-09-19 amendment (user ruling): conditional remote phrasing is a GENUINE possible-remote fact, not
# boilerplate -- "remote work may be considered for the right candidate" means the door is open, even
# though it is not a flat commitment. Counts toward is_remote (no reject) but earns a visible flag so the
# user can verify it before assuming full remote.
_REMOTE_CONDITIONAL_RE = re.compile(
    r"remote(?:\s+work)?\s+(?:will|may)\s+be\s+considered|remote\s+(?:is\s+)?considered\s+for\s+the\s+right\s+"
    r"candidate|remote\s+work\s+may\s+be\s+considered\s+for", re.I)


def _remote_in_context(text: str) -> bool:
    """A REMOTE_TERMS hit on a line/sentence that reads as a location or workplace statement --
    "Location: US-REMOTE with the ability to travel...", "This role is fully remote" -- as opposed
    to the same word used in passing inside a duties sentence (`_REMOTE_DUTY_RE`) or one of the boilerplate
    shapes in `_REMOTE_BOILERPLATE_RES` (negation, generic enumeration, policy glossary, pay-band
    disclosure, EEO/benefits paragraph). Conditional phrasing ("remote work may be considered") is
    deliberately NOT excluded here -- it is a genuine remote signal (see `_REMOTE_CONDITIONAL_RE` and the
    2026-09-19 amendment); `conditional_remote_phrase` below surfaces it as a flag."""
    for line in re.split(r"[\n.]+", text or ""):
        if not line.strip() or _REMOTE_DUTY_RE.search(line):
            continue
        if any(rx.search(line) for rx in _REMOTE_BOILERPLATE_RES):
            continue
        if _has(line.lower(), P.REMOTE_TERMS):
            return True
    return False


def conditional_remote_phrase(text: str) -> Optional[str]:
    """The matched phrase when the JD states remote is conditional ("will/may be considered", "considered
    for the right candidate") rather than a flat offer or a flat non-offer -- None otherwise. Used to add a
    visible, unpenalized flag (UNPENALIZED_FLAG_PATTERNS: `^remote is conditional`) without rejecting or
    silently treating the posting as fully remote."""
    m = _REMOTE_CONDITIONAL_RE.search(text or "")
    return m.group(0).strip() if m else None


# A location that carries a street address, suite/building token or an address separator is ONE office,
# however many of them an ATS lists. Used to keep the multi-city remote signature in is_remote() off
# employer office directories (Accenture, RTX) -- see the comment at its call site.
# A street-type word must FOLLOW a word ("Accenture Tower", "Duncan Avenue"), so a city that opens with
# one ("St. Louis, MO") is not read as an address. "fl" is deliberately absent: it is Florida far more
# often than a floor number.
_ADDRESS_LIKE_RE = re.compile(
    r"\d{2,}\s+\w|~|(?<=\w)\s+(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|"
    r"pkwy|parkway|hwy|highway|ste|suite|bldg|building|plaza|tower)\b\.?", re.I)


def is_remote(job: Listing) -> bool:
    workplace = job.extra.get("workplace_type")
    if workplace == "remote":  # the ATS's own flag (finder rows)
        return True
    # "US Off-Site" as a location segment always means remote, whichever field carries it (sec 20.3).
    offsite_blob = "\n".join([job.location or "", *(job.locations or []), job.description or ""])
    if _US_OFFSITE_RE.search(offsite_blob):
        return True
    # The full JD, not just the first 600 characters -- Blue Yonder's "Location: US-REMOTE with the
    # ability to travel up to 30%" line sits well past that cutoff. REMOTE_TERMS now also carries
    # "#li-remote" / "#bi-remote", so a JD footer tag reads here too; when #LI-Hybrid and #LI-Remote
    # both appear the ATS-flag branch below never returns False (jd_remote is already True), so the
    # posting is remote -- the user's ruling. A lone #LI-Hybrid never matches REMOTE_TERMS, so it adds
    # no signal either way, positive or negative.
    jd_remote = _remote_in_context(f"{job.title}\n{job.location}\n{job.description}")
    if workplace in ("hybrid", "onsite"):  # stated (or read from the JD) beats a guess...
        generic_location = bool(_GENERIC_LOCATION_RE.search(job.location or ""))
        if not (generic_location or jd_remote):
            return False
        # ...unless the ATS location is too generic to mean anything ("Multiple Locations", "United
        # States", nationwide), or the JD itself explicitly states remote -- the JD read wins.
    if jd_remote:
        return True
    # Same company + title posted in 3+ STATES is the multi-city "remote" signature (e.g. Coinbase).
    # Reworked 2026-09-22 after Guidehouse 39017 (three WA offices) was read as remote, which skips the
    # commute rule entirely. Three corrections, all found on real rows:
    #   1. Count STATES, not comma tails. The old parse took the text after the last comma as the state,
    #      silently assuming every ATS writes "City, ST". Guidehouse writes it reversed ("US - WA, Seattle"
    #      / "US - WA, Tacoma" / "US - WA, Olympia"), so three offices in ONE state parsed as three
    #      "states" -- {seattle, tacoma, olympia}. states_in() resolves codes and names in either order.
    #   2. Skip office DIRECTORIES. Accenture lists 43 street addresses ("Milwaukee, 790 N Milwaukee St,
    #      Corp"), RTX three plant addresses ("US-AZ-TUCSON-807A ~ 1151 E Hermans Rd ~ BLDG 807A"): both
    #      span many states and neither is remote -- you sit at one of the buildings. Correction 1 would
    #      otherwise have started waving them through, since the old parse only missed them by accident
    #      (it read "Corp" / nothing as the state). A street address is the tell.
    #   3. Require 3+ entries: one field naming three states is an address or a coverage area, not a spread.
    if sum(_ADDRESS_LIKE_RE.search(loc or "") is not None for loc in job.locations) * 2 >= len(job.locations):
        return False
    states = set().union(*(states_in(loc) for loc in job.locations)) if job.locations else set()
    return len(job.locations) >= 3 and len(states) >= 3


_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "DC": "district of columbia", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina", "SD": "south dakota",
    "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington",
    "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
}
_STATE_BY_NAME = {name: code for code, name in _STATES.items()} | {"d.c.": "DC", "d. c.": "DC"}
# Longest names first, so "west virginia" is read whole and never also as "virginia".
_STATE_NAME_RE = re.compile(r"(?<![a-z])(" + "|".join(re.escape(n) for n in sorted(_STATE_BY_NAME, key=len, reverse=True))
                            + r")(?![a-z])", re.I)
_STATE_CODE_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(_STATES) + r")(?![A-Za-z])")  # uppercase codes only


def states_in(location: str) -> set:
    """US state codes a location string names: an uppercase two-letter code or the full name."""
    found = {m.group(1) for m in _STATE_CODE_RE.finditer(location or "")}
    return found | {_STATE_BY_NAME[m.group(1).lower()] for m in _STATE_NAME_RE.finditer(location or "")}


def place_matches(location: str, places) -> list:
    """Configured places named in a location string, matched as whole words.

    The string is split on ';' and '|' so several locations in one field are judged separately.
    An entry "name, st" (st = a US state code) pins the state: the segment must name that state.
    So "springfield, il" matches "Springfield, IL", "US-IL-Springfield" and "Springfield, Illinois",
    but not "Springfield, MO", "Springfield, Ontario" or a bare "Springfield".
    "name, st?" makes the state optional: the segment may name that state or no US state at all, so a
    bare "Springfield" matches but "Springfield, MO" still does not. An entry without a state matches anywhere.
    """
    hits = []
    for entry in places or []:
        name, _, code = entry.rpartition(",")
        code = code.strip().upper()
        optional = code.endswith("?")
        code = code.rstrip("?")
        if not name.strip() or code not in _STATES:
            name, code, optional = entry, None, False
        name = name.strip().lower()
        if not name:
            continue
        for segment in re.split(r"[;|]", location or ""):
            if not re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", segment.lower()):
                continue
            if code:
                states = states_in(segment)
                if code not in states and not (optional and not states):
                    continue
            hits.append(entry)
            break
    return hits


# 2026-09-19 fix (blind-grading defects b1a/b1b, see profile.py's CLEARANCE_* comment):
_CLEARANCE_START_FIELD_RE = re.compile(
    r"minimum\s+clearance\s+required\s+to\s+start\s*:\s*([^\n]{0,120}?)(?:\.|;|\n|$)", re.I)
_CLEARANCE_EMPTY_VALUE_RE = re.compile(r"^\s*(?:none|n/?a|not\s+applicable)\s*$", re.I)
# A second structured field, "Security Clearance Status: Active and existing security clearance required
# after day 1" -- the employer states WHEN the clearance must exist. "after day 1" means it is obtained on
# the job (sponsored: the user's pass-through ruling), even though the words "active and existing" read as
# held; "on day 1" / "prior to start" means it must already be held. Found 2026-09-19 in the dry run: the
# proximity rule rejected a remote operations-analyst posting whose own text says "ability to obtain and
# maintain" because this field's wording tripped CLEARANCE_HELD_RE.
_CLEARANCE_TIMING_RE = re.compile(
    r"clearance\s+(?:is\s+)?required\s+(after|on|by|before|prior\s+to)\s+(?:day\s*(?:1|one)|start(?:ing)?(?:\s+date)?|hire)",
    re.I)


def _term_windows(blob: str, terms) -> list:
    """[(start, end, term)] for every occurrence of any of `terms` in blob (substring, not regex)."""
    out = []
    for term in terms:
        start = 0
        while True:
            idx = blob.find(term, start)
            if idx < 0:
                break
            out.append((idx, idx + len(term), term))
            start = idx + len(term)
    return out


def _obtain_governs(window: str, anchor_term: str) -> bool:
    """True when an "ability to obtain" phrase inside `window` governs `anchor_term` itself -- its object
    (the ~40 chars right after "obtain") names the anchor term or the word "clearance" generically -- as
    opposed to naming only "polygraph" while the anchor is a different, separately-stated term (bug b1b:
    "TS/SCI ... with ability to obtain a polygraph" must not launder the TS/SCI into "reachable" just
    because the polygraph is the obtainable half)."""
    for m in re.finditer(P.CLEARANCE_OBTAIN_RE, window):
        obj = window[m.end():m.end() + 70]
        if re.match(r"\W*(?:an?\s+)?poly", obj) and anchor_term != "polygraph":
            continue   # this occurrence governs only the polygraph, not this anchor
        if anchor_term in obj or "clearance" in obj or _clearance_level(obj):
            return True
        # anything else -- a certification, a badge, a licence -- is not the clearance and governs nothing
    return False


_CLEARANCE_LEVELS = ((4, r"ts\s*/\s*sci|\bsci\b|sensitive compartmented"), (3, r"top\s+secret"),
                     (2, r"secret"), (1, r"public\s+trust"))
_CLEARANCE_GENERIC_LEVEL = 9    # "ability to obtain a security clearance" with no level named: covers any level
# A "Nice If You Have:" / "Preferred Qualifications" heading makes every bullet under it soft, even though
# the bullet's own line never says "preferred" (2026-09-19 audit: "Top Secret clearance" listed under such a
# heading rejected a posting whose basic qualifications say "ability to obtain a Secret clearance").
_CLEARANCE_SOFT_HEADING_RE = re.compile(
    r"nice\s+if\s+you\s+have|nice\s+to\s+have|preferred\s+(?:qualifications|skills|experience)|desired\s*:|"
    r"desired\s+(?:qualifications|skills)|additional\s+qualifications|bonus\s+points", re.I)
_CLEARANCE_HARD_HEADING_RE = re.compile(
    r"basic\s+qualifications|minimum\s+(?:qualifications|requirements)|required\s*(?:qualifications|skills)?\s*:|"
    r"(?<!if )you\s+have\s*:|what\s+you\s+will\s+need|clearance\s*:", re.I)
_CLEARANCE_STRUCTURED_ACTIVE_RE = re.compile(r"clearance\s+required\s*:\s*active", re.I)


def _clearance_level(text: str) -> int:
    """Highest clearance level named in `text` (4 TS/SCI, 3 Top Secret, 2 Secret, 1 Public Trust), 0 if none."""
    for level, rx in _CLEARANCE_LEVELS:
        if re.search(rx, text, re.I):
            return level
    return 0


def _obtainable_level(blob: str) -> int:
    """The highest clearance level the JD says the candidate may OBTAIN (the user's pass-through ruling).
    The object of each "ability to obtain" phrase decides: a named level, the generic word "clearance"
    (covers any level), or something else entirely -- a polygraph, a certification -- which covers nothing."""
    best = 0
    for m in re.finditer(P.CLEARANCE_OBTAIN_RE, blob):
        obj = blob[m.end():m.end() + 70]
        level = _clearance_level(obj)
        if not level and re.search(r"clearance", obj) and not re.search(r"^\W*(?:an?\s+)?poly", obj):
            level = _CLEARANCE_GENERIC_LEVEL
        best = max(best, level)
    return best


def _under_soft_heading(blob: str, start: int) -> bool:
    """True when the nearest qualifications heading before `start` (within 700 chars) is a soft one."""
    before = blob[max(0, start - 700):start]
    soft = [m.end() for m in _CLEARANCE_SOFT_HEADING_RE.finditer(before)]
    if not soft:
        return False
    hard = [m.end() for m in _CLEARANCE_HARD_HEADING_RE.finditer(before)]
    return not hard or max(soft) > max(hard)


def clearance_call(blob: str) -> tuple:
    """The clearance question, resolved per-anchor with proximity rather than a single blob-wide search.
    Returns (verdict, detail):

    'held'      -- at least one CONFIDENT anchor (a hard/conditional level, or a direct "actively held"
                   phrase) is neither governed by a nearby "obtain" nor stated as merely preferred: screen.py
                   turns this into a REJECT reason.
    'sponsored' -- every anchor is governed by a nearby "ability to obtain" (P.CLEARANCE_PROXIMITY chars):
                   reachable, unpenalized flag, never a reject.
    'ambiguous' -- every anchor is stated as merely preferred/nice-to-have (and none is confidently held):
                   stays a flag, never a reject.
    None        -- no clearance language, or only incidental boilerplate (e.g. a compensation paragraph
                   listing "security clearances" among many unrelated nouns) -- no flag at all.

    'held' wins over 'ambiguous'/'sponsored' when anchors disagree (one confident hard requirement is
    enough to reject, even if the JD also name-drops an unrelated sponsored/preferred clearance elsewhere).
    """
    if not _has(blob, P.CLEARANCE_TERMS):
        return None, None

    # Structured field ("Minimum Clearance Required to Start: TS/SCI with Polygraph") is definitive on its
    # own unless its own value is empty/None/Not Applicable.
    m = _CLEARANCE_START_FIELD_RE.search(blob)
    if m:
        value = m.group(1).strip()
        if value and not _CLEARANCE_EMPTY_VALUE_RE.match(value):
            if re.search(P.CLEARANCE_OBTAIN_RE, value):
                return "sponsored", value
            if _obtainable_level(blob) >= max(_clearance_level(value), 1):
                # the field says it is needed to start, the qualifications say "ability to obtain" the same
                # level: the posting contradicts itself, so it is shown with a flag, never rejected.
                return "ambiguous", value
            return "held", value

    # Structured timing field (see _CLEARANCE_TIMING_RE). "after day 1", or an "ability to obtain" governing
    # the same sentence, is sponsored; "on day 1" / "prior to start" is held. A posting that states BOTH
    # contradicts itself and is shown with a flag, never rejected.
    timing = set()
    for m in _CLEARANCE_TIMING_RE.finditer(blob):
        when = re.sub(r"\s+", " ", m.group(1).lower())
        obtained = re.search(P.CLEARANCE_OBTAIN_RE, blob[max(0, m.start() - 110):m.start()])
        timing.add("sponsored" if when == "after" or obtained else "held")
    if timing == {"sponsored"}:
        return "sponsored", "obtainable (required after day 1 / before start)"
    if timing == {"held"}:
        return "held", "clearance required on day 1 / prior to start"
    if timing:
        return "ambiguous", "posting states both held and obtainable"

    hard_hits = _term_windows(blob, P.CLEARANCE_HARD_LEVELS)
    cond_hits = [(s, e, t) for s, e, t in _term_windows(blob, P.CLEARANCE_CONDITIONAL_LEVELS)
                if re.search(r"\bactive\b", blob[max(0, s - P.CLEARANCE_CONDITIONAL_PROXIMITY):
                                                  e + P.CLEARANCE_CONDITIONAL_PROXIMITY])]
    anchors = [(s, e, t) for s, e, t in hard_hits if t != "polygraph"] + cond_hits
    anchors += [(m.start(), m.end(), "active") for m in re.finditer(P.CLEARANCE_HELD_RE, blob)]
    if not anchors:
        # No hard/conditional level and no direct "held" phrasing, but generic clearance language (bare
        # "security clearance") with an "obtain" phrase anywhere: reachable.
        if re.search(P.CLEARANCE_OBTAIN_RE, blob):
            return "sponsored", None
        return None, None

    calls = []
    obtainable = _obtainable_level(blob)
    for s, e, t in anchors:
        soft_window = blob[max(0, s - 60):e + 60]
        if re.search(P.CLEARANCE_SOFT_RE, soft_window) or _under_soft_heading(blob, s):
            calls.append(("ambiguous", t))
            continue
        window = blob[max(0, s - P.CLEARANCE_PROXIMITY):e + P.CLEARANCE_PROXIMITY]
        if _obtain_governs(window, t):
            calls.append(("sponsored", t))
            continue
        # Level-aware: the JD explicitly lets the candidate OBTAIN this level (or a higher one) somewhere, so
        # a second mention of the same level elsewhere -- an employer's footer boilerplate ("Secret clearance
        # is required"), a duty ("maintain active Secret clearance") -- does not make it held. A HIGHER level
        # than the obtainable one still does ("TS/SCI ... ability to obtain a polygraph"), and so does the
        # structured "Clearance Required : Active ..." header.
        level = _clearance_level(blob[max(0, s - 12):e + 45]) or 2
        structured = _CLEARANCE_STRUCTURED_ACTIVE_RE.search(blob[max(0, s - 40):e + 10])
        if obtainable >= level:
            # a structured "Clearance Required : Active X" header against an explicit "able to obtain X" in the
            # qualifications is a self-contradicting posting: flag, never reject.
            calls.append(("ambiguous" if structured else "sponsored", t))
        else:
            calls.append(("held", t))
    for verdict in ("held", "ambiguous", "sponsored"):
        hit = next((t for c, t in calls if c == verdict), None)
        if hit is not None:
            return verdict, hit
    return None, None   # unreachable (calls is always non-empty here)


def commutable_locations(job: Listing) -> list:
    """The listing locations that name a commutable place, headline location first; [] when none do.

    COMMUTE_REMOTE_ONLY_PLACES are dropped when the ATS says the role sits in an office: a place can be
    close enough to satisfy a remote role's residence restriction and still be a punishing commute several
    days a week (2026-09-22 user ruling, on M&T R88502). Only an explicit hybrid/onsite flag triggers the
    narrowing -- an unknown workplace keeps every place, so this never rejects on a guess. §23's residence
    check reads P.COMMUTABLE_PLACES directly and is deliberately unaffected.
    """
    places = P.COMMUTABLE_PLACES
    remote_only = set(getattr(P, "COMMUTE_REMOTE_ONLY_PLACES", None) or ())
    if remote_only and job.extra.get("workplace_type") in ("hybrid", "onsite"):
        places = [p for p in places if p not in remote_only]
    return [loc for loc in [job.location, *job.locations] if loc and place_matches(loc, places)]


def is_commutable(job: Listing) -> bool:
    """A commutable place is named in one of the listing's locations (each checked on its own, so a
    state in one location never vouches for a city in another). A `hybrid` workplace flag alone is
    not commutable: hybrid in another metro is still out of the area."""
    return bool(commutable_locations(job))


# ---------------------------------------------------------------- §23: residence-restricted remote
# `screen.is_remote` trusts an ATS remote flag or a remote phrase in the JD. Some employers flag a
# posting remote and then restrict where the person may live: "must live within 50 miles of a hub
# office", "must be based in one of the following states". Anchored on a residence verb plus a
# restriction, or an explicit hub/eligible-place list. Reuses the boilerplate exclusion windows the
# 9/19 remote fix already carries (pay-transparency, EEO, policy glossaries) rather than new ones.
_RESIDENCE_TRIGGER_RES = (
    re.compile(r"must\s+(?:live|reside|be\s+located|be\s+based)\s+(?:in|within|near)", re.I),
    re.compile(r"within\s+\d+\s*(?:miles?|mi\.?)\s+of", re.I),
    re.compile(r"commut(?:able|ing)\s+distance\s+(?:to|of)", re.I),
    re.compile(r"remote\s+(?:in|within|from)\s+the\s+following\s+(?:states|locations)", re.I),
    re.compile(r"eligible\s+(?:states|locations)\b", re.I),
    re.compile(r"\bhub\s+(?:city|cities|office|offices|location|locations)\b", re.I),
)
# A sentence naming a "hub" must also carry a residence-obligation word ("must"/"required") somewhere in
# it -- a bare list of hub offices offered as an option ("or work from any of our hub offices") is not a
# restriction on its own; see _RESIDENCE_OPTION_RE below, which excludes that shape outright.
_RESIDENCE_LIVE_OBLIGATION_RE = re.compile(
    r"\b(?:must|required\s+to|needs?\s+to|have\s+to|expected\s+to)\s+(?:currently\s+)?"
    r"(?:live|reside|be\s+located|be\s+based|be\s+within|be\s+in)\b", re.I)
_RESIDENCE_HUB_OBLIGATION_RE = re.compile(r"\b(?:must|required|require)\b", re.I)

# Exclusions -- false-reject guards found 9/19 and in the 9/20 dry-run audit (§23 rework):
# a pay-transparency clause naming a location for comp-band purposes, a "states we cannot hire in"
# (exclude) list, an office list offered as an alternative, time-zone wording phrased as merely
# "preferred", a conditional clause that binds only people who already meet it ("if you live within
# 50 miles of..."), an outright preference ("preference will be given to candidates residing
# within..."), and a "state where <employer> has a registered entity" boilerplate line that covers
# most of the country and names no real place.
_RESIDENCE_PAY_TRANSPARENCY_RE = re.compile(
    r"for\s+(?:the\s+location\s+of|candidates?\s+(?:located|based)\s+in|employees?\s+(?:located|based)\s+in)",
    re.I)
_RESIDENCE_CANNOT_HIRE_RE = re.compile(
    r"(?:we\s+)?(?:are\s+unable|cannot|can\s*not|do\s+not|don.t|not\s+able)\s+to\s+hire\s+(?:in|from)|"
    r"unable\s+to\s+hire\s+(?:in|from)|(?:not|no)\s+(?:currently\s+)?hiring\s+in", re.I)
_RESIDENCE_OPTION_RE = re.compile(
    r"\bor\s+work\s+from\s+(?:any\s+of\s+|one\s+of\s+)?(?:our\s+|the\s+)?offices?\b|"
    r"\bwork\s+from\s+any\s+of\s+our\s+offices\b", re.I)
_RESIDENCE_TIMEZONE_PREFERRED_RE = re.compile(r"time\s*zone.{0,30}\bpreferred\b|\bpreferred\b.{0,30}time\s*zone", re.I)
# A sentence that opens on a condition ("If you live within...", "If employees are within...", "For
# those who live within...") imposes nothing on someone who does NOT meet the condition -- it is a
# perk/hybrid-cadence clause, not a residence gate. Anchored to the start of the sentence, not a
# search anywhere in it, so it never eats an unrelated "if" deep in an unconditional restriction.
_RESIDENCE_CONDITIONAL_RE = re.compile(
    r"^\s*if\s+(?:you|employees?|candidates?|team\s+members?)\b|"
    r"^\s*for\s+those\s+who\b|^\s*(?:employees?|candidates?)\s+who\b", re.I)
_RESIDENCE_PREFERENCE_RE = re.compile(
    r"preference\s+(?:will\s+be|is)\s+given|\bpreferred\b|\bideally\b|\ba\s+plus\b", re.I)
_RESIDENCE_REGISTERED_ENTITY_RE = re.compile(
    r"state\s+where\b.{0,80}?\b(?:has|is)\b.{0,30}?\b(?:registered|legal)\s+entity\b"
    # the sentence splitter cuts "...a state where Acme, Inc. has a registered entity" at "Inc.", so the
    # opening alone has to be enough: an employer-defined set of states names no place to check
    r"|\b(?:live|reside|be\s+located|be\s+based)\s+in\s+a\s+(?:state|location)\s+where\b", re.I)
_RESIDENCE_EXCLUSION_RES = (_RESIDENCE_PAY_TRANSPARENCY_RE, _RESIDENCE_CANNOT_HIRE_RE, _RESIDENCE_OPTION_RE,
                            _RESIDENCE_TIMEZONE_PREFERRED_RE, _RESIDENCE_CONDITIONAL_RE, _RESIDENCE_PREFERENCE_RE,
                            _RESIDENCE_REGISTERED_ENTITY_RE, *_REMOTE_BOILERPLATE_RES)

# Generic words that show up in the tail of a restriction sentence but never name a place -- filtered out
# of the extracted place list ("one of our hubs: A, B, C" -> "A, B, C", not "one of our hubs A B C").
# Deliberately does NOT include "state(s)" -- a lone "United States" fragment must still read as a whole
# country phrase (see _RESIDENCE_COUNTRY_RE), not get shredded into the meaningless leftover "United".
_RESIDENCE_FILLER_WORDS = {
    "one", "of", "our", "the", "a", "an", "these", "those", "following", "hub", "hubs", "office",
    "offices", "location", "locations", "and", "or", "must", "be", "in", "within", "near", "distance",
    "to", "commutable", "commuting", "miles", "mi", "eligible", "based", "live", "reside", "residing",
    "located", "headquarters", "hq", "area", "region", "vicinity", "team", "company",
}
# A bare country reference -- never a place a residence rule can act on. "Canada" is included
# unconditionally (the 9/19 ruling: never reject on it) rather than only "when listed beside the US".
_RESIDENCE_COUNTRY_RE = re.compile(
    r"^(?:the\s+)?(?:united\s+states(?:\s+of\s+america)?|u\.?s\.?a?\.?|america|canada)$", re.I)
# "US Citizen(ship)" is eligibility-to-work boilerplate that regularly rides along with a country
# mention ("...within the United States and US Citizen") -- stripped before splitting so it never
# survives as a bogus non-country candidate that would turn a country-only sentence into 'unclear'.
_RESIDENCE_CITIZENSHIP_RE = re.compile(r"\b(?:u\.?s\.?a?\.?)\s+citizen(?:ship)?\b", re.I)


def _split_places_delims(text: str) -> list:
    return re.split(r"\s*;\s*|\s+(?:or|and)\s+|\s*/\s*", text)


def _clean_words(text: str) -> str:
    text = re.sub(r"[’']s\b", "", text)   # drop a possessive ("McKesson's" -> "McKesson")
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z.]*", text) if w.lower() not in _RESIDENCE_FILLER_WORDS]
    return " ".join(words).strip()


def _leading_state(text: str):
    """The state name/code `text` STARTS with, or None -- used to pair a city with a state that runs
    on into the rest of the sentence with no comma to delimit it ("Irving, TX will be required to be
    onsite...": the comma-split part after "Irving" is "TX will be required...", not a bare "TX")."""
    text = text.strip()
    m = re.match(r"^([A-Za-z]{2})\b", text)
    if m and m.group(1).upper() in _STATES:
        return m.group(1).upper()
    m = _STATE_NAME_RE.match(text)
    return m.group(1) if m else None


def _candidate_places(tail: str) -> tuple:
    """Raw place-shaped candidates in the tail of a residence-restriction sentence (the text from
    the trigger phrase to the end of the sentence, capped at ~200 chars), before validation. Returns
    (candidates, saw_country): `saw_country` is True when at least one segment was a bare country
    reference (dropped, not a candidate) -- the caller uses it to tell "just a country, no real
    restriction" (kind 'none') apart from "named something, but nothing validates" (kind 'unclear').

    A colon introduces the list when present ("hubs: Austin, TX; Denver, CO", discarding whatever
    came before the colon -- a country-prefixed lead-in like "the United States: Rhode Island, ..."
    must not swallow the real list that follows). A "City, ST" pair is kept whole so the state stays
    pinned to its own city. A comma list that alternates city/state without "and"/"or" between pairs
    ("Windsor, CT, Boston, MA") is walked pairwise: a token immediately followed by a real state name
    or code is paired with it; anything left over is a candidate on its own (validated, or not, by
    the caller)."""
    if ":" in tail:
        tail = tail.split(":", 1)[1]
    tail = _RESIDENCE_CITIZENSHIP_RE.sub(" ", tail)
    tail = re.sub(r"\(.*?\)", "", tail).strip(" .")[:200]
    if not tail:
        return [], False
    candidates, saw_country = [], False
    for segment in _split_places_delims(tail):
        segment = segment.strip(" .")
        if not segment:
            continue
        if _RESIDENCE_COUNTRY_RE.match(segment):
            saw_country = True
            continue
        m = re.match(r"^(.+?),\s*([A-Za-z]{2})$", segment)
        if m and m.group(2).upper() in _STATES:
            city = _clean_words(m.group(1))
            candidates.append(f"{city}, {m.group(2).upper()}" if city else segment.strip())
            continue
        parts = [p.strip() for p in segment.split(",")]
        i = 0
        while i < len(parts):
            cur = _clean_words(parts[i])
            nxt = parts[i + 1].strip() if i + 1 < len(parts) else ""
            # `cur` pairs with a following state name/code as its own suffix only when `cur` is not
            # ALREADY a state itself -- "Rhode Island, Vermont" is two independent states in a list,
            # not a city named "Rhode Island" in the state of Vermont; "Austin, Texas" is a real pair.
            cur_is_state = cur.lower() in _STATE_BY_NAME or cur.upper() in _STATES
            st = None if cur_is_state else _leading_state(nxt)
            if cur and st:
                candidates.append(f"{cur}, {st}")
                i += 2
                continue
            if cur:
                if _RESIDENCE_COUNTRY_RE.match(cur):
                    saw_country = True
                else:
                    candidates.append(cur)
            i += 1
    return candidates, saw_country


def _validate_place(candidate: str) -> bool:
    """A candidate counts as a real place only if it validates: a US state name or code (anywhere in
    it -- covers a bare state and a "City, ST" pair alike), or a token `place_matches` finds in
    P.COMMUTABLE_PLACES. Anything else (a bare city with no state, a stray boilerplate fragment, a
    marketing sentence a trigger accidentally swallowed) is dropped -- the rule would rather flag it
    `unclear` than reject on a place it cannot actually confirm."""
    return bool(states_in(candidate)) or bool(place_matches(candidate, P.COMMUTABLE_PLACES))


def residence_restriction(text: str) -> tuple:
    """The residence-restriction question for a posting already read as remote (§23). Takes the
    ORIGINAL-CASE title/location/description text (like `conditional_remote_phrase`), not the
    lowercased `blob` screen() builds for the other rules -- `place_matches`/`states_in` need a real
    "City, ST" case to pin a two-letter state code, which a lowercased "st" can never match.

    Returns (kind, places, phrase):
        'none'    -- no restriction language found, or the only place-shaped text was a bare country
                     reference ("must reside in the United States"); (places=[], phrase=None or the
                     line, respectively -- screen() only acts on 'places'/'unclear').
        'places'  -- at least one candidate VALIDATES (a real state name/code, or a configured
                     commutable place); `places` holds only the validated ones, `phrase` is the
                     triggering sentence (quoted in the Why when it does not resolve to a pass).
        'unclear' -- restriction language whose candidates (if any) all fail validation; `phrase` is
                     the sentence, quoted verbatim so the user can verify -- never a reject (same
                     pass-through principle as `clearance_call`'s ambiguous verdict).
    """
    for sentence in re.split(r"[\n.]+", text or ""):
        line = sentence.strip()
        if not line:
            continue
        if any(rx.search(line) for rx in _RESIDENCE_EXCLUSION_RES):
            continue
        matches = []
        for rx in _RESIDENCE_TRIGGER_RES:
            m = rx.search(line)
            if not m:
                continue
            if "distance" in rx.pattern or "miles" in rx.pattern:
                # A distance phrase restricts residence only when the sentence obliges the person to LIVE
                # there. "Candidates within 50 miles of X will be required to be onsite 2 days a week" puts
                # an office duty on people who happen to live nearby and nothing on anyone else.
                if not _RESIDENCE_LIVE_OBLIGATION_RE.search(line):
                    continue
            if "hub" in rx.pattern and not _RESIDENCE_HUB_OBLIGATION_RE.search(line):
                continue   # a hub list with no obligation word is an office menu, not a restriction
            matches.append(m)
        if not matches:
            continue
        # The rightmost-ending trigger anchors the tail, so a more specific phrase further into the
        # sentence ("within 50 miles of") wins over a broader one earlier in it ("must live within"),
        # keeping the extracted tail free of the words between them.
        anchor = max(matches, key=lambda m: m.end())
        candidates, saw_country = _candidate_places(line[anchor.end():])
        places = [c for c in candidates if _validate_place(c)]
        if places:
            return "places", places, line[:200]
        if saw_country:
            return "none", [], None   # a country and no validated place: "must reside in the United States, and ..."
        return "unclear", [], line[:200]
    return "none", [], None


# ---------------------------------------------------------------- employer residence note (private)
# §23's residence_restriction() can only catch a restriction the JD text actually states. Some
# employers restrict remote hires to residents near one of a handful of hub offices without ever
# writing that policy into the posting -- the owner's own experience, not something a text rule can
# discover. backend.profile.EMPLOYER_RESIDENCE_NOTES holds these privately (default empty; set only in
# the gitignored profile_local.py), keyed by employer name or the ATS registry's employer slug --
# whichever text the note author used. Matching reuses norm_company/company_matches, the same
# normalizer the rest of the file already uses for employer names, so 'Acme Payments', 'ACME PAYMENTS
# INC.' and a registry slug like 'acme-payments' all resolve to one note.
def employer_residence_note(company: str) -> Optional[dict]:
    """Returns the EMPLOYER_RESIDENCE_NOTES entry matching `company`, or None."""
    notes = getattr(P, "EMPLOYER_RESIDENCE_NOTES", None) or {}
    if not notes or not (company or "").strip():
        return None
    key = norm_company(company)
    if not key:
        return None
    # EXACT match on the normalized name (or a parenthetical alias), never company_matches(): its
    # whole-word containment and shared-first-word fallbacks are right for dedup, too loose for a rule
    # that REJECTS -- a note on 'Acme' must not reject 'Acme Robotics'.
    mine = set(company_keys(company)) | {key}
    for raw_key, note in notes.items():
        if mine & set(company_keys(raw_key)):
            return note
    return None


def screen(job: Listing, tracker_rows=None, recent_titles=None, *, skip_tracker: bool = False) -> Listing:
    """Card-level screen. `skip_tracker=True` bypasses section 9 (the finder dedups in SQL)."""
    title = f" {job.title.lower()} "
    company = job.company.lower()
    desc = job.description.lower()
    blob = f"{title} {desc}"
    reasons, flags = [], []

    # 1. Hard skips on the poster
    for name, why in P.BLOCKED_POSTERS.items():
        if re.search(rf"\b{re.escape(name)}\b", company):
            reasons.append(why)
    if _has(title, P.AI_GIG_TITLE_TERMS):
        reasons.append("AI-data gig, not an employed or consulting engagement")
    if ("advisor" in title or "coach" in title) and re.search(r"starting at \$\d{3}k", blob):
        reasons.append("Advisor/Coach fraud template")

    # 2. Function first -- level is layered on top, never the other way around
    function_hits = _has(title, P.TITLE_FUNCTION_TERMS)
    if not function_hits:
        if (job.source == "USAJobs" and _has(title, P.FEDERAL_FUNCTION_TITLES)
                and _has(desc, [p.lower() for p in P.FUNCTION_PHRASES])):
            flags.append("federal analyst title -- function appears in duties")
        else:
            reasons.append("off-function title")
    platform = _has(title, P.PLATFORM_GATED_TERMS)
    if platform:
        flags.append(f"platform-gated title ({platform[0].strip()}) -- check Required vs Preferred")
    if _has(company, P.AGGREGATOR_POSTERS):
        flags.append("aggregator repost -- find the employer's own requisition")

    off = _has(title, P.OFF_LANE_TITLE_TERMS)
    if off:
        reasons.append(f"off-lane title ({off[0].strip()})")
    junior = [] if _has(title, P.ASSOCIATE_OK) else _has(title, P.JUNIOR_TITLE_TERMS)
    if " associate " in title and not _has(title, P.ASSOCIATE_OK):
        junior.append("associate")
    if junior:
        reasons.append(f"below target level ({junior[0].strip()})")

    # 3. Change Management collisions
    if "change" in title and _has(blob, P.ITSM_CHANGE_TERMS):
        reasons.append("ITIL/ITSM change control, not adoption")

    # 4. Discipline test -- engineer titles and plant vocabulary
    plant = _has(blob, P.PLANT_DISCIPLINE_TERMS)
    if "engineer" in title and plant:
        reasons.append(f"different discipline (plant/industrial: {plant[0]})")
    elif plant:
        flags.append(f"plant/manufacturing vocabulary on listing ({plant[0]}) -- read REQUIRED quals")
    elif "engineer" in title:
        flags.append("engineer title -- apply the discipline test on the live JD")
    if _has(blob, P.HARD_AVOID_INDUSTRY_TERMS):
        reasons.append("hard-avoid industry")

    # 5. Clearance -- a blocker only when it must already be HELD. An employer asking for the "ability
    #    to obtain" sponsors and funds it, so that is reachable and must not be flagged away. 2026-09-19:
    #    a CONFIDENTLY held clearance is now a REJECT reason (it leaves the rank, like commute/pay), not
    #    merely a flag -- see clearance_call above and profile.py's CLEARANCE_* comment.
    clearance_verdict, clearance_detail = clearance_call(blob)
    if clearance_verdict == "held":
        reasons.append(f"clearance must already be held -- likely unreachable ({clearance_detail})"
                       if clearance_detail else "clearance must already be held -- likely unreachable")
    elif clearance_verdict == "sponsored":
        flags.append("clearance is sponsored (ability to obtain) -- reachable")
    elif clearance_verdict == "ambiguous":
        flags.append(f"clearance requirement unclear ({clearance_detail}) -- verify" if clearance_detail
                    else "clearance requirement unclear -- verify")

    # 6. Comp -- overlap test on the band top; unposted is never a rejection
    top = annual_top(job)
    job.annual_top = top
    if top is not None and P.COMP_FLOOR and top < P.COMP_FLOOR:
        reasons.append(f"comp: band top ${top:,.0f} under the ${P.COMP_FLOOR:,} floor")
    elif top is not None and P.COMP_ASK and top < P.COMP_ASK:
        flags.append(f"${P.COMP_ASK / 1000:,.0f}K ask sits above the ${top:,.0f} top")

    # 7. Location -- remote anywhere, or inside the commute radius
    remote = is_remote(job)
    local = job.is_local_pass or is_commutable(job)
    nationwide = job.location.strip().lower() in P.NATIONWIDE_LOCATIONS
    if nationwide and not remote:
        flags.append("nationwide listing -- remote status unverified")
    elif not remote and not local:
        reasons.append("not remote and outside the commute area (per listing)")
    if local and not remote:
        # Name the location that carried the match. On a multi-site posting the headline location is
        # routinely out of the area while one alternate site is inside it (M&T R88502: headline
        # Wilmington, DE, one of its eight sites inside COMMUTABLE_PLACES). Without the detail the flag
        # reads as if the commute rule had ignored the other sites, when it walked all of them and found
        # a hit -- a false bug report the reviewer then has to chase. Verdict-neutral: the "^local/hybrid"
        # prefix is what UNPENALIZED_FLAG_PATTERNS anchors on, so scoring is untouched either way.
        hits = commutable_locations(job)
        if hits and not place_matches(job.location or "", P.COMMUTABLE_PLACES):
            flags.append(f"local/hybrid via {hits[0]} (headline: {job.location}) "
                         f"-- judge on route, not radius")
        else:
            flags.append("local/hybrid -- judge on route, not radius")
    # 2026-09-19 amendment: conditional remote phrasing ("remote work may be considered for the right
    # candidate") is a genuine possible-remote fact, not boilerplate -- it must never cost the posting a
    # reject, but it is worth surfacing so the user verifies before assuming a flat remote offer.
    cond = conditional_remote_phrase(f"{job.title}\n{job.location}\n{job.description}")
    if cond:
        flags.append(f'remote is conditional ("{cond}") -- verify')
    # §23: a posting read as remote can still restrict where the person lives. Applies only when the
    # posting was already read as remote -- an on-site/hybrid posting is handled by the commute rule above.
    if remote:
        home_states = states_in(P.HOME or "")
        kind, places, phrase = residence_restriction(f"{job.title}\n{job.location}\n{job.description}")
        if kind == "places":
            matched = any(place_matches(p, P.COMMUTABLE_PLACES) or (states_in(p) & home_states) for p in places)
            if not matched:
                reasons.append(f"remote restricted to: {', '.join(places)}")
        elif kind == "unclear":
            flags.append(f'remote-residence-check ("{(phrase or "")[:140]}")')
        # Private supplement: only consulted when the JD text itself said nothing (kind == "none") --
        # an employer note is a fallback for what the text rule cannot catch, never a second vote on a
        # restriction the text rule already resolved one way or the other. Reason/flag text is
        # deliberately generic (never the note's hub cities or free text) so nothing personal reaches a
        # committed artifact if a Why string is ever copied into one.
        if kind == "none":
            note = employer_residence_note(job.company)
            if note:
                hubs = note.get("hubs") or []
                if hubs and any(place_matches(h, P.COMMUTABLE_PLACES) for h in hubs):
                    flags.append("employer-residence-note -- reachable (commutable hub)")
                else:
                    reasons.append("remote restricted to: employer residence note (user)")

    # 8. Mission signal
    if _has(f"{company} {desc}", P.FAITH_SIGNALS):
        flags.append("faith-based signal -- $140K-$180K floor applies")

    # 9. Already tracked / already surfaced
    if tracker_rows and not skip_tracker:
        ck = norm_company(job.company)
        hits = [r for r in tracker_rows if company_matches(ck, r["company_keys"])]
        same_role = [r for r in hits if similar_title(job.title, r["role"])]
        if same_role:
            r = same_role[0]
            reasons.append(f"already in Application_Tracker ({r['section']} {r['date']}: {r['role'][:60]})")
        elif hits:
            flags.append(f"employer has {len(hits)} tracker row(s)")
    if recent_titles and not skip_tracker:
        key = (norm_company(job.company), norm_title(job.title))
        if key in recent_titles:
            reasons.append(f"already surfaced in {recent_titles[key]}")

    # Score for ordering only; a Tier 1 hit must never sit below a pile of Tier 2 matches
    job.extra["tier"] = 1 if _has(title, P.PRECISE_TITLE_TERMS) else 2
    score = 10 * len(function_hits) + (15 if job.extra["tier"] == 1 else 0)
    score += 5 if _has(title, P.SENIOR_TITLE_TERMS) else 0
    score += 5 if remote else 0
    score += 5 if top and P.COMP_ASK and top >= P.COMP_ASK else (2 if top else 0)
    score -= 3 * len(flags)

    job.reasons, job.flags, job.score = reasons, flags, score
    job.verdict = "reject" if reasons else ("review" if flags else "candidate")
    return job


_CO_SUFFIX = re.compile(r"\b(inc|llc|ltd|corp|corporation|company|co|group|holdings|the|plc|lp|l\.l\.c)\b\.?")


def norm_company(name: str) -> str:
    n = re.sub(r"\(.*?\)", "", (name or "").lower())
    n = n.replace("*", "")
    n = _CO_SUFFIX.sub("", n)
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


def company_keys(name: str) -> list:
    """Main name plus any parenthetical alias -- 'Eagle One Solutions (Chenega ...)' matches both."""
    keys = [norm_company(name)]
    keys += [norm_company(m) for m in re.findall(r"\((.*?)\)", name or "")]
    return [k for k in keys if len(k) >= 3]


def company_matches(key: str, keys: list) -> bool:
    """Whole-word containment either way, so 'ey' never matches 'honeywell'."""
    if not key:
        return False
    padded = f" {key} "
    if any(f" {k} " in padded or padded in f" {k} " for k in keys):
        return True
    # 'Sentara Hospitals' vs 'Sentara Health': a distinctive first word is enough.
    first = key.split()[0]
    return (len(first) >= 5 and first not in _GENERIC_FIRST_WORDS
            and any(k.split()[0] == first for k in keys))


_GENERIC_FIRST_WORDS = {
    "american", "united", "general", "national", "global", "international", "first", "south",
    "north", "west", "east", "central", "pacific", "atlantic", "advanced", "applied", "strategic",
    "systems", "health", "digital", "federal", "premier", "integrated", "professional", "capital",
    "office", "department", "internal", "defense", "bureau", "state", "county", "commonwealth",
}


def norm_title(title: str) -> str:
    t = re.sub(r"\(.*?\)", "", (title or "").lower())
    t = re.sub(r"\b(remote|hybrid|onsite|on-site)\b", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def similar_title(a: str, b: str) -> bool:
    ta, tb = set(norm_title(a).split()), set(norm_title(b).split())
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.6
