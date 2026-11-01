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

SPLITTER_VERSION = "2026-11-01.1"   # bump when splitting or classing changes (part of the requirement cache key)
                                    # 2026-11-01.1 (docs/SPLITTER_STUDY_20261101.md, 9,315 JDs): a topic heading
                                    # after Preferred stays Preferred and one over duty lines stays Responsibilities;
                                    # a short line whose only cue is a topic noun is content; "one of the
                                    # following" lists are one
                                    # line; team / company / benefit / bonus / tag headings drop; "Job
                                    # Description" resets to intro; new duty, Required and Preferred openers;
                                    # leading, mid-line and widened trailing preferred status; "X required, Y
                                    # preferred" splits; "as required" is not a status; sentence-case duty labels
                                    # 2026-10-30.1: employer footer statements (EEO, fair chance, pay-range and
                                    # benefit notices, entity disclaimers) drop by content wherever they sit, so the
                                    # last heading no longer absorbs the footer; a qualification-shaped line is never
                                    # dropped. Intro "You will ..." sentences are responsibilities, not intro
                                    # 2026-09-30.1: USAJobs paragraph dumps -- an inline qualification opener
                                    # ("SPECIALIZED EXPERIENCE GS-14:", "Specialized experience is defined as:")
                                    # opens Required mid-paragraph; application mechanics (time-in-grade, SF-50,
                                    # transcripts) drop sentence by sentence; sentences split on a missing space
                                    # and on inline "1) ... 2) ..." enumerations
                                    # 2026-09-29.3: "What Makes You a Great Fit" (and good/strong fit) opens Required
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
                           r"|success metrics|^you will$|^you.ll$"
                           # 2026-10-23.1, mined from the 53 active postings still without a Required section
                           r"|how you.ll make an impact|what you will be responsible for|problems you.ll solve"
                           r"|work you.ll own|what you will transform"
                           # 2026-11-01.1 (docs/SPLITTER_STUDY_20261101.md §2): duty openers left as intro or body
                           r"|work you.ll do|^your mission$|^mission$|^the work$|^the job$|essential (?:job )?functions"
                           r"|^job functions$|what you.ll work on|how you.ll make a difference|primary duties|major duties"
                           r"|key duties|core duties|what you.ll own|^your day$|main tasks|what the role involves"
                           r"|successful candidate will (?!have|possess|bring|need|be\b)")
# Person-side headings the rules' REQUIRED_HEADINGS does not carry; counted as Required for coverage only.
PERSON_HEADINGS = (r"skills|knowledge|experience|education|who you are|about you|your background"
                   r"|what we.re looking for|what we are looking for|competenc|you have|you bring|you.ll bring"
                   # 2026-09-28.1, mined from the active corpus (the heading above the first years line of
                   # postings where no Required section was found)
                   r"|what we look for|right fit|we expect|you must have|must.haves?|expertise|what you will need"
                   r"|what you need|you.ll need|what it takes|ideal candidate|candidate profile|your profile"
                   r"|this is you|what we seek|you should have|you will bring|you.re bringing|what we need"
                   r"|need from you|to be successful"
                   # 2026-09-29.3: Grafana's "What Makes You a Great Fit:" was read as more responsibilities
                   r"|great fit|good fit|strong fit|makes you"
                   # 2026-10-23.1
                   r"|who we are looking for|who we.re looking for|thrive here if"
                   # 2026-11-01.1: required openers the study found unrecognized
                   r"|minimum requirement\b|^qualification$|qualification requirements|mandatory|essential criteria"
                   r"|essential qualifications?|key qualifications|licen[cs](?:e|es|ing)\b|^certifications?$|^languages?$"
                   r"|successful candidates? will (?:have|possess|bring|need)")
# 2026-10-20.1: the SOFT person headings -- a portrait of the candidate, not a list of gates. Capital One's "Who you
# are:" ("Do-er:", "Team Player:", "You ask why...") filed seven traits as Required at weight 1.0, above its real
# Basic Qualifications (Bachelor's 0.54), so the top-18 cut, the judge and Jev read the traits as the bar. When the
# posting ALSO has an explicit Required heading, a soft block's lines are Preferred unless they are qualification-
# shaped (years / degree / "Must"); with no other Required heading, the soft block still carries the requirements.
SOFT_PERSON_HEADINGS = (r"who you are|about you|this is you|ideal candidate|candidate profile|your profile|right fit"
                        r"|great fit|good fit|strong fit|makes you|to be successful|what we look for"
                        r"|what we.re looking for|what we are looking for|what we seek|your background"
                        r"|who we are looking for|who we.re looking for|thrive here if")
# A heading that also names a gate list stays hard: "Top skills or competencies to be successful:", "Here is what you
# need to be successful:" (both measured 2026-10-20 -- real experience lines that the soft rule alone demoted).
HARD_PERSON_HEADINGS = (r"skills|knowledge|experience|education|competenc|expertise|you have|you bring|you.ll bring"
                        r"|you will bring|you.re bringing|we expect|must.haves?|you must have|you should have"
                        r"|what you will need|you need|you.ll need|what we need|need from you|what it takes")
