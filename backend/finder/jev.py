"""The Jev typed-decision tier (SPRINT_PLAN §33, docs/JEV_PLAN.md §3): a typed appraiser that sits between
the free screen and the LLM judge.

Two small requests per posting (JEV_PLAN Change 1):
  - ROLE ("R"): the trimmed JD only, never any personal data. Three lens Scores, gate Nouls, an injection
    canary and two report-only Choices, all from `jev_questions.ROLE_QUESTIONS`.
  - LINES ("L"): the posting's required + responsibility lines plus the fact sheet, parsed into facts. Three
    Choices per line (kind, verdict, evidence); evidence is a Choice over fact ids, so it is verbatim by
    construction. Split into chunks when the estimated size passes MAX_REQUEST_TOKENS_EST.

The required-fit call is derived in code by `judge2.derive_required_fit` (unchanged), so Jev and the second
judge share one derivation and differ only in the per-line appraiser.

NOTHING HERE MAKES A LIVE CALL WITHOUT THE USER'S GO: `run()` refuses unless `live_ok=True` and a key is
present. The transport and sleep function are always injected; tests never touch the network.

Sections:
  1. Settings (endpoint, model, key, caps) and the fact-sheet parser.
  2. `prompt_version` and the request builders.
  3. Response parsers (`parse_role_response`, `parse_lines_response`) with the evidence guard.
  4. Client (`_post`, `default_transport`, `call_with_retry`).
  5. `review_posting`, `run`, and the population helpers.
"""
import copy
import hashlib
import json
import math
import os
import re
from datetime import datetime, timezone
from typing import Optional

from . import jev_questions as Q
from . import judge2
from . import requirements
from .jev_types import Caps, Fact, LineRecord, Posting, ReviewRecord, RunSummary

MAX_FACTS = 254                 # a Choice allows 255 options, one of which is "none"
LENS_NAMES = ("process", "technical", "ai")
REQUEST_ID_HEADER = "x-typesafe-request-id"
RETRY_AFTER_CAP_S = 60.0
_BULLET_RE = re.compile(r"^\s*(?:[-*+•·▪◦●○■□➢►✓–—]|\d{1,3}[.)])\s+")   # a marker, then whitespace
_WORD_RE = re.compile(r"\w")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)   # Markdown/HTML comments (edit notes) are never facts


class JevResponseError(ValueError):
    """A Jev response is missing an answer, or an answer has the wrong type or an unknown option."""


class JevAPIError(RuntimeError):
    """A request failed: a non-retryable 4xx, or every retry was used up."""

    def __init__(self, status: Optional[int], request_id: Optional[str], body_snippet: str):
        self.status = status
        self.request_id = request_id
        self.body_snippet = body_snippet
        super().__init__(f"Jev API error: status={status} request_id={request_id} body={body_snippet!r}")


# ---------------------------------------------------------------- 1. settings + fact sheet
def endpoint_from_env() -> str:
    """`JEV_ENDPOINT`: "typesafe" (default) or "vercel"; anything else is an error."""
    endpoint = (os.environ.get(Q.ENDPOINT_ENV) or "typesafe").strip().lower()
    if endpoint not in Q.ENDPOINTS:
        raise ValueError(f"{Q.ENDPOINT_ENV}={endpoint!r} is not one of {sorted(Q.ENDPOINTS)}")
    return endpoint


def model_for(endpoint: str) -> str:
    """The pinned id on TypeSafe; the gateway's unpinned name on Vercel."""
    if endpoint == "typesafe":
        return Q.PINNED_MODEL
    if endpoint == "vercel":
        return Q.VERCEL_MODEL
    raise ValueError(f"unknown Jev endpoint {endpoint!r}")


def api_key(endpoint: str) -> str:
    """The key for `endpoint` from its env var, or "" when unset. Never logged."""
    return os.environ.get(Q.KEY_ENV[endpoint], "").strip()


def caps_from_env() -> Caps:
    """Caps from `JEV_MAX_CALLS_PER_RUN` / `JEV_DAILY_TOKEN_CAP`, else the defaults."""
    return Caps(
        max_calls_per_run=int(os.environ.get(Q.MAX_CALLS_ENV) or Q.DEFAULT_MAX_CALLS_PER_RUN),
        daily_token_cap=int(os.environ.get(Q.DAILY_CAP_ENV) or Q.DEFAULT_DAILY_TOKEN_CAP),
    )


