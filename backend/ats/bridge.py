"""Pure helpers for the bridge-role track (sprint plan §31): parsing a place, deciding whether
a posting's location text actually names that place, and an advisory "voice" flag from the
title. No network, no DB -- everything here is table-tested in tests/test_bridge.py.

Why a separate module rather than adapters.py / store.py: the location check in particular has
to be right before a single live pull happens (§31.3's own probe found a free-text place search
returning a posting in a DIFFERENT town whose name contains the target -- "Prince Springfield, XX"
must never match a search for "Springfield, XX"), so it is isolated here where it can be tested
against a table of location-text shapes without touching the network or the DB.
"""
import re

# Per-place pull safety valve (sprint plan §31.3): a place-scoped CXS/Eightfold query stops after
# this many pages, logging a warning rather than looping forever on a place whose search terms
# turn out to be too broad. A page cap trip means the pull for THAT PLACE is not trusted for the
# close pass (see store.record_bridge_board).
PLACE_PAGE_CAP = 5

# The literal place name that means "the board's own remote facet / remote wording", never a real
# city. Matched case-insensitively wherever a place's `place` or `search_text` column is checked.
REMOTE_PLACE = "remote"

# Orchestrator audit 2026-09-21, letter E: a bridge posting whose `last_seen_at` has not moved in
# this many days is hidden by `finder.py bridge` / `vw_bridge_open` (unless `--include-stale`).
# This is what actually retires a place removed from bridge_places.csv, or heals a transient-empty
# pull that letter D's zero-result guard left open: staleness, not the close pass, does that work.
BRIDGE_STALE_DAYS = 10


def is_remote_place(place: str) -> bool:
    return (place or "").strip().lower() == REMOTE_PLACE


# Orchestrator audit 2026-09-21, letter C.6: remote wording recognized on the LOCATION field only
# (never on a title or JD) -- "remote" plus the synonyms the audit named.
_REMOTE_TEXT_RE = re.compile(
    r"\b(?:remote|virtual|work[- ]from[- ]home|work[- ]at[- ]home|wfh|telecommute|anywhere in the us)\b",
    re.I)


def _is_remote_text(text: str) -> bool:
    return bool(_REMOTE_TEXT_RE.search(text or ""))


# Orchestrator audit 2026-09-21, letter C.5: a multi-site facet string ("2 Locations") is dropped
# from a place's matches, same as before, but is now counted SEPARATELY from an ordinary
# non-matching drop so the per-place log line can show it as its own number.
_MULTI_SITE_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def is_multi_site_text(text: str) -> bool:
    return bool(_MULTI_SITE_RE.match((text or "").strip()))


# ---------------------------------------------------------------- US state name <-> abbreviation
# Public knowledge, not a place invented for this repo (per the fix instructions: "the US state
# table is fine"). Used only to let a location string spell a state out in full ("City, Statename")
# rather than the two-letter code the original shapes assumed (sprint plan §31 orchestrator audit,
# letter C.2).
US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY", "district of columbia": "DC",
}
# Longest-first so "new hampshire" is tried before a shorter alternative could steal a prefix.
_STATE_NAME_ALT = "|".join(re.escape(k) for k in sorted(US_STATES, key=len, reverse=True))


def _normalize_state(state: str) -> str:
    """A 2-letter code stays a 2-letter code (upper-cased); a full state name maps to its code;
    anything else (the `remote` pseudo-state, an unrecognized string) is returned unchanged --
    kept permissive, same spirit as parse_place's own "not something a pure function should raise
    on" note."""
    s = (state or "").strip()
    if len(s) == 2:
        return s.upper()
    return US_STATES.get(s.lower(), s)


# ---------------------------------------------------------------- location parsing
# Every shape probed in §31.1, plus the ones the 2026-09-21 orchestrator audit (letter C) added:
# plain "City, ST"; a Workday store-code form "(USA) ST CITY 01234 STORE NAME"; a dash-separated
# "ST - Area - City"; a leading store-number form "239 - City, ST"; a one-dash "ST - City"; and
# three full-state-name shapes -- "City, Statename", "CITY Statename" (upper-case city, no comma),
# "Statename - City". Each pattern captures a CITY field and a STATE field as discrete groups --
# the match is always on those two fields taken as whole values, never a substring search over
# the raw text, which is what keeps "Prince Springfield, XX" from matching a search for
# "Springfield, XX" (its city field is "Prince Springfield", not "Springfield") in EVERY one of
# these shapes.
_CITY = r"[A-Za-z][A-Za-z .'\-]*?"
_ST = r"[A-Za-z]{2}"
_ST_NAME = rf"(?:{_STATE_NAME_ALT})"

