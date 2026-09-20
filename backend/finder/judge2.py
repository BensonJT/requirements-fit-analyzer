"""The LLM "second judge" (sprint plan §25): one narrow, strict call on a posting's Required block only.

WHY. §22.2 Gap 2: the first judge's lane call (grade_process/technical/ai) is sound; its Required call is
lenient. This module reads the JD and a public-safe background document and asks a stricter, independent
question -- it never sees the first judge's own output, a score, a rank or a URL.

NOTHING IN THIS MODULE MAY MAKE A LIVE API CALL WITHOUT THE USER'S EXPLICIT GO (`run()`'s live gate, §5 below).
The transport and the sleep function are always INJECTED (never `httpx` or `time.sleep` imported and called
directly at the top of a code path a test can reach), so the test suite never makes a network call and never
depends on wall-clock time.

Sections:
  1. Prompt + payload (`prompt_version`, `build_payload`, `get_background`) -- what is sent, and to whom.
  2. Validation (`parse_response`) -- the hallucination guard: an `unmet` quote must be a VERBATIM substring
     of the JD text that was actually sent, compared after the same whitespace normalization
     `store.normalize_for_hash` uses for description hashing (see the module-level `_WS_RE` note below) and
     NOTHING looser -- no case folding, no fuzzy match.
  3. Client (`_call_with_fallback`) -- httpx POST, model fallback, RPM throttle, all injected.
  4. `run()` -- population, dry-run, the live gate, one review per posting, at most one re-ask.
  5. `evaluate()` -- the §25 acceptance bar against `vw_report_feedback_blind`, storing pass/fail per
     prompt_version so the rank (`vw_lens_fit` in `backend/ats/store.py`) can read it back.

Schema note: `judge2_reviews` (v18) is keyed by (posting_id, description_hash, prompt_version) -- a changed
JD, or a changed prompt, makes the old review stale/incomparable rather than silently reused. See
`vw_judge2_latest` / `vw_judge2_eval_latest` in backend/ats/store.py.
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
THINKING_LEVEL = "minimal"         # Gemma 4 is a thinking model: at "minimal" it emits no thought part, so the
                                   # per-minute token budget is spent on the posting, not on reasoning tokens
CHARS_PER_TOKEN = 4               # crude estimate ("tokens ~ chars/4"), logging/throttle only
REQUIRED_FIT_VALUES = ("meets", "partial", "fails")
CONFIDENCE_VALUES = ("low", "medium", "high")

BACKGROUND_ENV = "JUDGE2_BACKGROUND_FILE"
LIVE_OK_ENV = "JUDGE2_LIVE_OK"

# Same whitespace collapse as backend.ats.store.normalize_for_hash (v11 note), WITHOUT the case-fold: the
# hallucination guard is deliberately stricter than description hashing -- no case folding, no fuzzy match.
_WS_RE = re.compile("[\\s​‌‍﻿]+")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_ws(text: str) -> str:
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text or "")).strip()


# ---------------------------------------------------------------- 1. prompt + payload
PROMPT_TEMPLATE = """You are a strict, independent reviewer. You are given a job posting's Required
qualifications and a background document describing a candidate. Answer ONE question: does the candidate
meet every REQUIRED (not preferred) qualification?

Be strict. Rules:
- "Ability to obtain" a clearance is NOT a held clearance. A clearance that must ALREADY be held or active
  (e.g. "must hold an active TS/SCI", "current Public Trust required") IS a held-clearance requirement
  (held_clearance=true). "TS/SCI with ability to obtain a polygraph" is a HELD requirement (the TS/SCI itself
  must already be held; only the polygraph is obtainable).
- A line asking for N+ years in a NAMED function, domain or platform that the background does not show is a
  years_gap: record the named function and the number of years.
- YEARS IN AN "OR" LIST. When a years line lists several functions or domains joined by commas, "or" or
  "and/or" ("N+ years in A, B, or C"), it is MET only when the background shows that at least ONE listed
  item was the candidate's actual job for N or more years: check it against the background's years-by-
  function figures and name to yourself which item and which years. It is NOT met by adjacent or related
  experience, by work that merely touched a listed item, by adding partial years across different items, or
  by a catch-all tail such as "or a related field". If exactly one listed item clearly was the job for N+
  years, the line is met even when every other item is absent. If none was, it is a years_gap and a hard
  gate: put the line in `unmet` and call `fails`. Do not call `meets` on such a line without being able to
  point to the specific item and years in the background.
