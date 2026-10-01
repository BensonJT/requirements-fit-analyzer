"""Stage 2 of the Jev tier (docs/JEV_PLAN.md §4; user's go 2026-09-30): Jev as the END-of-pipeline demotion
pass over the Top Jobs Apply / Review subset.

What it may do, and only this:
  - REVIEW the rows `report.write_top_jobs` is about to show in Apply and Review (judge-graded and
    model-graded alike), minus the user's own adjudicated rows -- `run_over_top` sends them to Jev, cached
    reviews cost nothing, and a failure or a cap stop leaves the report to be written from what is stored.
  - DEMOTE a shown row out of Apply / Review into a visible "Jev demoted" section when Jev's canonical review
    (vw_jev_latest: current text, pinned model, no run tag) says `required_fit = fails`, or grades every lens
    `wrong` -- and only under a prompt_version whose stored REQUIRED bar passed (vw_jev_bar.required_passed).
    Never a promotion, never a probability, never a change to `vw_lens_fit.rank_score` (tests/test_jev_store.py
    still asserts the rank is byte-identical with and without Jev rows).
  - EXEMPT every user-adjudicated row (vw_selection.adjudicated): human > Jev. A `finder.py mark ... build` on a
    demoted row is therefore the overrule -- it becomes a human label, the row is exempt from then on, and the
    label is a gold ruling the next `jev eval` scores Jev against.
  - REPORT Gemma (judge2) beside each demotion as evidence, never as a tiebreaker: on the 9/30 Apply set Gemma
    never failed a row Jev passed and would have overruled 12 of 18 real demotions (docs/STATUS.md 9/30).
"""
import time
from dataclasses import dataclass, field
from typing import Optional

from . import report
from .jev_types import Posting

DEMOTE_REQUIRED = ("fails",)
LENS_WRONG = "wrong"
UNMET_LINES_SHOWN = 2
UNMET_LINE_CHARS = 110


@dataclass
class Demotion:
    posting_id: str
    was: str                                   # apply | review
    required_fit: Optional[str]
    grades: tuple                              # (process, technical, ai) Jev grades
    derive_why: Optional[str]
    unmet: list = field(default_factory=list)  # Jev's unmet required lines, in document order
    prompt_version: Optional[str] = None
    gemma_required: Optional[str] = None
    gemma_why: Optional[str] = None
    gemma_unmet: Optional[str] = None
    reasons: list = field(default_factory=list)


# ---------------------------------------------------------------- the subset
def adjudicated_ids(con, ids: list) -> set:
    if not ids:
        return set()
    return {r[0] for r in con.execute(
        "SELECT posting_id FROM vw_selection WHERE adjudicated AND posting_id IN (SELECT unnest(?::VARCHAR[]))",
        [list(ids)]).fetchall()}


def top_subset_ids(con, *, include_decided: bool = False, levels=report.TOP_LEVELS, apply_cap: int = report.TOP_APPLY_CAP,
                   review_cap: int = report.TOP_REVIEW_CAP) -> dict:
    """{posting_id: 'apply' | 'review'} for the rows write_top_jobs would show, in its order and under its caps,
    judge-graded and model-graded alike (model rows minus non-US, as write_top_jobs drops them)."""
    from .judge import non_us_primary
    out = {}
    for tier, cap in (("apply", apply_cap), ("review", review_cap)):
        judged = report.top_rows(con, tier, include_decided=include_decided, levels=levels)
        model = [r for r in report.model_rows(con, tier, include_decided=include_decided, levels=levels)
                 if not non_us_primary(r[12])]
        for r in report._merge_by_rank(judged, model)[:cap]:
            out.setdefault(r[0], tier)
    return out


def subset_postings(con, ids: list) -> list:
    """Posting objects for `ids` (order kept) that have a JD; adjudicated rows are left out by the caller."""
    if not ids:
        return []
    rows = con.execute(
        "SELECT posting_id, description_hash, title, employer, description_text FROM postings "
        "WHERE posting_id IN (SELECT unnest(?::VARCHAR[])) AND description_text IS NOT NULL", [list(ids)]).fetchall()
    by_id = {r[0]: r for r in rows}
    return [Posting(posting_id=pid, description_hash=by_id[pid][1] or "", title=by_id[pid][2] or "",
                    employer=by_id[pid][3] or "", description_text=by_id[pid][4] or "")
            for pid in ids if pid in by_id]


