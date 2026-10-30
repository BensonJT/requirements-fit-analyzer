"""Bucket JD keywords with an LLM: 1 = claimable phrase to embed, 2 = noise, 3 = domain claim the user cannot make.

Moved from resume_ide_v2's jd_bucketize.py on 2026-10-30. Changes from the original:
  * Transport is httpx for both providers (no requests, no groq SDK).
  * Pacing by tokens per minute, not a fixed pause. Every call resends the ~2K-token system prompt, so the old
    60-keyword batches with a 3 s pause put 6 calls (~13-15K tokens) inside a minute on a 300-keyword JD and hit
    AI Studio's ~15K TPM free-tier ceiling; the 20-100 s retry ladder then ran past the caller's timeout. Now
    AI Studio gets one call for a typical JD, and any further call waits until a rolling 60 s window has room.
  * A 429 waits out the full window (60 s) instead of 20 s.
  * The career summary no longer carries purged figures ($2.8B) or the informal ministry name.

GEMINI_API_KEY is the same key judge2 uses (repo .env), so a sweep's judge pass and this tool share one quota.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque

import httpx

AISTUDIO_DEFAULT_MODEL = "gemma-4-31b-it"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-20b"
_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Per provider: keywords per call, token budget per rolling minute, max output tokens. Groq counts max_tokens
# against its 8K TPM, so it keeps small batches; AI Studio counts what is sent, so one call fits a typical JD.
PROVIDER_DEFAULTS = {
    "aistudio": {"batch": 400, "tpm": 12000, "max_tokens": 8192},
    "groq": {"batch": 120, "tpm": 7000, "max_tokens": 3000},
}

CAREER_SUMMARY = """
Jeff Benson — Senior Manager / Principal Consultant. Use this profile to judge
whether each keyword is something Jeff can authentically claim (Bucket 1) or
requires industry-specific experience he does not have (Bucket 3).

TECHNICAL SKILLS (can claim all of these):
- Programming: Python (pandas, NumPy, scikit-learn, Jupyter, SpaCy, FastAPI), SQL
  (CTEs, window functions, ETL/ELT pipelines), PostgreSQL, relational databases,
  serverless/NoSQL databases, Git, Bash/shell scripting
- Data & Analytics: statistical modeling, probabilistic forecasting (PERT distributions),
  predictive analytics, regression analysis, data validation, data governance,
  data integrity, large data sets, TF-IDF, NLP, semantic analysis, embedding models
  (sentence-transformers), machine learning (scikit-learn), operations research
- AI & GenAI: Claude (Anthropic), Gemini (Google), NotebookLM, prompt engineering,
  human-in-the-loop AI workflow design, RAG architectures, LLM orchestration,
  AI-enabled automation, GenAI tool deployment
- Visualization & Reporting: Grafana, executive dashboards, KPI scorecards,
  validation reports, written communications, executive presentations
- Tools: Minitab, Microsoft Office Suite, Smartsheet, Jira, SharePoint, Visio

