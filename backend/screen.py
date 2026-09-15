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


def is_remote(job: Listing) -> bool:
    if job.extra.get("workplace_type") == "remote":  # the ATS's own flag (finder rows)
        return True
    text = f"{job.title} {job.location} {job.description[:600]}".lower()
    if _has(text, P.REMOTE_TERMS):
        return True
    # Same company + title posted in 3+ states is the multi-city "remote" signature (e.g. Coinbase).
    states = {loc.split(",")[-1].strip().lower() for loc in job.locations if "," in loc}
    return len(states) >= 3


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
    but not "Springfield, MO", "Springfield, Ontario", "8351 W Springfield" or a bare "Springfield"
    (a city with no state is ambiguous). An entry without a state code matches anywhere.
    """
    hits = []
    for entry in places or []:
        name, _, code = entry.rpartition(",")
        code = code.strip().upper()
        if not name.strip() or code not in _STATES:
            name, code = entry, None
        name = name.strip().lower()
        if not name:
            continue
        for segment in re.split(r"[;|]", location or ""):
            if not re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", segment.lower()):
                continue
            if code and code not in states_in(segment):
                continue
            hits.append(entry)
            break
    return hits


def is_commutable(job: Listing) -> bool:
    """A commutable place is named in one of the listing's locations (each checked on its own, so a
    state in one location never vouches for a city in another). A `hybrid` workplace flag alone is
    not commutable: hybrid in another metro is still out of the area."""
    return any(place_matches(loc, P.COMMUTABLE_PLACES) for loc in [job.location, *job.locations] if loc)


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

    # 5. Clearance
    if _has(blob, P.CLEARANCE_TERMS):
        flags.append("clearance language on listing -- likely unreachable")

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
        flags.append("local/hybrid -- judge on route, not radius")

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