_PATTERNS = (
    # "(USA) ST CITY 01234 STORE NAME"
    re.compile(rf"\(USA\)\s+(?P<state>{_ST})\s+(?P<city>{_CITY})\s+\d", re.I),
    # "239 - City, ST" (a leading store number before the city/state)
    re.compile(rf"^\s*\d+\s*-\s*(?P<city>{_CITY})\s*,\s*(?P<state>{_ST})\s*$", re.I),
    # "ST - Area - City"
    re.compile(rf"^\s*(?P<state>{_ST})\s*-\s*[^-]+-\s*(?P<city>{_CITY})\s*$", re.I),
    # "ST - City" (one-dash form, letter C.1)
    re.compile(rf"^\s*(?P<state>{_ST})\s*-\s*(?P<city>{_CITY})\s*$", re.I),
    # "Statename - City" (letter C.2)
    re.compile(rf"^\s*(?P<state>{_ST_NAME})\s*-\s*(?P<city>{_CITY})\s*$", re.I),
    # "City, Statename" (letter C.2)
    re.compile(rf"^\s*(?P<city>{_CITY})\s*,\s*(?P<state>{_ST_NAME})\s*$", re.I),
    # "CITY Statename" -- upper-case city token(s), no comma (letter C.2). Case-insensitivity is
    # scoped to the state alternation only ((?i:...)) so [A-Z] still means "really upper-case" --
    # an outer re.I here would also let a lower-case city through, which is the one shape the
    # audit did NOT ask for.
    re.compile(rf"^\s*(?P<city>[A-Z][A-Z .'\-]*?)\s+(?P<state>(?i:{_ST_NAME}))\s*$"),
    # plain "City, ST" -- tried last so the more specific store-code forms win when they also
    # happen to contain a comma (the store-number form above does)
    re.compile(rf"^\s*(?P<city>{_CITY})\s*,\s*(?P<state>{_ST})\s*$", re.I),
)

# City-only text, no state at all -- e.g. a per-store facet board's "Springfield (0350)" (letter
# C.3). Only ever consulted when the place row opts in via `allow_no_state`.
_CITY_ONLY_RE = re.compile(rf"^\s*(?P<city>{_CITY})\s*(?:\(\s*\d+\s*\))?\s*$")


def _extract_city_state(text: str):
    """(city, state) from ONE location-text shape, or None. Tries every known shape; the first
    that matches the WHOLE (stripped) text wins. `state` is always normalized to a 2-letter code
    (or left as-is for the `remote` pseudo-state / anything unrecognized)."""
    text = (text or "").strip()
    if not text:
        return None
    for pat in _PATTERNS:
        m = pat.match(text) if pat.pattern.startswith("^") else pat.search(text)
        if m:
            return m.group("city").strip(), _normalize_state(m.group("state").strip())
    return None


def location_matches(location_text: str, place_city: str, place_state: str, allow_no_state: bool = False) -> bool:
    """True when `location_text` names the given place as a WHOLE city + the right state, in any
    of the shapes _PATTERNS knows. Case-insensitive. A location string that names several places
    (';' or '|' separated, as `locations` JSON is sometimes flattened before this is called) is
    checked piece by piece -- any piece matching is enough.

    `place_state` of "remote" (via is_remote_place) switches to a remote-wording check instead of
    a city/state match -- see the `remote` pseudo-place in bridge_places.csv (§31.3).

    `allow_no_state` (orchestrator audit 2026-09-21, letter C.3): when a piece names the city with
    NO state or store-code shape recognized (a per-store facet board that only ever prints
    "Springfield (0350)"), it is accepted ONLY when the caller passes this -- default off, so a
    care-less board with genuinely ambiguous city-only text never gets matched by accident. Still
    a WHOLE-city match, so "Prince Springfield (0350)" still fails against "Springfield".
    """
    if is_remote_place(place_state) or is_remote_place(place_city):
        return _is_remote_text(location_text)
    if not location_text or not place_city or not place_state:
        return False
    want_city = place_city.strip().lower()
    want_state = _normalize_state(place_state)
    for piece in re.split(r"[;|]", location_text):
        piece = piece.strip()
        parsed = _extract_city_state(piece)
        if parsed:
            city, state = parsed
            if city.lower() == want_city and state.lower() == str(want_state).lower():
                return True
            continue
        if allow_no_state:
            m = _CITY_ONLY_RE.match(piece)
            if m and m.group("city").strip().lower() == want_city:
                return True
    return False


