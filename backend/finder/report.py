"""The daily Jobs_Found file, lock-free snapshots, and reading decisions back from the file.

Output format: docs/SPRINT_PLAN.md section 7. Batch Mode in the vault keys each application
on its `# Company:` heading, so a JD body must never contain a markdown heading.
"""
import html
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend import profile as P
from backend.ats import store

from .tracker_sync import job_search_dir

SUMMARY_HEADING = "## Summary — decide here"
DECISIONS = ("build", "pass", "hold")
SURFACED_DAYS = 14
SNAPSHOT_DIR = os.path.join(os.path.dirname(store.DEFAULT_DB_PATH), "snapshots")


@dataclass
class Decision:
    posting_id: str
    decision: str
    reason: Optional[str] = None


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cell(value) -> str:
    """Table-safe text: no pipes or newlines."""
    return re.sub(r"\s+", " ", str(value if value is not None else "")).replace("|", "/").strip()


def _link(title: str, url: str) -> str:
    text = _cell(title).replace("[", "(").replace("]", ")")
    return f"[{text}]({(url or '').replace(' ', '%20')})" if url else text


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    return list(value) if isinstance(value, (list, tuple)) else [value]


def jd_body(text: str) -> str:
    """The JD verbatim except: leading '#'s stripped from a line, and a bare '---' / '===' line
    (a fence or setext heading underline) blanked, so no heading appears inside a body."""
    lines = []
    for line in html.unescape(text or "").splitlines():
        if re.match(r"^\s*#", line):
            line = re.sub(r"^(\s*)#+\s*", r"\1", line)
        if re.fullmatch(r"\s*(-{3,}|={3,}|\*{3,}|_{3,})\s*", line):
            line = ""
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while (candidate := path.with_name(f"{path.stem}_{n}{path.suffix}")).exists():
        n += 1
    return candidate


def _allowed_bands(min_band: str) -> list:
    names = [name for _, name in P.SCORE_BANDS]
    return names[: names.index(min_band) + 1] if min_band in names else names[:2]


# Level ordering for the report (sprint plan sec 20.2): in_range first, then stretch_up, then unknown/NULL,
# then out_of_reach / too_low last (those two never take a block or a top-table row -- write_jobs_found
# routes them to the collapsed tail instead; here they simply sort after everything scored).
def _level_order_sql(col: str) -> str:
    return (f"CASE WHEN {col} = 'in_range' THEN 0 WHEN {col} = 'stretch_up' THEN 1 "
            f"WHEN {col} IS NULL OR {col} = 'unknown' THEN 2 ELSE 3 END")


def _coverage_rows(con, meta: dict) -> list:
    rows = []
    for platform, ok, failed in con.execute("""
            SELECT platform, count(*) FILTER (WHERE ok), count(*) FILTER (WHERE NOT ok)
            FROM vw_board_health GROUP BY 1 ORDER BY 1""").fetchall():
        rows.append((f"{platform} boards", "✅" if not failed else "⚠️", f"{ok} ok, {failed} failed (vw_board_health)"))
    s = meta.get("screen")
    if s:
        rows.append(("Screen", "✅", f"{s['screened']} rows, {s['seconds']} s"))
    else:
        rows.append(("Screen", "⏭️", "no screen in this run; rows come from the latest screens"))
    stages = meta.get("stages") or {}
    for key, label in (("model", "Model"), ("embed", "Embed"), ("llm", "LLM")):
        rows.append((label, "✅" if stages.get(key) else "⏭️", "on" if stages.get(key) else "not built / off"))
    return rows


def coverage_parts(c_req=None, c_role=None, n_req=None, n_req_s=None, n_req_p=None, n_role=None, n_role_s=None,
                   n_role_p=None, gaps=None, matches=None) -> list:
    """Fit-stanza coverage figures (§15.4 / §16.3): required (the gated one) and role, with their counts."""
    out = []
    if c_req is not None:
        out.append(f"coverage required {c_req:.0f} ({n_req_s} of {n_req} strong, {n_req_p} partial)")
    if c_role is not None:
        out.append(f"role {c_role:.0f} ({n_role_s} of {n_role} strong, {n_role_p} partial)")
    return out


def coverage_detail(c_req=None, c_role=None, n_req=None, n_req_s=None, n_req_p=None, n_role=None, n_role_s=None,
                    n_role_p=None, gaps=None, matches=None) -> list:
    """Gaps and the best matched requirement for the stanza (each trimmed; pipes and newlines removed)."""
    out = []
    gap_list = [_cell(g)[:140] for g in _as_list(gaps)]
    if gap_list:
        out.append("gaps: " + "; ".join(gap_list))
    match_list = _as_list(matches)
    if match_list:
        m = match_list[0]
        out.append(f"matched: {_cell(m[0])[:100]} ← {_cell(m[1])[:60]} ({m[3]}, {m[2]:.2f})")
    return out