- A qualification listed under a Preferred / Desired / Nice-to-have / Bonus heading is NOT required. Never
  put a preferred line in `unmet`, and never let one lower required_fit. The parsed lists below are a
  machine's best split of the JD and can be wrong; the full JD text and its own headings are the authority.
- Every `unmet` entry must be copied CHARACTER-FOR-CHARACTER from the JD text below. Do not paraphrase,
  summarize or combine lines. A quote that is not an exact substring of the JD will be discarded.
- If the background is SILENT on a requirement (it neither shows nor rules it out), that is `partial`, not
  `fails` -- UNLESS the requirement is a hard gate: a held clearance, a professional licence, or a named-
  function years requirement the background clearly does not show.
- TOOLS. A line naming tools or platforms with "such as", "e.g.", "or similar", "or equivalent", or a list
  joined by "or", is MET when the background shows ANY comparable tool of the same kind (one dashboard or
  visualization tool for another, one SQL database for another, one work-tracking tool for another). A
  single tool, "working knowledge of" or "familiarity with" line the background does not show is a
  learnable gap, NOT a hard gate: list it in `unmet`, but it alone never makes the call `fails`, and when the
  role's core function and years are clearly met it does not lower `meets` either. The exception is a tool
  that IS the job: it is named in the title, or the line asks for N+ years of development in that one tool.
  That is a named-function years requirement (a hard gate).
- required_fit is the overall call:
    meets   : every required qualification is met (a lone learnable tool gap, as above, does not count).
    partial : most are met; one or more are unclear, light, or a genuine but non-fatal gap.
    fails   : a hard gate is unmet (held clearance, licence, or a named-function years requirement clearly
              absent), or most required qualifications are unmet.

Output ONLY a JSON object, nothing else, no markdown fences, no commentary:
{"required_fit": "meets|partial|fails", "unmet": ["<verbatim JD line>", ...], "held_clearance": true|false,
 "years_gap": {"function": "<named function>", "years": <number>} or null,
 "confidence": "low|medium|high"}
"""

OUTPUT_CONTRACT = (
    '{"required_fit": "meets|partial|fails", "unmet": [...verbatim JD lines...], '
    '"held_clearance": true|false, "years_gap": {"function": str, "years": number} | null, '
    '"confidence": "low|medium|high"}'
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


def prompt_version(background_text: str) -> str:
    """sha1(prompt template + background text + output contract)[:12]."""
    payload = PROMPT_TEMPLATE + background_text + OUTPUT_CONTRACT
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
class ValidatedReview:
    required_fit: str
    unmet: list = field(default_factory=list)
    unmet_discarded: int = 0
    downgraded: bool = False
    held_clearance: Optional[bool] = None
    years_gap: Optional[dict] = None
    confidence: Optional[str] = None
    raw_response: str = ""


def parse_response(raw_text: str, jd_text_sent: str, *, log=print) -> Optional[ValidatedReview]:
    """Parses and validates one model response against the JD text that was actually SENT. Returns None
    (unparseable / invalid shape -- logged and counted, never recorded as a verdict) or a ValidatedReview with
    the hallucination guard already applied (non-verbatim `unmet` lines discarded; a `fails` with zero
    surviving lines downgraded to `partial`)."""
    obj = _extract_json(raw_text)
    if obj is None:
        log("judge2: response was not parseable JSON; discarding")
        return None
    required_fit = obj.get("required_fit")
    if required_fit not in REQUIRED_FIT_VALUES:
        log(f"judge2: invalid required_fit {required_fit!r}; discarding response")
        return None
    confidence = obj.get("confidence")
    if confidence is not None and confidence not in CONFIDENCE_VALUES:
        confidence = None
    held_clearance = obj.get("held_clearance")
    if not isinstance(held_clearance, bool):
        held_clearance = None
    years_gap = obj.get("years_gap")
    if not (years_gap is None or (isinstance(years_gap, dict) and "function" in years_gap and "years" in years_gap)):
        years_gap = None
    raw_unmet = obj.get("unmet")
    if not isinstance(raw_unmet, list):
        raw_unmet = []

    jd_norm = _norm_ws(jd_text_sent)
    survived, discarded = [], 0
    for quote in raw_unmet:
        if not isinstance(quote, str) or not quote.strip():
            discarded += 1
            continue
        if _norm_ws(quote) in jd_norm:
            survived.append(quote)
        else:
            discarded += 1

    downgraded = False
    if required_fit == "fails" and not survived:
        required_fit, downgraded = "partial", True

    return ValidatedReview(required_fit=required_fit, unmet=survived, unmet_discarded=discarded,
                           downgraded=downgraded, held_clearance=held_clearance, years_gap=years_gap,
                           confidence=confidence, raw_response=raw_text)


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
                                 "thinkingConfig": {"thinkingLevel": THINKING_LEVEL}}}
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
    con.execute("""
        INSERT OR REPLACE INTO judge2_reviews (
            posting_id, description_hash, prompt_version, provider, model, required_fit, unmet,
            unmet_discarded, downgraded, held_clearance, years_gap, confidence, raw_response, prompt_chars,
            reviewed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [pid, description_hash, pv, PROVIDER, model, review.required_fit, json.dumps(review.unmet),
          review.unmet_discarded, review.downgraded, review.held_clearance,
          json.dumps(review.years_gap) if review.years_gap is not None else None, review.confidence,
          review.raw_response, prompt_chars, _now()])


