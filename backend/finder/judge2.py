"""The LLM "second judge" (sprint plan §25, extended by §29): a strict, independent read of a posting's
Required block, one JD line at a time.

WHY. §22.2 Gap 2: the first judge's lane call (grade_process/technical/ai) is sound; its Required call is
lenient. §25's first version asked the model for one overall call plus a list of unmet lines; four live
evaluations all missed the bar (see SPRINT_PLAN.md §29's "Why"), and a bare `meets` with an empty `unmet` list
left nothing to audit. §29 keeps the model to one narrow judgment -- rate ONE line -- and moves the policy that
turns lines into a call (`derive_required_fit`) into code, where it is pure, unit-tested and re-tunable with NO
new API call (`finder.py judge2 rederive`). This module still never sees the first judge's own output, a
score, a rank or a URL.

NOTHING IN THIS MODULE MAY MAKE A LIVE API CALL WITHOUT THE USER'S EXPLICIT GO (`run()`'s live gate, §4 below).
The transport and the sleep function are always INJECTED (never `httpx` or `time.sleep` imported and called
directly at the top of a code path a test can reach), so the test suite never makes a network call and never
depends on wall-clock time.

Sections:
  1. Prompt + payload (`prompt_version`, `build_payload`, `get_background`) -- what is sent, and to whom.
  2. Validation (`parse_response`) -- TWO guards. The §25 guard: a line's `line` must be a VERBATIM substring
     of the JD text that was actually sent, compared after the same whitespace normalization
     `store.normalize_for_hash` uses for description hashing (see the module-level `_WS_RE` note below) and
     NOTHING looser -- no case folding, no fuzzy match. The §29 guard: a `met` verdict's `evidence` must
     likewise be a verbatim substring of the BACKGROUND text sent; empty or non-substring evidence downgrades
     the verdict to `unclear` rather than being trusted at face value (the fix for the bare `meets` above).
  2b. Derivation (`derive_required_fit`, §29.2) -- a PURE function, no I/O: turns validated per-line verdicts
      into one `required_fit` call and a short `why`. Module-level constants are the only tunables.
  3. Client (`_call_with_fallback`) -- httpx POST, model fallback, RPM throttle, all injected.
  4. `run()` -- population, dry-run, the live gate, one review per posting, at most one re-ask.
  5. `evaluate()` -- the §25 acceptance bar against `vw_report_feedback_blind`, storing pass/fail per
     prompt_version so the rank (`vw_lens_fit` in `backend/ats/store.py`) can read it back. Also
     `line_level_report()` (§29.4) and `compare()` (§29.4's `--compare`).

Schema note: `judge2_reviews` (v18, extended v20) is keyed by (posting_id, description_hash, prompt_version) --
a changed JD, or a changed prompt, makes the old review stale/incomparable rather than silently reused. The
per-line answers themselves live in `judge2_lines` (v20), same key plus `line_no`. See `vw_judge2_latest` /
`vw_judge2_eval_latest` in backend/ats/store.py -- both are untouched by §29: `judge2_reviews.required_fit` /
`unmet` / `years_gap` are still filled in, now from the derived call rather than asked of the model directly.
"""
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import requirements
from . import rules

PROVIDER = "gemini"
API_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_JD_CAP = 12_000
BACKOFF_SECONDS = (2, 8)          # §25 / §10: back off, then fall to the next model
DEFAULT_RPM = 10                  # sane conservative default when GEMINI_RPM is unset
DEFAULT_TPM = 12_000               # free tier is ~14K tokens/minute PER MODEL; stay under it (GEMINI_TPM overrides)
CHARS_PER_TOKEN = 4               # crude estimate ("tokens ~ chars/4"), logging/throttle only
EXPECTED_OUTPUT_TOKENS = 900       # §29.5: per-line output runs ~600-1,200 tokens/posting (was ~100 under the
                                   # §25 overall-call contract, where the pacing allowance was a flat 400);
                                   # the token-aware pacing (`_wait` below) must budget for the ANSWER too
MAX_OUTPUT_TOKENS = 4096          # §29.5: raised so a real per-line answer is never truncated mid-response;
                                   # a response that IS truncated still parses as unparseable (never partial)
REQUIRED_FIT_VALUES = ("meets", "partial", "fails")
CONFIDENCE_VALUES = ("low", "medium", "high")
LINE_SECTION_VALUES = ("required", "preferred")
LINE_KIND_VALUES = ("clearance", "licence", "years_function", "degree", "tool", "skill")
LINE_VERDICT_VALUES = ("met", "unmet", "unclear")

BACKGROUND_ENV = "JUDGE2_BACKGROUND_FILE"
LIVE_OK_ENV = "JUDGE2_LIVE_OK"
THINKING_ENV = "JUDGE2_THINKING"   # §29.5: thinkingLevel is now a setting, not a constant -- it joins the
                                   # prompt_version hash (below), so a higher level is evaluated as its own
                                   # prompt_version rather than silently changing what a passed bar covers
DEFAULT_THINKING = "minimal"       # Gemma 4 is a thinking model: at "minimal" it emits no thought part, so the
                                   # per-minute token budget is spent on the posting, not on reasoning tokens


def thinking_level() -> str:
    return os.environ.get(THINKING_ENV) or DEFAULT_THINKING

# Same whitespace collapse as backend.ats.store.normalize_for_hash (v11 note), WITHOUT the case-fold: the
# hallucination guard is deliberately stricter than description hashing -- no case folding, no fuzzy match.
_WS_RE = re.compile("[\\s​‌‍﻿]+")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_ws(text: str) -> str:
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text or "")).strip()