def write_jobs_found(con, vault_dir: Optional[str], run_meta: dict, *, max_blocks: int = 15,
                     block_min_band: str = "strong", table_min: int = 50, table_cap: int = 150,
                     passed_cap: int = 200, out_path=None) -> Path:
    """Writes Jobs_Found_YYYYMMDD_HHMM.md (never overwriting) and records the block rows in `surfaced`."""
    now_local = datetime.now()
    stamp = now_local.strftime("%Y%m%d_%H%M")
    name = f"Jobs_Found_{stamp}.md"
    if out_path:
        out = Path(out_path)
        path = out / name if (out.is_dir() or not out.suffix) else out
    else:
        path = job_search_dir(vault_dir) / "Search_Results" / name
    path = _unique_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    bands = _allowed_bands(block_min_band)
    # out_of_reach / too_low never take a block (sprint plan sec 20.2: "listed in a collapsed tail, never
    # in the top blocks"); the collapsed query below picks them back up.
    blocks = con.execute(f"""
        SELECT v.posting_id, v.employer, v.title, v.url, v.final_score, v.band, v.tier, v.level_fit,
               v.rule_score, v.fit_prob, v.embed_sim, v.top_terms, v.flags, p.description_text,
               c.coverage_required, c.coverage_role, c.n_required, c.n_required_strong, c.n_required_partial,
               c.n_role, c.n_role_strong, c.n_role_partial, c.gaps, c.matches
        FROM vw_shortlist v JOIN postings p USING (posting_id) LEFT JOIN vw_coverage_latest c USING (posting_id)
        WHERE v.band IN (SELECT unnest(?::VARCHAR[]))
          AND (v.level_fit IS NULL OR v.level_fit NOT IN ('out_of_reach', 'too_low'))
          AND v.posting_id NOT IN (SELECT posting_id FROM surfaced
                                   WHERE surfaced_at >= now() - INTERVAL {SURFACED_DAYS} DAY)
        ORDER BY {_level_order_sql('v.level_fit')}, v.final_score DESC, v.first_seen_at DESC
        LIMIT ?""", [bands, max_blocks]).fetchall()
    block_ids = [b[0] for b in blocks]
    table = con.execute(f"""
        SELECT v.posting_id, v.employer, v.title, v.url, v.final_score, v.band, v.tier, v.level_fit,
               (SELECT max(surfaced_at) FROM surfaced s WHERE s.posting_id = v.posting_id) AS shown
        FROM vw_shortlist v
        WHERE (v.final_score >= ? OR v.posting_id IN (SELECT unnest(?::VARCHAR[])))
          AND (v.level_fit IS NULL OR v.level_fit NOT IN ('out_of_reach', 'too_low'))
        ORDER BY {_level_order_sql('v.level_fit')}, v.final_score DESC, v.first_seen_at DESC
        LIMIT ?""", [table_min, block_ids, table_cap]).fetchall()
    # Collapsed tail (20.2): the same candidacy test as the table above (score at/over table_min), but only
    # the two levels the table just excluded. Never promoted to a block regardless of score or band.
    collapsed = con.execute(f"""
        SELECT v.posting_id, v.employer, v.title, v.url, v.final_score, v.band, v.tier, v.level_fit,
               (SELECT max(surfaced_at) FROM surfaced s WHERE s.posting_id = v.posting_id) AS shown
        FROM vw_shortlist v
        WHERE v.level_fit IN ('out_of_reach', 'too_low')
          AND (v.final_score >= ? OR v.posting_id IN (SELECT unnest(?::VARCHAR[])))
        ORDER BY v.final_score DESC, v.first_seen_at DESC LIMIT ?""",
        [table_min, block_ids, table_cap]).fetchall()
    s = run_meta.get("screen")
    passed = []
    if s:
        passed = con.execute("""
            SELECT p.employer, p.title, p.url, v.reasons, v.posting_id
            FROM vw_screen_latest v JOIN postings p USING (posting_id)
            WHERE v.verdict = 'reject' AND v.tier IS NOT NULL AND v.screened_at >= ? AND p.status = 'active'
            ORDER BY v.tier, v.rule_score DESC, p.first_seen_at DESC LIMIT ?""", [s["started"], passed_cap]).fetchall()
    elif run_meta.get("passed_since") is not None:
        passed = con.execute("""
            SELECT p.employer, p.title, p.url, v.reasons, v.posting_id
            FROM vw_screen_latest v JOIN postings p USING (posting_id)
            WHERE v.verdict = 'reject' AND v.tier IS NOT NULL AND p.first_seen_at >= ? AND p.status = 'active'
            ORDER BY v.tier, v.rule_score DESC, p.first_seen_at DESC LIMIT ?""",
                             [run_meta["passed_since"], passed_cap]).fetchall()

    boards = con.execute("SELECT platform, count(*) FROM vw_board_health GROUP BY 1 ORDER BY 2 DESC").fetchall()
    n_boards = sum(n for _, n in boards)
    tracker = run_meta.get("tracker") or {}
    n_tracker = tracker.get("rows", con.execute("SELECT count(*) FROM tracker").fetchone()[0])
    n_matched = tracker.get("exact", 0) + tracker.get("fuzzy", 0) if tracker else \
        con.execute("SELECT count(*) FROM tracker WHERE matched_posting_id IS NOT NULL").fetchone()[0]
    n_decisions = con.execute("SELECT count(*) FROM vw_decisions").fetchone()[0]
    n_surfaced = con.execute(f"SELECT count(DISTINCT posting_id) FROM surfaced "
                             f"WHERE surfaced_at >= now() - INTERVAL {SURFACED_DAYS} DAY").fetchone()[0]
    funnel = s or run_meta.get("funnel") or {}
    since = run_meta.get("since")
    window = f"postings first seen since {since:%Y-%m-%d %H:%M}" if since else "all active postings (no window)"
    verdicts = funnel.get("verdict", {})
    rv = (s or {}).get("rules_version") or run_meta.get("rules_version", "?")
    mv = (s or {}).get("model_version") or run_meta.get("model_version", "none")

    w = []
    w.append(f"---\nnode_id: JOBS:found-{stamp.replace('_', '-')}\nnode_type: search_results\n"
             f"tags: [#job-search #pipeline #ats]\n---\n")
    w.append(f"# Jobs Found — ATS pipeline (`jobsearch/finder.py`), {now_local:%Y-%m-%d %H:%M}\n")
    w.append(f"**Run type:** ATS pipeline screen. **Sources:** {n_boards} boards "
             f"({', '.join(f'{p} {n}' for p, n in boards) or 'none logged'}). **Window:** {window}. "
             f"**The bar:** rule engine + model score; every block is live on the employer's own ATS at run time. "
             f"**Dedup:** tracker {n_tracker} rows ({n_matched} matched), decisions {n_decisions}, "
             f"surfaced-in-last-{SURFACED_DAYS}-days {n_surfaced}. "
             f"**Funnel:** {funnel.get('screened', 0)} screened → {verdicts.get('candidate', 0)} candidate / "
             f"{verdicts.get('review', 0)} review / {verdicts.get('reject', 0)} reject → {len(blocks)} blocks. "
             f"**Versions:** rules {rv} · model {mv}.\n")
    w.append("## Coverage Log\n\n| Source / step | Status | Notes |\n|---|---|---|")
    w.extend(f"| {_cell(a)} | {b} | {_cell(c)} |" for a, b, c in _coverage_rows(con, run_meta))
    w.append("")
    w.append(f"{SUMMARY_HEADING}\n\n| Posting ID | Company | Title | Score | Band | Tier | Level | Decision | "
             "Reason |\n|---|---|---|---|---|---|---|---|---|")
    for pid, employer, title, url, score, band, tier, level_fit, shown in table:
        note = f" (shown {shown:%Y-%m-%d})" if shown else ""
        w.append(f"| {pid} | {_cell(employer)} | {_link(title, url)}{note} | {score} | {band} | "
                 f"{tier if tier is not None else ''} | {level_fit or '—'} |  |  |")
    w.append("")
    w.append("## Escalated Roles (pipeline-scored)\n")
    if not blocks:
        w.append(f"_No row reached the block bar ({' / '.join(bands)}) this run._\n")
    for (pid, employer, title, url, score, band, tier, level_fit, rule_score, fit_prob, embed_sim, top_terms,
         flags, text, *cov) in blocks:
        parts = [f"profile {rule_score}"]
        parts.extend(coverage_parts(*cov))
        if fit_prob is not None:
            parts.append(f"fit {fit_prob:.2f}")
        if embed_sim is not None:
            parts.append(f"embed {embed_sim:.2f}")
        parts.extend(coverage_detail(*cov))
        terms = [t[0] if isinstance(t, (list, tuple)) else str(t) for t in _as_list(top_terms)]
        if terms:
            parts.append("top terms: " + ", ".join(terms))
        parts.append(f"tier {tier}")
        flag_list = _as_list(flags)
        if flag_list:
            parts.append("flags: " + "; ".join(flag_list))
        w.append("---\n")
        w.append(f"# Company: {_cell(employer)}\n## Title: {_cell(title)}\nApply: {url or ''}\n"
                 f"{jd_body(text) or '(no JD text fetched yet)'}\n")
        w.append("---\n")
        w.append(f"**Fit: ~{score}%.** " + " · ".join(parts) + "\n")
    if blocks:
        w.append("---\n")
    if collapsed:
        w.append(f"<details><summary>Out of level range ({len(collapsed)})</summary>\n")
        w.append("| Posting ID | Company | Title | Score | Band | Tier | Level | Decision | Reason |\n"
                 "|---|---|---|---|---|---|---|---|---|")
        for pid, employer, title, url, score, band, tier, level_fit, shown in collapsed:
            note = f" (shown {shown:%Y-%m-%d})" if shown else ""
            w.append(f"| {pid} | {_cell(employer)} | {_link(title, url)}{note} | {score} | {band} | "
                     f"{tier if tier is not None else ''} | {level_fit or '—'} |  |  |")
        w.append("\n</details>\n")
    w.append("## Passed / Filtered Out\n\n| Company | Role | Reason |\n|---|---|---|")
    for employer, title, url, reasons, pid in passed:
        w.append(f"| {_cell(employer)} | {_link(title, url)} | {_cell('; '.join(_as_list(reasons)))} `pid:{pid}` |")
    path.write_text("\n".join(w) + "\n", encoding="utf-8")

    if block_ids:
        now = _now()
        con.executemany("INSERT OR REPLACE INTO surfaced VALUES (?, ?, ?)", [[pid, path.name, now] for pid in block_ids])
    return path