# Preferred headings the rules' PREFERRED_HEADINGS does not carry (splitter only; the rule engine is unchanged).
SPLITTER_PREFERRED_HEADINGS = (r"desirable|a plus|good to have|\bwe prefer\b|\bprefer(?:red)? to have\b"   # 2026-09-29.4: RTX "Qualifications We Prefer"
                               # 2026-11-01.1: "What will make you stand out", "Additional skills we'll prioritize",
                               # Carrot's "While not required, we would be especially excited if you bring"
                               r"|stand out|we.ll prioritize|even better|extra credit|differentiators|^preferences$"
                               r"|bonus if|would be (?:great|nice|a plus)|not required|especially excited|^optional$"
                               r"|^pluses$")
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
                 r"|what you can expect|highlights include|a few highlights"
                 # 2026-11-01.1 (study §2): team, company, benefit and tag headings whose lines stayed in Required
                 # (977 postings) or Preferred (647); anchored where a bare word would catch real headings
                 r"|company overview|company profile|what we believe|our culture|how we work|department description"
                 r"|^(?:the|our) team$|team overview|meet the team|about this team|employee referral|parental leave"
                 r"|paid time off|^work shift$|^sponsorship$|^the details$|^pay$|^keywords$|unleash your potential"
                 r"|tuition reimbursement|educational assistance|^#?li-[a-z0-9]+$|^\d{4,7}\s*-?\s*\d{4,7}\s*usd$")
# 2026-11-01.1: headings that end a metadata block and start the description proper. GDIT's "Public Trust/Other
# Required:" field opened Required and "Job Description:" (unrecognized) left the whole JD there (106 postings).
# A reset returns to intro (or body when the posting has no section headings).
RESET_HEADINGS = r"^(?:job description|description|overview|position overview|role overview|the position)$"
# 2026-11-01.1: "requirements" as the object of the work, not a list of them: "Requirements Gathering", "Business
# Analysis & Requirements Management", "Technical Requirements & Solution Framing" opened Required inside duties.
REQ_WORK_OBJECT = re.compile(
    r"\brequirements?\s+(?:gathering|management|translation|definition|discovery|elicitation|analysis|framing"
    r"|engineering|documentation)|\b(?:business|technical|product|stakeholder|functional|user|system)\s+requirements"
    r"|\brequirements\s*(?:&|and)\s*(?:discovery|solution|design|delivery|scoping)", re.I)
# 2026-11-01.1: a heading that re-opens Required after a Preferred section must say so; a topic heading
# ("Competencies", "Education", "Knowledge, Skills and Abilities", Fannie Mae's keyword-dump "Qualifications")
# after Preferred stays Preferred (1,003 postings flipped back). Its qualification-shaped lines still go to
# Required (the persona rule).
GATE_HEADING = re.compile(r"\brequire(?:d|ments?)\b|\bminimum\b|\bbasic\b|\bmust\b|\bmandatory\b|\bneed(?:ed)?\b",
                          re.I)
# 2026-11-01.1: a duty-led line (imperative or third person), for telling a topic heading inside Responsibilities
# ("Content and knowledge amplification", "Communication Skills") from a heading that opens Required.
DUTY_LEAD = re.compile(
    r"^\W*(?:lead|manage|develop|partner|drive|own|build|design|collaborate|support|coordinate|oversee|create|ensure"
    r"|define|establish|serve|act|analy[sz]e|identify|implement|deliver|monitor|prepare|provide|maintain|conduct"
    r"|facilitate|track|present|communicate|work|participate|perform|review|translate|evaluate|assist|plan|report"
    r"|champion|run|operate|direct|guide|influence|advise|use|leverage|engage|respond|recommend|operate|cultivate"
    r"|recruit|embed|enable|contribute|set|shape|drive|automate|integrate|document|conduct|answer|prioritize"
    r"|utilize|apply|organize|negotiate|mentor|coach|train|research|write|draft|test|validate)(?:s|es|d|ed)?\b"
    r"|^\W*you(?:['’]ll| will)\b", re.I)
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
# 2026-09-30.1: USAJobs announcements arrive as a few giant paragraphs. The qualifications are sentences inside
# them, opened by a label the heading pass cannot see ("SPECIALIZED EXPERIENCE GS-14:", "Specialized experience
# is defined as:", "To qualify for this position, ..."); everything from that opener on is Required until the next
# heading. The application mechanics around them (time-in-grade, SF-50, transcripts, OPM standards) are notices
# dropped piece by piece: dropping the whole paragraph would drop the requirements with it.
USAJOBS_REQUIRED_OPENER = re.compile(
    r"\b(?:specialized experience(?: requirements?)?\s*:|specialized experience (?:is defined as|would be demonstrated by"
    r"|includes?|include|for this position includes?)|examples? of specialized experience includes?"
    r"|qualifying specialized experience includes?"
    r"|(?:you|applicants) must have (?:at least )?one(?: \(1\))?[- ]year of specialized experience"
    r"|to qualify (?:for|at) (?:this position|the [A-Z0-9/-]+ (?:grade |pay band|level)|the GS-\d+)"
    r"|basic requirements?(?: for [^:]{0,60})?\s*:|minimum qualifications?\s*:|basic education requirement)", re.I)
