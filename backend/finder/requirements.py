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

from .labels import strip_boilerplate

SPLITTER_VERSION = "2026-09-29.2"   # bump when splitting or classing changes (part of the requirement cache key)
                                    # 2026-09-29.2: a bare clearance label ("Clearance Required", "Security
                                    # Clearance:") is joined to the requirement that follows it; benefit sections
                                    # ("What you can expect of us", "A few highlights include") drop
                                    # 2026-09-29.1: units carry their source text (judges read it, years intact);
                                    # YEARS_PHRASE adjectives end on a word boundary ("direct" no longer eats
                                    # "directly"); short qualification lines are content, not subheadings; a line
                                    # ending ", required" / ", preferred" sets its own section
                                    # 2026-09-28.1: heading vocabulary mined from the active corpus, the known-heading
                                    # bypass of the verb filter, "About You" no longer dropped, and the no-Required
                                    # rescue of years/degree lines (docs/JEV_PLAN.md, splitter Layer 1)
MIN_UNIT, MAX_UNIT, MAX_UNITS = 25, 400, 40

RESPONSIBILITY_HEADINGS = (r"responsibilit|what you.ll do|what you will do|duties|the role|key accountabilities"
                           r"|in this role|the opportunity|your impact|job overview|position summary|job summary"
                           r"|role summary|day to day|day-to-day|what you.ll be doing"
                           # 2026-09-28.1, mined from the active corpus (the heading above requirement or duty text)
                           r"|typical day|any given day|essential functions|what you will be doing|what you get to do"
                           r"|your role|role description|position description|the impact you will have"
                           r"|how you will make an impact|success looks like|measures of success|success measures"
                           r"|success metrics|^you will$|^you.ll$")
# Person-side headings the rules' REQUIRED_HEADINGS does not carry; counted as Required for coverage only.
PERSON_HEADINGS = (r"skills|knowledge|experience|education|who you are|about you|your background"
                   r"|what we.re looking for|what we are looking for|competenc|you have|you bring|you.ll bring"
                   # 2026-09-28.1, mined from the active corpus (the heading above the first years line of
                   # postings where no Required section was found)
                   r"|what we look for|right fit|we expect|you must have|must.haves?|expertise|what you will need"
                   r"|what you need|you.ll need|what it takes|ideal candidate|candidate profile|your profile"
                   r"|this is you|what we seek|you should have|you will bring|you.re bringing|what we need"
                   r"|need from you|to be successful")
# Preferred headings the rules' PREFERRED_HEADINGS does not carry (splitter only; the rule engine is unchanged).
SPLITTER_PREFERRED_HEADINGS = r"desirable|a plus|good to have"
DROP_HEADINGS = (r"about (us|the company|the team|our|[A-Z])|who we are|benefits|perks|what we offer|why join"
                 r"|compensation|pay range|salary|equal (employment )?opportunity|eeo|our commitment|life at"
                 r"|accommodation|privacy|disclaimer|additional information|how to apply|pay transparency"
                 # 2026-09-28.1: logistics sections whose lines were being read as requirements
                 r"|physical (?:demands|requirements)|work(?:ing)? (?:environment|conditions)|total rewards"
                 r"|we offer|what you.ll get|what you get|in it for you|why you.ll love|why work (?:here|with|for)"
                 # 2026-09-29.1: applicant notices that followed the last qualification heading
                 r"|fair chance|usage policy|third.party applications|candidate privacy|e-verify|know your rights"
                 r"|recruitment fraud|notice to (?:applicants|candidates|recruiters|agencies)"
                 # 2026-09-29.2: benefit sections seen in gold postings (Amgen, Toyota)
                 r"|what you can expect|highlights include|a few highlights")
# A degree or education-level line: with a years line, the shape of a qualification wherever it sits (the
# no-Required rescue in split_requirements).
DEGREE_LINE = re.compile(r"\b(?:bachelor|master|associate|doctorate|ph\.?\s?d|mba)[’']?s?\b[^.]{0,40}\bdegree\b"
                         r"|\bdegree (?:in|from)\b|\bhigh school (?:diploma|education)\b|\bGED\b", re.I)
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
    r"|combined|direct|practical|working|total|increasing(?:ly)?|responsible|work|industry)\b)*"
    r"(?:\s+(?:experience|exp\.?))?(?:\s+(?:in|with|of|as|leading|working|doing|within|at|across))?\s*", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦●○■□➢►✓–—]+|\d{1,2}[.)])\s*")