LENS_LIST_CAP = 60

# The three lists the user asked for. `both` is first because it is the least substitutable shape: a role
# that needs the process lane AND the data lane is one only a handful of people can fill, and it is the one
# he most wants to see. The `neither` bucket is not a list -- it is the corpus.
LENS_LISTS = (
    ("both", "Strong on BOTH lenses", "process excellence AND data/analytics — the least substitutable shape"),
    ("process", "Strong on PROCESS", "process excellence / operating model / change management"),
    ("technical", "Strong on TECHNICAL", "data, analytics, quantitative modelling and engineering"),
)

# Applied-AI lens (sprint plan sec 21): a fourth list reported beside the three lens_bucket ones above, read
# from `ai_strong` rather than `lens_bucket` (which stays untouched, per the sprint plan: "never folded in").
# A row can appear here and in one of the three lists above -- that is intended.
AI_LIST = ("ai", "Strong on APPLIED AI",
          "agentic workflow delivery, evals, enablement — reported beside the other lenses, never folded in")


def _lens_cell(grade: Optional[str], prob: Optional[float]) -> str:
    """The judge's word where there is one, the model's probability where there is not."""
    if grade:
        return grade
    return f"~{prob:.2f}" if prob is not None else "—"


def lens_rows(con, bucket: str, *, cap: int = LENS_LIST_CAP, include_decided: bool = False) -> list:
    """Actionable rows in one lens bucket, best first.

    Ordered by the ONE rank (`vw_lens_fit.rank_score`), which already prices the level call, so level is no
    longer a separate sort key. Never on `lens_source`: sorting by evidence weight would bury a model row at 92
    under a judged row at 55, and the point of scoring the whole corpus was to stop the judged 3k being the
    only thing visible. Source breaks a tie, and is a column the reader can see on every row.
    """
    where = ["lens_bucket = ?", "verdict != 'reject'"]
    if not include_decided:
        where += ["NOT decided", "NOT in_tracker"]
    return con.execute(f"""
        SELECT posting_id, employer, title, url, location_primary, pay_min, pay_max, pay_interval,
               final_score, band, grade_process, fit_process, grade_technical, fit_technical, lens_source,
               days_since_first_seen, blocker, level_fit, required_fit, fit_required, rank_score, rank_why
        FROM vw_lens_fit
        WHERE {' AND '.join(where)}
        ORDER BY rank_score DESC, final_score DESC,
                 CASE lens_source WHEN 'user' THEN 0 WHEN 'judge' THEN 1 WHEN 'judge+model' THEN 2 ELSE 3 END,
                 lens_max_p DESC, first_seen_at DESC
        LIMIT ?""", [bucket, cap]).fetchall()


