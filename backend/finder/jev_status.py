"""Per-posting Jev status for the Postgres snapshot (2026-10-25, user request).

Two values per posting, so the focus report can show and filter them:
  jev_result  what Jev did with the row on the last Top_Jobs pass:
                Demoted         left Apply / Review (required fails, or wrong on every lens)
                Check Required  `fails` that rests only on judgment-call lines; not demoted
                Kept            reviewed, nothing demotes it (Jev agrees it belongs on the list)
                Not acted on    reviewed under a prompt_version whose required bar has not passed
                No review yet   in Jev's subset but no stored review (no JD, or the token cap ended the run)
                Not judged      never in Jev's subset
  jev_note    Jev's reason, or for a Not judged row the gate that kept it out of the subset, so a strong
              posting the pipeline filtered out can be traced to the filter (and a bad filter found).

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


def statuses(con, ids=None, *, levels=report.TOP_LEVELS) -> dict:
    """{posting_id: (jev_result, jev_note)} for `ids` (default: every active fit posting)."""
    subset = jev_gate.top_subset_ids(con, levels=levels)
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
        if pid in subset:
            d = flagged.get(pid)
            if d and not d.check:
                out[pid] = ("Demoted", "; ".join(d.reasons) + (f": {d.unmet[0]}" if d.unmet else ""))
            elif d:
                out[pid] = ("Check Required", "fails only on judgment-call lines" + (f": {d.unmet[0]}" if d.unmet else ""))
            elif rev is None:
                out[pid] = ("No review yet", f"in Jev's subset ({subset[pid]}) with no stored review: no JD, or the "
                                             "token cap ended the run")
            elif pid in exempt:
                out[pid] = ("Kept", f"your own grade is on record (adjudicated); Jev: Required {rev[1]}, "
                                    f"lenses {_lenses(*rev[2:5])}")
            elif not rev[5]:
                out[pid] = ("Not acted on", f"reviewed {rev[6]} under prompt_version {rev[0]}, whose required bar "
                                            f"has not passed; Jev: Required {rev[1]}, lenses {_lenses(*rev[2:5])}")
            else:
                out[pid] = ("Kept", f"Jev: Required {rev[1]}, lenses {_lenses(*rev[2:5])}")
            continue
        sel = selection.get(pid, (None, None, None))
        note = exclusion_note(track=track, verdict=verdict, level=level, decided=decided, in_tracker=in_tracker,
                              listed=listed.get(pid), judge_tier=sel[0], n_lenses_good=sel[1],
                              judge_required=sel[2], unclear=pid in unclear, band=band, fit_required=fit_required,
                              levels=levels)
        if rev is not None:
            note += f"; older Jev review {rev[6]}: Required {rev[1]}, lenses {_lenses(*rev[2:5])}"
        out[pid] = ("Not judged", note)
    return out