# Verb-ish tokens: a non-bulleted line carrying one of these reads as a sentence, not a heading label.
_VERBISH = re.compile(
    r"\b(?:\w+ing|is|are|was|were|be|been|do|does|did|have|has|had|will|shall|can|must"
    r"|manage[sd]?|lead[s]?|develop[s]?|design[s]?|build[s]?|coordinat\w*|deliver[s]?"
    r"|implement[s]?|analy\w*|drive[s]?|own[s]?|partner[s]?|migrat\w*|creat\w*"
    r"|provide[s]?|support[s]?|ensure[sd]?|maintain[s]?|perform[s]?|collaborat\w*)\b", re.I)
_JOIN_WORDS = re.compile(r"(?:\b(?:and|or|the|a|an|of|to|with|for|in|on|as|by|at)|[&,])\s*$", re.I)
# Employer notices that survive boilerplate stripping (pay-range statements, scam warnings, career-site pointers).
NOTICE = re.compile(r"\b(scams?|fraud\w*|money transfers?|credit card numbers|official (?:u\.s\. )?website"
                    r"|verify the job posting|career opportunities|posted (?:pay |salary )?range|starting base salary"
                    r"|eligible for (?:a |an )?(?:bonus|incentive|commission)|for more information about career"
                    r"|to advance to a new job level|not genuine)\b", re.I)
PAY = re.compile(r"\$\s?\d|\b(?:salary|pay range|base pay|hourly rate)\b", re.I)
# Action verbs that rescue a line from `logistics`: a line naming a piece of work (even one that
# also mentions hybrid/remote/clearance/etc.) is `work`, not logistics -- the verb is the subject.
WORK_RESCUE = re.compile(
    r"\b(?:experience|process\w*|lead\w*|manag\w*|improv\w*|coordinat\w*|design\w*|build\w*|deliver\w*"
    r"|migrat\w*|implement\w*|analy\w*|develop\w*|own\w*|driv\w*|partner\w*)\b", re.I)


@dataclass
class Requirement:
    text: str
    section: str
    group: str
    weight: float
    klass: str
    source: str = ""   # 2026-09-29.1: the piece as the JD states it (bullet removed, years phrase KEPT). `text` is
                       # the coverage form (a years line reduced to its skill); a judge reads `source`.

    @property
    def unit_hash(self) -> str:
        return hashlib.sha1(self.text.lower().encode("utf-8")).hexdigest()[:16]


# 2026-09-29.2: a bare clearance label on its own line. In 4,408 stored postings it is almost always followed by the
# requirement itself ("Clearance:" / "Active TS/SCI ..."), so it is joined to that line rather than kept alone (a
# lone label read as a requirement) or skipped (the requirement read without its label).
CLEARANCE_LABEL = re.compile(r"(?:[-*•·]\s*)?(?:security\s+)?clearances?\s*(?:level\s*)?"
                             r"(?:required|requirements?|needed)?\s*:?", re.I)
_LEVEL_END = re.compile(r"\b(?:secret|sci|ts|top[- ]secret|public trust|suitability)\W*$", re.I)