def ai_lens_rows(con, *, cap: int = LENS_LIST_CAP, include_decided: bool = False) -> list:
    """Rows strong on the applied-AI lens (sec 21) -- `ai_strong`, never `lens_bucket`, so a row here can
    also appear in one of the three `lens_rows` lists above."""
    where = ["ai_strong", "verdict != 'reject'"]
    if not include_decided:
        where += ["NOT decided", "NOT in_tracker"]
    return con.execute(f"""
        SELECT posting_id, employer, title, url, location_primary, pay_min, pay_max, pay_interval,
               final_score, band, grade_ai, fit_ai, lens_source, days_since_first_seen, blocker, level_fit,
               required_fit, fit_required, rank_score, rank_why
        FROM vw_lens_fit
        WHERE {' AND '.join(where)}
        ORDER BY rank_score DESC, final_score DESC,
                 CASE lens_source WHEN 'user' THEN 0 WHEN 'judge' THEN 1 WHEN 'judge+model' THEN 2 ELSE 3 END,
                 lens_max_p DESC, first_seen_at DESC
        LIMIT ?""", [cap]).fetchall()


def _pay(lo, hi, interval) -> str:
    if lo is None and hi is None:
        return ""
    unit = {"year": "", "hour": "/hr", "month": "/mo"}.get(interval or "year", f"/{interval}")
    fmt = (lambda v: f"{v/1000:.0f}K" if (interval or "year") == "year" and v and v >= 1000 else
           (f"{v:.0f}" if v is not None else "?"))
    return f"${fmt(lo)}–{fmt(hi)}{unit}" if lo is not None and hi is not None else f"${fmt(lo or hi)}{unit}"