METHODOLOGIES & FRAMEWORKS (can claim):
- Lean Six Sigma Black Belt (DMAIC, PERT, process capability analysis)
- PMO design and implementation (Master's Certificate in Project Management, PMBOK)
- Agile / Scrum (Professional Scrum Master PSM I)
- ITIL v3 Service Management
- ISO 9000 quality systems
- Workforce and capacity planning models (builds the models; not a workforce planner)
- Geospatial data governance
- Change management
- Organizational design

DOMAIN EXPERTISE (can claim — 31 years at Verizon Fortune 20 + Cornerstone):
- Enterprise IT operations and modernization (30,000+ server migrations, data centers)
- Network operations workforce analytics (field technician capacity planning)
- Business transformation and process reengineering
- PMO leadership and portfolio governance (roughly 50 initiatives, $455M in benefits)
- Capital and expense budget governance, forecasting and variance analysis (partnered with FP&A)
- Workforce planning, capacity modeling, demand forecasting
- Vendor consumption analysis and financial validation
- Data analytics platform design and architecture
- Analytical tool validation (methodology assessment, risk evaluation)
- Cross-functional leadership (technology, operations, finance, HR stakeholders)
- Coaching, mentoring, and team development
- Stakeholder management and executive communication
- Strategic planning, roadmap development, OKR/KPI frameworks
- Full-stack web development (React, TypeScript, FastAPI — Benson Academy / Abide project)

CERTIFICATIONS:
- Lean Six Sigma Black Belt
- Master's Certificate in Project Management (PMBOK)
- Professional Scrum Master (PSM I)
- ITIL v3 Service Management

EDUCATION:
- Master of Arts in Organizational Management (University of Phoenix)
- Bachelor of Arts in Computer Systems & Psychology (Grove City College)

INDUSTRIES WORKED IN:
- Telecommunications (Verizon, 31 years, Fortune 20)
- Enterprise IT / technology operations
- Nonprofit / ministry technology (Benson Academy, unpaid)
- Independent consulting (Cornerstone Solutions, Dec 2025–present)

INDUSTRIES NOT WORKED IN — flag keywords from these as Bucket 3:
- Banking, fintech, financial services (no: ACH, ATM, deposits, wires, debit,
  credit cards operations, branch operations, core banking, payment processing,
  AML, KYC, Basel, CECL, DFAST, loan origination, underwriting)
- Healthcare (no: HEDIS, prior authorization, payer/provider, EHR, HIPAA clinical,
  ICD codes, clinical trials, pharmacy operations, care management)
- Government / defense (no: ITAR, FISMA, FedRAMP, TS/SCI clearance, FAR/DFARS,
  government contracting, DoD programs)
- Retail / consumer goods (no: merchandising, planogram, store operations,
  point-of-sale, inventory replenishment, consumer packaged goods)
- Manufacturing / supply chain (no: MRP, ERP production modules, shop floor,
  Bill of Materials, lean manufacturing in factory settings)
- Investment management / capital markets (no: portfolio management, trading,
  securities, derivatives, asset management, hedge funds)

CLASSIFICATION GUIDANCE:
- A keyword is Bucket 1 if Jeff's background includes genuine hands-on experience
  with it, even if the exact term wasn't always used in his titles.
- A keyword is Bucket 3 only if it requires deep, role-specific experience in an
  excluded industry or credential Jeff does not hold.
- When in doubt between Bucket 1 and Bucket 3, prefer Bucket 1 — false negatives
  (missing a claimable phrase) hurt more than false positives.
- Single common words (leadership, analysis, communication, results) should almost
  always be Bucket 1 unless they are clearly domain-specific jargon.
"""

SYSTEM_PROMPT = (
    "You are a keyword classifier for a job application tool. "
    "A candidate needs to know which keywords from a job description to embed in their resume.\n\n"
    f"Candidate background:\n{CAREER_SUMMARY}\n\n"
    "Classify each keyword into exactly one bucket:\n"
    "  Bucket 1: Meaningful multi-word phrases or domain-agnostic skills the candidate "
    "CAN authentically claim. These should appear verbatim (or near-verbatim) in the resume.\n"
    "  Bucket 2: Noise — single trivial words, boilerplate, stopwords, or filler terms "
    "that add no real signal (e.g., 'making', 'hours', 'full-time', 'those processes'). "
    "ALSO Bucket 2: single common adjectives and generic nouns that could appear on any "
    "resume (e.g., 'effective', 'detailed', 'strategic', 'professional', 'technical', "
    "'comprehensive', 'solutions', 'structure', 'technology', 'objectives').\n"
    "  Bucket 3: Domain-specific claims the candidate CANNOT honestly make based on their "
    "background (e.g., 'ATM', 'deposits', 'ACH', 'prior auth', 'HEDIS', 'defense clearance'). "
    "ALSO Bucket 3: federal security clearance status terms and adjudication language "
    "(e.g., 'public trust', 'active public trust', 'clearance required', 'approved adjudication', "
    "'suitability determination', 'national security organizations', 'federal financial policy').\n\n"
    "CALIBRATION: Assign approximately 30-35% of the keywords you receive to Bucket 1 "
    "(roughly 30-50 terms per 100-150 keywords). When uncertain, prefer Bucket 2. "
    "Strong Bucket 1 candidates are specific multi-word skill/requirement phrases. "
    "Single common words default to Bucket 2 unless they are a named certification, tool, "
    "or methodology (Agile, Scrum, SharePoint, ITIL, Visio, PMP, SAFe) or a standalone "
    "skill noun that ATS systems explicitly scan for (governance, analytics, automation, "
    "documentation, leadership, onboarding, management, communication, performance).\n\n"
    "Return ONLY a JSON object with keys 'bucket1', 'bucket2', 'bucket3', each containing "
    "a list of keyword strings exactly as provided. No commentary, no markdown, just JSON."
)


def estimate_tokens(text: str) -> int:
    """~4 characters per token, the usual English rule of thumb (no tokenizer dependency)."""
    return len(text) // 4 + 1


class TokenPacer:
    """Rolling-window token budget: wait() blocks until `tokens` fits in the last `window` seconds."""

    def __init__(self, tpm: int, window: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        self.tpm, self.window, self.clock, self.sleep = tpm, window, clock, sleep
        self.events: deque[tuple[float, int]] = deque()

    def wait(self, tokens: int) -> float:
        waited = 0.0
        while True:
            now = self.clock()
            while self.events and now - self.events[0][0] >= self.window:
                self.events.popleft()
            used = sum(t for _, t in self.events)
            if not self.events or used + tokens <= self.tpm:
                break
            pause = self.window - (now - self.events[0][0]) + 0.5
            self.sleep(pause)
            waited += pause
        self.events.append((self.clock(), tokens))
        return waited

    def refund(self) -> None:
        """Drop the last reservation: a call the server rejected without processing (5xx, timeout) used none."""
        if self.events:
            self.events.pop()


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _aistudio_call(user_prompt: str, model: str, max_tokens: int, timeout: float) -> str:
    """Gemma 4 rejects the older thinkingBudget; thinkingConfig.thinkingLevel is the knob."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GEMINI_API_KEY is not set (expected in the repo .env).")
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": max_tokens,
            "thinkingConfig": {"thinkingLevel": os.environ.get("JD_BUCKETIZE_REASONING", "minimal")},
        },
    }
    try:
        resp = httpx.post(f"{_GEMINI_BASE_URL}/models/{model}:generateContent", params={"key": api_key},
                          json=payload, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError("aistudio: timed out", status=408) from None
    except httpx.HTTPError as exc:
        raise ProviderError(f"aistudio: request failed ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise ProviderError(f"aistudio: HTTP {resp.status_code}: {resp.text[:300]}".replace(api_key, "***"),
                            status=resp.status_code)
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, ValueError):
        raise ProviderError(f"aistudio: unexpected response shape: {resp.text[:300]}") from None
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def _groq_call(user_prompt: str, model: str, max_tokens: int, timeout: float) -> str:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise EnvironmentError("GROQ_API_KEY is not set (expected in the repo .env).")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    # gpt-oss reasoning tokens come out of max_tokens (and Groq's TPM); classification needs little of it
    body["reasoning_effort"] = os.environ.get("JD_BUCKETIZE_REASONING", "low")
    try:
        resp = httpx.post(_GROQ_URL, headers={"Authorization": f"Bearer {api_key}"}, json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise ProviderError("groq: timed out", status=408) from None
    except httpx.HTTPError as exc:
        raise ProviderError(f"groq: request failed ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise ProviderError(f"groq: HTTP {resp.status_code}: {resp.text[:300]}", status=resp.status_code)
    return resp.json()["choices"][0]["message"]["content"].strip()


_RETRYABLE = (408, 413, 429, 500, 502, 503, 504)
# Gemma's free endpoint answers 500/503 in under a second when it is overloaded (2026-10-30: one tiny request in
# three, and a two-word reply took 20-25 s), so server errors retry fast and a 429 waits out the window.
_SERVER_RETRY_PAUSES = (5, 10, 20, 40)


def bucketize(keywords: list[dict], provider: str | None = None, log=print) -> dict[str, list[str]]:
    """{'bucket1': [...], 'bucket2': [...], 'bucket3': [...]}. A phrase the model returns in two buckets keeps the
    more conservative one (3 > 2 > 1); a keyword it never returns goes to bucket 2.

    When AI Studio still fails after its retries, the whole job is re-run on Groq (if GROQ_API_KEY is set and
    JD_BUCKETIZE_FALLBACK is not 'none'), so a package build never stalls on the free tier."""
    provider = (provider or os.environ.get("JD_BUCKETIZE_PROVIDER", "aistudio")).strip().lower()
    try:
        return _bucketize(keywords, provider, log)
    except ProviderError as exc:
        fallback = os.environ.get("JD_BUCKETIZE_FALLBACK", "groq").strip().lower()
        if provider != "aistudio" or fallback != "groq" or not os.environ.get("GROQ_API_KEY"):
            raise
        log(f"  aistudio gave up ({exc}); falling back to groq")
        return _bucketize(keywords, "groq", log)


def _bucketize(keywords: list[dict], provider: str, log) -> dict[str, list[str]]:
    if provider not in PROVIDER_DEFAULTS:
        raise EnvironmentError(f"JD_BUCKETIZE_PROVIDER={provider!r}: use 'aistudio' (default) or 'groq'.")
    d = PROVIDER_DEFAULTS[provider]
    batch_size = int(os.environ.get("JD_BUCKETIZE_BATCH_SIZE", d["batch"]))
    max_tokens = int(os.environ.get("JD_BUCKETIZE_MAX_TOKENS", d["max_tokens"]))
    pacer = TokenPacer(int(os.environ.get("JD_BUCKETIZE_TPM", d["tpm"])))
    timeout = float(os.environ.get("JD_BUCKETIZE_TIMEOUT", "180"))
    if provider == "aistudio":
        model = os.environ.get("JD_BUCKETIZE_MODEL") or AISTUDIO_DEFAULT_MODEL
        call = _aistudio_call
    else:
        model = os.environ.get("LLM_GROQ_MODEL") or GROQ_DEFAULT_MODEL
        call = _groq_call

    batches = [keywords[i:i + batch_size] for i in range(0, len(keywords), batch_size)]
    merged: dict[str, list[str]] = {"bucket1": [], "bucket2": [], "bucket3": []}
    for n, batch in enumerate(batches, 1):
        user_prompt = "Keywords to classify:\n" + "\n".join(f"- {kw['keyword']}" for kw in batch)
        # Groq counts max_tokens against the budget; AI Studio counts roughly what comes back (~8 tokens a keyword)
        est = estimate_tokens(SYSTEM_PROMPT + user_prompt) + (max_tokens if provider == "groq" else 8 * len(batch))
        log(f"  {provider} ({model}): batch {n}/{len(batches)}, {len(batch)} keywords, ~{est:,} tokens")
        raw = None
        for attempt in range(5):
            waited = pacer.wait(est)
            if waited:
                log(f"    paced {waited:.0f}s to stay under the per-minute token budget")
            try:
                raw = call(user_prompt, model, max_tokens, timeout)
                break
            except ProviderError as exc:
                if exc.status != 429:
                    pacer.refund()
                if exc.status not in _RETRYABLE or attempt == 4:
                    raise
                pause = 60 if exc.status == 429 else _SERVER_RETRY_PAUSES[attempt]
                log(f"    {exc.status} from {provider}; retrying in {pause}s")
                time.sleep(pause)
        result = json.loads(raw)
        for key in merged:
            merged[key].extend(result.get(key, []))

    for key in merged:
        seen, deduped = set(), []
        for kw in merged[key]:
            if kw.lower() not in seen:
                deduped.append(kw)
                seen.add(kw.lower())
        merged[key] = deduped
    b3 = {kw.lower() for kw in merged["bucket3"]}
    b2 = {kw.lower() for kw in merged["bucket2"]}
    merged["bucket1"] = [kw for kw in merged["bucket1"] if kw.lower() not in b3 and kw.lower() not in b2]
    merged["bucket2"] = [kw for kw in merged["bucket2"] if kw.lower() not in b3]
    assigned = {kw.lower() for lst in merged.values() for kw in lst}
    merged["bucket2"] += [kw["keyword"] for kw in keywords if kw["keyword"].lower() not in assigned]
    return merged