USAJOBS_NOTICE = re.compile(
    r"\b(?:time-in-grade|sf-50|(?:your )?resume must|transcripts?|opm|office of personnel management|qualification standards"
    r"|closing date of (?:this|the) announcement|paid and unpaid experience|volunteer work|to be creditable"
    r"|substitution of education|superior academic achievement|usa hire|equivalent in other pay systems"
    r"|next lower grade|federal employees|52 weeks|education credentials have been deemed|conventional u\.s\. education"
    r"|accredited \(or pre-accredited\)|department of education|standard position descriptions?|spd library"
    r"|position descriptions?|who may apply|credit for specialized experience|per week for each period"
    r"|level of difficulty and responsibility|equivalent to (?:at least )?the [A-Z]{2}-\d+|in the federal (?:service|government)"
    r"|receive credit for|qualifying experience|volunteer experience|full year of work|hours of work per week"
    r"|part-time experience|credited on the basis|nature of (?:their|your) duties|physical (?:requirements|demands)"
    r"|sedentary|americorps|peace corps|office setting|you may qualify based on|as described below"
    # assessment mechanics
    r"|will be (?:evaluated|assessed) (?:based )?on|assessment (?:results|process|for this application)|situational judgment test"
    r"|ssjt|timed test|cut score|answer each item|must be fully documented|application materials|supporting documents"
    r"|applicants may (?:also )?(?:substitute|combine)|education portion|month and year start)\b", re.I)
# 2026-10-30.1: employer footer statements. Nothing closes the last qualification heading, so a footer that follows
# it was filed as Preferred requirements (Capital One: 27 lines after "Preferred Qualifications"; Microsoft: the base
# pay paragraph). Matched by content on every line wherever it sits. Each pattern is a statement form, never a
# topic: "manage reasonable accommodation requests" or "design incentive compensation programs" are real duties and
# must not match. A qualification-shaped line (years / degree / leading "Must") is never dropped by it.
FOOTER_LINE = re.compile(
    r"\b(?:(?:is|are) an? (?:equal (?:employment )?opportunity|eoe|affirmative action)\b|opportunity employer"
    r"|without regard to (?:race|age|sex|religion|color)|applicants? with (?:a |arrest (?:and|or) )?(?:criminal|conviction)"
    r" (?:histor|record)|criminal background inquiries|fair chance (?:act|ordinance)|criminal records screening"
    r"|drug-free workplace|(?:require|need|request)s? (?:an? )?(?:reasonable )?accommodations?\b"
    r"|recruitingaccommodation|will not sponsor (?!(?:a |an |the )?(?:security )?clearance|this requirement)|not (?:be )?(?:able to )?(?:offer |provide )?(?:visa )?sponsor(?:ship)?"
    r" (?:a new|for this|now or)|minimum and maximum (?:full-time )?(?:annual )?salar|salary information is solely"
    r"|subject to the pay range|salaries for part-time|will be prorated|eligible (?:to earn|for) [^.]{0,40}incentive"
    r" compensation|discretionary or non[- ]?discretionary|eligibility varies based on|accept applications for a"
    r" minimum|no agencies please|does not provide, endorse|not liable for third-party|several different entities"
    r"|any position posted in|questions about [^.]{0,40}recruiting process|in search of information on employment"
    r"|kept confidential and will be used only|typical base pay range|base pay range for this (?:role|position)"
    r"|this role is hybrid meaning|in-person days for associates|enterprise-? ?wide virtual work days"
    r"|learn more at the [^.]{0,40}careers? (?:web)?site"
    # 2026-11-01.1: Deloitte's pay-range statements, filed as Preferred lines
    r"|wage range for this role|disclosed range estimate|hired at or near the top of the range"
    r"|reasonable estimate of the current range|discretionary annual incentive program)", re.I)


# A pay paragraph the line rejoiner glued onto the requirement before it (a Microsoft certification bullet ending in
# ", or" ran into "Solution Engineering IC5 - The typical base pay range ..."): cut the tail, keep the requirement.
PAY_TAIL = re.compile(r",?\s*(?:(?:or|and)\s+)?(?:[A-Z][\w&/]*\s+){1,4}IC\d+\s*[-–]\s*The typical base pay range\b.*$")


def _footer(piece: str) -> bool:
    return bool(FOOTER_LINE.search(piece)) and not _qualification_shaped(piece)


def _cut_pay_tail(piece: str) -> str:
    return PAY_TAIL.sub("", piece).rstrip(" ,;") if PAY_TAIL.search(piece) else piece


# 2026-10-30.1: an intro sentence addressed to the candidate's work ("You will create and modify processes", "As a
# Senior Manager on our team, you will drive strategy") is a responsibility; the intro section had dropped it (the
# keyword tool lost "tool creation and evolution" and "data integrity" from Capital One R248994). Joining, reporting
# and benefit sentences ("You will join", "you'll report to", "You will be eligible") stay intro.
YOU_WILL = re.compile(
    r"^\W*(?:in this role,?\s+|as (?:a|an|the|our)\b[^.]{0,120}?,\s*)?you(?:['’]ll| will)\s+"
    r"(?!(?:be (?:part|joining|eligible|offered|paid|considered|reporting)|join|report|receive|enjoy|get|have access"
    r"|have the (?:opportunity|chance)|work (?:alongside|with a team))\b)", re.I)


