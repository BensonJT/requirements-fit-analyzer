"""A JD as requirement units: the claims coverage tries to match (sprint plan §15.2 as amended by §16.3 / §16.4).

`split_requirements(text)` returns `Requirement(text, section, group, weight, klass)`:
- section   required | preferred | responsibility | intro | body (body = a JD with no recognized headings)
- group     'required' (Required + Preferred: describes the person) or 'role' (Responsibilities, intro, body:
            describes the seat). The coverage gate reads the required group.
- weight    section weight (required 1.0 · responsibility 0.8 · body 0.7 · intro 0.5 · preferred 0.4); coverage
            multiplies it by the unit's specificity later.
- klass     work | level | logistics | domain. Only `work` counts toward coverage. A years-of-experience line
            keeps its skill content: the years phrase is stripped and the remainder stays `work` (§16.4).
Career-site HTML often splits one sentence across lines around bold spans; lines are rejoined first.
"""
import hashlib
import re
from dataclasses import dataclass

from backend import profile as P

from . import rules
from .labels import strip_boilerplate

SPLITTER_VERSION = "2026-09-16.1"   # bump when splitting or classing changes (part of the requirement cache key)
MIN_UNIT, MAX_UNIT, MAX_UNITS = 25, 400, 40

RESPONSIBILITY_HEADINGS = (r"responsibilit|what you.ll do|what you will do|duties|the role|key accountabilities"
                           r"|in this role|the opportunity|your impact|job overview|position summary|job summary"
                           r"|role summary|day to day|day-to-day|what you.ll be doing")
# Person-side headings the rules' REQUIRED_HEADINGS does not carry; counted as Required for coverage only.
PERSON_HEADINGS = (r"skills|knowledge|experience|education|who you are|about you|your background"
                   r"|what we.re looking for|what we are looking for|competenc|you have|you bring|you.ll bring")
DROP_HEADINGS = (r"about (us|the company|the team|our|[A-Z])|who we are|benefits|perks|what we offer|why join"
                 r"|compensation|pay range|salary|equal (employment )?opportunity|eeo|our commitment|life at"
                 r"|accommodation|privacy|disclaimer|additional information|how to apply|pay transparency")
SECTION_WEIGHTS = {"required": 1.0, "responsibility": 0.8, "body": 0.7, "intro": 0.5, "preferred": 0.4}
GROUPS = {"required": "required", "preferred": "required", "responsibility": "role", "intro": "role", "body": "role"}

LOGISTICS = re.compile(
    r"\b(travel\w*|relocat\w*|on-?site|hybrid|remote|in[- ]office|commut\w*|shifts?|weekends?|overtime|on-call"
    r"|hours per week|clearance|ts/sci|polygraph|salary|pay range|base pay|hourly rate|\$\s?\d|sponsorship"
    r"|authori[sz]\w* to work|work authori[sz]ation|visa|citizenship|u\.s\. citizen|background (check|investigation)"
    r"|drug (test|screen)|driver.?s licen[cs]e|lift(ing)? (up to )?\d+|pounds|physical demands)\b", re.I)
_NUMWORD = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"
YEARS_PHRASE = re.compile(
    rf"(?:\b(?:a\s+)?(?:minimum|min\.?|at least|over|more than)\s+(?:of\s+)?)?(?<![\w$.]){_NUMWORD}\s*(?:\(\s*\d+\s*\)\s*)?"
    rf"(?:\+|plus)?\s*(?:(?:-|–|to)\s*{_NUMWORD}\s*\+?\s*)?(?:or more\s+|or greater\s+)?(?:years?|yrs?)\b[’']?"
    r"(?:\s+or more)?(?:\s+of)?(?:\s+(?:progressive|professional|relevant|related|proven|demonstrated|hands-on"
    r"|combined|direct|practical|working|total|increasing(?:ly)?|responsible|work|industry))*"
    r"(?:\s+(?:experience|exp\.?))?(?:\s+(?:in|with|of|as|leading|working|doing|within|at|across))?\s*", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦●○■□➢►✓–—]+|\d{1,2}[.)])\s*")
_JOIN_WORDS = re.compile(r"(?:\b(?:and|or|the|a|an|of|to|with|for|in|on|as|by|at)|[&,])\s*$", re.I)
# Employer notices that survive boilerplate stripping (pay-range statements, scam warnings, career-site pointers).
NOTICE = re.compile(r"\b(scams?|fraud\w*|money transfers?|credit card numbers|official (?:u\.s\. )?website"
                    r"|verify the job posting|career opportunities|posted (?:pay |salary )?range|starting base salary"
                    r"|eligible for (?:a |an )?(?:bonus|incentive|commission)|for more information about career"
                    r"|to advance to a new job level|not genuine)\b", re.I)
PAY = re.compile(r"\$\s?\d|\b(?:salary|pay range|base pay|hourly rate)\b", re.I)


@dataclass
class Requirement:
    text: str
    section: str
    group: str
    weight: float
    klass: str

    @property
    def unit_hash(self) -> str:
        return hashlib.sha1(self.text.lower().encode("utf-8")).hexdigest()[:16]


