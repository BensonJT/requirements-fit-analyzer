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

from ..ats import store
from . import report
from . import jev_questions as Q
from .jev_types import Posting, RunSummary

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
    check: bool = False                        # 2026-10-20: `fails` rests only on grey lines -> "Check the
                                               # Required block", not demoted (jev_questions.GREY_AT)


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


# ---- The Jev review set (2026-10-25, user's go): every eligible posting, not only the Top Jobs cut ----
# Jev used to review just the rows write_top_jobs shows (first 100 Apply / 40 Review); 148 of 303 lens-strong
# untapped postings never reached it for that reason alone. It now reviews every active, screened, non-rejected,
# US, undecided fit posting at a shown level that is strong on at least one lens: a judge grade of bullseye or
# adjacent, or (ungraded) the lens model at or above JEV_LENS_BAR. Technical carries 0.80, the bar model_rows
# uses, because it is the weakest lens model. One review grades all three lenses and is cached by JD hash and
# prompt_version, so a posting strong on several lenses costs one review, and later nights pay only for new or
# changed JDs. Demotion still applies only to the Top Jobs rows (top_subset_ids); for the rest Jev's call is a
# second opinion shown in the focus report (jev_status.py).
JEV_LENS_BAR = {"process": 0.70, "technical": 0.80, "ai": 0.70}
# New (uncached) reviews per run: a safety limit so one huge new board cannot spend the day's budget in one go.
# Cached rows are never counted. Rows over the limit stay queued for the next run.
JEV_MAX_NEW_PER_RUN = 300

_ELIGIBLE_LENS_SQL = " OR ".join(
    f"f.grade_{lens} IN ('bullseye', 'adjacent') OR (f.grade_{lens} IS NULL AND coalesce(f.fit_{lens}, 0) >= {bar})"
    for lens, bar in JEV_LENS_BAR.items())


def eligible_ids(con, *, include_decided: bool = False, levels=report.TOP_LEVELS) -> list:
    """Posting ids strong on at least one lens and past the list's own gates, strongest rank first."""
    from .judge import non_us_primary
    where = ["f.verdict != 'reject'", "f.level_fit IN (SELECT unnest(?::VARCHAR[]))", "p.track = 'fit'",
             f"({_ELIGIBLE_LENS_SQL})"]
    if not include_decided:
        where += ["NOT coalesce(f.decided, FALSE)", "NOT coalesce(f.in_tracker, FALSE)"]
    rows = con.execute(f"""
        SELECT f.posting_id, f.location_primary FROM vw_lens_fit f JOIN postings p USING (posting_id)
        WHERE {' AND '.join(where)}
        ORDER BY f.rank_score DESC, f.final_score DESC, f.first_seen_at DESC""", [list(levels)]).fetchall()
    return [pid for pid, loc in rows if not non_us_primary(loc)]


def review_subset_ids(con, *, include_decided: bool = False, levels=report.TOP_LEVELS, **top_kw) -> dict:
    """{posting_id: 'apply' | 'review' | 'eligible'}: the Top Jobs rows first (in their order, so the rows Jev
    can demote are reviewed before a cap stops the run), then every other eligible posting by rank."""
    out = dict(top_subset_ids(con, include_decided=include_decided, levels=levels, **top_kw))
    for pid in eligible_ids(con, include_decided=include_decided, levels=levels):
        out.setdefault(pid, "eligible")
    return out


def limit_new(con, rows: list, *, facts=None, limit: int = JEV_MAX_NEW_PER_RUN, run_tag=None) -> tuple:
    """(rows to send, ids held back): every cached row, plus the first `limit` uncached rows in order."""
    from . import jev
    facts = facts if facts is not None else jev.load_facts()
    pv = jev.prompt_version(facts, jev.endpoint_from_env())
    pv = f"{pv}:{run_tag}" if run_tag else pv
    persist, keep, held, new = jev.StorePersist(), [], [], 0
    for p in rows:
        if persist.jev_already_reviewed(con, p.posting_id, p.description_hash, pv, run_tag):
            keep.append(p)
        elif new < limit:
            keep.append(p)
            new += 1
        else:
            held.append(p.posting_id)
    return keep, held