def _you_will(line: str) -> bool:
    return any(YOU_WILL.search(s) for s in _SENTENCE_SPLIT.split(line))


LONG_LINE = 600   # a paragraph-dump line: handled sentence by sentence, never dropped or kept whole
_SENTENCE_SPLIT = re.compile(r"(?<!\bU\.S)(?<=[.!?])\s+(?=[A-Z(])|(?<=[a-z][a-z][.!?])(?=[A-Z][a-z])|\s(?=\(?\d{1,2}\)\s+[A-Z])")


def _unwrap_paragraphs(text: str) -> str:
    """2026-09-30.1: a line longer than LONG_LINE (a USAJobs announcement is a handful of 2-6K-character paragraphs)
    becomes one line per sentence BEFORE boilerplate stripping, so `strip_boilerplate`'s two-markers-per-line drop
    and the heading pass judge sentences, not paragraphs. Sentences split on ". Capital", on a missing space after a
    period ("service.Specialized"), and before inline "1) ..." / "(2) ..." items."""
    out = []
    for line in (text or "").splitlines():
        if len(line) > LONG_LINE:
            out.extend(p.strip() for p in _SENTENCE_SPLIT.split(line) if p and p.strip())
        else:
            out.append(line)
    return "\n".join(out)
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
    # 2026-11-01.1: Toast's "What will help you stand out (Nonessential Skills/Nice to Haves)" -- a line that names its
    # own optional status in a short label is a Preferred heading, whatever its case
    if (not _BULLET.match(line) and len(s.split()) <= 12 and not s.endswith(".")
            and re.search(r"\b(?:nice[- ]to[- ]haves?|non-?essential|bonus points|(?:help you |make you )?stand out)\b", s, re.I)
            and not re.search(r"\b(?:experience|years?|degree)\b.{3,}", re.sub(r"\([^)]*\)", "", s), re.I)):
        return "preferred"
    if not (ends_colon or short_label or _known_heading_label(s) or drop_question or _whole_heading(s)):
        # 2026-11-01.1: GDIT's "...will execute the following responsibilities:" ran past the 80-character colon limit
        if raw.endswith(":") and len(s) <= 150 and re.search(
                r"(?:the following|these|key|primary|additional) (?:responsibilities|duties)"
                r"|(?:responsibilities|duties) (?:include|are|will include)", s, re.I):
            return "responsibility"
        return None
    # 2026-11-01.1: a degree line is a claim, never a heading (Fannie Mae's "Bachelor's Level Degree (Required)")
    if re.search(r"\b(?:bachelor|master|associate|doctorate|mba|ph\.?\s?d)[’']?s?\b", s, re.I) and not ends_colon:
        return None
    # 2026-11-01.1: a short line that only LOOKS like a label (no colon, not a known heading phrase end to end) and
    # whose only cue is a topic noun ("experience", "skills", "knowledge") is content, not a heading: Deloitte's first
    # Preferred item "Azure DevOps experience" re-opened Required for the rest of the list; "Excellent time
    # management skills", "Local market knowledge" did the same. "Technical skills", "Key responsibilities",
    # "Required skills" still open their sections.
    only_short = not (ends_colon or _known_heading_label(s) or drop_question or _whole_heading(s))
    s_req = REQ_WORK_OBJECT.sub(" ", s)
    topic_only = only_short and _topic_noun_line(s_req)
    # 2026-09-29.1: a short line naming a qualification ("Advanced degree preferred", "Public Trust") is a claim
    if (QUALIFICATION_TERM.search(s) and not ends_colon and not _known_heading_label(s) and not _whole_heading(s)
            and not re.search(DROP_HEADINGS, s, re.I)):
        return None
    if LOGISTICS.search(s) and not re.search(DROP_HEADINGS, s, re.I):
        return None
    if re.search(r"physical (?:demands?|requirements)|work(?:ing)? (?:environment|conditions)", s, re.I):
        return "drop"   # 2026-09-29.1: "Physical Requirements" names 'requirements' but is logistics (11-01.1: "Physical
                        # Demand Requirements", USAA)
    # 2026-11-01.1: a benefit heading naming a bonus ("Peer Bonus Awards", "Annual Bonus Eligibility": 221 postings)
    # opened Preferred through PREFERRED_HEADINGS' "bonus"; "Bonus points", "Bonus if", a bare "Bonus" stay Preferred
    if re.search(r"tuition reimbursement|educational assistance|parental leave|paid time off|employee referral", s, re.I):
        return "drop"   # 2026-11-01.1: benefit headings that also name a person word ("Educational Assistance")
    if re.search(r"\b(?:bonus|incentive)", s, re.I) and re.search(
            r"annual|eligib|award|potential|target|discretionary|\blti\b|program|plan\b|structure|sign[- ]on|referral"
            r"|peer|\d", s, re.I):
        return "drop"
    if re.search(RESET_HEADINGS, s, re.I):
        return "reset"
    if re.search(DROP_HEADINGS, s, re.I if not re.match(r"about [A-Z]", s) else 0) and not re.search(
            P.REQUIRED_HEADINGS + "|" + RESPONSIBILITY_HEADINGS + "|" + PERSON_HEADINGS, s, re.I):
        return "drop"   # 2026-09-28.1: PERSON_HEADINGS joins the exclusion, so "About You" is no longer dropped
    if topic_only:
        return None
    if re.fullmatch(REQUIRED_LABELS, s, re.I):
        return "required"
    if re.search(P.PREFERRED_HEADINGS, s, re.I) or re.search(SPLITTER_PREFERRED_HEADINGS, s, re.I):
        return "preferred"
    # 2026-11-01.1: a heading naming duties outright ("Key Roles and Responsibilities") is Responsibilities even when
    # it also carries a person phrase ("What we are looking for: Key Roles and Responsibilities", 30 postings)
    if re.search(r"responsibilit|duties", s, re.I) and not re.search(P.REQUIRED_HEADINGS, s_req, re.I):
        return "responsibility"
    if re.search(P.REQUIRED_HEADINGS, s_req, re.I) or re.search(PERSON_HEADINGS, s_req, re.I):
        return "required"
    if re.search(RESPONSIBILITY_HEADINGS, s, re.I):
        return "responsibility"
    return None