# ---------------------------------------------------------------- 1. prompt + payload
# §29.1: the model rates ONE line at a time and is asked for no overall call -- that policy moved into code
# (`derive_required_fit`, §2b below). The clearance wording, the TOOLS rule and the years-in-an-OR-list rule
# carry over from §25's overall-call prompt, reworded as guidance for rating a single line.
PROMPT_TEMPLATE = """You are a strict, independent reviewer. You are given a job posting and a background
document describing a candidate. For EACH qualification line you find under the posting's Required heading
(and any Preferred lines you choose to include, marked as such), answer independently: does the background
show the candidate meets THIS ONE line? You are not asked for an overall call -- that is computed afterward
from your per-line answers.

For every REQUIRED qualification line (the parsed Required list below is a hint; the full JD text and its own
headings are the authority, and can override the hint when they disagree), and any Preferred line you choose
to rate, return one entry:
- "line": the JD line, copied CHARACTER-FOR-CHARACTER from the JD text below. Do not paraphrase, summarize or
  combine lines. A line that is not an exact substring of the JD text will be discarded.
- "section": "required" or "preferred" -- follow the JD's own heading, not the parsed hint, when they disagree.
- "kind": "clearance", "licence", "years_function", "degree", "tool", or "skill" -- the single best fit.
- "verdict": "met", "unmet", or "unclear". Use "unclear" when the background is SILENT on this one line (it
  neither shows nor rules it out) -- a forced two-way answer makes you guess, and "unclear" is not a penalty.
- "evidence": ONLY for a "met" verdict, the background sentence or phrase you relied on, copied CHARACTER-FOR-
  CHARACTER from the background document below. A "met" with no evidence, or evidence that is not an exact
  substring of the background, will be downgraded to "unclear" -- so only answer "met" when you can point to
  the exact background text that shows it. Leave it out (or empty) for "unmet" / "unclear".
- "years": for a "years_function" line only, {"function": "<named function>", "required": <number>,
  "shown": <number, or null if the background does not show a figure>}. Omit or set null for every other kind.

Rules for rating a single line:
- CLEARANCE. "Ability to obtain" a clearance is NOT a held clearance -- it is obtainable, not already
  required, and is never a "clearance"-kind hard gate; rate it on what the background shows about the
  candidate's ability to obtain it. A clearance that must ALREADY be held or active (e.g. "must hold an
  active TS/SCI", "current Public Trust required") IS a held-clearance line (kind="clearance"); rate it "met"
  only when the background shows the clearance is currently held. "TS/SCI with ability to obtain a
  polygraph" is a HELD-clearance line for the TS/SCI itself (kind="clearance"); the polygraph clause is a
  separate, obtainable matter and never changes this line's verdict.
- YEARS IN AN "OR" LIST. When a years line lists several functions or domains joined by commas, "or" or
  "and/or" ("N+ years in A, B, or C"), rate it "met" only when the background shows that at least ONE listed
  item was the candidate's actual job for N or more years: check it against the background's years-by-
  function figures and record which item and which years in "years". It is NOT met by adjacent or related
  experience, by work that merely touched a listed item, by adding partial years across different items, or
  by a catch-all tail such as "or a related field". If exactly one listed item clearly was the job for N+
  years, rate "met" even when every other item is absent. If none was, rate "unmet" and set "years" to the
  item and years asked. Do not rate "met" on such a line without being able to point to the specific item and
  years in the background.
- TOOLS. A line naming tools or platforms with "such as", "e.g.", "or similar", "or equivalent", or a list
  joined by "or", is "met" when the background shows ANY comparable tool of the same kind (one dashboard or
  visualization tool for another, one SQL database for another, one work-tracking tool for another). A single
  named tool, "working knowledge of" or "familiarity with" line the background does not show is "unmet", kind
  "tool" -- record it plainly. Whether that makes it a hard gate or a learnable gap is decided afterward, in
  code, from the posting's title and this line's own wording -- not something you need to judge.
- PREFERRED lines are informational only. Rate them the same way as required lines, but they never affect the
  Required call -- that logic lives outside this prompt entirely.

Output ONLY a JSON object, nothing else, no markdown fences, no commentary:
{"lines": [{"line": "<verbatim JD line>", "section": "required|preferred",
            "kind": "clearance|licence|years_function|degree|tool|skill", "verdict": "met|unmet|unclear",
            "evidence": "<verbatim background text, only for met>",
            "years": {"function": "<named function>", "required": <number>, "shown": <number>|null} | null},
           ...],
 "held_clearance": true|false, "confidence": "low|medium|high"}
"""

OUTPUT_CONTRACT = (
    '{"lines": [{"line": str, "section": "required|preferred", '
    '"kind": "clearance|licence|years_function|degree|tool|skill", "verdict": "met|unmet|unclear", '
    '"evidence": str, "years": {"function": str, "required": number, "shown": number|null} | null}, ...], '
    '"held_clearance": true|false, "confidence": "low|medium|high"}'
)

# The ONLY fields sent, in this order (a test asserts this list against build_payload's actual keys).
PAYLOAD_FIELDS = ("instructions", "background", "title", "employer", "jd_text", "required_lines",
                  "preferred_lines")


def public_background() -> str:
    """The default, public-safe background: the three neutral lens descriptions from `rubric.py` ITSELF,
    captured into `rubric.JUDGE2_PUBLIC_BACKGROUND` before rubric.py's own `rubric_local` star import, so a
    real or fake `rubric_local` can never change what this sends (see rubric.py's comment on that constant)."""
    from . import rubric
    return rubric.JUDGE2_PUBLIC_BACKGROUND


def file_background(path: Optional[str] = None) -> str:
    """A user-curated, gitignored fact sheet. Path from `path`, else the `JUDGE2_BACKGROUND_FILE` env var."""
    resolved = path or os.environ.get(BACKGROUND_ENV)
    if not resolved:
        raise ValueError(f"background='file' requires a path or the {BACKGROUND_ENV} env var")
    p = Path(resolved).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"{BACKGROUND_ENV} points at a file that does not exist: {p}")
    return p.read_text(encoding="utf-8")


def get_background(mode: str = "public", *, path: Optional[str] = None) -> str:
    """`mode` is 'public' (default, safe on a free API tier) or 'file' (a user-curated sheet at `path` or
    `JUDGE2_BACKGROUND_FILE`). There is no mode that sends RUBRIC_PERSONAL* -- see tests/test_judge2.py."""
    if mode == "public":
        return public_background()
    if mode == "file":
        return file_background(path)
    raise ValueError(f"unknown background mode {mode!r} (expected 'public' or 'file')")


def prompt_version(background_text: str, *, thinking: Optional[str] = None) -> str:
    """sha1(prompt template + background text + output contract + thinking level)[:12]. §29.5: `thinking`
    joins the hash (default: whatever `JUDGE2_THINKING` / DEFAULT_THINKING resolves to right now) so a run
    under a higher thinking level is evaluated as its own prompt_version, never silently folded into a bar a
    lower level already passed."""
    thinking = thinking if thinking is not None else thinking_level()
    payload = PROMPT_TEMPLATE + background_text + OUTPUT_CONTRACT + thinking
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _trim_jd(text: str, cap: int) -> str:
    """Trims to `cap` characters, keeping the Required/Qualifications section preferentially: the Required
    block (via `rules.find_required_block`, reused rather than re-implemented) first, then as much of the
    start of the JD as still fits."""
    text = text or ""
    if len(text) <= cap:
        return text
    block = rules.find_required_block(text)
    if not block:
        return text[:cap]
    block = block[:cap]
    remaining = cap - len(block)
    if remaining <= 0:
        return block
    head = text[:remaining]
    return f"{head}\n...\n{block}" if head not in block else block


def section_lines(jd_text: str, section: str) -> list:
    """The parsed lines of one JD SECTION ('required' or 'preferred'), text only, in document order (reuses
    `requirements.split_requirements`, never re-implemented). Reads `section`, NOT `group`: requirements.py's
    'required' GROUP is Required + Preferred together (the person-facing group), and labelling a Preferred line
    as Required here is exactly the false `fails` this judge exists to avoid."""
    return [u.text for u in requirements.split_requirements(jd_text) if u.section == section]


def required_lines(jd_text: str) -> list:
    return section_lines(jd_text, "required")


def build_payload(*, title: str, employer: str, jd_text: str, background: str = "public",
                  background_path: Optional[str] = None, jd_cap: int = DEFAULT_JD_CAP) -> dict:
    """The exact payload sent, and ONLY these fields (PAYLOAD_FIELDS): no posting_id, no URL, no pay, no
    scores, no first-judge output -- the second judge must judge independently."""
    background_text = get_background(background, path=background_path)
    trimmed = _trim_jd(jd_text, jd_cap)
    return {
        "instructions": PROMPT_TEMPLATE + "\n" + OUTPUT_CONTRACT,
        "background": background_text,
        "title": title or "",
        "employer": employer or "",
        "jd_text": trimmed,
        "required_lines": required_lines(jd_text or ""),
        "preferred_lines": section_lines(jd_text or "", "preferred"),
    }