def run_over_top(con, *, facts=None, live_ok: bool = False, transport=None, sleep_fn=None, log=print, **top_kw):
    """Jev reviews the Top Jobs Apply / Review subset (adjudicated rows skipped). Cached reviews are skipped by
    `jev.run` itself; a cap stop or an error is logged and the caller still writes the report. Returns the
    RunSummary, or None when nothing was sent."""
    from . import jev
    wanted = top_subset_ids(con, **top_kw)
    exempt = adjudicated_ids(con, list(wanted))
    rows = subset_postings(con, [pid for pid in wanted if pid not in exempt])
    log(f"Jev gate: {len(wanted)} Top Jobs row(s) ({sum(1 for t in wanted.values() if t == 'apply')} apply / "
        f"{sum(1 for t in wanted.values() if t == 'review')} review), {len(exempt)} adjudicated exempt, "
        f"{len(rows)} with a JD to review")
    if not rows:
        return None
    t = time.monotonic()
    try:
        summary = jev.run(con, rows, live_ok=live_ok, facts=facts, transport=transport, sleep_fn=sleep_fn, log=log)
    except Exception as exc:  # logged, never fatal: the report is written from what is stored
        log(f"Jev gate: run failed ({type(exc).__name__}: {exc}); the report uses the stored reviews")
        return None
    log(f"Jev gate: {summary.reviewed} reviewed, {summary.skipped_cached} cached, {summary.errors} error(s), "
        f"{summary.input_tokens} tokens ({time.monotonic() - t:.1f}s)")
    return summary


def run_gemma_on_demoted(con, *, background_path=None, live_ok: bool = False, log=print, **top_kw):
    """Gemma (judge2) as the second opinion on Jev's demotions ONLY (user's call 2026-09-30: Gemma leaves the
    nightly top-100 run; it is reported beside each demotion, never a veto). Cached verdicts cost nothing; a
    failure is one log line. Returns judge2.run's dict, or None when nothing was sent."""
    from . import judge2
    shown = top_subset_ids(con, **top_kw)
    ids = sorted(demotions(con, shown))
    if not ids:
        log("Gemma beside Jev: no demotions to second-read")
        return None
    if not live_ok:
        log(f"Gemma beside Jev: {len(ids)} demotion(s), skipped (JUDGE2_LIVE_OK not set)")
        return None
    t = time.monotonic()
    try:
        result = judge2.run(con, top_n=len(ids), posting_ids=ids, i_have_approval=True, background="file",
                            background_path=background_path, log=log)
    except Exception as exc:
        log(f"Gemma beside Jev: failed ({type(exc).__name__}: {exc}); the page shows what is stored")
        return None
    log(f"Gemma beside Jev: {len(ids)} demotion(s), {result.get('reviewed', 0)} reviewed fresh "
        f"({time.monotonic() - t:.1f}s)")
    return result


# ---------------------------------------------------------------- the demotion rule
_DEMOTION_SQL = """
    SELECT j.posting_id, j.required_fit, j.lens_process_grade, j.lens_technical_grade, j.lens_ai_grade,
           j.derive_why, j.prompt_version, coalesce(b.required_passed, FALSE) AS required_passed,
           (SELECT list(left(l.line_text, ?) ORDER BY l.line_no)
              FROM jev_lines l
             WHERE l.posting_id = j.posting_id AND l.description_hash = j.description_hash
               AND l.prompt_version = j.prompt_version AND l.run_tag = '' AND l.section = 'required'
               AND l.verdict = 'unmet') AS unmet,
           f.judge2_required, f.judge2_derive_why, f.judge2_unmet_first
    FROM vw_jev_latest j
    LEFT JOIN vw_jev_bar b ON b.prompt_version = j.prompt_version
    LEFT JOIN vw_lens_fit f ON f.posting_id = j.posting_id
    WHERE j.posting_id IN (SELECT unnest(?::VARCHAR[]))
"""


def decide(required_fit: Optional[str], grades: tuple, *, required_passed: bool, adjudicated: bool) -> list:
    """The reasons a row is demoted ([] = keep). Pure, so the rule is testable without a database."""
    if adjudicated or not required_passed:
        return []
    reasons = []
    if required_fit in DEMOTE_REQUIRED:
        reasons.append("required fails")
    if grades and all(g == LENS_WRONG for g in grades):
        reasons.append("wrong on every lens")
    return reasons


def demotions(con, shown: dict) -> dict:
    """{posting_id: Demotion} for the rows in `shown` ({pid: 'apply'|'review'}) Jev demotes. Rows with no
    canonical review, adjudicated rows and reviews under a prompt_version without a passed required bar are
    never demoted."""
    if not shown:
        return {}
    exempt = adjudicated_ids(con, list(shown))
    out = {}
    for (pid, req, gp, gt, ga, why, pv, passed, unmet, g_req, g_why, g_unmet) in con.execute(
            _DEMOTION_SQL, [UNMET_LINE_CHARS, list(shown)]).fetchall():
        grades = (gp, gt, ga)
        reasons = decide(req, grades if all(grades) else (), required_passed=bool(passed), adjudicated=pid in exempt)
        if reasons:
            out[pid] = Demotion(posting_id=pid, was=shown[pid], required_fit=req, grades=grades, derive_why=why,
                                unmet=list(unmet or [])[:UNMET_LINES_SHOWN], prompt_version=pv,
                                gemma_required=g_req, gemma_why=g_why, gemma_unmet=g_unmet, reasons=reasons)
    return out


def gate_status(con) -> str:
    """One line for the report header: which prompt_version(s) may demote right now."""
    rows = con.execute("SELECT prompt_version FROM vw_jev_bar WHERE required_passed ORDER BY 1").fetchall()
    if not rows:
        return "INACTIVE (no Jev prompt_version has a passed required bar; run the Jev gold score preset)"
    return "active under prompt_version " + ", ".join(r[0] for r in rows)