_CONNECTIVES = ("and", "or", "of", "the", "a", "an", "to", "for", "in", "on", "with", "&")
# 2026-11-01.1: modifiers a topic heading may carry beside its noun ("Technical Skills", "Relevant experience").
_TOPIC_MODIFIERS = {"technical", "professional", "core", "key", "required", "relevant", "additional", "your", "other",
                    "soft", "hard", "general", "specific", "functional", "leadership", "work", "job", "skills",
                    "experience", "knowledge", "education", "expertise", "competencies", "competency", "abilities",
                    "qualifications", "background", "and", "&", "/", "of", "the", "our", "preferred", "desired",
                    "minimum", "basic", "essential", "critical", "must", "have", "nice", "to", "requirements"}


def _topic_noun_line(s: str) -> bool:
    """True when the line's only heading cue is a person topic noun (PERSON_HEADINGS) and it carries other content
    words besides the allowed modifiers -- "Azure DevOps experience", not "Technical Skills"."""
    if re.search("|".join([P.REQUIRED_HEADINGS, P.PREFERRED_HEADINGS, SPLITTER_PREFERRED_HEADINGS,
                           RESPONSIBILITY_HEADINGS]), s, re.I):
        return False
    if not re.search(PERSON_HEADINGS, s, re.I):
        return False
    words = [w.lower() for w in re.findall(r"[A-Za-z][\w'’-]*|&|/", s)]
    return any(w not in _TOPIC_MODIFIERS for w in words)
# 2026-09-29.1: a short line naming a qualification ("Public Trust", "Bachelor's degree", "PMP certification") is a
# requirement even without a bullet -- career-site HTML often renders each list item as a bare line. It is never a
# subheading, so it is no longer skipped.
QUALIFICATION_TERM = re.compile(
    r"\b(?:public trust|clearance|ts/sci|polygraph|secret|citizen(?:ship)?|degree|bachelor|master|mba|ph\.?\s?d"
    r"|doctorate|diploma|certif\w*|licen[cs]\w*|pmp|cpa|six sigma|black belt)\b", re.I)
# 2026-09-29.1: a line that states its own status ("..., required" / "..., preferred") overrides the heading it sits
# under (BDO-style lists put every qualification under one heading and mark each line).
INLINE_STATUS = re.compile(r"(?:[,;(\-–—]\s*|\s)(?<!\bas )(?<!\bwhen )(?<!\bif )(?<!\bwhere )(?<!\bunless )"
                           r"(?<!\bnot )(required|preferred|desired|desirable|advantageous|a plus|a big plus|a bonus"
                           r"|an asset|nice[- ]to[- ]have|good to have|optional)\s*\)?\s*\.?\s*$", re.I)
# 2026-11-01.1 (study §3): preferred status stated at the start or in the middle of a Required line. Leading:
# "Desired experience with C, C++...", "Preferred: ...", "Good to have: ...". Mid-line, outside parentheses: "Hospitality
# experience is a plus, in particular ...", "JD degree desirable, but not required", "Bachelor's not required, but
# recommended". A soft qualifier on a real gate ("5+ years ..., ideally with X", "cloud (AWS preferred)") is NOT
# matched: the gate is real, Jev's grey question reads the qualifier.
LEADING_PREFERRED = re.compile(r"^\W*(?:preferred|desired|desirable|nice[- ]to[- ]have|good[- ]to[- ]have|bonus(?: points)?"
                               r"|a plus|advantageous|optional|ideally)\b", re.I)