def parse_facts(text: str) -> list:
    """Deterministic fact-sheet parser. A Markdown heading ('#...') sets the current heading and is not a
    fact; every other non-blank line (bullet marker stripped) is one Fact, ids f01, f02, ... in document order
    (three digits for every id once there are more than 99). A line with no letters or digits (a '---' rule)
    is skipped, and HTML comments (<!-- ... -->, single- or multi-line) are removed first. Raises ValueError
    past MAX_FACTS."""
    heading = ""
    items = []
    for raw in _COMMENT_RE.sub("", text or "").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        body = _BULLET_RE.sub("", stripped, count=1).strip()
        if _WORD_RE.search(body):
            items.append((heading, body))
    if len(items) > MAX_FACTS:
        raise ValueError(f"fact sheet parses to {len(items)} facts; a Choice allows at most {MAX_FACTS}")
    width = 3 if len(items) > 99 else 2
    return [Fact(id=f"f{n:0{width}d}", heading=h, text=t) for n, (h, t) in enumerate(items, start=1)]


def load_facts(path: Optional[str] = None) -> list:
    """Reads the fact sheet via `judge2.file_background` (path, else `JUDGE2_BACKGROUND_FILE`)."""
    return parse_facts(judge2.file_background(path))


# ---------------------------------------------------------------- 2. prompt version + requests
def _facts_hash(facts: list) -> str:
    rows = [[f.id, f.heading, f.text] for f in facts]
    return hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def prompt_version(facts: list, endpoint: str) -> str:
    """sha1[:12] over everything that shapes a request: the effective question set, the rendered line
    templates, the model and endpoint, the splitter, the facts and the size caps. Any change re-keys the cache."""
    parts = [
        Q.QUESTION_SET_VERSION,
        json.dumps(Q.ROLE_QUESTIONS, sort_keys=True),
        json.dumps(Q.READING_RULES),
        json.dumps(Q.KIND_CRITERIA, sort_keys=True),
        json.dumps(Q.VERDICT_CRITERIA, sort_keys=True),
        json.dumps(Q.kind_question(0), sort_keys=True),
        json.dumps(Q.verdict_question(0, "required"), sort_keys=True),
        json.dumps(Q.verdict_question(0, "responsibility"), sort_keys=True),
        json.dumps(Q.evidence_question(0, ["f01"]), sort_keys=True),
        model_for(endpoint),
        endpoint,
        requirements.splitter_fingerprint(),
        _facts_hash(facts),
        str(Q.JD_CAP_CHARS),
        str(Q.MAX_REQUIRED_LINES),
        str(Q.MAX_RESPONSIBILITY_LINES),
        str(Q.LOCAL_OVERRIDE),
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()[:12]


def build_role_request(posting: Posting, model: str) -> dict:
    """Request R: title, employer and the trimmed JD. No personal data ever goes in this request."""
    return {
        "state": {
            "title": posting.title or "",
            "employer": posting.employer or "",
            "posting": judge2._trim_jd(posting.description_text or "", Q.JD_CAP_CHARS),
        },
        "model": model,
        "questions": copy.deepcopy(Q.ROLE_QUESTIONS),
    }


def select_lines(posting: Posting, *, log=print) -> list:
    """(section, text) pairs sent in request L: required lines, then responsibility lines, each capped and in
    document order. Preferred lines are never sent."""
    jd = posting.description_text or ""
    required = judge2.section_lines(jd, "required")
    duties = judge2.section_lines(jd, "responsibility")
    dropped_req = max(0, len(required) - Q.MAX_REQUIRED_LINES)
    dropped_resp = max(0, len(duties) - Q.MAX_RESPONSIBILITY_LINES)
    if dropped_req or dropped_resp:
        log(f"jev: {posting.posting_id} lines capped: dropped {dropped_req} required, "
            f"{dropped_resp} responsibility")
    return ([("required", t) for t in required[:Q.MAX_REQUIRED_LINES]]
            + [("responsibility", t) for t in duties[:Q.MAX_RESPONSIBILITY_LINES]])


def _lines_body(chunk: list, facts: list, model: str) -> dict:
    fact_ids = [f.id for f in facts]
    questions = {}
    for i, (section, _text) in enumerate(chunk):
        kid, vid, eid = Q.line_question_ids(i)
        questions[kid] = Q.kind_question(i)
        questions[vid] = Q.verdict_question(i, section)
        questions[eid] = Q.evidence_question(i, fact_ids)
    return {
        "state": {
            "reading_rules": list(Q.READING_RULES),
            "facts": [{"id": f.id, "heading": f.heading, "text": f.text} for f in facts],
            "lines": [{"id": f"L{i:02d}", "section": s, "text": t} for i, (s, t) in enumerate(chunk)],
        },
        "model": model,
        "questions": questions,
    }


def estimate_tokens(body: dict) -> int:
    """A size estimate only; the response's usage.input_tokens is the real count."""
    return math.ceil(len(json.dumps(body)) / Q.CHARS_PER_TOKEN)


def plan_tokens(body: dict) -> int:
    """The spend-planning estimate (dry-run total, daily cap check): calibrated on real usage, see
    PLAN_CHARS_PER_TOKEN. Chunking still uses estimate_tokens."""
    return math.ceil(len(json.dumps(body)) / Q.PLAN_CHARS_PER_TOKEN)


def build_lines_requests(posting: Posting, facts: list, model: str, *, log=print) -> list:
    """Request(s) L. Line ids and question ids use each line's index within its own request. Lines are packed
    greedily into chunks under MAX_REQUEST_TOKENS_EST; every chunk carries the full facts and reading rules.
    No lines -> [] (no request). Raises ValueError if one line alone cannot fit."""
    lines = select_lines(posting, log=log)
    chunks, cur = [], []
    for item in lines:
        trial = cur + [item]
        if cur and estimate_tokens(_lines_body(trial, facts, model)) > Q.MAX_REQUEST_TOKENS_EST:
            chunks.append(cur)
            cur = [item]
        else:
            cur = trial
    if cur:
        chunks.append(cur)
    bodies = [_lines_body(c, facts, model) for c in chunks]
    for body in bodies:
        if estimate_tokens(body) > Q.MAX_REQUEST_TOKENS_EST:
            raise ValueError(f"jev: a lines request for {posting.posting_id} is over "
                             f"{Q.MAX_REQUEST_TOKENS_EST} estimated tokens even with one line")
    return bodies


def chunks_of(lines_requests: list) -> list:
    """The (section, text) chunks behind a list of lines requests, in the order `parse_lines_response` wants."""
    return [[(l["section"], l["text"]) for l in body["state"]["lines"]] for body in lines_requests]


# ---------------------------------------------------------------- 3. response parsers
def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _answer(resp: dict, qid: str, qtype: str) -> dict:
    answers = resp.get("answers") if isinstance(resp, dict) else None
    if not isinstance(answers, dict):
        raise JevResponseError("response has no 'answers' object")
    ans = answers.get(qid)
    if not isinstance(ans, dict) or ans.get("type") != qtype:
        raise JevResponseError(f"answer {qid!r} is missing or not a {qtype}")
    return ans


def _choice(resp: dict, qid: str, options) -> dict:
    """A validated Choice answer: {choice, confidence, probabilities}; the choice must be one of `options`."""
    ans = _answer(resp, qid, "choice")
    choice, conf, probs = ans.get("choice"), ans.get("confidence"), ans.get("probabilities")
    if choice not in options:
        raise JevResponseError(f"answer {qid!r} chose unknown option {choice!r}")
    if not _is_num(conf) or not isinstance(probs, dict) or not all(_is_num(p) for p in probs.values()):
        raise JevResponseError(f"answer {qid!r} has a malformed confidence or probabilities")
    return {"choice": choice, "confidence": float(conf), "probabilities": dict(probs)}


def _noul(resp: dict, qid: str) -> float:
    ans = _answer(resp, qid, "noul")
    if not _is_num(ans.get("noul")):
        raise JevResponseError(f"answer {qid!r} has no numeric noul")
    return float(ans["noul"])


def _grade_for(score: float) -> str:
    """Nearest level (ties round up), clamped to the GRADE_LEVELS range."""
    level = int(math.floor(score + 0.5))
    return Q.GRADE_LEVELS[min(max(level, 0), len(Q.GRADE_LEVELS) - 1)]


def _lens(resp: dict, qid: str) -> dict:
    ans = _answer(resp, qid, "score")
    score, conf, probs = ans.get("score"), ans.get("confidence"), ans.get("probabilities")
    if not _is_num(score) or not _is_num(conf) or not isinstance(probs, dict):
        raise JevResponseError(f"answer {qid!r} has a malformed score, confidence or probabilities")
    named = {}
    for key, p in probs.items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            raise JevResponseError(f"answer {qid!r} has a non-numeric level key {key!r}") from None
        if not 0 <= level < len(Q.GRADE_LEVELS) or not _is_num(p):
            raise JevResponseError(f"answer {qid!r} has an out-of-range level {key!r}")
        named[Q.GRADE_LEVELS[level]] = float(p)
    return {"score": float(score), "grade": _grade_for(float(score)), "conf": float(conf), "probs": named}


def parse_role_response(resp: dict) -> dict:
    """The ReviewRecord lens_* fields, `gates` (every non-lens role question) and `injection_p`."""
    out = {}
    for name in LENS_NAMES:
        lens = _lens(resp, f"lens_{name}")
        for key, value in lens.items():
            out[f"lens_{name}_{key}"] = value
    gates = {}
    for qid, question in Q.ROLE_QUESTIONS.items():
        if qid in Q.LENS_QUESTIONS:
            continue
        if question["type"] == "noul":
            gates[qid] = _noul(resp, qid)
        elif question["type"] == "choice":
            gates[qid] = _choice(resp, qid, question["criteria"])
        else:
            raise JevResponseError(f"role question {qid!r} has unsupported type {question['type']!r}")
    out["gates"] = gates
    out["injection_p"] = gates.get("canary_ai_directed")
    return out


def parse_lines_response(resps: list, chunks: list, facts: list) -> list:
    """One LineRecord per line, `line_no` in global order across chunks. Guards, mirroring judge2 §30.1:
    `adjacent` on a clearance/licence line becomes `unmet`; otherwise `met`/`adjacent` with evidence "none"
    becomes `unclear` (evidence_downgraded). Evidence text is resolved from the chosen fact id in code."""
    if len(resps) != len(chunks):
        raise JevResponseError(f"{len(resps)} lines response(s) for {len(chunks)} request(s)")
    by_id = {f.id: f for f in facts}
    evidence_options = set(by_id) | {"none"}
    records = []
    for resp, chunk in zip(resps, chunks):
        for i, (section, text) in enumerate(chunk):
            kid, vid, eid = Q.line_question_ids(i)
            kind = _choice(resp, kid, Q.KIND_CRITERIA)
            verdict = _choice(resp, vid, Q.VERDICT_CRITERIA)
            evidence = _choice(resp, eid, evidence_options)
            fact_id = None if evidence["choice"] == "none" else evidence["choice"]
            raw = verdict["choice"]
            final, downgraded = raw, False
            if raw == "adjacent" and kind["choice"] in ("clearance", "licence"):
                final = "unmet"
            elif raw in ("met", "adjacent") and fact_id is None:
                final, downgraded = "unclear", True
            records.append(LineRecord(
                line_no=len(records), section=section, line_text=text,
                kind=kind["choice"], kind_conf=kind["confidence"],
                verdict=final, verdict_raw=raw, verdict_probs=verdict["probabilities"],
                verdict_conf=verdict["confidence"],
                evidence_fact_id=fact_id, evidence_p=evidence["probabilities"].get(evidence["choice"]),
                evidence_text=by_id[fact_id].text if fact_id else None,
                evidence_downgraded=downgraded))
    return records


# ---------------------------------------------------------------- 4. client (transport + sleep injected)
class _Meter:
    """Counts HTTP attempts and input tokens across a run, including postings that later fail."""

    def __init__(self):
        self.calls = 0
        self.tokens = 0


def _post(transport, url: str, headers: dict, body: dict):
    """`transport` is a callable(url, headers=, json=) or an httpx.Client-like object with `.post`."""
    if hasattr(transport, "post"):
        return transport.post(url, headers=headers, json=body)
    return transport(url, headers=headers, json=body)


def default_transport():
    """A real `httpx.Client` for production wiring; never built by a test or a dry run."""
    import httpx
    return httpx.Client(timeout=float(Q.REQUEST_TIMEOUT_S))


def _header(resp, name: str) -> Optional[str]:
    headers = getattr(resp, "headers", None) or {}
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return None


def _snippet(resp) -> str:
    text = getattr(resp, "text", None)
    if not isinstance(text, str):
        try:
            text = json.dumps(resp.json())
        except Exception:  # noqa: BLE001 -- a snippet is best effort
            text = ""
    return text[:300]


def _retry_after(resp) -> Optional[float]:
    raw = _header(resp, "retry-after")
    try:
        seconds = float(raw) if raw is not None else None
    except ValueError:
        return None   # an HTTP-date retry-after is ignored; the plain backoff applies
    if seconds is None or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_S)