def rejoin_lines(text: str) -> list:
    """Lines with sentence fragments glued back: a line starting lowercase / with punctuation, or following a
    line that ends on a joining word, continues the previous line."""
    out = []
    label = None
    for raw in text.splitlines():
        line = raw.strip()
        if label is not None:
            if not line or line == ":":
                continue
            if _heading_label(line):
                out.append(label)             # nothing followed the label; it stays a lone (skipped) label
                label = None
            else:
                out.append(f"{label}: {line.lstrip(': ')}")
                label = None
                continue
        if CLEARANCE_LABEL.fullmatch(line):
            if out and out[-1] and _LEVEL_END.search(out[-1]):
                out[-1] = out[-1] + " " + line    # "Top-Secret/SCI" / "Security Clearance required"
            elif line[:1].isupper() or _BULLET.match(line):
                label = line.rstrip(": ").strip()
            elif out and out[-1]:
                out[-1] = out[-1] + " " + line    # "Active Secret" / "clearance"
            else:
                out.append(line)
            continue
        if not line:
            out.append("")
            continue
        # 2026-09-29.1: a number the career-site HTML split digit by digit ("1" / "2" / "+ years in ...") is
        # glued back without spaces, then joins the text it counts.
        if out and out[-1]:
            prev = out[-1]
            if (re.fullmatch(r"\d{1,2}", line) and not re.fullmatch(r"\d{1,2}", prev) and not _heading_label(prev)
                    and not re.search(r"[.!?:;]$", prev)):
                out[-1] = prev + " " + line          # "... with at least" / "4"
                continue
            if re.search(r"(?:^|\s)\d{1,2}$", prev) and re.match(r"^[\d+]", line):
                out[-1] = prev + line                # "1" / "2" / "+ years" -> "12+ years"
                continue
            if re.search(r"(?:^|\s)\d{1,2}\+?$", prev) and re.match(r"^(?:-|–|years?\b|yrs?\b|to\b)", line):
                out[-1] = prev + " " + line          # "12+" / "years in ..."
                continue
        # 2026-09-29.1: a heading never absorbs the next line ("What We're Looking For" ends on a joining word)
        if out and out[-1] and not _heading_label(out[-1]) and (
                re.match(r"^[a-z,.;:)’'%]", line) or _JOIN_WORDS.search(out[-1])):
            sep = "" if re.match(r"^[,.;:)%’']", line) else " "
            out[-1] = out[-1] + sep + line
        else:
            out.append(line)
    if label is not None:
        out.append(label)
    return out


def _heading_kind(line: str):
    """The section a heading-shaped line opens, 'drop', or None when the line is content.
    A bulleted line is always content -- a bullet marks a claim, never a heading."""
    if _BULLET.match(line):
        return None
    raw = line.rstrip()
    s = raw.rstrip(":").strip()
    ends_colon = raw.endswith(":") and len(s) <= 80
    short_label = bool(s) and len(s) <= 60 and len(s.split()) <= 4 and not s.endswith(".") and not _VERBISH.search(s)
    # A short line phrased as a question that names a DROP section ("What do we offer?") is a heading too: a
    # question is a label, never a claim, so it cannot swallow a real requirement the way a statement could.
    drop_question = s.endswith("?") and len(s.split()) <= 8 and bool(re.search(DROP_HEADINGS, s, re.I))
    if not (ends_colon or short_label or _known_heading_label(s) or drop_question or _whole_heading(s)):
        return None
    # 2026-09-29.1: a short line naming a qualification ("Advanced degree preferred", "Public Trust") is a claim
    if (QUALIFICATION_TERM.search(s) and not ends_colon and not _known_heading_label(s) and not _whole_heading(s)
            and not re.search(DROP_HEADINGS, s, re.I)):
        return None
    if LOGISTICS.search(s) and not re.search(DROP_HEADINGS, s, re.I):
        return None
    if re.search(r"physical (?:demands|requirements)|work(?:ing)? (?:environment|conditions)", s, re.I):
        return "drop"   # 2026-09-29.1: "Physical Requirements" names 'requirements' but is logistics
    if re.search(DROP_HEADINGS, s, re.I if not re.match(r"about [A-Z]", s) else 0) and not re.search(
            P.REQUIRED_HEADINGS + "|" + RESPONSIBILITY_HEADINGS + "|" + PERSON_HEADINGS, s, re.I):
        return "drop"   # 2026-09-28.1: PERSON_HEADINGS joins the exclusion, so "About You" is no longer dropped
    if re.fullmatch(REQUIRED_LABELS, s, re.I):
        return "required"
    if re.search(P.PREFERRED_HEADINGS, s, re.I) or re.search(SPLITTER_PREFERRED_HEADINGS, s, re.I):
        return "preferred"
    if re.search(P.REQUIRED_HEADINGS, s, re.I) or re.search(PERSON_HEADINGS, s, re.I):
        return "required"
    if re.search(RESPONSIBILITY_HEADINGS, s, re.I):
        return "responsibility"
    return None


_CONNECTIVES = ("and", "or", "of", "the", "a", "an", "to", "for", "in", "on", "with", "&")
# 2026-09-29.1: a short line naming a qualification ("Public Trust", "Bachelor's degree", "PMP certification") is a
# requirement even without a bullet -- career-site HTML often renders each list item as a bare line. It is never a
# subheading, so it is no longer skipped.
QUALIFICATION_TERM = re.compile(
    r"\b(?:public trust|clearance|ts/sci|polygraph|secret|citizen(?:ship)?|degree|bachelor|master|mba|ph\.?\s?d"
    r"|doctorate|diploma|certif\w*|licen[cs]\w*|pmp|cpa|six sigma|black belt)\b", re.I)