MIDLINE_PREFERRED = re.compile(r"\b(?:is|are|would be|will be considered|considered) (?:a |an )?(?:big |huge |strong |added )?"
                               r"(?:plus|asset|advantage|bonus)\b|\b(?:preferred|desirable|desired|recommended|helpful),?"
                               r" (?:but|though|however) not (?:required|necessary|mandatory)|\bnot (?:required|necessary"
                               r"|mandatory)\b(?!\s+(?:to|for)\b)", re.I)
# 2026-11-01.1: "Bachelor's degree required, Master's preferred" -- two statuses in one line are two requirements.
_MIXED_SPLIT = re.compile(r";\s*|,\s+(?=(?:and |or |with )?(?:a |an )?(?:master|mba|ph\.?\s?d|advanced|graduate"
                          r"|doctorate|[A-Z]))", re.I)


# 2026-09-29.1: a line that opens by declaring itself required ("Must have a PhD", "Minimum 7 years ...").
LEADING_REQUIRED = re.compile(r"^\W*(?:must(?: have| be| hold| possess)?|minimum|required|requires|you must)\b", re.I)


def inline_section(line: str):
    """'required' / 'preferred' when the line marks its own status (a trailing ", required" / ", preferred", a
    preferred cue at the start or mid-line, or a leading "Must" / "Minimum"), else None. A trailing status wins,
    then a stated preferred status, then a leading "Must"."""
    m = INLINE_STATUS.search(line.strip())
    if m:
        return "required" if m.group(1).lower() == "required" else "preferred"
    bare = _BULLET.sub("", line)
    if LEADING_PREFERRED.search(bare) or MIDLINE_PREFERRED.search(re.sub(r"\([^)]*\)", " ", bare)):
        return "preferred"
    return "required" if LEADING_REQUIRED.search(bare) else None


def split_mixed_status(piece: str) -> list:
    """2026-11-01.1: a piece stating both a required and a preferred status ("Bachelor's degree required, Master's
    preferred") as its clauses, each to be classed on its own; any other piece unchanged."""
    bare = re.sub(r"\([^)]*\)", " ", piece)
    if not (re.search(r"(?<!not )\brequired\b", bare, re.I)
            and re.search(r"\b(?:preferred|desired|desirable|a plus)\b", bare, re.I)):
        return [piece]
    parts = [p.strip(" ,;") for p in _MIXED_SPLIT.split(piece) if p and p.strip(" ,;")]
    return parts if len(parts) > 1 and all(len(p) >= 10 for p in parts) else [piece]


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
    # 2026-10-23: case-insensitive -- a sentence-case "What you’ll bring" kept its "What", failed the length test,
    # and Oracle 348110's whole requirement list was read as responsibilities (0 Required lines)
    return bool(m) and len(m.group(0)) >= len(re.sub(r"^(?:what|who)\s+|\W+$", "", s, flags=re.I)) - 4


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
    for sent in _SENTENCE_SPLIT.split(line):
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


# 2026-11-01.1 (study §4): "One of the following active accreditations obtained:" followed by 22 certifications sent
# 22 Required lines, any one of which satisfies the gate, so a judge failed the posting on the 21 the candidate lacks.
ANY_OF = re.compile(r"\b(?:(?:at least )?one|any(?: one)?|one or more|either) of the following\b|\bany one of\b", re.I)
_ANY_OF_MAX = 380   # under MAX_UNIT, so _pieces never splits the joined line back up


def _collapse_any_of(lines: list) -> list:
    """An any-of opener line ending in ':' plus the short list after it (to the next heading, blank-separated
    paragraph or long line) becomes ONE line: "<opener>: A; B; C (+N more)". Sub-labels inside the list ("If not
    CPA eligible:") are dropped."""
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line and line.rstrip().endswith(":") and ANY_OF.search(line) and not _BULLET.match(line or "x"):
            items, j = [], i + 1
            while j < len(lines):
                nxt = lines[j]
                if not nxt:
                    if j + 1 < len(lines) and lines[j + 1] and _heading_kind(lines[j + 1]) is None and len(items) < 40:
                        j += 1
                        continue
                    break
                if _heading_kind(nxt) is not None or len(nxt) > 160:
                    break
                if not nxt.rstrip().endswith(":"):
                    items.append(re.sub(r"\.\s+", " ", _BULLET.sub("", nxt).strip().rstrip(".")))
                j += 1
            if len(items) >= 2:
                head = line.rstrip().rstrip(":").strip()
                joined, kept = head + ": ", 0
                for it in items:
                    if len(joined) + len(it) + 14 > _ANY_OF_MAX:
                        break
                    joined += ("; " if kept else "") + it
                    kept += 1
                if kept < len(items):
                    joined += f" (+{len(items) - kept} more)"
                out.append(joined)
                i = j
                continue
        out.append(line)
        i += 1
    return out


def _duty_block(lines: list, kinds: list, i: int) -> bool:
    """The content under heading i (up to 5 lines, to the next heading) is duties: at least 2 lines, >= 60% duty-led,
    none qualification-shaped."""
    body = []
    for line, kind in zip(lines[i + 1:], kinds[i + 1:]):
        if kind is not None:
            break
        if line:
            body.append(_BULLET.sub("", line))
        if len(body) == 5:
            break
    if len(body) < 2 or any(_qualification_shaped(b) for b in body):
        return False
    return sum(bool(DUTY_LEAD.search(b)) for b in body) / len(body) >= 0.6