def run(con, *, top_n: int = 150, dry_run: bool = False, force: bool = False, show: int = 1,
       background: str = "public", background_path: Optional[str] = None, jd_cap: int = DEFAULT_JD_CAP,
       only_blind: bool = False, i_have_approval: bool = False, transport=None, sleep_fn=None,
       rpm: Optional[int] = None, log=print) -> dict:
    """Builds payloads for the population (top N by rank, or every blind human-graded row when
    `only_blind=True`) and, unless `dry_run`, sends them to the live API through the INJECTED `transport`.

    `dry_run=True` builds and prints the full payload for the first `show` postings plus a summary, and NEVER
    calls `transport` (a test asserts this). It needs no API key and bypasses the live gate below -- it makes
    no call, so there is nothing to gate.

    The live gate (belt and braces, §5): a non-dry-run call refuses to start unless `JUDGE2_LIVE_OK=1` is set
    or `i_have_approval=True` is passed, so a scheduled pipeline (`pipeline.judge2_stage`) can never make the
    first live call by accident.
    """
    sql = EVAL_SET_SQL if only_blind else POPULATION_SQL
    rows = con.execute(sql).fetchall()
    if not only_blind:
        rows = rows[:top_n]
    background_text = get_background(background, path=background_path)
    pv = prompt_version(background_text)

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
              "provider": PROVIDER, "models": models, "prompt_version": pv,
              "payload_fields": list(PAYLOAD_FIELDS)}

    if dry_run:
        for pid, description_hash, title, employer, payload in candidates[:show]:
            log(f"--- judge2 dry-run payload: {pid} ({title!r} @ {employer!r}) ---")
            log(json.dumps(payload, indent=2))
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
    reviewed, unparseable, skipped_no_model = 0, 0, 0
    pace = (60.0 / rpm) if rpm > 0 else 0.0   # paid on EVERY request (a failed or re-asked one too), never only on success
    tpm = int(os.environ.get("GEMINI_TPM", DEFAULT_TPM) or DEFAULT_TPM)

    def _wait(prompt_text: str) -> float:
        # Token-aware pacing: the free tier's binding limit is tokens per minute, not requests. Wait long
        # enough AFTER a request of N estimated tokens that the rolling minute stays under `tpm`.
        est = len(prompt_text) / CHARS_PER_TOKEN + 400   # + a small allowance for the JSON answer
        return max(pace, 60.0 * est / tpm) if tpm > 0 else pace
    last_prompt = ""
    for i, (pid, description_hash, title, employer, payload) in enumerate(candidates):
        prompt_text = render_prompt(payload)
        if i and _wait(last_prompt):
            sleep_fn(_wait(last_prompt))
        last_prompt = prompt_text
        model, data = _call_with_fallback(transport, sleep_fn, models, api_key, prompt_text, log=log)
        if data is None:
            skipped_no_model += 1
            continue
        raw_text = _gemini_text(data) or ""
        review = parse_response(raw_text, payload["jd_text"], log=log)
        if review is None:
            # at most one re-ask per posting
            if _wait(prompt_text):
                sleep_fn(_wait(prompt_text))
            model, data = _call_with_fallback(transport, sleep_fn, models, api_key, prompt_text, log=log)
            raw_text = _gemini_text(data) if data else ""
            review = parse_response(raw_text or "", payload["jd_text"], log=log) if raw_text else None
        if review is None:
            unparseable += 1
            log(f"judge2: {pid} gave no usable verdict after one re-ask; recording nothing")
            continue
        _insert_review(con, pid=pid, description_hash=description_hash, pv=pv, model=model, review=review,
                       prompt_chars=len(prompt_text))
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
        LEFT JOIN vw_judge2_latest r ON r.posting_id = rf.posting_id AND r.prompt_version = ?
        WHERE rf.required_fit IS NOT NULL
    """, [pv]).fetchall()

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
