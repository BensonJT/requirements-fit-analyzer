"""Per-posting Jev status for the Postgres snapshot (2026-10-25, user request).

Two values per posting, so the focus report can show and filter them:
  jev_result  what Jev did with the row:
                Demoted         a Top Jobs row Jev took out of Apply / Review (required fails, or wrong on
                                every lens)
                Check Required  a Top Jobs row whose `fails` rests only on judgment-call lines; not demoted
                Would demote    an eligible row NOT on Top Jobs that Jev would demote if it were (a second
                                opinion only; 2026-10-25, Jev reviews every eligible row)
                Would check     the same for a judgment-call-only `fails`
                Kept            reviewed, nothing demotes it (Jev agrees the row belongs)
                Not acted on    reviewed under a prompt_version whose required bar has not passed
                No review yet   in the review set, no stored review: queued (new-review limit or token cap,
                                the next run continues) or no JD
                Not judged      outside the review set
  jev_note    Jev's reason; for an eligible row off Top Jobs, also why it is off (the list's gate); for a
              Not judged row, the gate that kept it from Jev. A strong posting the pipeline filtered out can
              be traced to the filter, and a bad filter found.

Jev never promotes: its pass only removes rows. A row the user thinks is a fit after Jev demoted it is the
disagreement to record (`finder.py mark <id> build`, a gold ruling even when `seen`).

Reads stored data only (the same functions write_top_jobs and jev_gate use), never calls a model.
"""
from typing import Optional

from . import jev_gate, report
from .judge import non_us_primary

_INITIAL = {"bullseye": "B", "adjacent": "A", "stretch": "S", "wrong": "W"}


def _lenses(p, t, a) -> str:
    return "·".join(_INITIAL.get(g or "", "-") for g in (p, t, a))


def exclusion_note(*, track: Optional[str], verdict: Optional[str], level: Optional[str], decided: bool,
                   in_tracker: bool, listed: Optional[str], judge_tier: Optional[str], n_lenses_good: Optional[int],
                   judge_required: Optional[str], unclear: bool, band: Optional[str], fit_required: Optional[float],
                   levels=report.TOP_LEVELS) -> str:
    """Why a posting never reached Jev's subset, first gate first (the order write_top_jobs applies them).
    Pure, so the wording is testable without a database. `listed` is the uncapped list the row sits in
    ('judge apply', 'model review', ... with ' (non-US)' when the location gate dropped it)."""
    if track not in (None, "fit"):
        return f"{track} track: never on the fit list"
    if decided:
        return "already decided (build, pass or hold): decided rows leave the list"
    if in_tracker:
        return "already in the tracker"
    if verdict is None:
        return "never screened (no screen row)"
    if verdict == "reject":
        return "screen rejected (see screen_reasons)"
    if level not in levels:
        return f"level gate: {level or 'unknown'} (list shows {', '.join(levels)})"
    if listed and listed.endswith("(non-US)"):
        return "non-US primary location: left out before Jev"
    if listed:
        cap = report.TOP_APPLY_CAP if listed.endswith("apply") else report.TOP_REVIEW_CAP
        return f"{listed}, but below the Top_Jobs cap: Jev reviews only the first {cap}"
    if judge_tier == "hidden":
        return (f"judge tier hidden: {n_lenses_good or 0} lens(es) graded bullseye/adjacent, "
                f"Required {judge_required or 'not judged'} (apply needs a good lens and Required meets)")
    if unclear:
        fr = f"{fit_required:.2f}" if fit_required is not None else "none"
        return (f"model: Requirement unclear (required-fit {fr}; Review needs {report.MODEL_REQ_REVIEW:.2f} "
                f"and a lens at its bar)")
    return f"score band {band or 'none'}: below the strong / very_strong shortlist bar"


HARD_GATES = ("bridge track", "already decided", "already in the tracker", "never screened", "screen rejected",
              "level gate", "non-US primary")
NO_LENS_NOTE = ("no lens at Jev's bar: needs a judge grade of bullseye or adjacent, or the lens model at process "
                f"{jev_gate.JEV_LENS_BAR['process']:.2f} / technical {jev_gate.JEV_LENS_BAR['technical']:.2f} / "
                f"AI {jev_gate.JEV_LENS_BAR['ai']:.2f}")