def reusable_ids(con, wanted: dict, pv: str) -> set:
    """Rows in `wanted` ({pid: tier}) whose stored review is kept instead of re-sent under `pv`.
    2026-10-28 (user's call): a fact-sheet edit re-keys prompt_version, which made every stored review look stale
    (527 re-reviews queued, ~$0.95). A review of the same JD under an OLDER prompt_version with a passed required
    bar stays in force -- except the rows it demotes or sends to check: a fact change can only rescue those, so
    they are reviewed again, and only once `pv` has passed its own bar (until then a new review could not
    demote, and would silently undo a demotion that still stands)."""
    if not wanted:
        return set()
    older = {r[0] for r in con.execute("""
        SELECT j.posting_id FROM vw_jev_latest j JOIN vw_jev_bar b ON b.prompt_version = j.prompt_version
        WHERE b.required_passed AND j.prompt_version <> ? AND j.posting_id IN (SELECT unnest(?::VARCHAR[]))""",
        [pv, list(wanted)]).fetchall()}
    current = con.execute("SELECT required_passed FROM vw_jev_bar WHERE prompt_version = ?", [pv]).fetchone()
    if older and current and current[0]:
        older -= set(demotions(con, {pid: wanted[pid] for pid in older}))
    return older


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
    """Jev reviews the review set: the Top Jobs Apply / Review rows, then every other eligible posting
    (review_subset_ids; adjudicated rows skipped; at most JEV_MAX_NEW_PER_RUN uncached reviews). Cached
    reviews are skipped by `jev.run` itself; a cap stop or an error is logged and the caller still writes the
    report. Returns the RunSummary, or None when nothing was sent."""
    from . import jev
    facts = facts if facts is not None else jev.load_facts()
    ensure_required_bar(con, facts=facts, live_ok=live_ok, transport=transport, sleep_fn=sleep_fn, log=log)
    wanted = review_subset_ids(con, **top_kw)
    exempt = adjudicated_ids(con, list(wanted))
    reused = reusable_ids(con, {pid: t for pid, t in wanted.items() if pid not in exempt},
                          jev.prompt_version(facts, jev.endpoint_from_env()))
    rows = subset_postings(con, [pid for pid in wanted if pid not in exempt and pid not in reused])
    rows, held = limit_new(con, rows, facts=facts)
    log(f"Jev gate: {len(wanted)} row(s) in the review set ({sum(1 for t in wanted.values() if t == 'apply')} "
        f"apply / {sum(1 for t in wanted.values() if t == 'review')} review / "
        f"{sum(1 for t in wanted.values() if t == 'eligible')} other eligible), {len(exempt)} adjudicated exempt, "
        f"{len(reused)} kept from an earlier passed prompt_version, "
        f"{len(rows)} with a JD to send or skip as cached, {len(held)} held for the next run "
        f"(over {JEV_MAX_NEW_PER_RUN} new reviews)")
    if not rows:
        return None
    t = time.monotonic()
    try:
        summary = jev.run(con, rows, live_ok=live_ok, facts=facts, transport=transport, sleep_fn=sleep_fn, log=log)
    except Exception as exc:  # logged, never fatal: the report is written from what is stored
        log(f"Jev gate: run failed ({type(exc).__name__}: {exc}); the report uses the stored reviews")
        return RunSummary(considered=len(rows), stopped_by_error=f"run failed ({type(exc).__name__})")
    log(f"Jev gate: {summary.reviewed} reviewed, {summary.skipped_cached} cached, {summary.errors} error(s), "
        f"{summary.input_tokens} tokens ({time.monotonic() - t:.1f}s)")
    return summary


def run_warning(summary) -> Optional[str]:
    """One line naming this run's Jev problem, or None. 2026-10-28: Jev's errors were logged and never fatal,
    so a night with 300 failed calls (no credits) ended "status 0" and Top_Jobs looked normal. cmd_top prints
    this with a `!!! JEV` prefix (launch.sh repeats those lines in its final summary) and the report header
    carries it."""
    if summary is None:
        return None
    if summary.stopped_by_error:
        return (f"stopped at the first account error ({summary.stopped_by_error}); {summary.reviewed} reviewed, "
                f"{summary.errors} failed -- the page uses the stored reviews")
    if summary.errors:
        return f"{summary.errors} of {summary.considered - summary.skipped_cached} review(s) failed"
    if summary.stopped_by_cap:
        return f"stopped at {summary.stopped_by_cap} after {summary.reviewed} review(s); the rest wait for the next run"
    return None


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
           (SELECT count(*) FILTER (WHERE l.grey_p IS NULL OR l.grey_p < ?)
              FROM jev_lines l
             WHERE l.posting_id = j.posting_id AND l.description_hash = j.description_hash
               AND l.prompt_version = j.prompt_version AND l.run_tag = '' AND l.section = 'required'
               AND l.verdict = 'unmet') AS clear_unmet,
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


