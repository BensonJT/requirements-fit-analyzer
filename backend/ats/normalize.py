"""Field normalization shared by every ATS adapter.

Every platform returns its own shape; the adapters map each record into ONE common
posting dict (see NORMALIZED_FIELDS) so a single `postings` table can be filtered
uniformly. The platform's original record is kept verbatim in `raw_json`, so a
field that was mapped badly can be re-derived later without re-fetching.
"""
import html
import json
import re
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

NORMALIZED_FIELDS = (
    "req_id", "title", "url",
    "location_primary", "locations", "country",
    "workplace_type", "employment_type", "job_family", "job_level",
    "pay_min", "pay_max", "pay_currency", "pay_interval", "pay_source",
    "posted_at", "posting_end_at",
    "description_text", "raw_json",
)


def base(**kw):
    """A posting dict with every normalized field present (None unless given)."""
    row = {k: None for k in NORMALIZED_FIELDS}
    row.update(kw)
    return row


# ---------------------------------------------------------------- workplace type
_WORKPLACE = {
    "remote": "remote", "fully remote": "remote", "ora_remote": "remote",
    "hybrid": "hybrid", "ora_hybrid": "hybrid",
    "onsite": "onsite", "on-site": "onsite", "on site": "onsite", "in office": "onsite",
    "in-office": "onsite", "ora_onsite": "onsite", "ora_on_site": "onsite", "office": "onsite",
}


def workplace_type(value, *location_texts):
    """Maps a platform's workplace/remote flag to remote | hybrid | onsite | None.
    Falls back to sniffing the location text for 'remote' / 'hybrid'."""
    if isinstance(value, bool):
        return "remote" if value else None
    if value:
        v = str(value).strip().lower()
        if v in _WORKPLACE:
            return _WORKPLACE[v]
        for key, out in _WORKPLACE.items():
            if key in v:
                return out
    joined = " ".join(t for t in location_texts if t).lower()
    if "remote" in joined:
        return "remote"
    if "hybrid" in joined:
        return "hybrid"
    return None


# ---------------------------------------------------------------- employment type
def employment_type(value):
    if not value:
        return None
    v = str(value).lower()
    if "intern" in v:
        return "intern"
    if "part" in v:
        return "part_time"
    if "contract" in v or "temp" in v or "contingent" in v:
        return "contract"
    if "full" in v:
        return "full_time"
    return v[:40]


# ---------------------------------------------------------------- dates
_REL = re.compile(r"posted\s+(today|yesterday|(\d+)\s+days?\s+ago|(\d+)\+\s+days?\s+ago)", re.I)


def parse_date(value, run_date=None):
    """Turns the many date shapes ATS platforms use into a `date` (or None).

    Handles ISO strings, epoch millis (Lever), and Workday's relative text
    ('Posted Today', 'Posted 3 Days Ago'). 'Posted 30+ Days Ago' is a floor, not
    a date, so it returns None; the Workday detail call fills the real date later.
    """
    if value is None or value == "":
        return None
    run_date = run_date or datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        if value > 1e11:  # epoch millis
            value = value / 1000
        return datetime.fromtimestamp(value, tz=timezone.utc).date()
    s = str(value).strip()
    m = _REL.search(s)
    if m:
        word = m.group(1).lower()
        if word == "today":
            return run_date
        if word == "yesterday":
            return run_date - timedelta(days=1)
        if m.group(2):
            return run_date - timedelta(days=int(m.group(2)))
        return None  # "30+ days ago"
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s[:20], fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- locations
_N_LOCATIONS = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)


def locations_json(values):
    """A JSON-array string of distinct, non-empty location names, or None."""
    seen = []
    for v in values or []:
        if isinstance(v, dict):
            v = v.get("name") or v.get("Name") or v.get("descriptor") or v.get("location")
        if v and str(v).strip() and str(v).strip() not in seen:
            seen.append(str(v).strip())
    return json.dumps(seen) if seen else None


def primary_location(text):
    """Workday's list endpoint collapses multi-site reqs to '7 Locations' — that's
    not a location, so it maps to None until the detail call fills it in."""
    if not text or _N_LOCATIONS.match(text):
        return None
    return text.strip()


# ---------------------------------------------------------------- description
def html_to_text(raw):
    """Plain text from an HTML (possibly double-escaped, as Greenhouse does) blob."""
    if not raw:
        return None
    s = str(raw)
    if "<" not in s and "&lt;" in s:
        s = html.unescape(s)
    text = BeautifulSoup(s, "lxml").get_text("\n")
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text or None


# ---------------------------------------------------------------- pay
_PAY = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\.\d{1,2})?\s*(?:k)?\s*(?:-|–|—|to)\s*\$?\s?(\d{1,3}(?:,\d{3})+|\d{4,6})(?:\.\d{1,2})?\s*(k)?",
    re.I,
)
_PAY_K = re.compile(r"\$\s?(\d{2,3})\s*k\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3})\s*k\b", re.I)
_HOURLY = re.compile(r"\$\s?(\d{2,3})(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?(\d{2,3})(?:\.\d{2})?\s*(?:/|per)\s*(?:hr|hour)", re.I)


def pay_from_text(text):
    """Pulls the first salary range out of a JD. Returns (min, max, interval) or None.
    Deliberately conservative: a range under $20,000/yr is ignored unless it's
    explicitly hourly."""
    if not text:
        return None
    m = _HOURLY.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if 15 <= lo <= hi <= 400:
            return lo, hi, "hour"
    m = _PAY_K.search(text)
    if m:
        lo, hi = int(m.group(1)) * 1000, int(m.group(2)) * 1000
        if 20_000 <= lo <= hi <= 1_500_000:
            return lo, hi, "year"
    for m in _PAY.finditer(text):
        lo = int(m.group(1).replace(",", ""))
        hi = int(m.group(2).replace(",", ""))
        if m.group(3):
            lo, hi = lo * 1000, hi * 1000
        if 20_000 <= lo <= hi <= 1_500_000:
            return lo, hi, "year"
    return None


def raw(record):
    """Compact JSON of the platform's own record, for `raw_json`."""
    try:
        return json.dumps(record, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