def write_lens_lists(con, vault_dir: Optional[str], *, cap: int = LENS_LIST_CAP, out_path=None) -> Path:
    """Writes Lens_Lists_YYYYMMDD_HHMM.md: strong on process, strong on technical, strong on both.

    Every row carries `lens_source`, because the three lists mix evidence of very different weight -- the
    user's own adjudication, the judge's grade, and a model prediction on a JD nobody has read. Without the
    column the lists would read as one uniform verdict, and a 0.7 from a TF-IDF model is not that.
    """
    now_local = datetime.now()
    stamp = now_local.strftime("%Y%m%d_%H%M")
    name = f"Lens_Lists_{stamp}.md"
    if out_path:
        out = Path(out_path)
        path = out / name if (out.is_dir() or not out.suffix) else out
    else:
        path = job_search_dir(vault_dir) / "Search_Results" / name
    path = _unique_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    strong_p, standout_p = con.execute("SELECT lens_strong_p(), lens_standout_p()").fetchone()
    sources = dict(con.execute("SELECT lens_source, count(*) FROM vw_lens_fit GROUP BY 1").fetchall())
    buckets = dict(con.execute("SELECT lens_bucket, count(*) FROM vw_lens_fit GROUP BY 1").fetchall())
    n_active = sum(buckets.values())

    w = [f"---\nnode_id: JOBS:lens-lists-{stamp.replace('_', '-')}\nnode_type: search_results\n"
         f"tags: [#job-search #pipeline #lens]\n---\n",
         f"# Lens Lists — process vs technical, {now_local:%Y-%m-%d %H:%M}\n",
         f"**What this is:** every active posting placed on both lenses, not only the judged ones. "
         f"**Placed by:** " + ", ".join(f"{k} {v}" for k, v in sorted(sources.items())) +
         f" across {n_active} active rows. "
         f"**Model thresholds:** strong ≥ {strong_p:.2f}, standout ≥ {standout_p:.2f} "
         f"(there is no model bullseye — the models cannot reproduce that split). "
         f"**Buckets:** " + ", ".join(f"{k} {v}" for k, v in sorted(buckets.items())) + ".\n",
         "> `lens_source` is the weight of the evidence: **user** is the candidate's "
         "own adjudication, **judge** is the LLM rubric on both lenses, **judge+model** is a row graded "
         "before the second lens existed, and **model** is a TF-IDF prediction on a JD nobody has read. "
         "Only the first two are grades; a model row is a candidate for grading, not a verdict.\n"]

    for bucket, heading, subtitle in LENS_LISTS:
        rows = lens_rows(con, bucket, cap=cap)
        total = buckets.get(bucket, 0)
        w.append(f"## {heading} ({len(rows)} shown of {total})\n\n_{subtitle}_\n")
        if not rows:
            w.append("_Nothing in this bucket is still actionable (undecided and not already applied to)._\n")
            continue
        w.append("| Posting ID | Rank | Company | Title | Why | Score | Band | Level | Process | Technical | Required | "
                 "Placed by | Pay | Location | Age |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for (pid, employer, title, url, loc, lo, hi, interval, score, band, gp, fp, gt, ft, src, age,
             blocker, level_fit, rf, freq, rank, why) in rows:
            w.append(f"| {pid} | {rank:.0f} | {_cell(employer)} | {_link(title, url)} | {_cell(why)} | {score} | {band} | "
                     f"{level_fit or '—'} | {_lens_cell(gp, fp)} | {_lens_cell(gt, ft)} | {_lens_cell(rf, freq)} | "
                     f"{src} | {_pay(lo, hi, interval)} | {_cell(loc)[:34]} | {age}d |")
        w.append("")

    _ai_bucket, ai_heading, ai_subtitle = AI_LIST
    ai_rows = ai_lens_rows(con, cap=cap)
    ai_total = con.execute("SELECT count(*) FROM vw_lens_fit WHERE ai_strong AND verdict != 'reject'").fetchone()[0]
    w.append(f"## {ai_heading} ({len(ai_rows)} shown of {ai_total})\n\n_{ai_subtitle}_\n")
    if not ai_rows:
        w.append("_Nothing in this bucket is still actionable (undecided and not already applied to)._\n")
    else:
        w.append("| Posting ID | Rank | Company | Title | Why | Score | Band | Level | AI | Required | Placed by | Pay | "
                 "Location | Age |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for (pid, employer, title, url, loc, lo, hi, interval, score, band, ga, fa, src, age, blocker,
             level_fit, rf, freq, rank, why) in ai_rows:
            w.append(f"| {pid} | {rank:.0f} | {_cell(employer)} | {_link(title, url)} | {_cell(why)} | {score} | {band} | "
                     f"{level_fit or '—'} | {_lens_cell(ga, fa)} | {_lens_cell(rf, freq)} | {src} | "
                     f"{_pay(lo, hi, interval)} | {_cell(loc)[:34]} | {age}d |")
        w.append("")
    path.write_text("\n".join(w) + "\n", encoding="utf-8")
    return path