def _tokens_used(data: dict, body: dict) -> int:
    usage = data.get("usage") if isinstance(data, dict) else None
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    return int(tokens) if _is_num(tokens) else plan_tokens(body)


def call_with_retry(transport, sleep_fn, url: str, key: str, body: dict, *, log=print,
                    meter: Optional[_Meter] = None) -> dict:
    """POSTs `body` and returns the response JSON. Retries RETRY_STATUSES, any 5xx and transport exceptions
    with BACKOFF_SECONDS, sleeping a numeric `retry-after` (capped at 60 s) instead when one is sent. Any other
    non-2xx raises JevAPIError at once. Never logs the key."""
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    attempts = 1 + len(Q.BACKOFF_SECONDS)
    last = JevAPIError(None, None, "no attempt made")
    wait = 0.0
    for attempt in range(attempts):
        if attempt:
            sleep_fn(wait)
        if meter is not None:
            meter.calls += 1
        backoff = Q.BACKOFF_SECONDS[attempt] if attempt < len(Q.BACKOFF_SECONDS) else 0
        try:
            resp = _post(transport, url, headers, body)
        except Exception as exc:  # noqa: BLE001 -- any transport failure is retried
            last = JevAPIError(None, None, f"{type(exc).__name__}: {exc}"[:300])
            log(f"jev: request failed ({type(exc).__name__}); attempt {attempt + 1} of {attempts}")
            wait = backoff
            continue
        status = getattr(resp, "status_code", 200)
        if 200 <= status < 300:
            data = resp.json() if hasattr(resp, "json") else resp
            if meter is not None:
                meter.tokens += _tokens_used(data, body)
            return data
        request_id = _header(resp, REQUEST_ID_HEADER)
        last = JevAPIError(status, request_id, _snippet(resp))
        if status not in Q.RETRY_STATUSES and status < 500:
            raise last
        log(f"jev: HTTP {status} (request {request_id}); attempt {attempt + 1} of {attempts}")
        retry_after = _retry_after(resp)
        wait = retry_after if retry_after is not None else backoff   # retry-after replaces the backoff
    raise last


