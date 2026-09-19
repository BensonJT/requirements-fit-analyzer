"""The golden source (sprint plan 20.2 / 20.5): load the user's own hand-graded `report_feedback` rows from
a vault CSV, check the level rule against them, and re-export the sheet with the rule's answer, the judge's
grades, and `needs_you` beside the human columns so the user grades only what disagrees.

Nothing here calls an LLM or touches the network -- it is table maintenance plus two read-only reports.
"""
import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# CSV columns, in the order the golden-source export uses. `employer`, `title`, `url` and `judge_agreement`
# describe the row for a human reader only -- report_feedback identifies a posting by posting_id /
# description_hash, and a stale hash is what makes a row read as "expired" (vw_report_feedback_latest),
# rather than a duplicate copy of employer/title drifting out of sync with `postings`.
FEEDBACK_CSV_COLUMNS = (
    "posting_id", "report_files", "employer", "title", "url", "description_hash",
    "snapshot_final_score", "snapshot_band", "snapshot_grade_process", "snapshot_grade_technical",
    "human_grade", "level_fit", "verdict", "reason_code", "reason_detail", "positioning",
    "judge_agreement", "confidence", "basis", "assessor", "assessed_at", "confirmed_by_user",
    "note", "grade_before_split", "needs_confirm", "split_reason",
)