TOP_APPLY_CAP = 100
TOP_REVIEW_CAP = 40
TOP_LEVELS = ("in_range", "stretch_up")

_GRADE_ABBR = {"bullseye": "bull", "adjacent": "adj", "stretch": "stretch", "wrong": "wrong"}


def _grade_compact(grade: Optional[str]) -> str:
    return _GRADE_ABBR.get(grade, "—")


def top_rows(con, tier: str, *, include_decided: bool = False, levels=TOP_LEVELS) -> list:
    """Judged rows in one selection tier (apply / review / hidden), ordered by the ONE rank (`vw_lens_fit.rank_score`).

    `vw_selection` is LEFT JOINed to `vw_screen_latest`, so a judged posting can carry a NULL
    `level_fit` and no location/pay/verdict at all -- that happens when the posting has never
    been screened. `vw_lens_fit`, by contrast, INNER JOINs the screen, so such a row is simply
    absent from it. This function LEFT JOINs to `vw_lens_fit` on purpose so that absence shows
    up as a NULL `verdict` here (and is excluded by the verdict gate below, deliberately) rather
    than silently vanishing before the query even runs -- `_gate_counts` below counts it
    separately as "no screen row" so it is never confused with an actual rejection.
    """
    where = ["v.tier = ?", "v.status = 'active'", "f.verdict IS NOT NULL", "f.verdict != 'reject'",
             "v.level_fit IN (SELECT unnest(?::VARCHAR[]))"]
    if not include_decided:
        where += ["NOT coalesce(f.decided, FALSE)", "NOT coalesce(f.in_tracker, FALSE)"]
    return con.execute(f"""
        SELECT v.posting_id, v.employer, v.title, v.url, v.grade_process, v.grade_technical, v.grade_ai,
               v.n_lenses_good, v.any_bullseye, v.required_fit, v.required_unmet, v.level_fit,
               f.location_primary, f.pay_min, f.pay_max, f.pay_interval, f.final_score, f.first_seen_at,
               f.days_since_first_seen, f.lens_breadth, f.rank_score, f.rank_why
        FROM vw_selection v
        LEFT JOIN vw_lens_fit f USING (posting_id)
        WHERE {' AND '.join(where)}
        ORDER BY f.rank_score DESC, f.final_score DESC, f.first_seen_at DESC
        """, [tier, list(levels)]).fetchall()


def _gate_counts(con, tier: str, levels=TOP_LEVELS) -> dict:
    """How many of this tier's rows each individual gate would exclude, independently of the
    others -- not a sequential funnel -- so a row lost to more than one gate is never hidden
    behind whichever gate happened to run first."""
    total = con.execute("SELECT count(*) FROM vw_selection WHERE tier = ?", [tier]).fetchone()[0]
    inactive = con.execute("SELECT count(*) FROM vw_selection WHERE tier = ? AND status != 'active'",
                           [tier]).fetchone()[0]
    # vw_lens_fit itself filters to active postings, so a missing row can mean either "inactive" (already
    # counted above) or "never screened" -- only the latter belongs in this gate, or an inactive posting
    # would be double-counted as though it also lacked a screen.
    no_screen = con.execute("""
        SELECT count(*) FROM vw_selection v LEFT JOIN vw_lens_fit f USING (posting_id)
        WHERE v.tier = ? AND v.status = 'active' AND f.posting_id IS NULL""", [tier]).fetchone()[0]
    reject = con.execute("""
        SELECT count(*) FROM vw_selection v JOIN vw_lens_fit f USING (posting_id)
        WHERE v.tier = ? AND f.verdict = 'reject'""", [tier]).fetchone()[0]
    out_of_level = con.execute("""
        SELECT count(*) FROM vw_selection v
        WHERE v.tier = ? AND (v.level_fit IS NULL OR v.level_fit NOT IN (SELECT unnest(?::VARCHAR[])))""",
        [tier, list(levels)]).fetchone()[0]
    decided_tracker = con.execute("""
        SELECT count(*) FROM vw_selection v JOIN vw_lens_fit f USING (posting_id)
        WHERE v.tier = ? AND (f.decided OR f.in_tracker)""", [tier]).fetchone()[0]
    return {"total": total, "inactive": inactive, "no_screen_row": no_screen, "verdict_reject": reject,
            "level_out_of_range": out_of_level, "decided_or_in_tracker": decided_tracker}