# ---------------------------------------------------------------- 5. review + run + population
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def review_posting(posting: Posting, facts: list, *, transport, sleep_fn, endpoint: str, key: str, pv: str,
                   run_tag: Optional[str] = None, log=print, meter: Optional[_Meter] = None) -> ReviewRecord:
    """Sends the role request and the lines request(s) for one posting and returns its ReviewRecord, with
    required fit derived in code by judge2. Raises JevAPIError / JevResponseError on failure."""
    model = model_for(endpoint)
    url = Q.ENDPOINTS[endpoint]
    role_body = build_role_request(posting, model)
    lines_bodies = build_lines_requests(posting, facts, model, log=log)

    role_resp = call_with_retry(transport, sleep_fn, url, key, role_body, log=log, meter=meter)
    role = parse_role_response(role_resp)
    model_answered = role_resp.get("model")
    if not isinstance(model_answered, str) or not model_answered:
        raise JevResponseError("role response has no 'model' field")
    lines_resps = [call_with_retry(transport, sleep_fn, url, key, b, log=log, meter=meter) for b in lines_bodies]
    lines = parse_lines_response(lines_resps, chunks_of(lines_bodies), facts)

    responses = [role_resp] + lines_resps
    input_tokens = sum(_tokens_used(r, b) for r, b in zip(responses, [role_body] + lines_bodies))
    drift = endpoint == "typesafe" and any(r.get("model") != Q.PINNED_MODEL for r in responses)
    fit, why = judge2.derive_required_fit(lines, title=posting.title or "")
    lfit, _lwhy = judge2.lines_fit(lines, title=posting.title or "")
    sfit, sscore, _counts = judge2.shape_fit(lines)
    return ReviewRecord(
        posting_id=posting.posting_id, description_hash=posting.description_hash, prompt_version=pv,
        run_tag=run_tag, endpoint=endpoint, model_requested=model, model_answered=model_answered,
        version_drift=drift, input_tokens=input_tokens,
        **{k: v for k, v in role.items() if k.startswith("lens_")},
        gates=role["gates"], injection_p=role["injection_p"],
        required_fit=fit, derive_why=why, lines_fit=lfit, shape_fit=sfit, shape_score=sscore,
        raw_response={"role": role_resp, "lines": lines_resps}, reviewed_at=_now_iso(), lines=lines)