def grey_only(required_fit: Optional[str], unmet: list, clear_unmet: int) -> bool:
    """True when a `fails` rests only on grey lines: there are unmet required lines and none of them is
    clear-cut. A review stored before the grey question existed has grey_p NULL, which counts as clear-cut, so
    old reviews demote exactly as before. Pure, so the rule is testable without a database."""
    return required_fit in DEMOTE_REQUIRED and bool(unmet) and clear_unmet == 0


def demotions(con, shown: dict) -> dict:
    """{posting_id: Demotion} for the rows in `shown` ({pid: 'apply'|'review'}) Jev demotes, plus the rows it
    sends to "Check the Required block" (`check=True`: a `fails` that rests only on grey lines and nothing else
    demotes them). Rows with no canonical review, adjudicated rows and reviews under a prompt_version without a
    passed required bar are never demoted or checked."""
    if not shown:
        return {}
    exempt = adjudicated_ids(con, list(shown))
    out = {}
    for (pid, req, gp, gt, ga, why, pv, passed, unmet, clear_unmet, g_req, g_why, g_unmet) in con.execute(
            _DEMOTION_SQL, [UNMET_LINE_CHARS, Q.GREY_AT, list(shown)]).fetchall():
        grades = (gp, gt, ga)
        grey = grey_only(req, unmet or [], clear_unmet or 0)
        reasons = decide(None if grey else req, grades if all(grades) else (), required_passed=bool(passed),
                         adjudicated=pid in exempt)
        check = grey and not reasons and bool(passed) and pid not in exempt
        if reasons or check:
            out[pid] = Demotion(posting_id=pid, was=shown[pid], required_fit=req, grades=grades, derive_why=why,
                                unmet=list(unmet or [])[:UNMET_LINES_SHOWN], prompt_version=pv,
                                gemma_required=g_req, gemma_why=g_why, gemma_unmet=g_unmet,
                                reasons=reasons or ["required fails on judgment-call lines only"], check=check)
    return out


def gate_status(con, shown_ids=None) -> str:
    """One line for the report header: which prompt_version(s) may demote right now, and -- 2026-10-15 -- how
    many shown rows Jev reviewed under a prompt_version WITHOUT a passed required bar (those are never demoted,
    so a fact-sheet edit that re-keys the prompt_version must not look like 'active, 0 demoted')."""
    rows = con.execute("SELECT prompt_version FROM vw_jev_bar WHERE required_passed ORDER BY 1").fetchall()
    head = ("INACTIVE (no Jev prompt_version has a passed required bar; run the Jev gold score preset)" if not rows
            else "active under prompt_version " + ", ".join(r[0] for r in rows))
    if shown_ids:
        unmeasured = con.execute("""
            SELECT j.prompt_version, count(*) FROM vw_jev_latest j
            LEFT JOIN vw_jev_bar b ON b.prompt_version = j.prompt_version
            WHERE j.posting_id IN (SELECT unnest(?::VARCHAR[])) AND NOT coalesce(b.required_passed, FALSE)
            GROUP BY 1 ORDER BY 2 DESC""", [list(shown_ids)]).fetchall()
        if unmeasured:
            head += (" — **WARNING: " + ", ".join(f"{n} shown row(s) reviewed under {pv}" for pv, n in unmeasured)
                     + ", whose required bar has not passed, are NOT demoted** (a fact-sheet edit re-keys the "
                     "prompt_version; `top --jev` re-measures it automatically)")
    return head