def write_top_jobs(con, vault_dir: Optional[str], *, out_path=None, apply_cap: int = TOP_APPLY_CAP,
                   review_cap: int = TOP_REVIEW_CAP, include_decided: bool = False, levels=TOP_LEVELS) -> Path:
    """Writes Top_Jobs_YYYYMMDD.md: the END-of-pipeline list, run by hand after a judge import
    (`finder.py judge import`) -- never from the automated sweep, which always runs `--no-report`.

    Ranked apply / review lists off `vw_selection`, gated on posting status, the screen's own
    verdict (where location and pay rejections live), level fit, and -- unless `include_decided`
    -- whether the posting is already decided or already in the tracker. Every gate's exclusion
    count is reported in the footer, including postings judged before they were ever screened,
    so a row can never simply disappear from the list without a paper trail.
    """
    now_local = datetime.now()
    stamp = now_local.strftime("%Y%m%d")
    name = f"Top_Jobs_{stamp}.md"
    if out_path:
        out = Path(out_path)
        path = out / name if (out.is_dir() or not out.suffix) else out
    else:
        path = job_search_dir(vault_dir) / "Search_Results" / name
    path = _unique_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    tiers = dict(con.execute("SELECT tier, count(*) FROM vw_selection GROUP BY 1").fetchall())
    judged = sum(tiers.values())
    rubric_versions = [r[0] for r in con.execute("""
        SELECT DISTINCT l.rubric_version FROM vw_llm_labels_latest l JOIN vw_selection v USING (posting_id)
        WHERE v.tier IN ('apply', 'review') ORDER BY 1""").fetchall()]

    apply_rows = top_rows(con, "apply", include_decided=include_decided, levels=levels)
    review_rows = top_rows(con, "review", include_decided=include_decided, levels=levels)
    apply_gates = _gate_counts(con, "apply", levels)
    review_gates = _gate_counts(con, "review", levels)

    w = [f"---\nnode_id: JOBS:top-{stamp}\nnode_type: search_results\ntags: [#job-search #pipeline #top]\n---\n",
         f"# Top Jobs — end of pipeline, after judge import, {now_local:%Y-%m-%d %H:%M}\n",
         f"**Rubric version(s):** {', '.join(rubric_versions) or 'none'}. "
         f"**Funnel:** {judged} judged → {tiers.get('apply', 0)} apply / {tiers.get('review', 0)} review / "
         f"{tiers.get('hidden', 0)} hidden → after active + verdict + level" +
         (" + undecided" if not include_decided else "") +
         f" gates: {len(apply_rows)} apply, {len(review_rows)} review. "
         f"**Levels shown:** {', '.join(levels)}. **Decided/in-tracker rows:** "
         f"{'included' if include_decided else 'hidden'}.\n"]

    def gate_line(gates: dict, label: str) -> str:
        return (f"of {gates['total']} {label}-tier rows: "
                f"{gates['inactive']} inactive, {gates['verdict_reject']} verdict reject, "
                f"{gates['level_out_of_range']} level out of range, "
                f"{gates['decided_or_in_tracker']} already decided/in tracker, "
                f"{gates['no_screen_row']} judged with no screen row")

    w.append("## Apply\n")
    if not apply_rows:
        w.append("_Nothing clears the apply gates this run._\n")
    else:
        w.append("| Rank | Company | Title | Why | Process / Technical / AI | Breadth | Required | Level | Location | Pay | Age |\n"
                 "|---|---|---|---|---|---|---|---|---|---|---|")
        for row in apply_rows[:apply_cap]:
            (pid, employer, title, url, gp, gt, ga, n_good, bullseye, req_fit, req_unmet, level_fit, loc, lo, hi,
             interval, score, first_seen, age, breadth, rank, why) = row
            star = "★ " if n_good == 3 else ""
            grades = f"{_grade_compact(gp)} / {_grade_compact(gt)} / {_grade_compact(ga)}"
            w.append(f"| {rank:.0f} | {star}{_cell(employer)} | {_link(title, url)} | {_cell(why)} | {grades} | "
                     f"{breadth:.2f} | {req_fit or '—'} | {level_fit or '—'} | {_cell(loc)[:34]} | "
                     f"{_pay(lo, hi, interval)} | {age if age is not None else '—'}d |")
    w.append("")
    w.append("## Review (requirement arguable — read the unmet lines)\n")
    if not review_rows:
        w.append("_Nothing clears the review gates this run._\n")
    else:
        w.append("| Rank | Company | Title | Why | Process / Technical / AI | Breadth | Required | Level | Location | Pay | Age |\n"
                 "|---|---|---|---|---|---|---|---|---|---|---|")
        for row in review_rows[:review_cap]:
            (pid, employer, title, url, gp, gt, ga, n_good, bullseye, req_fit, req_unmet, level_fit, loc, lo, hi,
             interval, score, first_seen, age, breadth, rank, why) = row
            star = "★ " if n_good == 3 else ""
            grades = f"{_grade_compact(gp)} / {_grade_compact(gt)} / {_grade_compact(ga)}"
            w.append(f"| {rank:.0f} | {star}{_cell(employer)} | {_link(title, url)} | {_cell(why)} | {grades} | "
                     f"{breadth:.2f} | {req_fit or '—'} | {level_fit or '—'} | {_cell(loc)[:34]} | "
                     f"{_pay(lo, hi, interval)} | {age if age is not None else '—'}d |")
    w.append("")
    w.append(f"**Gate detail — Apply:** {gate_line(apply_gates, 'apply')}.\n")
    w.append(f"**Gate detail — Review:** {gate_line(review_gates, 'review')}.\n")
    path.write_text("\n".join(w) + "\n", encoding="utf-8")
    return path