class StorePersist:
    """The default persistence: a thin adapter over `backend.ats.store` (imported lazily)."""

    def jev_already_reviewed(self, con, posting_id: str, description_hash: str, prompt_version: str,
                             run_tag: Optional[str]) -> bool:
        from backend.ats import store
        return store.jev_already_reviewed(con, posting_id, description_hash, prompt_version, run_tag)

    def insert_jev_review(self, con, record: ReviewRecord) -> None:
        from backend.ats import store
        store.insert_jev_review(con, record)

    def jev_tokens_since(self, con, since_iso: str) -> int:
        from backend.ats import store
        return store.jev_tokens_since(con, since_iso)


def _today_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def run(con, rows: list, *, transport=None, sleep_fn=None, live_ok: bool = False, caps: Optional[Caps] = None,
        dry_run: bool = False, show: int = 1, force: bool = False, run_tag: Optional[str] = None,
        facts: Optional[list] = None, endpoint: Optional[str] = None, persist=None, log=print) -> RunSummary:
    """Reviews `rows` (Posting objects). `dry_run` prints the first `show` full request bodies and never calls
    the transport. A live run refuses unless `live_ok` is True and the endpoint's key is set. Cached reviews
    (same hash, prompt_version and run_tag) are skipped unless `force`. Stops cleanly at either cap; a
    posting's error is logged and counted, never fatal. `run_tag` gives a repeat run its own storage key
    (`<prompt_version>:<run_tag>`), as in judge2."""
    summary = RunSummary(considered=len(rows), dry_run=dry_run)
    endpoint = endpoint or endpoint_from_env()
    model = model_for(endpoint)
    facts = facts if facts is not None else load_facts()
    base_pv = prompt_version(facts, endpoint)
    pv = f"{base_pv}:{run_tag}" if run_tag else base_pv
    persist = persist or StorePersist()

    key = ""
    if not dry_run:
        key = api_key(endpoint)
        if not live_ok or not key:
            why = "live_ok is not set" if not live_ok else f"{Q.KEY_ENV[endpoint]} is not set"
            log(f"jev.run refused: {why}. Run --dry-run first, review the payload, then pass the approval.")
            return summary

    todo = [p for p in rows if force
            or not persist.jev_already_reviewed(con, p.posting_id, p.description_hash, pv, run_tag)]
    summary.skipped_cached = len(rows) - len(todo)

    if dry_run:
        est = 0
        for i, p in enumerate(todo):
            role_body = build_role_request(p, model)
            try:
                lines_bodies = build_lines_requests(p, facts, model, log=log)
            except ValueError as exc:
                summary.errors += 1
                log(f"jev: {p.posting_id} could not be built ({exc})")
                continue
            est += sum(plan_tokens(b) for b in [role_body] + lines_bodies)
            if i < show:
                log(f"--- jev dry-run role request: {p.posting_id} ({p.title!r} @ {p.employer!r}) ---")
                log(json.dumps(role_body, indent=2, ensure_ascii=False))
                for j, body in enumerate(lines_bodies):
                    log(f"--- jev dry-run lines request {j + 1}/{len(lines_bodies)}: {p.posting_id} ---")
                    log(json.dumps(body, indent=2, ensure_ascii=False))
        log(f"jev dry-run: endpoint={endpoint} model={model} prompt_version={pv} to_send={len(todo)} "
            f"skipped_cached={summary.skipped_cached} estimated_input_tokens={est} "
            f"(~${est * Q.PRICE_PER_M_INPUT / 1e6:.2f})")
        cap = (caps or caps_from_env()).daily_token_cap
        used = persist.jev_tokens_since(con, _today_utc_iso()) if con is not None else 0
        log(f"jev dry-run: used today (UTC) {used} + this run {est} = {used + est} of daily_token_cap={cap} -> "
            + ("fits" if used + est <= cap else f"WOULD STOP at the cap; raise {Q.DAILY_CAP_ENV} or wait for 00:00 UTC"))
        return summary

    caps = caps or caps_from_env()
    transport = transport if transport is not None else default_transport()
    if sleep_fn is None:
        import time
        sleep_fn = time.sleep
    meter = _Meter()
    tokens_before = persist.jev_tokens_since(con, _today_utc_iso())

    for n, p in enumerate(todo, start=1):
        try:
            planned = [build_role_request(p, model)] + build_lines_requests(p, facts, model, log=lambda *a: None)
        except Exception as exc:  # noqa: BLE001 -- one bad posting never stops the run
            summary.errors += 1
            log(f"jev: {p.posting_id} could not be built ({type(exc).__name__}: {exc})")
            continue
        if meter.calls + len(planned) > caps.max_calls_per_run:
            summary.stopped_by_cap = "max_calls_per_run"
            log(f"jev: stopping, {meter.calls} calls made and {len(planned)} more would pass "
                f"max_calls_per_run={caps.max_calls_per_run}")
            break
        used = tokens_before + meter.tokens
        if used + sum(plan_tokens(b) for b in planned) > caps.daily_token_cap:
            summary.stopped_by_cap = "daily_token_cap"
            log(f"jev: stopping, {used} tokens used today; the next posting would pass "
                f"daily_token_cap={caps.daily_token_cap}")
            break
        try:
            record = review_posting(p, facts, transport=transport, sleep_fn=sleep_fn, endpoint=endpoint,
                                    key=key, pv=pv, run_tag=run_tag, log=log, meter=meter)
            persist.insert_jev_review(con, record)
        except Exception as exc:  # noqa: BLE001 -- logged and counted, never fatal
            summary.errors += 1
            log(f"jev: {p.posting_id} failed ({type(exc).__name__}: {exc})")
            continue
        summary.reviewed += 1
        if record.version_drift:
            summary.drift += 1
            log(f"jev: {p.posting_id} VERSION DRIFT: answered by {record.model_answered}")
        if record.injection_p is not None and record.injection_p >= Q.CANARY_FLAG_AT:
            summary.canary_flags += 1
            log(f"jev: {p.posting_id} CANARY flagged (injection_p={record.injection_p:.2f})")
        log(f"jev.run: {n}/{len(todo)} {p.posting_id} required_fit={record.required_fit} "
            f"tokens={record.input_tokens}")

    summary.calls = meter.calls
    summary.input_tokens = meter.tokens
    log(f"jev.run: {summary}")
    return summary