# 2026-09-29.1: a line that states its own status ("..., required" / "..., preferred") overrides the heading it sits
# under (BDO-style lists put every qualification under one heading and mark each line).
INLINE_STATUS = re.compile(r"(?:[,;(\-–—]\s*|\s)(required|preferred|desired|a plus|nice to have)\s*\)?\s*\.?\s*$",
                           re.I)


# 2026-09-29.1: a line that opens by declaring itself required ("Must have a PhD", "Minimum 7 years ...").
LEADING_REQUIRED = re.compile(r"^\W*(?:must(?: have| be| hold| possess)?|minimum|required|requires|you must)\b", re.I)


def inline_section(line: str):
    """'required' / 'preferred' when the line marks its own status (a trailing ", required" / ", preferred", or a
    leading "Must" / "Minimum"), else None. A trailing status wins over a leading cue."""
    m = INLINE_STATUS.search(line.strip())
    if m:
        return "required" if m.group(1).lower() == "required" else "preferred"
    return "required" if LEADING_REQUIRED.search(_BULLET.sub("", line)) else None


def _title_case(s: str) -> bool:
    """Every significant word starts upper-case (Title Case or ALL CAPS): the look of a label, not a sentence."""
    words = [w for w in re.findall(r"[A-Za-z][\w'’-]*", s) if w.lower() not in _CONNECTIVES]
    return bool(words) and all(w[0].isupper() for w in words)


# 2026-09-29.1: labels that read as preferred but, as the posting's only qualifications list, carry its
# requirements (LinkedIn's standard "Desired Skills and Experience" field).
REQUIRED_LABELS = r"desired skills (?:and|&) experience"


def _heading_label(line: str) -> bool:
    """A line that is unmistakably a heading (ends with ':', or names a known section as a label): never glued to
    the line after it."""
    s = line.strip()
    return (s.endswith(":") and len(s) <= 80) or _known_heading_label(s.rstrip(":")) or _whole_heading(s.rstrip(":"))


def _whole_heading(s: str) -> bool:
    """2026-09-29.1: a short line (<= 4 words, no final period) that IS a known heading phrase end to end, in any
    case ("You have", "what you bring"). Matching the whole line keeps a content line that merely contains a
    heading word ("Strong communication skills") out."""
    if not s or len(s.split()) > 4 or s.endswith("."):
        return False
    known = "|".join([P.REQUIRED_HEADINGS, P.PREFERRED_HEADINGS, SPLITTER_PREFERRED_HEADINGS, PERSON_HEADINGS,
                      RESPONSIBILITY_HEADINGS, REQUIRED_LABELS])
    m = re.search(known, s, re.I)
    return bool(m) and len(m.group(0)) >= len(re.sub(r"^(?:what|who)\s+|\W+$", "", s)) - 4


def _known_heading_label(s: str) -> bool:
    """2026-09-28.1: a short line that names a KNOWN section (required, preferred, person or responsibility) is a
    heading even when it carries a verb-ish word -- "What You'll Do", "Who You Are", "What You'll Bring" ('bring'
    ends in -ing) were all rejected by the verb filter. Narrow on purpose: at most 8 words, no final period, and
    Title Case / ALL CAPS or a closing '?'. DROP patterns never qualify (a stray "Competitive salary and
    benefits" line must not swallow what follows it)."""
    if not s or len(s) > 70 or len(s.split()) > 8 or s.endswith("."):
        return False
    if not (_title_case(s) or s.endswith("?")):
        return False
    known = "|".join([P.REQUIRED_HEADINGS, P.PREFERRED_HEADINGS, SPLITTER_PREFERRED_HEADINGS, PERSON_HEADINGS,
                      RESPONSIBILITY_HEADINGS])
    return bool(re.search(known, s, re.I))