def rejoin_lines(text: str) -> list:
    """Lines with sentence fragments glued back: a line starting lowercase / with punctuation, or following a
    line that ends on a joining word, continues the previous line."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if out and out[-1] and (re.match(r"^[a-z,.;:)’'%]", line) or _JOIN_WORDS.search(out[-1])):
            sep = "" if re.match(r"^[,.;:)%’']", line) else " "
            out[-1] = out[-1] + sep + line
        else:
            out.append(line)
    return out


def _heading_kind(line: str):
    """The section a heading-shaped line opens, 'drop', or None when the line is content."""
    s = _BULLET.sub("", line).strip().rstrip(":").strip()
    shaped = s and len(s) <= 60 and len(s.split()) <= 7 and not s.endswith(".")
    if not shaped and not (line.rstrip().endswith(":") and len(s) <= 80):
        return None
    if LOGISTICS.search(s) and not re.search(DROP_HEADINGS, s, re.I):
        return None
    if re.search(DROP_HEADINGS, s, re.I if not re.match(r"about [A-Z]", s) else 0) and not re.search(
            P.REQUIRED_HEADINGS + "|" + RESPONSIBILITY_HEADINGS, s, re.I):
        return "drop"
    if re.search(P.PREFERRED_HEADINGS, s, re.I):
        return "preferred"
    if re.search(P.REQUIRED_HEADINGS, s, re.I) or re.search(PERSON_HEADINGS, s, re.I):
        return "required"
    if re.search(RESPONSIBILITY_HEADINGS, s, re.I):
        return "responsibility"
    return None


def _subheading(line: str) -> bool:
    """An unrecognized heading-shaped line (a label, not a claim): ends with ':' or is Title Case."""
    s = _BULLET.sub("", line).strip()
    if len(s) > 60 or len(s.split()) > 7 or s.endswith("."):
        return False
    if s.endswith(":"):
        return True
    words = [w for w in re.findall(r"[A-Za-z][\w'’-]*", s) if w.lower() not in ("and", "or", "of", "the", "a", "an",
                                                                            "to", "for", "in", "on", "with", "&")]
    return bool(words) and all(w[0].isupper() for w in words)


def _pieces(line: str) -> list:
    """A content line as unit-sized pieces: sentences, then ';' splits, then word-bounded chunks."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z(])", line):
        parts = [sent] if len(sent) <= MAX_UNIT else re.split(r";\s*", sent)
        for part in parts:
            while len(part) > MAX_UNIT:
                cut = part.rfind(" ", 0, MAX_UNIT)
                cut = cut if cut > MIN_UNIT else MAX_UNIT
                out.append(part[:cut])
                part = part[cut:].strip()
            out.append(part)
    return out


def strip_years(text: str) -> str:
    """The line without its years-of-experience phrase(s) and the connective words left dangling."""
    stripped = YEARS_PHRASE.sub(" ", text)
    stripped = re.sub(r"^\W*(?:and|with|in|of|including)?\s*", "", stripped, flags=re.I)
    return re.sub(r"\s{2,}", " ", stripped).strip(" ,;:-–")


def _domain_line(text: str) -> bool:
    terms = [t for t in (P.DOMAIN_TENURE_TERMS or []) if t.strip()]
    if not terms:
        return False
    alt = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return bool(re.search(rf"(?<!\d)\d{{1,2}}\+?\s*years?[^.\n]{{0,60}}?(?<![a-z0-9])({alt})(?![a-z0-9])", text, re.I))


def classify(text: str) -> tuple:
    """(klass, text): logistics and domain lines keep their text; a years line becomes its skill remainder
    (work) or `level` when nothing is left."""
    if PAY.search(text):
        return "logistics", text
    if LOGISTICS.search(text) and len(rules.find_terms(["experience", "process", "lead", "manage", "improve"], text)) == 0:
        return "logistics", text
    if _domain_line(text):
        return "domain", text
    if YEARS_PHRASE.search(text) and re.search(r"\b(?:years?|yrs?)\b", text, re.I):
        rest = strip_years(text)
        if len(rest) >= MIN_UNIT:
            return "work", rest[0].upper() + rest[1:]
        return "level", text
    return "work", text


def split_requirements(text: str, max_units: int = 0) -> list:
    """Requirement units in document order. `max_units` > 0 keeps the highest-weight units (ties: earlier first);
    coverage applies the §16.3 cap itself after specificity is known."""
    lines = rejoin_lines(strip_boilerplate(text or ""))
    kinds = [_heading_kind(l) if l else None for l in lines]
    has_headings = any(k in ("required", "preferred", "responsibility") for k in kinds)
    section = "intro" if has_headings else "body"
    units, seen = [], set()
    for line, kind in zip(lines, kinds):
        if not line:
            continue
        if kind is not None:
            section = kind
            continue
        if section == "drop" or NOTICE.search(line) or _subheading(line):
            continue
        for piece in _pieces(_BULLET.sub("", line).strip()):
            piece = piece.strip()
            if not (10 <= len(piece) <= MAX_UNIT + 50):   # short level lines count as level; short work drops below
                continue
            klass, unit_text = classify(piece)
            if klass == "work" and len(unit_text) < MIN_UNIT:
                continue
            key = unit_text.lower()
            if key in seen:
                continue
            seen.add(key)
            units.append(Requirement(unit_text, section, GROUPS[section], SECTION_WEIGHTS[section], klass))
    if max_units and len(units) > max_units:
        keep = sorted(range(len(units)), key=lambda i: (-units[i].weight, i))[:max_units]
        units = [units[i] for i in sorted(keep)]
    return units


def splitter_fingerprint() -> str:
    """Changes when the splitter version or any heading pattern changes (hashed into rubric / cache versions)."""
    payload = "|".join([SPLITTER_VERSION, RESPONSIBILITY_HEADINGS, PERSON_HEADINGS, DROP_HEADINGS,
                        P.REQUIRED_HEADINGS, P.PREFERRED_HEADINGS])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