def snapshots(con, out_dir: Optional[str] = None) -> list:
    """Parquet exports of the shortlist, latest screens, decisions and tracker, plus shortlist CSVs."""
    out = Path(out_dir or SNAPSHOT_DIR)
    out.mkdir(parents=True, exist_ok=True)

    def copy(query: str, target: Path, fmt: str) -> Path:
        opts = "(FORMAT PARQUET)" if fmt == "parquet" else "(HEADER, DELIMITER ',')"
        con.execute(f"COPY ({query}) TO '{str(target).replace(chr(39), chr(39) * 2)}' {opts}")
        return target

    paths = [copy("SELECT * FROM vw_shortlist", out / "shortlist.parquet", "parquet"),
             copy("SELECT * FROM vw_screen_latest", out / "screen_latest.parquet", "parquet"),
             copy("SELECT * FROM decisions", out / "decisions.parquet", "parquet"),
             copy("SELECT * FROM tracker", out / "tracker.parquet", "parquet"),
             copy("SELECT * FROM vw_shortlist", out / "shortlist.csv", "csv"),
             copy("SELECT * FROM vw_shortlist", out / f"shortlist_{datetime.now():%Y%m%d}.csv", "csv")]
    return paths


def parse_decisions(path) -> list:
    """Rows of the Summary table whose Decision cell is build / pass / hold."""
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    start = text.find(SUMMARY_HEADING)
    if start < 0:
        return []
    header, out = None, []
    for line in text[start + len(SUMMARY_HEADING):].splitlines():
        if line.startswith("#"):
            break
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("|- :"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        row = dict(zip(header, cells))
        decision = (row.get("decision") or "").strip().lower()
        pid = (row.get("posting id") or "").strip()
        if decision in DECISIONS and re.fullmatch(r"[0-9a-f]{20}", pid):
            out.append(Decision(pid, decision, (row.get("reason") or "").strip() or None))
    return out


def read_back(con, vault_dir: str) -> int:
    """Reads Decision cells from Jobs_Found files new or changed since last read; returns decisions added."""
    folder = job_search_dir(vault_dir) / "Search_Results"
    if not folder.exists():
        return 0
    seen = dict(con.execute("SELECT file, mtime FROM readback_log").fetchall())
    added = 0
    for path in sorted(folder.glob("Jobs_Found_*.md")):
        mtime = path.stat().st_mtime
        if path.name in seen and seen[path.name] >= mtime:
            continue
        now = _now()
        con.execute("BEGIN")
        try:
            for d in parse_decisions(path):
                exists = con.execute("SELECT 1 FROM postings WHERE posting_id = ?", [d.posting_id]).fetchone()
                dup = con.execute("SELECT 1 FROM decisions WHERE posting_id = ? AND decision = ? AND source = 'file' "
                                  "AND source_ref = ?", [d.posting_id, d.decision, path.name]).fetchone()
                if exists and not dup:
                    con.execute("INSERT INTO decisions VALUES (?, ?, ?, 'file', ?, ?)",
                                [d.posting_id, d.decision, d.reason, path.name, now])
                    added += 1
            con.execute("INSERT OR REPLACE INTO readback_log VALUES (?, ?, ?)", [path.name, mtime, now])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return added
