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
    blocks = con.execute(f"""
        SELECT v.posting_id, v.employer, v.title, v.url, v.final_score, v.band, v.tier, v.rule_score,
               v.fit_prob, v.embed_sim, v.top_terms, v.flags, p.description_text
        FROM vw_shortlist v JOIN postings p USING (posting_id)
        WHERE v.band IN (SELECT unnest(?::VARCHAR[]))
          AND v.posting_id NOT IN (SELECT posting_id FROM surfaced
                                   WHERE surfaced_at >= now() - INTERVAL {SURFACED_DAYS} DAY)
        ORDER BY v.final_score DESC, v.first_seen_at DESC LIMIT ?""", [bands, max_blocks]).fetchall()
    block_ids = [b[0] for b in blocks]
    table = con.execute("""
        SELECT v.posting_id, v.employer, v.title, v.url, v.final_score, v.band, v.tier,
               (SELECT max(surfaced_at) FROM surfaced s WHERE s.posting_id = v.posting_id) AS shown
        FROM vw_shortlist v
        WHERE v.final_score >= ? OR v.posting_id IN (SELECT unnest(?::VARCHAR[]))
        ORDER BY v.final_score DESC, v.first_seen_at DESC LIMIT ?""", [table_min, block_ids, table_cap]).fetchall()
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
    w.append(f"{SUMMARY_HEADING}\n\n| Posting ID | Company | Title | Score | Band | Tier | Decision | Reason |\n"
             "|---|---|---|---|---|---|---|---|")
    for pid, employer, title, url, score, band, tier, shown in table:
        note = f" (shown {shown:%Y-%m-%d})" if shown else ""
        w.append(f"| {pid} | {_cell(employer)} | {_link(title, url)}{note} | {score} | {band} | "
                 f"{tier if tier is not None else ''} |  |  |")
    w.append("")
    w.append("## Escalated Roles (pipeline-scored)\n")
    if not blocks:
        w.append(f"_No row reached the block bar ({' / '.join(bands)}) this run._\n")
    for (pid, employer, title, url, score, band, tier, rule_score, fit_prob, embed_sim, top_terms, flags,
         text) in blocks:
        parts = [f"profile {rule_score}"]
        if fit_prob is not None:
            parts.append(f"fit {fit_prob:.2f}")
        if embed_sim is not None:
            parts.append(f"embed {embed_sim:.2f}")
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
    w.append("## Passed / Filtered Out\n\n| Company | Role | Reason |\n|---|---|---|")
    for employer, title, url, reasons, pid in passed:
        w.append(f"| {_cell(employer)} | {_link(title, url)} | {_cell('; '.join(_as_list(reasons)))} `pid:{pid}` |")
    path.write_text("\n".join(w) + "\n", encoding="utf-8")

    if block_ids:
        now = _now()
        con.executemany("INSERT OR REPLACE INTO surfaced VALUES (?, ?, ?)", [[pid, path.name, now] for pid in block_ids])
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