def _resolve_kinds(lines: list, kinds: list) -> list:
    """2026-11-01.1 (study §1): the section a heading opens, given the section it follows.
      - After Preferred, a Required-kind heading that is not an explicit gate (GATE_HEADING) is a topic inside the
        Preferred list: 'persona' (its qualification-shaped lines still go to Required).
      - After Responsibilities, a Required-kind heading that is not a gate and is followed by duty-led lines is a
        topic inside the duties: it stays Responsibilities."""
    out, cur = list(kinds), None
    for i, (line, kind) in enumerate(zip(lines, kinds)):
        if kind is None:
            continue
        # a heading that is itself a qualification ("5+ years of relevant experience in:") always opens Required
        if (kind == "required" and not GATE_HEADING.search(REQ_WORK_OBJECT.sub(" ", line))
                and not _qualification_shaped(line)):
            if cur == "preferred":
                out[i] = "persona"
                continue
            if cur == "responsibility" and _duty_block(lines, kinds, i):
                out[i] = "responsibility"
                continue
        cur = "intro" if kind == "reset" else kind
    return out


def _labels(lines: list, kinds: list) -> set:
    """2026-11-01.1 (study §6): indexes of sentence-case labels inside duties ("Community strategy and governance",
    "Measurement and continuous improvement"): a short (<= 6 words, <= 60 chars), unbulleted, unpunctuated line with
    no qualification or person word, followed by a longer line. `_subheading` only knew Title Case labels, so these
    counted as duty lines and pushed real duties past Jev's 12-line cap."""
    out, section = set(), None
    nonempty = [i for i, line in enumerate(lines) if line]
    for n, i in enumerate(nonempty):
        line, kind = lines[i], kinds[i]
        if kind is not None:
            section = kind
            continue
        if section not in ("responsibility", "intro", None) or _BULLET.match(line):
            continue
        st = line.strip()
        if (len(st) > 60 or len(st.split()) > 6 or re.search(r"[.!?:;,]$", st) or QUALIFICATION_TERM.search(st)
                or re.search(PERSON_HEADINGS, st, re.I) or YEARS_PHRASE.search(st) or DUTY_LEAD.search(st)):
            continue
        nxt = lines[nonempty[n + 1]] if n + 1 < len(nonempty) else ""
        if len(nxt) > max(60, 2 * len(st)) and kinds[nonempty[n + 1]] is None:
            out.add(i)
    return out


def _prepare(text: str):
    """Lines, resolved heading kinds, whether the posting has section headings, and the explicit-Required flag: the
    shared front half of split_requirements and section_lines."""
    lines = _collapse_any_of(rejoin_lines(strip_boilerplate(_unwrap_paragraphs(text or ""))))
    kinds = _resolve_kinds(lines, [_heading_kind(l) if l else None for l in lines])
    has_headings = any(k in ("required", "preferred", "responsibility", "persona") for k in kinds)
    persona = [k == "persona" or (k == "required" and _soft_person_heading(l)) for l, k in zip(lines, kinds)]
    explicit_required = any(k == "required" and not soft for k, soft in zip(kinds, persona))
    return lines, kinds, persona, has_headings, explicit_required, _labels(lines, kinds)


def _open_section(kind: str, soft: bool, has_headings: bool) -> str:
    if soft:
        return "persona"
    if kind == "reset":
        return "intro" if has_headings else "body"
    return kind


def split_requirements(text: str, max_units: int = 0) -> list:
    """Requirement units in document order. `max_units` > 0 keeps the highest-weight units (ties: earlier first);
    coverage applies the §16.3 cap itself after specificity is known."""
    lines, kinds, persona, has_headings, explicit_required, labels = _prepare(text)
    section = "intro" if has_headings else "body"
    units, seen, qual_shaped = [], set(), []
    for idx, (line, kind, soft) in enumerate(zip(lines, kinds, persona)):
        if not line:
            continue
        if kind is not None:
            section = _open_section(kind, soft, has_headings)
            continue
        if section == "drop" or NOTICE.search(line) or _subheading(line) or idx in labels:
            continue
        bulleted = bool(_BULLET.match(line)) or bool(QUALIFICATION_TERM.search(line))   # an itemized bullet (or a
        # named qualification) is a deliberate claim, even a short one
        for piece in [c for p in _pieces(_BULLET.sub("", line).strip()) for c in split_mixed_status(p)]:
            piece = re.sub(r"^\(?\d{1,2}\)\s*", "", piece.strip())    # an inline "1) ..." item
            opener = USAJOBS_REQUIRED_OPENER.search(piece)
            if opener:
                section = "required"     # 2026-09-30.1: the opener holds for the rest of the paragraph and beyond
                if opener.start() <= 3:
                    piece = piece[opener.end():].lstrip(":;,- ").strip()
            piece = _cut_pay_tail(piece)
            if USAJOBS_NOTICE.search(piece) or _footer(piece):
                continue
            # 2026-09-29.2: a stated status ("..., preferred", "Must ...") belongs to its own sentence, not to every
            # sentence the rejoined line carries
            line_section = inline_section(piece) or section
            if line_section == "intro" and _you_will(piece):
                line_section = "responsibility"
            if line_section == "persona":
                line_section = ("required" if not explicit_required or _qualification_shaped(piece)
                                else "preferred")
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
    units = _rescue_required(units, qual_shaped,
                             required_heading=any(k in ("required", "persona") for k in kinds))
    if max_units and len(units) > max_units:
        keep = sorted(range(len(units)), key=lambda i: (-units[i].weight, i))[:max_units]
        units = [units[i] for i in sorted(keep)]
    return units