def _postings(result_rows) -> list:
    return [Posting(posting_id=pid, description_hash=dh or "", title=title or "", employer=employer or "",
                    description_text=text or "")
            for pid, dh, title, employer, text, *_rest in result_rows]


GATED_SQL = """
    SELECT f.posting_id, p.description_hash, p.title, p.employer, p.description_text
    FROM vw_lens_fit f
    JOIN postings p USING (posting_id)
    WHERE p.status = 'active' AND f.verdict != 'reject' AND NOT f.decided
      AND p.description_text IS NOT NULL
      AND greatest(coalesce(f.fit_process, 0), coalesce(f.fit_technical, 0), coalesce(f.fit_ai, 0)) >= ?
    ORDER BY f.rank_score DESC, f.final_score DESC, f.first_seen_at DESC
"""


def eval_set_postings(con) -> list:
    """Every blind human-graded Required row (judge2.EVAL_SET_SQL)."""
    return _postings(con.execute(judge2.EVAL_SET_SQL).fetchall())


def gated_postings(con, lens_min: float = Q.GATE_LENS_MIN, limit: Optional[int] = None) -> list:
    """Active, unrejected, undecided postings whose latest screen has any lens model fit >= `lens_min`, in
    report rank order (the same exclusions as judge2.POPULATION_SQL)."""
    sql = GATED_SQL + (" LIMIT ?" if limit is not None else "")
    params = [lens_min] + ([int(limit)] if limit is not None else [])
    return _postings(con.execute(sql, params).fetchall())
