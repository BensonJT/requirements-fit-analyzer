"""The frozen interface contract for the Jev typed-decision tier (docs/JEV_PLAN.md §3, §5).

Every Jev module builds against these types. `jev.py` produces them, `store.py` persists them
(`jev_reviews` / `jev_lines`, one column per field below unless noted), and `jev_eval.py` reads
them back. Do not rename or retype a field without updating all three and the plan.

Dataclasses only, no I/O. `LineRecord` exposes `line` and `evidence` properties so a list of
them can be passed straight to `judge2.derive_required_fit` / `lines_fit` / `shape_fit`, which
read those names through `judge2._get` (getattr on objects).
"""
from dataclasses import dataclass, field
from typing import Optional

# --- the posting a request is built from (a row of judge2.EVAL_SET_SQL / the gated population)
@dataclass
class Posting:
    posting_id: str
    description_hash: str
    title: str
    employer: str
    description_text: str


@dataclass
class Fact:
    """One candidate fact parsed deterministically from the fact sheet (judge2_background.local.md):
    one bullet or paragraph line, with the heading it sits under. `id` is "f01", "f02", ... in
    document order, so it is stable for an unchanged sheet."""
    id: str
    heading: str
    text: str


@dataclass
class Caps:
    """Spend guards. `jev.run` stops (never errors) when either is reached and says which in
    `RunSummary.stopped_by_cap`. Daily tokens are counted from stored `input_tokens` of rows
    reviewed since 00:00 UTC today, plus this run's own calls."""
    max_calls_per_run: int
    daily_token_cap: int


@dataclass
class LineRecord:
    """One JD line rated by the lines request. Stored in `jev_lines`, keyed by the parent review's
    (posting_id, description_hash, prompt_version) plus `line_no`."""
    line_no: int
    section: str                      # judge2.LINE_SECTION_VALUES: required | preferred | responsibility
    line_text: str
    kind: str                         # judge2.LINE_KIND_VALUES
    kind_conf: float
    verdict: str                      # judge2.LINE_VERDICT_VALUES, AFTER the evidence guard below
    verdict_raw: str                  # Jev's own top choice, before the guard
    verdict_probs: dict               # {"met": p, "adjacent": p, "unmet": p, "unclear": p}
    verdict_conf: float
    evidence_fact_id: Optional[str]   # a Fact.id, or None when Jev chose "none"
    evidence_p: Optional[float]       # probability of the chosen evidence option
    evidence_text: Optional[str]      # the chosen Fact.text (resolved in code, never generated)
    evidence_downgraded: bool         # True when verdict_raw was met/adjacent but evidence was "none"

    # judge2's derivation reads these names; `years` is always None (Jev does no arithmetic).
    @property
    def line(self) -> str:
        return self.line_text

    @property
    def evidence(self) -> Optional[str]:
        return self.evidence_text

    @property
    def years(self) -> None:
        return None


@dataclass
class ReviewRecord:
    """One posting's Jev review: the role request plus the lines request. Stored in `jev_reviews`,
    PK (posting_id, description_hash, prompt_version). `lines` goes to `jev_lines`; `gates` and
    the `*_probs` dicts are stored as JSON; `raw_response` holds both raw responses as JSON
    ({"role": {...}, "lines": [{...}, ...]}) and never leaves the gitignored DB."""
    posting_id: str
    description_hash: str
    prompt_version: str
    run_tag: Optional[str]            # None for the canonical run; "r2", "r3", ... for repeat runs
    endpoint: str                     # "typesafe" | "vercel"
    model_requested: str
    model_answered: str               # the `model` field of the response (the version that answered)
    version_drift: bool               # model_answered != the pinned id (typesafe endpoint only)
    input_tokens: int                 # sum over every request for this posting
    # role request: three lens Scores (Score levels in jev_questions.GRADE_LEVELS order)
    lens_process_score: Optional[float]      # Jev's probability-weighted level, 0..3
    lens_process_grade: Optional[str]        # rubric.GRADES value of the nearest level
    lens_process_conf: Optional[float]
    lens_process_probs: Optional[dict]       # {"wrong": p, "stretch": p, "adjacent": p, "bullseye": p}
    lens_technical_score: Optional[float]
    lens_technical_grade: Optional[str]
    lens_technical_conf: Optional[float]
    lens_technical_probs: Optional[dict]
    lens_ai_score: Optional[float]
    lens_ai_grade: Optional[str]
    lens_ai_conf: Optional[float]
    lens_ai_probs: Optional[dict]
    gates: dict                              # {question_id: noul or {"choice", "confidence", "probabilities"}}
    injection_p: Optional[float]             # the canary Noul; >= jev_questions.CANARY_FLAG_AT flags the posting
    # lines request, derived in code by judge2.derive_required_fit (never by Jev)
    required_fit: Optional[str]              # judge2.REQUIRED_FIT_VALUES or None ("no call")
    derive_why: Optional[str]
    lines_fit: Optional[str]
    shape_fit: Optional[str]
    shape_score: Optional[float]
    raw_response: dict
    reviewed_at: str                         # UTC ISO timestamp
    lines: list = field(default_factory=list)   # list[LineRecord]


@dataclass
class RunSummary:
    considered: int = 0          # postings in the population
    skipped_cached: int = 0      # already reviewed under this prompt_version (and run_tag)
    reviewed: int = 0            # postings with a stored ReviewRecord from this run
    calls: int = 0               # HTTP requests sent (role + lines, including splits)
    input_tokens: int = 0
    errors: int = 0              # postings that failed after retries (logged, never fatal)
    drift: int = 0               # reviews marked version_drift
    canary_flags: int = 0        # reviews with injection_p >= CANARY_FLAG_AT
    stopped_by_cap: Optional[str] = None   # "max_calls_per_run" | "daily_token_cap" | None
    dry_run: bool = False