def section_lines(text: str) -> list:
    """(section, line, is_subheading) for each content line, in document order: the same boilerplate strip,
    headings and section rules as split_requirements, but whole lines, sub-headings included ("Financial &
    Resource Management" under Responsibilities), and no unit classification. For callers that want a section's
    text, not requirement units (backend/jd_keywords)."""
    lines, kinds, persona, has_headings, _explicit, labels = _prepare(text)
    section, out = ("intro" if has_headings else "body"), []
    for idx, (line, kind, soft) in enumerate(zip(lines, kinds, persona)):
        if not line:
            continue
        if kind is not None:
            section = _open_section(kind, soft, has_headings)
            continue
        if section == "drop" or NOTICE.search(line):
            continue
        line = _cut_pay_tail(line)
        if not line:
            continue
        if _you_will(line) and section == "intro":
            out.append(("responsibility", line, False))
        elif not _footer(line):
            out.append((section, line, _subheading(line) or idx in labels))
    return out


def _soft_person_heading(line: str) -> bool:
    """A Required-kind heading that is a candidate portrait ("Who you are"), not a gate list ("Basic
    Qualifications", "Requirements", "What you bring")."""
    s = line.strip().rstrip(":")
    return (bool(re.search(SOFT_PERSON_HEADINGS, s, re.I)) and not re.search(P.REQUIRED_HEADINGS, s, re.I)
            and not re.search(HARD_PERSON_HEADINGS, s, re.I))


def qualification_shaped(piece: str) -> bool:
    """Public: a years / degree / leading-"Must" line, or one naming a credential (clearance, certification)."""
    return _qualification_shaped(piece) or bool(QUALIFICATION_TERM.search(piece))


def _qualification_shaped(piece: str) -> bool:
    """A years-of-experience or degree line: the shape of a qualification, whatever heading it sits under."""
    return bool((YEARS_PHRASE.search(piece) and re.search(r"\b(?:years?|yrs?)\b", piece, re.I))
                or DEGREE_LINE.search(piece) or LEADING_REQUIRED.search(piece))


def _rescue_required(units: list, qual_shaped: list, required_heading: bool = True) -> list:
    """2026-09-28.1: a posting whose headings name no Required section still states its qualifications -- under
    "Responsibilities", "Job Description", or no heading at all. When NO unit landed in `required`, every
    years/degree-shaped unit outside `preferred` moves to `required`. A posting that has a Required section is
    left exactly as it was. 2026-11-01.1: also when no HEADING opened Required (`required_heading` False) -- one
    inline "Must be open to travel." line no longer blocks the rescue of Perficient's "10+ years ..." lines."""
    if required_heading and any(u.section == "required" for u in units):
        return units
    return [Requirement(u.text, "required", GROUPS["required"], SECTION_WEIGHTS["required"], u.klass, u.source)
            if shaped and u.section in ("intro", "body", "responsibility") else u
            for u, shaped in zip(units, qual_shaped)]


def splitter_fingerprint() -> str:
    """Changes when the splitter version or any heading pattern changes (hashed into rubric / cache versions)."""
    payload = "|".join([SPLITTER_VERSION, RESPONSIBILITY_HEADINGS, PERSON_HEADINGS, SOFT_PERSON_HEADINGS, HARD_PERSON_HEADINGS,
                        DROP_HEADINGS,
                        P.REQUIRED_HEADINGS, P.PREFERRED_HEADINGS, SPLITTER_PREFERRED_HEADINGS])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def current_units_sql(where: str = "TRUE") -> str:
    """SELECT over `requirement_units u` at each posting's CURRENT JD hash, from ONE splitter per posting: the
    current fingerprint where it has rows, else the newest one stored. The table keeps every splitter a JD was
    ever split with, so a reader that filtered on the hash alone read old and new splits together (2026-10-20:
    645 active postings, every line twice, so the top-18 cut held ~9 distinct lines). `where` filters `u`
    (and `p`, the postings row) before the splitter is chosen; `?` placeholders in it bind in order."""
    return f"""SELECT u.* FROM requirement_units u
        JOIN postings p ON p.posting_id = u.posting_id AND coalesce(p.description_hash, '') = u.description_hash
        WHERE {where}
        QUALIFY u.splitter = first_value(u.splitter) OVER (
            PARTITION BY u.posting_id ORDER BY (u.splitter = '{splitter_fingerprint()}') DESC, u.embedded_at DESC,
                                               u.splitter)"""
