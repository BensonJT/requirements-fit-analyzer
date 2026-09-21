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


def is_remote_place(place: str) -> bool:
    return (place or "").strip().lower() == REMOTE_PLACE


_REMOTE_TEXT_RE = re.compile(r"\bremote\b", re.I)


def _is_remote_text(text: str) -> bool:
    return bool(_REMOTE_TEXT_RE.search(text or ""))


# ---------------------------------------------------------------- location parsing
# Every shape probed in §31.1: plain "City, ST"; a Workday store-code form "(USA) ST CITY 01234
# STORE NAME"; a dash-separated "ST - Area - City"; and a leading store-number form
# "239 - City, ST". Each pattern captures a CITY field and a STATE field as discrete groups --
# the match is always on those two fields taken as whole values, never a substring search over
# the raw text, which is what keeps "Prince Springfield, XX" from matching a search for
# "Springfield, XX" (its city field is "Prince Springfield", not "Springfield").
_CITY = r"[A-Za-z][A-Za-z .'\-]*?"
_ST = r"[A-Za-z]{2}"

_PATTERNS = (
    # "(USA) ST CITY 01234 STORE NAME"
    re.compile(rf"\(USA\)\s+(?P<state>{_ST})\s+(?P<city>{_CITY})\s+\d", re.I),
    # "239 - City, ST" (a leading store number before the city/state)
    re.compile(rf"^\s*\d+\s*-\s*(?P<city>{_CITY})\s*,\s*(?P<state>{_ST})\s*$", re.I),
    # "ST - Area - City"
    re.compile(rf"^\s*(?P<state>{_ST})\s*-\s*[^-]+-\s*(?P<city>{_CITY})\s*$", re.I),
    # plain "City, ST" -- tried last so the more specific store-code forms win when they also
    # happen to contain a comma (the store-number form above does)
    re.compile(rf"^\s*(?P<city>{_CITY})\s*,\s*(?P<state>{_ST})\s*$", re.I),
)


def _extract_city_state(text: str):
    """(city, state) from ONE location-text shape, or None. Tries every known shape; the first
    that matches the WHOLE (stripped) text wins."""
    text = (text or "").strip()
    if not text:
        return None
    for pat in _PATTERNS:
        m = pat.match(text) if pat.pattern.startswith("^") else pat.search(text)
        if m:
            return m.group("city").strip(), m.group("state").strip()
    return None


def location_matches(location_text: str, place_city: str, place_state: str) -> bool:
    """True when `location_text` names the given place as a WHOLE city + the right state, in any
    of the shapes _PATTERNS knows. Case-insensitive. A location string that names several places
    (';' or '|' separated, as `locations` JSON is sometimes flattened before this is called) is
    checked piece by piece -- any piece matching is enough.

    `place_state` of "remote" (via is_remote_place) switches to a remote-wording check instead of
    a city/state match -- see the `remote` pseudo-place in bridge_places.csv (§31.3).
    """
    if is_remote_place(place_state) or is_remote_place(place_city):
        return _is_remote_text(location_text)
    if not location_text or not place_city or not place_state:
        return False
    for piece in re.split(r"[;|]", location_text):
        parsed = _extract_city_state(piece)
        if not parsed:
            continue
        city, state = parsed
        if city.lower() == place_city.strip().lower() and state.lower() == place_state.strip().lower():
            return True
    return False


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
VOICE_HIGH = ("cashier", "greeter", "call center", "call centre", "phone", "customer service",
              "front end", "sales associate", "barista", "host", "sales", "client advisor",
              "service advisor", "bdc", "teller", "receptionist", "concierge", "server")
VOICE_LOW = ("stock", "stocker", "overnight", "night crew", "receiving", "inventory",
             "fulfillment", "fulfilment", "order picker", "personal shopper", "cart",
             "maintenance", "porter", "driver", "warehouse", "business office", "clerk",
             "title", "warranty", "deal processor", "accounting", "accounts payable", "payroll",
             "data entry", "administrator", "merchandising", "pricing", "scan coordinator",
             "reconditioning", "detailer")
# "consultant" is only a HIGH signal alongside "sales" (a bare "IT Consultant" title says
# nothing about voice work) -- handled as its own check rather than a plain substring.
_VOICE_CONSULTANT_SALES_RE = re.compile(r"\bsales\b.*\bconsultant\b|\bconsultant\b.*\bsales\b", re.I)


def voice_flag(title: str) -> str:
    """'high' / 'low' / '' (no opinion). A title matching both tables is HIGH -- a poor voice-work
    fit is checked first and wins over any low-voice keyword also present."""
    t = (title or "").lower()
    if any(k in t for k in VOICE_HIGH) or _VOICE_CONSULTANT_SALES_RE.search(t):
        return "high"
    if any(k in t for k in VOICE_LOW):
        return "low"
    return ""


# Sort order for the `high`/`low`/'' voice flag inside one ring/employer group (sprint plan
# §31.6, user ruling 2026-09-21): low-voice first (the better fit), then no opinion, then
# high-voice LAST -- voice-heavy roles are a poor fit, so they sort to the bottom, never off.
VOICE_SORT_ORDER = {"low": 0, "": 1, "high": 2}