def location_matches_fields(city: str, state: str, place_city: str, place_state: str,
                            allow_no_state: bool = False) -> bool:
    """Seam for an adapter that gets city and state as separate fields rather than one location
    string (orchestrator audit 2026-09-21, letter C.4): builds the same "City, ST"-shaped text
    `location_matches` already knows how to parse, so no platform ever needs its own parsing
    logic. No adapter uses separate city/state fields today -- this exists for the day one does."""
    text = ", ".join(x for x in (city, state) if x)
    return location_matches(text, place_city, place_state, allow_no_state=allow_no_state)


def parse_place(place: str):
    """('City', 'ST') from a registry `place` value like "Springfield, XX", or (place, '') for
    the `remote` pseudo-place (no state to match)."""
    if is_remote_place(place):
        return place, ""
    parsed = _extract_city_state(place)
    if parsed:
        return parsed
    # No comma in the configured place string -- treat the whole thing as the city with no state
    # (kept permissive here; a bad bridge_places.csv row is the user's to fix, not something a
    # pure function should raise on).
    return place.strip(), ""


# ---------------------------------------------------------------- voice flag (advisory only)
# A rough, non-authoritative signal for how much of the shift is spent talking to the public,
# from the title alone. `high` / `low` / '' (no opinion) -- never hidden, never used to filter
# anything (sprint plan §31.6: "advisory only, nothing hidden"; voice-heavy roles are a poor fit,
# so the finder's `bridge` output sorts low-voice first, but every row still prints).
#
# Orchestrator audit 2026-09-21, letter H: every keyword here is matched on a WORD boundary
# (a multi-word keyword as a phrase), not a bare substring -- "server" no longer lights up inside
# "hostler" and "administrator" no longer decides a title that is plainly a technical/IT role (see
# the SQL short-circuit below).
VOICE_HIGH = ("cashier", "greeter", "call center", "call centre", "phone", "customer service",
              "front end", "sales associate", "barista", "host", "hostess", "sales",
              "client advisor", "service advisor", "bdc", "teller", "receptionist", "concierge",
              "server")
VOICE_LOW = ("stock", "stocker", "overnight", "night crew", "receiving", "inventory",
             "fulfillment", "fulfilment", "order picker", "personal shopper", "cart",
             "maintenance", "porter", "driver", "warehouse", "business office", "clerk",
             "title", "warranty", "deal processor", "accounting", "accounts payable", "payroll",
             "data entry", "administrator", "merchandising", "pricing", "scan coordinator",
             "reconditioning", "detailer")
# "consultant" is only a HIGH signal alongside "sales" (a bare "IT Consultant" title says
# nothing about voice work) -- handled as its own check rather than a plain substring.
_VOICE_CONSULTANT_SALES_RE = re.compile(r"\bsales\b.*\bconsultant\b|\bconsultant\b.*\bsales\b", re.I)
# A title naming "SQL" is a technical/IT posting, not a retail voice-work question at all (a
# bridge board's "SQL Server Administrator" is not the same "server"/"administrator" this table
# means) -- short-circuits to no-opinion before either table is even checked.
_VOICE_SQL_RE = re.compile(r"\bsql\b", re.I)


def _word_res(keywords):
    return [re.compile(r"\b" + r"\s+".join(re.escape(w) for w in kw.split()) + r"\b", re.I) for kw in keywords]


_VOICE_HIGH_RES = _word_res(VOICE_HIGH)
_VOICE_LOW_RES = _word_res(VOICE_LOW)


def voice_flag(title: str) -> str:
    """'high' / 'low' / '' (no opinion). A title matching both tables is HIGH -- a poor voice-work
    fit is checked first and wins over any low-voice keyword also present."""
    t = title or ""
    if _VOICE_SQL_RE.search(t):
        return ""
    if any(r.search(t) for r in _VOICE_HIGH_RES) or _VOICE_CONSULTANT_SALES_RE.search(t):
        return "high"
    if any(r.search(t) for r in _VOICE_LOW_RES):
        return "low"
    return ""


# Sort order for the `high`/`low`/'' voice flag inside one ring/employer group (sprint plan
# §31.6, user ruling 2026-09-21): low-voice first (the better fit), then no opinion, then
# high-voice LAST -- voice-heavy roles are a poor fit, so they sort to the bottom, never off.
VOICE_SORT_ORDER = {"low": 0, "": 1, "high": 2}