def _subheading(line: str) -> bool:
    """An unrecognized heading-shaped line (a label, not a claim): ends with ':', or is a short
    (<=4 word), verb-free, Title Case label. A bulleted line is never a subheading -- it is a claim."""
    if _BULLET.match(line):
        return False
    s = line.strip()
    if QUALIFICATION_TERM.search(s) and not s.endswith(":"):
        return False
    if s.endswith(":"):
        return len(s.rstrip(":").strip()) <= 80
    if len(s) > 60 or len(s.split()) > 4 or s.endswith(".") or _VERBISH.search(s):
        return False
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


def _logistics_is_subject(text: str) -> bool:
    """The logistics term reads as what the line is about, not an aside inside a work line: the
    line is short, opens with the logistics phrasing, or logistics terms dominate its words."""
    words = text.split()
    if len(words) <= 6:
        return True
    if LOGISTICS.search(" ".join(words[:3])):
        return True
    hits = LOGISTICS.findall(text)
    return len(hits) >= max(2, len(words) // 6)


def classify(text: str) -> tuple:
    """(klass, text): logistics and domain lines keep their text; a years line becomes its skill remainder
    (work) or `level` when nothing is left."""
    if PAY.search(text):
        return "logistics", text
    if LOGISTICS.search(text) and not WORK_RESCUE.search(text) and _logistics_is_subject(text):
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
    units, seen, qual_shaped = [], set(), []
    for line, kind in zip(lines, kinds):
        if not line:
            continue
        if kind is not None:
            section = kind
            continue
        if section == "drop" or NOTICE.search(line) or _subheading(line):
            continue
        bulleted = bool(_BULLET.match(line)) or bool(QUALIFICATION_TERM.search(line))   # an itemized bullet (or a
        # named qualification) is a deliberate claim, even a short one
        for piece in _pieces(_BULLET.sub("", line).strip()):
            piece = piece.strip()
            # 2026-09-29.2: a stated status ("..., preferred", "Must ...") belongs to its own sentence, not to every
            # sentence the rejoined line carries
            line_section = inline_section(piece) or section
            if not (10 <= len(piece) <= MAX_UNIT + 50):   # short level lines count as level; short work drops below
                continue
            klass, unit_text = classify(piece)
            if klass == "work" and not bulleted and len(unit_text) < MIN_UNIT:
                continue
            key = unit_text.lower()
            if key in seen:
                continue
            seen.add(key)
            units.append(Requirement(unit_text, line_section, GROUPS[line_section], SECTION_WEIGHTS[line_section],
                                     klass, piece))
            qual_shaped.append(_qualification_shaped(piece))
    units = _rescue_required(units, qual_shaped)
    if max_units and len(units) > max_units:
        keep = sorted(range(len(units)), key=lambda i: (-units[i].weight, i))[:max_units]
        units = [units[i] for i in sorted(keep)]
    return units


def qualification_shaped(piece: str) -> bool:
    """Public: a years / degree / leading-"Must" line, or one naming a credential (clearance, certification)."""
    return _qualification_shaped(piece) or bool(QUALIFICATION_TERM.search(piece))


def _qualification_shaped(piece: str) -> bool:
    """A years-of-experience or degree line: the shape of a qualification, whatever heading it sits under."""
    return bool((YEARS_PHRASE.search(piece) and re.search(r"\b(?:years?|yrs?)\b", piece, re.I))
                or DEGREE_LINE.search(piece) or LEADING_REQUIRED.search(piece))


def _rescue_required(units: list, qual_shaped: list) -> list:
    """2026-09-28.1: a posting whose headings name no Required section still states its qualifications -- under
    "Responsibilities", "Job Description", or no heading at all. When NO unit landed in `required`, every
    years/degree-shaped unit outside `preferred` moves to `required`. A posting that has a Required section is
    left exactly as it was."""
    if any(u.section == "required" for u in units):
        return units
    return [Requirement(u.text, "required", GROUPS["required"], SECTION_WEIGHTS["required"], u.klass, u.source)
            if shaped and u.section in ("intro", "body", "responsibility") else u
            for u, shaped in zip(units, qual_shaped)]


def splitter_fingerprint() -> str:
    """Changes when the splitter version or any heading pattern changes (hashed into rubric / cache versions)."""
    payload = "|".join([SPLITTER_VERSION, RESPONSIBILITY_HEADINGS, PERSON_HEADINGS, DROP_HEADINGS,
                        P.REQUIRED_HEADINGS, P.PREFERRED_HEADINGS, SPLITTER_PREFERRED_HEADINGS])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