def render_prompt(payload: dict) -> str:
    """The payload as one prompt string. Gemma models do not accept a separate system instruction, so
    everything -- instructions, background, posting facts, JD, parsed Required lines -- goes in one user turn."""
    lines_block = "\n".join(f"- {l}" for l in payload["required_lines"]) or "(none parsed)"
    preferred_block = "\n".join(f"- {l}" for l in payload["preferred_lines"]) or "(none parsed)"
    return (
        f"{payload['instructions']}\n\n"
        f"=== BACKGROUND ===\n{payload['background']}\n\n"
        f"=== POSTING ===\nTitle: {payload['title']}\nEmployer: {payload['employer']}\n\n"
        f"=== PARSED REQUIRED LINES ===\n{lines_block}\n\n"
        f"=== PARSED PREFERRED LINES (NOT required; an unmet line here is never a reason for `fails`) ===\n"
        f"{preferred_block}\n\n"
        f"=== FULL JD TEXT (trimmed) ===\n{payload['jd_text']}\n"
    )


# ---------------------------------------------------------------- 2. validation (the hallucination guard)
def _extract_json(text: str) -> Optional[dict]:
    """Tolerant of fenced / prose-wrapped JSON (Gemma models may not honour response_mime_type JSON mode):
    tries a ```json fence first, then the first balanced {...} object in the text."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else None
    if candidate is None:
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        candidate = None
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    break
        if candidate is None:
            return None
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


@dataclass
class LineVerdict:
    """One validated, per-line answer (§29.1). `evidence` is '' unless `verdict == "met"` and it survived the
    substring guard; `evidence_downgraded` says whether THIS line's `met` was downgraded to `unclear` because
    its evidence was missing or fabricated."""
    line: str
    section: str
    kind: str
    verdict: str
    evidence: str = ""
    years: Optional[dict] = None
    evidence_downgraded: bool = False


@dataclass
class ValidatedReview:
    """One parsed-and-validated model response. `lines` are the entries that survived both guards (§2 above);
    `lines_discarded` counts entries dropped outright (bad enum, non-verbatim `line`, malformed shape) and
    `evidence_downgraded` counts `met` verdicts downgraded to `unclear` (kept, not dropped). The overall call
    is NOT computed here -- parse_response has no posting title to apply §29.2's tool-in-title check with;
    call `apply_derivation(title=...)` once the posting is known, which fills `required_fit` / `derive_why` /
    `unmet` / `years_gap` from `derive_required_fit` (§2b) so every caller reads one finished shape."""
    lines: list = field(default_factory=list)
    held_clearance: Optional[bool] = None
    confidence: Optional[str] = None
    lines_discarded: int = 0
    evidence_downgraded: int = 0
    raw_response: str = ""
    required_fit: Optional[str] = None
    derive_why: Optional[str] = None
    unmet: list = field(default_factory=list)
    years_gap: Optional[dict] = None

    def apply_derivation(self, *, title: str = "") -> None:
        fit, why = derive_required_fit(self.lines, title=title, lines_discarded=self.lines_discarded)
        self.required_fit = fit
        self.derive_why = why
        self.unmet = [l.line for l in self.lines if l.section == "required" and l.verdict == "unmet"]
        self.years_gap = next((l.years for l in self.lines
                               if l.section == "required" and l.kind == "years_function"
                               and l.verdict == "unmet" and l.years), None)


def parse_response(raw_text: str, jd_text_sent: str, background_text_sent: str = "",
                   *, log=print) -> Optional[ValidatedReview]:
    """Parses and validates one model response against the JD and background text that were actually SENT.
    Returns None (unparseable / no usable `lines` list -- logged and counted, never recorded as a verdict) or
    a ValidatedReview with both §29.1/§29.2 guards already applied:
      - `line` must be a VERBATIM (whitespace-normalized, case-sensitive) substring of the JD text sent, or
        the whole entry is discarded (invalid `section`/`kind`/`verdict` enums discard it the same way).
      - a `met` verdict whose `evidence` is empty or not a verbatim substring of the background text sent is
        downgraded to `unclear` (kept, counted) rather than trusted at face value.
    Never partially accepts a truncated/unparseable response: `_extract_json` already returns None for a
    response cut off mid-object (no balanced `{...}` to find), so this function does too."""
    obj = _extract_json(raw_text)
    if obj is None:
        log("judge2: response was not parseable JSON; discarding")
        return None
    raw_lines = obj.get("lines")
    if not isinstance(raw_lines, list):
        log("judge2: response has no usable 'lines' list; discarding")
        return None
    confidence = obj.get("confidence")
    if confidence is not None and confidence not in CONFIDENCE_VALUES:
        confidence = None
    held_clearance = obj.get("held_clearance")
    if not isinstance(held_clearance, bool):
        held_clearance = None

    jd_norm = _norm_ws(jd_text_sent)
    bg_norm = _norm_ws(background_text_sent)
    validated, discarded, downgraded_n = [], 0, 0
    for entry in raw_lines:
        if not isinstance(entry, dict):
            discarded += 1
            continue
        line, section, kind, verdict = (entry.get("line"), entry.get("section"),
                                        entry.get("kind"), entry.get("verdict"))
        if not isinstance(line, str) or not line.strip():
            discarded += 1
            continue
        if (section not in LINE_SECTION_VALUES or kind not in LINE_KIND_VALUES
                or verdict not in LINE_VERDICT_VALUES):
            discarded += 1
            continue
        if _norm_ws(line) not in jd_norm:
            discarded += 1
            continue
        evidence = entry.get("evidence")
        evidence = evidence if isinstance(evidence, str) else ""
        line_downgraded = False
        if verdict == "met" and (not evidence.strip() or _norm_ws(evidence) not in bg_norm):
            verdict, line_downgraded = "unclear", True
            downgraded_n += 1
        years = entry.get("years")
        if not (years is None or (isinstance(years, dict) and "function" in years and "required" in years)):
            years = None
        validated.append(LineVerdict(line=line, section=section, kind=kind, verdict=verdict,
                                     evidence=evidence if verdict == "met" else "", years=years,
                                     evidence_downgraded=line_downgraded))
        if line_downgraded:
            log(f"judge2: 'met' downgraded to 'unclear' (evidence missing/not verbatim): {line!r}")

    return ValidatedReview(lines=validated, held_clearance=held_clearance, confidence=confidence,
                           lines_discarded=discarded, evidence_downgraded=downgraded_n, raw_response=raw_text)


# ---------------------------------------------------------------- 2b. derivation (pure, no I/O -- §29.2)
# Thresholds as module constants, per §29.2, so `finder.py judge2 rederive` can tune them and recompute every
# stored review's `required_fit` from its stored `judge2_lines` with NO new API call.
HARD_GATE_KINDS = ("clearance", "licence", "years_function")   # always a hard gate
SOFT_GATE_KINDS = ("tool", "skill", "degree")                  # a `tool` line is promoted to a hard gate by
                                                                 # `_tool_is_the_job` below; the rest stay soft
SOFT_UNMET_MEETS_MAX = 1            # `meets` tolerates at most this many soft required lines rated `unmet`
                                    # (the lone learnable gap)
SOFT_UNCLEAR_MEETS_MAX_FRACTION = 0.5   # ...and at most this fraction of soft required lines `unclear`. A
                                    # background sheet is SILENT on generic lines ("strong communication
                                    # skills"), and the evidence guard turns an unsupported `met` into
                                    # `unclear`, so counting every soft `unclear` as a gap would make `meets`
                                    # unreachable for an ordinary posting (orchestrator audit, 2026-09-20)
DISCARDED_LINES_CAP_MEETS = True    # a review with >= 1 entry dropped by validation can be at most `partial`:
                                    # a dropped entry may have been an unmet hard gate the model misquoted, so
                                    # `meets` cannot be confirmed (the §25 guard's spirit, kept under §29)
MAJORITY_UNMET_FRACTION = 0.5       # > this fraction of ALL required lines `unmet` -> `fails`, regardless of kind
_YEARS_IN_LINE_RE = re.compile(r"\b\d+\+?\s*years?\b", re.I)   # "N+ years" / "N years" written on the line itself
_TITLE_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9+.#]{1,}\b")    # candidate tool-name tokens (capitalized/acronym)
# Capitalized words that open or pad a requirement line and also appear in ordinary titles. Without this list
# "Data visualization tools such as ..." is a hard gate for every "Data Analyst" (orchestrator audit).
_GENERIC_TITLE_WORDS = frozenset("""experience experienced knowledge proficiency proficient strong ability
    skills skill familiarity working understanding demonstrated proven advanced expert expertise excellent
    data business analytics analysis analyst analytical operations operational management manager managing
    senior principal lead leader leadership director associate specialist consultant engineer engineering
    developer development architect administrator program project product process processes strategy strategic
    planning intelligence reporting systems system solutions services service technology technical digital
    enterprise global customer financial finance risk quality performance improvement transformation change
    tools tool platform platforms software applications application cloud ai it bi and or the of in with""".split())


def _get(obj, key, default=None):
    """Reads `key` off a dict OR an attribute-holding object (LineVerdict or a plain dict), so
    `derive_required_fit` works identically on real parsed lines and on the plain-dict fixtures its own
    table-style unit tests use."""
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _tool_is_the_job(line_obj, title: str) -> bool:
    """§29.2: a `tool` line is a hard gate ONLY when the tool IS the job -- its name appears in the posting
    title, or the line itself asks for N+ years in that one tool (the model should have typed such a line
    `years_function`; this is the code-side double-check the spec calls for). The "name in the title" check
    has no isolated tool-name field to compare against, only the line text and the title, so it looks for a
    capitalized/acronym token from the line (a plausible tool name, e.g. "Salesforce", "SQL", "Tableau")
    that also appears in the title -- a deliberately narrow heuristic; a miss just leaves the line soft."""
    line_text = _get(line_obj, "line") or ""
    if _YEARS_IN_LINE_RE.search(line_text):
        return True
    if not title:
        return False
    title_words = {w.lower() for w in re.findall(r"[A-Za-z0-9+.#]+", title)}   # WHOLE words: "AI" is not in "Retail"
    return any(tok.lower() in title_words and tok.lower() not in _GENERIC_TITLE_WORDS
               for tok in _TITLE_TOKEN_RE.findall(line_text))


def derive_required_fit(lines, *, title: str = "", lines_discarded: int = 0) -> tuple:
    """§29.2, exactly. Pure function, no I/O: `lines` is an iterable of validated per-line entries (LineVerdict
    or plain dicts with the same keys/attributes -- see `_get`); only `section == "required"` entries count,
    Preferred lines never affect the call. Returns `(required_fit, why)`:
      - zero required lines survived validation -> `(None, why)` -- "no call", the caller/evaluate() must
        treat this as unjudged, never as a verdict.
      - a HARD GATE (`clearance`, `licence`, `years_function`, or a `tool` line where the tool IS the job,
        per `_tool_is_the_job`) rated `unmet` -> `fails`.
      - more than `MAJORITY_UNMET_FRACTION` of ALL required lines rated `unmet` -> `fails` (a broader net than
        the hard-gate check alone: several soft gaps together are also disqualifying).
      - a hard gate rated `unclear` (and no `fails` condition above fired) -> `partial`, never `meets`.
      - at most `SOFT_UNMET_MEETS_MAX` soft required lines (`tool`/`skill`/`degree`, not promoted to hard)
        `unmet` AND at most `SOFT_UNCLEAR_MEETS_MAX_FRACTION` of the soft lines `unclear` -> `meets` (the "lone
        learnable gap" §25/§29 both allow) -- unless `lines_discarded` > 0 and `DISCARDED_LINES_CAP_MEETS`,
        which caps the call at `partial` (a dropped entry may have been an unmet hard gate).
      - otherwise -> `partial`.
    """
    required = [l for l in lines if _get(l, "section") == "required"]
    n_required = len(required)
    if n_required == 0:
        return None, "no required lines survived validation"

    hard = [l for l in required if _get(l, "kind") in HARD_GATE_KINDS
           or (_get(l, "kind") == "tool" and _tool_is_the_job(l, title))]
    soft = [l for l in required if l not in hard]

    hard_unmet = [l for l in hard if _get(l, "verdict") == "unmet"]
    if hard_unmet:
        return "fails", f"hard gate unmet: {_get(hard_unmet[0], 'line')}"

    n_unmet_all = sum(1 for l in required if _get(l, "verdict") == "unmet")
    if n_unmet_all > n_required * MAJORITY_UNMET_FRACTION:
        return "fails", f"majority of required lines unmet ({n_unmet_all} of {n_required})"

    hard_unclear = [l for l in hard if _get(l, "verdict") == "unclear"]
    if hard_unclear:
        return "partial", f"hard gate unclear: {_get(hard_unclear[0], 'line')}"

    soft_unmet = [l for l in soft if _get(l, "verdict") == "unmet"]
    soft_unclear = [l for l in soft if _get(l, "verdict") == "unclear"]
    if len(soft_unmet) > SOFT_UNMET_MEETS_MAX:
        return "partial", f"{len(soft_unmet)} soft required lines unmet"
    if soft and len(soft_unclear) > len(soft) * SOFT_UNCLEAR_MEETS_MAX_FRACTION:
        return "partial", f"{len(soft_unclear)} of {len(soft)} soft required lines unclear"
    if lines_discarded and DISCARDED_LINES_CAP_MEETS:
        return "partial", f"{lines_discarded} line(s) dropped by validation; meets cannot be confirmed"
    why = "all hard gates met, no soft gap"
    if soft_unmet:
        why = f"all hard gates met; lone soft gap: {_get(soft_unmet[0], 'line')}"
    return "meets", why


# ---------------------------------------------------------------- 3. client (transport + sleep always injected)
def _post(transport, url: str, headers: dict, body: dict):
    """`transport` is either a plain callable(url, headers=, json=) -> response, or an httpx.Client-like
    object with `.post(url, headers=, json=)`. Tests inject a fake callable; production wiring (never
    exercised by this module directly -- see `run()`'s live gate) would inject a real httpx.Client."""
    if hasattr(transport, "post"):
        return transport.post(url, headers=headers, json=body)
    return transport(url, headers=headers, json=body)


def default_transport():
    """Lazily constructs a real `httpx.Client` for production wiring. Never called by a test (every test
    passes a fake transport, or uses `dry_run=True`, which never reaches this) and never called from `run()`
    unless `dry_run=False` and no transport was injected -- which itself requires the live gate to have
    already passed. Constructing the client makes no network call by itself; `httpx` is an existing
    dependency (requirements.txt)."""
    import httpx
    return httpx.Client(timeout=120.0)


def _gemini_text(response_json: dict) -> Optional[str]:
    # A thinking model returns its reasoning as parts marked `thought: true` AHEAD of the answer; taking
    # parts[0] would parse the reasoning, not the answer. Join every non-thought text part.
    try:
        parts = response_json["candidates"][0]["content"]["parts"]
        texts = [p["text"] for p in parts if isinstance(p, dict) and p.get("text") and not p.get("thought")]
    except (KeyError, IndexError, TypeError):
        return None
    return "".join(texts) if texts else None


def _call_with_fallback(transport, sleep_fn, models: list, api_key: str, prompt_text: str, *,
                        temperature: float = 0.0, log=print):
    """Tries each model in order; within a model, up to two retries with the §10/§25 backoff (2s, 8s) on
    429/5xx/timeout/exception before falling to the next model. Returns (model_name, response_json) or
    (None, None) if every model failed. Never logs the API key. The request timeout lives on the transport
    itself (`default_transport()` builds an `httpx.Client(timeout=30.0)`), not here."""
    headers = {"content-type": "application/json", "x-goog-api-key": api_key}
    body = {"contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {"temperature": temperature, "response_mime_type": "application/json",
                                 "thinkingConfig": {"thinkingLevel": thinking_level()},
                                 "maxOutputTokens": MAX_OUTPUT_TOKENS}}
    last_reason = "no models configured"
    for model in models:
        url = API_URL_TMPL.format(model=model)
        for backoff in (0,) + BACKOFF_SECONDS:
            if backoff:
                sleep_fn(backoff)
            try:
                resp = _post(transport, url, headers, body)
            except Exception as exc:  # noqa: BLE001 -- any transport failure falls through to the next attempt
                last_reason = f"{type(exc).__name__}: {exc}"
                continue
            status = getattr(resp, "status_code", 200)
            if status == 200:
                data = resp.json() if hasattr(resp, "json") else resp
                return model, data
            last_reason = f"HTTP {status}"
            if not (status == 429 or status >= 500):
                break  # a non-retryable 4xx: stop retrying this model, fall to the next one
        log(f"judge2: model {model} failed ({last_reason}); trying next model")
    log(f"judge2: every model failed ({last_reason}); giving up on this posting")
    return None, None


# ---------------------------------------------------------------- 4. population + runner
POPULATION_SQL = """
    SELECT f.posting_id, p.description_hash, p.title, p.employer, p.description_text, f.rank_score
    FROM vw_lens_fit f
    JOIN postings p USING (posting_id)
    WHERE p.status = 'active' AND f.verdict != 'reject' AND NOT f.decided
      AND p.description_text IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM vw_report_feedback_latest rf
                      WHERE rf.posting_id = f.posting_id AND rf.required_fit IS NOT NULL)
    ORDER BY f.rank_score DESC
"""

# --eval-set population (sprint plan §25 correction 3, from the orchestrator): every blind human-graded
# Required row, regardless of screen status or decision -- a blind row that is a screen reject is still
# judged, because it exists to be scored against by evaluate().
EVAL_SET_SQL = """
    SELECT rf.posting_id, p.description_hash, p.title, p.employer, p.description_text
    FROM vw_report_feedback_blind rf
    JOIN postings p USING (posting_id)
    WHERE rf.required_fit IS NOT NULL AND p.description_text IS NOT NULL
"""


def _models_from_env() -> list:
    raw = os.environ.get("GEMINI_API_MODEL", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def _already_reviewed(con, pid: str, description_hash: str, pv: str) -> bool:
    return con.execute(
        "SELECT 1 FROM judge2_reviews WHERE posting_id = ? AND description_hash = ? AND prompt_version = ?",
        [pid, description_hash, pv]).fetchone() is not None


def _insert_review(con, *, pid, description_hash, pv, model, review: ValidatedReview, prompt_chars: int) -> None:
    """`review` must already have `apply_derivation()` applied (`required_fit`/`derive_why`/`unmet`/
    `years_gap` filled from §29.2's derived call, per §29.3). The legacy `unmet_discarded`/`downgraded`
    columns are kept meaningful rather than dropped: `unmet_discarded` mirrors `lines_discarded` (entries the
    validation guard dropped outright), `downgraded` is true when at least one line's evidence guard fired."""
    con.execute("""
        INSERT OR REPLACE INTO judge2_reviews (
            posting_id, description_hash, prompt_version, provider, model, required_fit, unmet,
            unmet_discarded, downgraded, held_clearance, years_gap, confidence, raw_response, prompt_chars,
            reviewed_at, derive_why, lines_discarded, evidence_downgraded, contract
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [pid, description_hash, pv, PROVIDER, model, review.required_fit, json.dumps(review.unmet),
          review.lines_discarded, review.evidence_downgraded > 0, review.held_clearance,
          json.dumps(review.years_gap) if review.years_gap is not None else None, review.confidence,
          review.raw_response, prompt_chars, _now(), review.derive_why, review.lines_discarded,
          review.evidence_downgraded, "lines"])


def _insert_lines(con, *, pid, description_hash, pv, lines: list) -> None:
    """Replaces (never appends to) this review's stored lines -- a `--force`/`--rerun` re-ask can return a
    different number of lines than the previous attempt, so DELETE-then-INSERT is simpler and safer than
    INSERT OR REPLACE against a PK that includes `line_no`."""
    con.execute("DELETE FROM judge2_lines WHERE posting_id = ? AND description_hash = ? AND prompt_version = ?",
               [pid, description_hash, pv])
    for i, l in enumerate(lines):
        con.execute("""
            INSERT INTO judge2_lines (posting_id, description_hash, prompt_version, line_no, line, section,
                                      kind, verdict, evidence, years, evidence_downgraded)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [pid, description_hash, pv, i, l.line, l.section, l.kind, l.verdict, l.evidence,
              json.dumps(l.years) if l.years is not None else None, l.evidence_downgraded])


def rederive(con, *, log=print) -> dict:
    """`finder.py judge2 rederive` (§29.2/§29.3): recomputes `required_fit`/`unmet`/`years_gap`/`derive_why`
    for every stored `contract='lines'` review from its OWN stored `judge2_lines`, with NO new API call --
    tuning the §29.2 constants (or `_tool_is_the_job`'s heuristic) is then free. §25-era `contract='overall'`
    rows have no stored lines to rederive from and are left untouched."""
    rows = con.execute("""
        SELECT r.posting_id, r.description_hash, r.prompt_version, p.title, r.lines_discarded
        FROM judge2_reviews r JOIN postings p USING (posting_id)
        WHERE r.contract = 'lines'
    """).fetchall()
    updated = 0
    for pid, dh, pv, title, n_discarded in rows:
        line_rows = con.execute("""
            SELECT line, section, kind, verdict, evidence, years, evidence_downgraded FROM judge2_lines
            WHERE posting_id = ? AND description_hash = ? AND prompt_version = ? ORDER BY line_no
        """, [pid, dh, pv]).fetchall()
        lines = [{"line": l, "section": s, "kind": k, "verdict": v, "evidence": e,
                 "years": json.loads(y) if y else None, "evidence_downgraded": ed}
                for (l, s, k, v, e, y, ed) in line_rows]
        fit, why = derive_required_fit(lines, title=title or "", lines_discarded=n_discarded or 0)
        unmet = [l["line"] for l in lines if l["section"] == "required" and l["verdict"] == "unmet"]
        years_gap = next((l["years"] for l in lines if l["section"] == "required"
                          and l["kind"] == "years_function" and l["verdict"] == "unmet" and l["years"]), None)
        con.execute("""
            UPDATE judge2_reviews SET required_fit = ?, unmet = ?, years_gap = ?, derive_why = ?
            WHERE posting_id = ? AND description_hash = ? AND prompt_version = ?
        """, [fit, json.dumps(unmet), json.dumps(years_gap) if years_gap is not None else None, why, pid, dh, pv])
        updated += 1
    log(f"judge2.rederive: {updated} review(s) recomputed from stored lines (no API call)")
    return {"updated": updated}


def run(con, *, top_n: int = 150, dry_run: bool = False, force: bool = False, show: int = 1,
       background: str = "public", background_path: Optional[str] = None, jd_cap: int = DEFAULT_JD_CAP,
       only_blind: bool = False, i_have_approval: bool = False, transport=None, sleep_fn=None,
       rpm: Optional[int] = None, rerun: bool = False, run_tag: Optional[str] = None, log=print) -> dict:
    """Builds payloads for the population (top N by rank, or every blind human-graded row when
    `only_blind=True`) and, unless `dry_run`, sends them to the live API through the INJECTED `transport`.

    `dry_run=True` builds and prints the full payload AND the fully rendered prompt for the first `show`
    postings plus a summary, and NEVER calls `transport` (a test asserts this). It needs no API key and
    bypasses the live gate below -- it makes no call, so there is nothing to gate.

    The live gate (belt and braces, §5): a non-dry-run call refuses to start unless `JUDGE2_LIVE_OK=1` is set
    or `i_have_approval=True` is passed, so a scheduled pipeline (`pipeline.judge2_stage`) can never make the
    first live call by accident.

    §29.4's noise-floor tool: `run_tag`, when given, is appended to the prompt_version used as the STORAGE/
    cache key (`f"{base_pv}:{run_tag}"`) -- so a repeat run under the IDENTICAL prompt template lands in its
    own set of `judge2_reviews` rows rather than overwriting the first run's, and `judge2 eval --compare` can
    diff the two by prompt_version. Default `run_tag=None` leaves the key exactly `base_pv`, so production
    caching is byte-for-byte unchanged. `rerun=True` implies `force=True` (a tagged or untagged repeat is
    pointless if `_already_reviewed` just skips it).
    """
    if rerun:
        force = True
    sql = EVAL_SET_SQL if only_blind else POPULATION_SQL
    rows = con.execute(sql).fetchall()
    if not only_blind:
        rows = rows[:top_n]
    background_text = get_background(background, path=background_path)
    base_pv = prompt_version(background_text)
    pv = f"{base_pv}:{run_tag}" if run_tag else base_pv

    candidates = []
    for pid, description_hash, title, employer, description_text, *_rest in rows:
        if not force and _already_reviewed(con, pid, description_hash, pv):
            continue
        payload = build_payload(title=title, employer=employer, jd_text=description_text,
                                background=background, background_path=background_path, jd_cap=jd_cap)
        candidates.append((pid, description_hash, title, employer, payload))

    total_chars = sum(len(json.dumps(c[4])) for c in candidates)
    est_tokens = total_chars // CHARS_PER_TOKEN
    models = _models_from_env()
    summary = {"count": len(candidates), "total_chars": total_chars, "estimated_tokens": est_tokens,
              "provider": PROVIDER, "models": models, "prompt_version": pv, "base_prompt_version": base_pv,
              "payload_fields": list(PAYLOAD_FIELDS)}

    if dry_run:
        for pid, description_hash, title, employer, payload in candidates[:show]:
            log(f"--- judge2 dry-run payload: {pid} ({title!r} @ {employer!r}) ---")
            log(json.dumps(payload, indent=2))
            log(f"--- judge2 dry-run rendered prompt: {pid} ---")
            log(render_prompt(payload))
        log(f"judge2 dry-run summary: {summary}")
        return {"dry_run": True, **summary}

    if not os.environ.get(LIVE_OK_ENV) and not i_have_approval:
        raise RuntimeError(
            f"judge2.run refused: no live call is authorized. Set {LIVE_OK_ENV}=1 or pass --i-have-approval "
            "after you have run `--dry-run` and reviewed the payload and provider/model list.")

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key or not models:
        raise RuntimeError("judge2.run refused: GEMINI_API_KEY and GEMINI_API_MODEL must both be set for a live run")
    rpm = rpm or int(os.environ.get("GEMINI_RPM", DEFAULT_RPM) or DEFAULT_RPM)
    if sleep_fn is None:
        import time as _time
        sleep_fn = _time.sleep
    if transport is None:
        transport = default_transport()
    import time as _time_mod
    run_start = _time_mod.monotonic()   # §29.5: elapsed time for the per-call progress line only -- never
                                        # read by any code path a test asserts on, so it stays real wall time
    reviewed, unparseable, skipped_no_model = 0, 0, 0
    pace = (60.0 / rpm) if rpm > 0 else 0.0   # paid on EVERY request (a failed or re-asked one too), never only on success
    tpm = int(os.environ.get("GEMINI_TPM", DEFAULT_TPM) or DEFAULT_TPM)

    def _wait(prompt_text: str) -> float:
        # Token-aware pacing: the free tier's binding limit is tokens per minute, not requests. Wait long
        # enough AFTER a request of N estimated tokens that the rolling minute stays under `tpm`. §29.5: the
        # allowance now budgets for the per-line ANSWER (EXPECTED_OUTPUT_TOKENS), not the old flat +400.
        est = len(prompt_text) / CHARS_PER_TOKEN + EXPECTED_OUTPUT_TOKENS
        return max(pace, 60.0 * est / tpm) if tpm > 0 else pace
    last_prompt = ""
    n_candidates = len(candidates)
    for i, (pid, description_hash, title, employer, payload) in enumerate(candidates):
        prompt_text = render_prompt(payload)
        if i and _wait(last_prompt):
            sleep_fn(_wait(last_prompt))
        last_prompt = prompt_text
        model, data = _call_with_fallback(transport, sleep_fn, models, api_key, prompt_text, log=log)
        # §29.5: one progress line per call -- n of N, the model that answered (or 'none'), the posting, elapsed.
        log(f"judge2.run: {i + 1}/{n_candidates} · model={model or 'none'} · {pid} · "
           f"elapsed={_time_mod.monotonic() - run_start:.1f}s")
        if data is None:
            skipped_no_model += 1
            continue
        raw_text = _gemini_text(data) or ""
        review = parse_response(raw_text, payload["jd_text"], payload["background"], log=log)
        if review is None:
            # at most one re-ask per posting
            if _wait(prompt_text):
                sleep_fn(_wait(prompt_text))
            model, data = _call_with_fallback(transport, sleep_fn, models, api_key, prompt_text, log=log)
            raw_text = _gemini_text(data) if data else ""
            review = (parse_response(raw_text or "", payload["jd_text"], payload["background"], log=log)
                      if raw_text else None)
        if review is None:
            unparseable += 1
            log(f"judge2: {pid} gave no usable verdict after one re-ask; recording nothing")
            continue
        review.apply_derivation(title=title)
        _insert_review(con, pid=pid, description_hash=description_hash, pv=pv, model=model, review=review,
                       prompt_chars=len(prompt_text))
        _insert_lines(con, pid=pid, description_hash=description_hash, pv=pv, lines=review.lines)
        reviewed += 1

    log(f"judge2.run: {reviewed} reviewed, {unparseable} unparseable (skipped), "
       f"{skipped_no_model} skipped (every model failed), prompt_version={pv}")
    return {"dry_run": False, "reviewed": reviewed, "unparseable": unparseable,
           "skipped_no_model": skipped_no_model, **summary}


# ---------------------------------------------------------------- 5. evaluation (the §25 acceptance bar)
CATCH_BAR = 0.70
AGREE_BAR = 0.85
MIN_N = 5
MAX_UNJUDGED_SHARE = 0.10


def evaluate(con, *, background: str = "public", background_path: Optional[str] = None,
            prompt_version_override: Optional[str] = None, log=print) -> dict:
    """The §25 bar, on `vw_report_feedback_blind` rows ONLY (never a report-anchored 'seen' grade).

    Per the 2026-09-19 orchestrator correction: a human `required_fit` has only two values, meets|fails
    (`blind_sheet.REQUIRED_FIT_VALUES`) -- there is no human `arguable`. "First judge" means the latest
    `llm_labels` row for the posting's current text whose scorer is NOT `feedback.USER_SCORER`
    (`vw_llm_labels_latest_judge`), never a human-bridged row.

    CATCH set  = blind rows where the FIRST judge said `meets` and the human said `fails`. The 70% bar counts
                 a second-judge `fails` OR `partial` as a catch; `fails`-only is reported alongside as
                 informational (`catch_rate_strict`), and the 85% agreement bar (below) is unaffected by it.
    ALL-FAILS  = every blind row the human graded `fails`, regardless of what the first judge said --
                 informational only (`catch_rate_all_fails`); the first-judge-`meets` subset above may be tiny.
    AGREE set  = blind rows the human graded `meets`. Agreement counts a second-judge `meets` ONLY -- a second
                 judge that says `partial` on everything must fail this bar, not pass it by courtesy.

    Bar: catch_rate >= 0.70 AND agree_rate >= 0.85, with a minimum-n guard: if either set has fewer than
    MIN_N=5 judged rows, the result is "insufficient data", the bar is NOT passed, and this is a clean,
    expected outcome (exit 0), not an error -- the live corpus as of 2026-09-19 has zero blind rows with a
    human `required_fit` at all.
    """
    background_text = get_background(background, path=background_path)
    pv = prompt_version_override or prompt_version(background_text)
    rows = con.execute("""
        SELECT rf.posting_id, p.employer, p.title, rf.required_fit AS human_fit,
               j.required_fit AS judge_fit, r.required_fit AS judge2_fit
        FROM vw_report_feedback_blind rf
        JOIN postings p USING (posting_id)
        LEFT JOIN vw_llm_labels_latest_judge j USING (posting_id)
        LEFT JOIN judge2_reviews r ON r.posting_id = rf.posting_id AND r.prompt_version = ?
                                  AND r.description_hash = coalesce(p.description_hash, '')
        WHERE rf.required_fit IS NOT NULL
    """, [pv]).fetchall()
    # Reads `judge2_reviews` for THIS prompt_version at the posting's current hash, NOT `vw_judge2_latest`:
    # that view keeps only the NEWEST review per posting, so evaluating an earlier run after a later one (two
    # rounds, or a `--run-tag` noise-floor repeat) would find every row "unjudged" (orchestrator audit).

    # row = (posting_id, employer, title, human_fit, judge_fit, judge2_fit)
    # Rates are over rows the second judge has actually JUDGED under this prompt_version. An unjudged row is
    # neither a catch nor a miss; but a bar passed on a partial run would be a bar passed on whichever rows
    # happened to come back, so more than MAX_UNJUDGED_SHARE unjudged is "insufficient", never a pass.
    n_eval_rows = len(rows)
    rows = [r for r in rows if r[5] is not None]
    n_unjudged = n_eval_rows - len(rows)
    catch_rows = [r for r in rows if r[4] == "meets" and r[3] == "fails"]
    catch_hits = [r for r in catch_rows if r[5] in ("fails", "partial")]
    catch_hits_strict = [r for r in catch_rows if r[5] == "fails"]
    n_catch = len(catch_rows)
    catch_rate = (len(catch_hits) / n_catch) if n_catch else None
    catch_rate_strict = (len(catch_hits_strict) / n_catch) if n_catch else None

    all_fails_rows = [r for r in rows if r[3] == "fails"]
    n_all_fails = len(all_fails_rows)
    all_fails_hits = [r for r in all_fails_rows if r[5] in ("fails", "partial")]
    catch_rate_all_fails = (len(all_fails_hits) / n_all_fails) if n_all_fails else None

    agree_rows = [r for r in rows if r[3] == "meets"]
    n_agree = len(agree_rows)
    agree_hits = [r for r in agree_rows if r[5] == "meets"]
    agree_rate = (len(agree_hits) / n_agree) if n_agree else None

    too_many_unjudged = n_eval_rows > 0 and n_unjudged / n_eval_rows > MAX_UNJUDGED_SHARE
    insufficient = n_catch < MIN_N or n_agree < MIN_N or too_many_unjudged
    passed = (not insufficient and catch_rate is not None and catch_rate >= CATCH_BAR
             and agree_rate is not None and agree_rate >= AGREE_BAR)

    if insufficient:
        reason = (f"insufficient blind human Required calls judged: n_catch={n_catch}, n_agree={n_agree}, "
                 f"unjudged={n_unjudged} of {n_eval_rows}; import graded blind sheets with required_fit, "
                 "then `judge2 run --eval-set`, first")
    else:
        reason = (f"catch_rate(fails-or-partial)={catch_rate:.2f} (bar {CATCH_BAR}), "
                 f"agree_rate(meets-only)={agree_rate:.2f} (bar {AGREE_BAR})")

    log(f"judge2.evaluate: prompt_version={pv} n_catch={n_catch} catch_rate={catch_rate} "
       f"catch_rate_strict={catch_rate_strict} n_all_fails={n_all_fails} "
       f"catch_rate_all_fails={catch_rate_all_fails} n_agree={n_agree} agree_rate={agree_rate} "
       f"-> {'PASSED' if passed else 'NOT PASSED'} ({reason})")
    if not insufficient:
        for pid, employer, title, human_fit, judge_fit, judge2_fit in catch_rows:
            if judge2_fit not in ("fails", "partial"):
                log(f"  CATCH MISS: {pid} · {employer} · {title} · human=fails judge1=meets judge2={judge2_fit}")
        for pid, employer, title, human_fit, judge_fit, judge2_fit in agree_rows:
            if judge2_fit != "meets":
                log(f"  AGREE MISS: {pid} · {employer} · {title} · human=meets judge2={judge2_fit}")

    import uuid
    con.execute("""
        INSERT INTO judge2_evals (run_id, prompt_version, evaluated_at, n_catch, n_catch_hits, catch_rate,
            n_catch_strict_hits, catch_rate_strict, n_all_fails, n_all_fails_hits, catch_rate_all_fails,
            n_agree, n_agree_hits, agree_rate, passed, reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [uuid.uuid4().hex, pv, _now(), n_catch, len(catch_hits), catch_rate, len(catch_hits_strict),
          catch_rate_strict, n_all_fails, len(all_fails_hits), catch_rate_all_fails, n_agree, len(agree_hits),
          agree_rate, passed, reason])
    return {"prompt_version": pv, "n_catch": n_catch, "catch_rate": catch_rate,
           "catch_rate_strict": catch_rate_strict, "n_all_fails": n_all_fails,
           "catch_rate_all_fails": catch_rate_all_fails, "n_agree": n_agree, "agree_rate": agree_rate,
           "n_unjudged": n_unjudged, "insufficient": insufficient, "passed": passed, "reason": reason}


# The separator `finder.py mark --unmet` / `feedback._mark_required_fit` actually joins multiple unmet lines
# with (`" ; ".join(unmet)`, backend/finder/feedback.py) -- used here rather than the sprint plan prose's
# looser "pipe-separated" phrasing, so this reads the SAME golden-source text `report_feedback.required_unmet`
# actually stores.
GOLD_UNMET_SEPARATOR = " ; "


def line_level_report(con, *, background: str = "public", background_path: Optional[str] = None,
                      prompt_version_override: Optional[str] = None, log=print) -> dict:
    """§29.4's line-level report: for each blind human-graded row that named at least one unmet requirement
    (`vw_report_feedback_blind.required_unmet`, split on GOLD_UNMET_SEPARATOR), report whether the second
    judge rated a MATCHING line (normalized containment either way -- neither side need quote the other
    exactly) `unmet` / `unclear` / `met`, or never listed a matching line at all. This is a finer-grained
    companion to `evaluate()`'s posting-level catch/agree rates: it names the specific line, so a miss can be
    traced to the background sheet, a prompt rule, or the model -- not just "the call disagreed"."""
    background_text = get_background(background, path=background_path)
    pv = prompt_version_override or prompt_version(background_text)
    gold_rows = con.execute("""
        SELECT rf.posting_id, p.employer, p.title, p.description_hash, rf.required_unmet
        FROM vw_report_feedback_blind rf JOIN postings p USING (posting_id)
        WHERE rf.required_unmet IS NOT NULL AND trim(rf.required_unmet) != ''
    """).fetchall()

    results = []
    for pid, employer, title, dh, gold_unmet in gold_rows:
        gold_lines = [g.strip() for g in gold_unmet.split(GOLD_UNMET_SEPARATOR) if g.strip()]
        judge_lines = con.execute("""
            SELECT line, verdict FROM judge2_lines
            WHERE posting_id = ? AND description_hash = ? AND prompt_version = ?
        """, [pid, dh, pv]).fetchall()
        for gold_line in gold_lines:
            gnorm = _norm_ws(gold_line).lower()
            verdict = None
            for jline, jverdict in judge_lines:
                jnorm = _norm_ws(jline).lower()
                if gnorm in jnorm or jnorm in gnorm:
                    verdict = jverdict
                    break
            results.append({"posting_id": pid, "employer": employer, "title": title,
                            "gold_unmet_line": gold_line, "judge_verdict": verdict or "never_listed"})
            if verdict != "unmet":
                log(f"  LINE MISS: {pid} · {employer} · {title} · gold_unmet={gold_line!r} "
                   f"judge={verdict or 'never_listed'}")

    n = len(results)
    n_caught = sum(1 for r in results if r["judge_verdict"] == "unmet")
    log(f"judge2 line-level report: prompt_version={pv} {n_caught}/{n} gold unmet line(s) rated unmet by the judge")
    return {"prompt_version": pv, "n_gold_unmet_lines": n, "n_caught_unmet": n_caught, "rows": results}


def compare(con, prompt_version_a: str, prompt_version_b: str, *, log=print) -> list:
    """§29.4's `judge2 eval --compare <A> <B>`: postings judged under BOTH prompt_version A and B, whose
    `required_fit` call differs between the two. Run on two runs of the SAME prompt (see `run(..., run_tag=)`,
    §29.4's noise-floor tool) this measures run-to-run noise rather than a real prompt change."""
    rows = con.execute("""
        SELECT a.posting_id, p.employer, p.title, a.required_fit AS fit_a, b.required_fit AS fit_b
        FROM judge2_reviews a
        JOIN judge2_reviews b ON b.posting_id = a.posting_id AND b.prompt_version = ?
        JOIN postings p ON p.posting_id = a.posting_id
        WHERE a.prompt_version = ?
    """, [prompt_version_b, prompt_version_a]).fetchall()
    diffs = [{"posting_id": pid, "employer": employer, "title": title, "fit_a": fit_a, "fit_b": fit_b}
            for pid, employer, title, fit_a, fit_b in rows if fit_a != fit_b]
    for d in diffs:
        log(f"  DIFF: {d['posting_id']} · {d['employer']} · {d['title']} · "
           f"{prompt_version_a}={d['fit_a']} vs {prompt_version_b}={d['fit_b']}")
    log(f"judge2 compare: {len(diffs)} differing of {len(rows)} common posting(s) between "
       f"{prompt_version_a} and {prompt_version_b}")
    return diffs


# ---------------------------------------------------------------- status
def status(con, *, background: str = "public", background_path: Optional[str] = None, log=print) -> dict:
    """Counts by verdict, discarded-quote rate, downgrade rate, and the last eval."""
    counts = dict(con.execute("SELECT required_fit, count(*) FROM judge2_reviews GROUP BY 1").fetchall())
    total = sum(counts.values())
    discarded, downgraded_n = con.execute(
        "SELECT sum(unmet_discarded), sum(downgraded::INTEGER) FROM judge2_reviews").fetchone()
    background_text = get_background(background, path=background_path)
    pv = prompt_version(background_text)
    last_eval = con.execute(
        "SELECT * FROM vw_judge2_eval_latest WHERE prompt_version = ?", [pv]).fetchone()
    out = {"total": total, "by_verdict": counts,
          "downgrade_rate": (downgraded_n or 0) / total if total else None,
          "total_discarded_quotes": discarded or 0, "prompt_version": pv, "last_eval": last_eval}
    log(f"judge2 status: {out}")
    return out