# Ordered low -> high; "one step" in the sprint plan means one position here.
LEVEL_ORDER = ["too_low", "in_range", "stretch_up", "out_of_reach"]
# Ordered best -> worst.
GRADE_ORDER = ["bullseye", "adjacent", "stretch", "wrong"]


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _clean(v) -> Optional[str]:
    """Empty string -> NULL; everything else stripped."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def _bool(v) -> Optional[bool]:
    v = _clean(v)
    if v is None:
        return None
    return v.upper() in ("TRUE", "T", "1", "YES")


def _int(v) -> Optional[int]:
    v = _clean(v)
    return int(v) if v is not None else None


EXCEL_TS_FORMATS = ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y")


def _ts(v):
    """ISO first; then the US formats Excel rewrites a timestamp into when the vault CSV is saved from it."""
    v = _clean(v)
    if v is None:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        for fmt in EXCEL_TS_FORMATS:
            try:
                return datetime.strptime(v, fmt)
            except ValueError:
                continue
        raise


def _steps_apart(order: list, a: Optional[str], b: Optional[str]) -> Optional[int]:
    if a not in order or b not in order:
        return None
    return abs(order.index(a) - order.index(b))


def load_csv(con, path, log=print) -> dict:
    """Upserts the golden-source CSV into `report_feedback`. A blank `posting_id` is looked up by `url`; a
    row that matches neither is skipped and counted, never invented. `description_hash` is loaded exactly as
    the CSV says it -- it is the row's own claim about which JD text it was assessed against, and
    `vw_report_feedback_latest` is what decides whether that claim still matches the posting's current text.

    Returns {"loaded", "skipped_no_posting", "matched_by_url"}.
    """
    path = Path(path)
    loaded = skipped_no_posting = matched_by_url = 0
    now = _now()
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:   # the vault export carries a BOM
        for raw in csv.DictReader(f):
            pid = _clean(raw.get("posting_id"))
            if not pid:
                url = _clean(raw.get("url"))
                hit = con.execute("SELECT posting_id FROM postings WHERE url = ?", [url]).fetchone() if url else None
                if not hit:
                    skipped_no_posting += 1
                    continue
                pid = hit[0]
                matched_by_url += 1
            rows.append([
                pid,
                _clean(raw.get("description_hash")) or "",
                _clean(raw.get("report_files")),
                _int(raw.get("snapshot_final_score")),
                _clean(raw.get("snapshot_band")),
                _clean(raw.get("snapshot_grade_process")),
                _clean(raw.get("snapshot_grade_technical")),
                _clean(raw.get("human_grade")),
                _clean(raw.get("level_fit")),
                _clean(raw.get("verdict")),
                _clean(raw.get("reason_code")),
                _clean(raw.get("reason_detail")),
                _clean(raw.get("positioning")),
                _clean(raw.get("confidence")),
                _clean(raw.get("basis")),
                _clean(raw.get("assessor")),
                _bool(raw.get("confirmed_by_user")) or False,
                _clean(raw.get("note")),
                _clean(raw.get("grade_before_split")),
                _bool(raw.get("needs_confirm")),
                _clean(raw.get("split_reason")),
                _ts(raw.get("assessed_at")) or now,
                now,
            ])
            loaded += 1
    if rows:
        con.executemany("""
            INSERT OR REPLACE INTO report_feedback (
                posting_id, description_hash, report_files, snapshot_final_score, snapshot_band,
                snapshot_grade_process, snapshot_grade_technical, human_grade, level_fit, verdict,
                reason_code, reason_detail, positioning, confidence, basis, assessor,
                confirmed_by_user, note, grade_before_split, needs_confirm, split_reason,
                assessed_at, loaded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
    counts = {"loaded": loaded, "skipped_no_posting": skipped_no_posting, "matched_by_url": matched_by_url}
    log(f"feedback load: {counts['loaded']} loaded ({counts['matched_by_url']} matched by url), "
        f"{counts['skipped_no_posting']} skipped (no matching posting)")
    return counts


def rule_level_fit(con, posting_ids) -> dict:
    """{posting_id: {"level_fit", "level_fit_hits"}} computed LIVE by the level rule, straight off the current
    `postings` row -- not the stored `screens.level_fit`. This works before any rescreen (sprint plan 20.4
    only runs `rescreen-all` after this build and a fresh ingest), which is the point: the agreement number
    and the re-export do not wait on it. Defensive against Agent A's rule not existing yet: `.get()` on notes
    that lack "level_fit" simply reads as unknown, same as an unscreened row."""
    from . import pipeline, rules
    ids = list(dict.fromkeys(posting_ids))  # de-dup, keep order
    out = {}
    for row in pipeline.fetch_rows(con, ids):
        rec = rules.screen_row(row)
        out[row["posting_id"]] = {
            "level_fit": rec.notes.get("level_fit"),
            "level_fit_hits": rec.notes.get("level_fit_hits") or [],
        }
    return out


def agreement(con, log=print) -> dict:
    """Confirmed human `level_fit` vs. the rule computed live (§20.2's acceptance bar: >= 80% exact on
    `in_range` / `out_of_reach`). Prints n, exact agreement %, the same restricted to human in_range/
    out_of_reach, a human x rule confusion table, and every disagreement. Returns the numbers."""
    rows = con.execute("""
        SELECT f.posting_id, p.employer, p.title, f.level_fit
        FROM vw_report_feedback_latest f JOIN postings p USING (posting_id)
        WHERE f.level_fit IS NOT NULL AND f.confirmed_by_user
    """).fetchall()
    rule = rule_level_fit(con, [r[0] for r in rows])
    n = len(rows)
    exact = bar_n = bar_exact = 0
    confusion: dict = {}
    disagreements = []
    for pid, employer, title, human in rows:
        info = rule.get(pid, {})
        r = info.get("level_fit")
        confusion.setdefault(human, Counter())[r or "unknown"] += 1
        if r == human:
            exact += 1
        else:
            disagreements.append((pid, employer, title, human, r, info.get("level_fit_hits") or []))
        if human in ("in_range", "out_of_reach"):
            bar_n += 1
            bar_exact += r == human
    pct = round(100 * exact / n, 1) if n else None
    bar_pct = round(100 * bar_exact / bar_n, 1) if bar_n else None
    log(f"Level agreement: {exact}/{n} exact ({pct}%) — in_range/out_of_reach only: "
        f"{bar_exact}/{bar_n} ({bar_pct}%, target >= 80%)")
    log("Confusion (human -> rule counts):")
    for human in sorted(confusion):
        log(f"  {human:<14} " + ", ".join(f"{k}={v}" for k, v in sorted(confusion[human].items())))
    for pid, employer, title, human, r, hits in disagreements:
        log(f"  {pid} · {employer} · {title} · human={human} · rule={r} · {', '.join(hits)}")
    return {"n": n, "exact": exact, "exact_pct": pct, "bar_n": bar_n, "bar_exact": bar_exact, "bar_pct": bar_pct,
            "confusion": {h: dict(c) for h, c in confusion.items()}, "disagreements": disagreements}


def export(con, out_path, log=print) -> Path:
    """Re-exports EVERY `report_feedback` row (every assessor, latest hash or not -- an `expired` column
    marks the stale ones) with the rule's live `level_fit`, the judge's three lens grades, and `needs_you` so
    the user grades only the rows that disagree instead of re-reading all 232 by hand."""
    rows = con.execute("""
        SELECT rf.posting_id, p.employer, p.title, p.url, rf.description_hash,
               coalesce(p.description_hash, '') != rf.description_hash AS expired,
               rf.report_files, rf.snapshot_final_score, rf.snapshot_band,
               rf.snapshot_grade_process, rf.snapshot_grade_technical, rf.human_grade, rf.level_fit,
               rf.verdict, rf.reason_code, rf.reason_detail, rf.positioning, rf.confidence, rf.basis,
               rf.assessor, rf.confirmed_by_user, rf.note, rf.grade_before_split, rf.needs_confirm,
               rf.split_reason, rf.assessed_at,
               j.grade_process AS judge_grade_process, j.grade_technical AS judge_grade_technical,
               j.grade_ai AS judge_grade_ai, j.grade AS judge_grade
        FROM report_feedback rf
        JOIN postings p USING (posting_id)
        LEFT JOIN vw_llm_labels_latest j USING (posting_id)
        ORDER BY rf.posting_id, rf.assessor
    """)
    cols = [d[0] for d in rows.description]
    fetched = rows.fetchall()
    rule = rule_level_fit(con, [r[0] for r in fetched])
    out_rows = []
    for raw in fetched:
        d = dict(zip(cols, raw))
        info = rule.get(d["posting_id"], {})
        d["rule_level_fit"] = info.get("level_fit")
        d["rule_level_hits"] = "; ".join(info.get("level_fit_hits") or [])
        level_steps = _steps_apart(LEVEL_ORDER, d["level_fit"], d["rule_level_fit"])
        grade_steps = _steps_apart(GRADE_ORDER, d["human_grade"], d["judge_grade"])
        d["needs_you"] = bool(
            d["confidence"] == "low"
            or (d["level_fit"] is not None and level_steps is not None and level_steps > 1)
            or (d["level_fit"] is None and d["rule_level_fit"] in ("out_of_reach", "too_low")
                and d["verdict"] in ("build", "consider"))
            or (d["human_grade"] is not None and grade_steps is not None and grade_steps > 1)
            or bool(d["needs_confirm"])
        )
        out_rows.append(d)
    out_rows.sort(key=lambda d: (not d["needs_you"], -(d["snapshot_final_score"] if d["snapshot_final_score"]
                                                        is not None else -1)))
    out_path = Path(out_path)
    fieldnames = list(out_rows[0].keys()) if out_rows else cols + ["rule_level_fit", "rule_level_hits", "needs_you"]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)
    log(f"feedback export: {len(out_rows)} rows ({sum(d['needs_you'] for d in out_rows)} need you) -> {out_path}")
    return out_path
