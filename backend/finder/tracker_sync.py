"""Mirrors the vault's Application_Tracker.md into the `tracker` table and matches rows to postings.

The markdown stays the source of truth; this module only reads it. A matched tracker row
also records a `decisions` row (source 'tracker', decision 'build'), so a tracked posting is
never surfaced again and later serves as a positive label.
"""
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.screen import company_keys, company_matches, norm_company, similar_title

TRACKER_FILE = "Application_Tracker.md"
VAULT_SUBDIR = os.path.join("Professional", "Areas", "Job_Search")
_POSTING_ID = re.compile(r"\b[0-9a-f]{20}\b")
_DATE_FMT = "%Y-%m-%d"
FUZZY_MATCH_WINDOW_DAYS = 90  # a fuzzy company+title match must have been first seen within this
                              # window of the tracker's date applied, or a 2025 application can
                              # match an unrelated 2026 req that happens to share a title


@dataclass
class TrackerRow:
    section: str
    date_applied: str
    company: str
    role: str
    status: Optional[str] = None
    posting_id: Optional[str] = None


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def job_search_dir(vault_dir: str) -> Path:
    """The Job_Search folder: `vault_dir` itself when it holds the tracker, else the vault subpath."""
    base = Path(os.path.expanduser(vault_dir))
    return base if (base / TRACKER_FILE).exists() else base / VAULT_SUBDIR


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\|\s*-+\s*\|", line)) or set(line) <= set("|- :")


def parse_tracker(path) -> list:
    """Company/Role rows from every tracker table except Consultant Networks (header cell 2 = Platform).

    Cells 0..2 are positional (date, company, role); status is cell 3 when present. The posting id
    is the cell under a `Posting ID` header when the table has one, else the first 20-hex token
    anywhere in the row.
    """
    rows, section, header, skip = [], "", None, False
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section, header, skip = line.strip("# ").strip(), None, False
            continue
        stripped = line.strip()
        if not stripped.startswith("|"):
            header, skip = None, False  # a table ended
            continue
        if _is_separator(stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None and len(cells) >= 2 and cells[1] in ("Company", "Platform"):
            header, skip = cells, cells[1] == "Platform"
            continue
        if skip or len(cells) < 3:
            continue
        date, company, role = cells[0], cells[1], cells[2]
        if company in ("Company", "Platform", "") or role in ("Role", "Status", ""):
            continue
        if header and "Posting ID" in header:
            idx = header.index("Posting ID")
            cell = cells[idx] if idx < len(cells) else ""
            pid = cell if _POSTING_ID.fullmatch(cell) else None
        else:
            m = _POSTING_ID.search(stripped)
            pid = m.group(0) if m else None
        status = cells[3] if len(cells) > 3 and cells[3] else None
        rows.append(TrackerRow(section, date, company, role, status, pid))
    return rows


def _parse_row_date(s):
    """The tracker's `Date Applied` cell as a date, or None when it's missing/unparseable --
    in which case the fuzzy match falls back to the old, date-blind behavior."""
    try:
        return datetime.strptime((s or "").strip(), _DATE_FMT).date()
    except ValueError:
        return None


def match_rows(rows: list, postings: list) -> list:
    """(row, matched_posting_id, match_kind) per tracker row. `postings` = (posting_id, employer, title, first_seen_at).

    Exact when the row's posting id exists (an exact req/url match, since the tracker only ever
    records a posting id when a build recorded one). Else fuzzy on company keys + similar title
    over that employer's postings (active and closed) -- but only among postings first seen
    within FUZZY_MATCH_WINDOW_DAYS of the row's date applied, when both dates are known, so a
    same-titled req from a different year doesn't steal the match; newest first_seen_at wins.
    Else none.
    """
    known = {p[0] for p in postings}
    by_employer = defaultdict(list)
    for pid, employer, title, first_seen in postings:
        by_employer[employer].append((pid, title or "", first_seen))
    employer_keys = {e: norm_company(e) for e in by_employer}
    out = []
    for r in rows:
        if r.posting_id and r.posting_id in known:
            out.append((r, r.posting_id, "exact"))
            continue
        keys = company_keys(r.company)
        applied_date = _parse_row_date(r.date_applied)
        cands = []
        for employer, key in employer_keys.items():
            if not company_matches(key, keys):
                continue
            for pid, title, first_seen in by_employer[employer]:
                if not similar_title(r.role, title):
                    continue
                if applied_date is not None and first_seen is not None:
                    fs_date = first_seen.date() if hasattr(first_seen, "date") else first_seen
                    if abs((fs_date - applied_date).days) > FUZZY_MATCH_WINDOW_DAYS:
                        continue
                cands.append((first_seen, pid))
        out.append((r, max(cands)[1], "fuzzy") if cands else (r, None, "none"))
    return out


def sync(con, vault_dir: str, verbose: bool = False, log=print) -> dict:
    """Rebuilds `tracker` from the markdown and records a tracker decision for each matched posting."""
    path = job_search_dir(vault_dir) / TRACKER_FILE
    if not path.exists():
        log(f"Tracker sync: {path} not found; skipped.")
        return {"rows": 0, "exact": 0, "fuzzy": 0, "none": 0}
    rows = parse_tracker(path)
    postings = con.execute("SELECT posting_id, employer, title, first_seen_at FROM postings").fetchall()
    matches = match_rows(rows, postings)
    now = _now()
    counts = {"rows": len(rows), "exact": 0, "fuzzy": 0, "none": 0, "decisions_added": 0}
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM tracker")
        for r, matched, kind in matches:
            counts[kind] += 1
            con.execute("INSERT INTO tracker VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [r.section, r.date_applied, r.company, r.role, r.status, r.posting_id, matched, kind, now])
            if matched and not con.execute("SELECT 1 FROM decisions WHERE posting_id = ? AND source = 'tracker'",
                                           [matched]).fetchone():
                con.execute("INSERT INTO decisions VALUES (?, 'build', NULL, 'tracker', ?, ?)", [matched, r.section, now])
                counts["decisions_added"] += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    if verbose:
        for r, _, kind in matches:
            if kind == "none":
                log(f"  unmatched: {r.section} | {r.date_applied} | {r.company} | {r.role}")
    return counts