def ensure_required_bar(con, *, facts=None, live_ok: bool = False, transport=None, sleep_fn=None, log=print):
    """2026-10-15: a fact-sheet edit re-keys Jev's prompt_version, and a review under a prompt_version with no
    passed REQUIRED bar never demotes -- so the gate silently stopped demoting on 10/5 (two sheet edits, no bar
    re-run). Before reviewing Top Jobs, if the CURRENT prompt_version has no stored required eval, Jev reviews
    the blind gold set (~5M tokens, ~$0.21) and `evaluate_required` scores it. Returns the
    required result dict, or None (already measured, not live, or failed -- logged, never raised).
    2026-10-30 (user's call): when only the fact sheet changed (same jev.prompt_base as a measured version), the
    bar is carried over from that version instead (inherit_required_bar) and the gold set is not re-run; the
    re-review of earlier demotions (reusable_ids) is the only spend. A change to the questions, model or caps
    still re-measures."""
    from . import jev, jev_eval
    from datetime import datetime, timezone
    endpoint = jev.endpoint_from_env()
    pv = jev.prompt_version(facts if facts is not None else jev.load_facts(), endpoint)
    store.record_jev_prompt_base(con, pv, jev.prompt_base(endpoint))
    done = con.execute("SELECT passed, reason FROM vw_jev_eval_latest WHERE prompt_version = ? "
                       "AND family = 'required'", [pv]).fetchone()
    # 2026-10-28: an "insufficient data" result means the gold set was not reviewed (10/27: the daily token cap
    # stopped it at 0 of 121), not that Jev failed the bar -- so it counts as unmeasured and is measured again.
    # Before this, that one row blocked every later measurement and the gate never demoted under this version.
    retry = done is not None and not done[0] and str(done[1] or "").startswith("insufficient data")
    if done is not None and not retry:
        if not done[0]:
            log(f"Jev gate: required bar at {pv} was measured and NOT passed -- its reviews will not demote")
        return None
    inherited = inherit_required_bar(con, pv, log=log)
    if inherited is not None:
        return inherited
    if not live_ok:
        log(f"Jev gate: prompt_version {pv} has no required bar yet; not live, so it was not measured")
        return None
    log(f"Jev gate: prompt_version {pv} "
        + ("had too little data last time" if retry else "has no required bar yet")
        + " -- reviewing the gold set and scoring it")
    try:
        gold = jev.run(con, jev.eval_set_postings(con), live_ok=True, facts=facts, transport=transport,
                       sleep_fn=sleep_fn, log=log)
        stopped = gold.stopped_by_error or gold.stopped_by_cap
        if stopped:   # a part-reviewed gold set is not scored; the next run measures it again
            log(f"Jev gate: gold set stopped early ({stopped}); required bar at {pv} not measured this run")
            return None
        run_id = datetime.now(timezone.utc).strftime("jev-gate-%Y%m%dT%H%M%SZ")
        res = jev_eval.evaluate_required(con, pv, run_id=run_id, write=True)
    except Exception as exc:
        log(f"Jev gate: required-bar measurement failed ({type(exc).__name__}: {exc}); reviews under {pv} won't demote")
        return None
    log(f"Jev gate: required bar at {pv}: {'PASSED' if res.get('passed') else 'NOT PASSED'} "
        f"({res.get('reason', '')})")
    return res


def inherit_required_bar(con, pv: str, *, log=print) -> Optional[dict]:
    """Carries a PASSED required bar over to `pv` from the newest-measured prompt_version with the same base
    (store.jev_prompt_bases), i.e. one that differs from `pv` only in the fact sheet. Writes a `required`
    jev_evals row (metrics.inherited_from / measured_at name the source) and returns its result dict; None when
    `pv` has no recorded base, no sibling was measured, or the newest sibling's bar did not pass (the gold set
    then decides). An "insufficient data" row is not a measurement and is never inherited."""
    import json
    from datetime import datetime, timezone
    src = con.execute("""
        SELECT e.prompt_version, e.passed, e.n, e.metrics
        FROM vw_jev_eval_latest e
        JOIN jev_prompt_bases s ON s.prompt_version = e.prompt_version
        JOIN jev_prompt_bases c ON c.base_version = s.base_version AND c.prompt_version = ?
        WHERE e.family = 'required' AND e.prompt_version <> ? AND NOT starts_with(coalesce(e.reason, ''), 'insufficient data')
        ORDER BY e.evaluated_at DESC LIMIT 1""", [pv, pv]).fetchone()
    if src is None or not src[1]:
        return None
    src_pv, _, n, metrics = src
    metrics = json.loads(metrics) if isinstance(metrics, str) else dict(metrics or {})
    measured_at = metrics.get("measured_at", src_pv)
    metrics.update(inherited_from=src_pv, measured_at=measured_at)
    reason = f"inherited from {src_pv} (only the fact sheet changed; gold set measured at {measured_at})"
    run_id = datetime.now(timezone.utc).strftime("jev-inherit-%Y%m%dT%H%M%SZ")
    store.insert_jev_eval(con, run_id=run_id, prompt_version=pv, family="required", n=n, metrics=metrics,
                          passed=True, reason=reason)
    log(f"Jev gate: required bar at {pv}: PASSED, {reason} -- no gold run; earlier demotions are re-reviewed")
    return {"passed": True, "reason": reason, "inherited_from": src_pv}