def statuses(con, ids=None, *, levels=report.TOP_LEVELS) -> dict:
    """{posting_id: (jev_result, jev_note)} for `ids` (default: every active fit posting)."""
    shown = jev_gate.top_subset_ids(con, levels=levels)
    subset = jev_gate.review_subset_ids(con, levels=levels)
    flagged = jev_gate.demotions(con, subset)
    exempt = jev_gate.adjudicated_ids(con, list(subset))
    reviews = {r[0]: r[1:] for r in con.execute("""
        SELECT j.posting_id, j.prompt_version, j.required_fit, j.lens_process_grade, j.lens_technical_grade,
               j.lens_ai_grade, coalesce(b.required_passed, FALSE), CAST(j.reviewed_at AS DATE)
        FROM vw_jev_latest j LEFT JOIN vw_jev_bar b USING (prompt_version)""").fetchall()}
    listed = {}
    for tier in ("apply", "review"):
        for r in report.top_rows(con, tier, levels=levels):
            listed.setdefault(r[0], f"judge {tier}")
        for r in report.model_rows(con, tier, levels=levels):
            listed.setdefault(r[0], f"model {tier}" + (" (non-US)" if non_us_primary(r[12]) else ""))
    unclear = {r[0] for r in report.unclear_rows(con, levels=levels)}
    selection = {r[0]: r[1:] for r in con.execute(
        "SELECT posting_id, tier, n_lenses_good, required_fit FROM vw_selection").fetchall()}
    where, params = "p.status = 'active'", []
    if ids is not None:
        where, params = "p.posting_id IN (SELECT unnest(?::VARCHAR[]))", [list(ids)]
    rows = con.execute(f"""
        SELECT p.posting_id, p.track, f.verdict, f.level_fit, coalesce(f.decided, FALSE),
               coalesce(f.in_tracker, FALSE), f.band, f.fit_required
        FROM postings p LEFT JOIN vw_lens_fit f USING (posting_id) WHERE {where}""", params).fetchall()
    out = {}
    for pid, track, verdict, level, decided, in_tracker, band, fit_required in rows:
        rev = reviews.get(pid)
        sel = selection.get(pid, (None, None, None))
        gate = exclusion_note(track=track, verdict=verdict, level=level, decided=decided, in_tracker=in_tracker,
                              listed=listed.get(pid), judge_tier=sel[0], n_lenses_good=sel[1],
                              judge_required=sel[2], unclear=pid in unclear, band=band, fit_required=fit_required,
                              levels=levels)
        off_list = "" if pid in shown else f" [not on Top Jobs: {gate}]"
        if pid in subset:
            d = flagged.get(pid)
            if d and not d.check:
                out[pid] = ("Demoted" if pid in shown else "Would demote",
                            "; ".join(d.reasons) + (f": {d.unmet[0]}" if d.unmet else "") + off_list)
            elif d:
                out[pid] = ("Check Required" if pid in shown else "Would check",
                            "fails only on judgment-call lines" + (f": {d.unmet[0]}" if d.unmet else "") + off_list)
            elif rev is None:
                out[pid] = ("No review yet", f"queued for Jev ({subset[pid]}): over this run's new-review limit or "
                                             "the token cap (the next run continues), or no JD" + off_list)
            elif pid in exempt:
                out[pid] = ("Kept", f"your own grade is on record (adjudicated); Jev: Required {rev[1]}, "
                                    f"lenses {_lenses(*rev[2:5])}" + off_list)
            elif not rev[5]:
                out[pid] = ("Not acted on", f"reviewed {rev[6]} under prompt_version {rev[0]}, whose required bar "
                                            f"has not passed; Jev: Required {rev[1]}, lenses {_lenses(*rev[2:5])}"
                            + off_list)
            else:
                out[pid] = ("Kept", f"Jev: Required {rev[1]}, lenses {_lenses(*rev[2:5])}" + off_list)
            continue
        # Outside the review set: a hard gate (decided, rejected, level, ...) or no lens at Jev's bar.
        note = gate if gate.startswith(HARD_GATES) else NO_LENS_NOTE
        if rev is not None:
            note += f"; older Jev review {rev[6]}: Required {rev[1]}, lenses {_lenses(*rev[2:5])}"
        out[pid] = ("Not judged", note)
    return out
