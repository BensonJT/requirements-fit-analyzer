"""Mirrors the vault's Application_Tracker.md into the `tracker` table and matches rows to postings.

The markdown stays the source of truth; this module only reads it. A matched tracker row
also records a `decisions` row (source 'tracker'). Only an application is a `build` (a positive
label for every lens model); a package built but not sent is a `hold`, and an explicit
not-applied status a `pass` (user ruling 2026-10-20: Claude recommends and builds packages, so
a built package is not the user's pick). See `decision_for`.
"""
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.screen import company_keys, company_matches, norm_company, norm_title, similar_title

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
    job_id: Optional[str] = None       # the employer's own job / req id, from the Posting ID column
    no_posting: bool = False           # Posting ID cell `none`: no posting in our DB, never guess one


# Tracker status text -> decision (user ruling 2026-10-20). Anything else (Applied, Rejected, Interviewing,
# a blank status...) is an application: `build`, as before.
_NOT_APPLIED_STATUS = re.compile(r"not pursuing|not applied|\bskip|\bpass\b|closed before submission", re.I)
_UNSENT_STATUS = re.compile(r"package (built|ready)|materials ready|unsent|evaluating|on hold|holding|pending", re.I)
_DECISION_RANK = {"build": 2, "pass": 1, "hold": 0}   # one tracker row of several for a posting decides it

TRACKER_SOURCE_ENV = "TRACKER_SOURCE"   # `markdown` (default) | `postgres`


class TrackerSourceError(RuntimeError):
    """TRACKER_SOURCE is invalid, or the Postgres tracker was selected but cannot be read."""


def decision_for(row) -> tuple:
    """(decision, reason) a matched tracker row records. A `pass` carries an `other:` reason, which trains no
    lens model (features.training_set drops label-0 decision rows) but stops the posting resurfacing."""
    status = (row.status or "").strip()
    if _NOT_APPLIED_STATUS.search(status):
        return "pass", f"other: tracker status {status}"
    if _UNSENT_STATUS.search(status) or row.section.strip().lower() == "on hold":
        return "hold", None
    return "build", None


def decision_for_pg(row) -> tuple:
    """(decision, reason) for a Postgres tracker row (design section 8). `built` is an unsent package: hold;
    `passed` / `closed-unsent` never went out: pass; `withdrawn` or `declined` before applying (no date_applied): pass;
    anything that was applied (date_applied set) is a build. A remaining status without a date (an interview or
    rejection logged without the apply date) was still an application: build."""
    status = (row.status or "").strip().lower()
    if status == "built":
        return "hold", None
    if status in ("passed", "closed-unsent"):
        return "pass", f"other: tracker status {status}"
    if not _has_date(row.date_applied):
        if status in ("withdrawn", "declined"):
            return "pass", f"other: tracker status {status}"
    return "build", None


def _has_date(value) -> bool:
    return bool((value or "").strip()) and value.strip() not in ("—", "-")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def job_search_dir(vault_dir: str) -> Path:
    """The Job_Search folder: `vault_dir` itself when it holds the tracker, else the vault subpath."""
    base = Path(os.path.expanduser(vault_dir))
    return base if (base / TRACKER_FILE).exists() else base / VAULT_SUBDIR


# A cell may hold an escaped pipe ("Grant Dawson (SVP IT) \\| Austin ..."); only a bare pipe splits cells.
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def split_cells(line: str) -> list:
    return [c.strip() for c in _CELL_SPLIT.split(line.strip().strip("|"))]


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\|\s*-+\s*\|", line)) or set(line) <= set("|- :")


def parse_tracker(path) -> list:
    """Company/Role rows from every tracker table except Consultant Networks (header cell 2 = Platform).

    Cells 0..2 are positional (date, company, role); status is cell 3 when present. The `Posting ID` column
    (2026-10-20, user decision) holds our 20-hex posting id, or else the employer's job id (`job_id`), or `none`
    (no posting in our DB: never fuzzy-matched); a table without the column, or a blank cell, falls back to the
    first 20-hex token anywhere in the row.
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
        cells = split_cells(stripped)
        if header is None and len(cells) >= 2 and cells[1] in ("Company", "Platform"):
            header, skip = cells, cells[1] == "Platform"
            continue
        if skip or len(cells) < 3:
            continue
        date, company, role = cells[0], cells[1], cells[2]
        if company in ("Company", "Platform", "") or role in ("Role", "Status", ""):
            continue
        cell = ""
        if header and "Posting ID" in header:
            idx = header.index("Posting ID")
            cell = cells[idx] if idx < len(cells) else ""
        pid = cell if _POSTING_ID.fullmatch(cell) else None
        no_posting = cell.lower() == "none"
        job_id = cell if cell and pid is None and not no_posting and cell not in ("—", "-") else None
        if pid is None and job_id is None:
            m = _POSTING_ID.search(stripped)
            pid = m.group(0) if m else None
        status = cells[3] if len(cells) > 3 and cells[3] else None
        rows.append(TrackerRow(section, date, company, role, status, pid, job_id, no_posting))
    return rows


def _parse_row_date(s):
    """The tracker's `Date Applied` cell as a date, or None when it's missing/unparseable --
    in which case the fuzzy match falls back to the old, date-blind behavior."""
    try:
        return datetime.strptime((s or "").strip(), _DATE_FMT).date()
    except ValueError:
        return None


# "req 31756", "Req #32609", "requisition R-12345" in the tracker's Role cell (2026-10-15).
_REQ_IN_ROLE = re.compile(r"\breq(?:uisition)?\.?\s*(?:id\s*)?#?\s*:?\s*([A-Za-z0-9][A-Za-z0-9_-]{2,})", re.I)


def _req_named(role: str):
    m = _REQ_IN_ROLE.search(role or "")
    return m.group(1).lower() if m else None


# Level words must agree for a FUZZY tracker match (2026-10-20): similar_title ignores them, so "Senior TPM"
# matched "Principal TPM" and "Sr Director, Business Analysis" matched "Sr Associate, Business Analysis".
# Tracker-only: similar_title also drives the screen's tracker dedup, which stays as it was.
_LEVEL_WORD = re.compile(r"\b(senior|sr|principal|lead|director|associate|vp|vice president|head|chief|junior|jr"
                         r"|entry|ii|iii|iv)\b", re.I)
_LEVEL_ALIAS = {"sr": "senior", "jr": "junior", "vice president": "vp"}


def _levels(title: str) -> set:
    return {_LEVEL_ALIAS.get(w.lower(), w.lower()) for w in _LEVEL_WORD.findall(norm_title(title))}


def match_rows(rows: list, postings: list) -> list:
    """(row, matched_posting_id, match_kind) per tracker row. `postings` = (posting_id, employer, title,
    first_seen_at[, req_id[, url]]).

    2026-10-15: when the Role cell names a req ("req 31756") and the postings carry req_id, ONLY a posting of
    that employer with that req matches -- a same-titled sibling req never does (NFCU's new Senior BI Analyst
    32609 stole the 31756 row's match the day it appeared, because that row has no date applied and the
    date-blind fallback picks the newest same-titled posting). A named req with no such posting is "none".

    Exact when the row's posting id exists (an exact req/url match, since the tracker only ever
    records a posting id when a build recorded one). Else fuzzy on company keys + similar title
    over that employer's postings (active and closed) -- but only among postings first seen
    within FUZZY_MATCH_WINDOW_DAYS of the row's date applied, when both dates are known, so a
    same-titled req from a different year doesn't steal the match; newest first_seen_at wins.
    Else none.
    """
    known = {p[0] for p in postings}
    by_employer = defaultdict(list)
    req_of, url_of = {}, {}
    for pid, employer, title, first_seen, *rest in postings:
        by_employer[employer].append((pid, title or "", first_seen))
        if rest and rest[0]:
            req_of[pid] = str(rest[0]).strip().lower()
        if len(rest) > 1 and rest[1]:
            url_of[pid] = str(rest[1]).lower()
    employer_keys = {e: norm_company(e) for e in by_employer}
    out = []
    for r in rows:
        if r.posting_id and r.posting_id in known:
            out.append((r, r.posting_id, "exact"))
            continue
        if r.no_posting:
            out.append((r, None, "none"))
            continue
        keys = company_keys(r.company)
        applied_date = _parse_row_date(r.date_applied)
        job_id = (r.job_id or "").strip().lower() or None
        named = (job_id or _req_named(r.role)) if req_of else None
        cands = []
        for employer, key in employer_keys.items():
            if not company_matches(key, keys):
                continue
            for pid, title, first_seen in by_employer[employer]:
                if named is not None:
                    # A job id from the Posting ID column may be the employer's display number, not our req_id
                    # (Microsoft shows 200058580; we store its apply id): it also matches inside the URL.
                    if req_of.get(pid) == named or (job_id and re.search(rf"(?<![0-9a-z]){re.escape(job_id)}(?![0-9a-z])",
                                                                        url_of.get(pid, ""))):
                        cands.append((first_seen, pid))
                    continue
                if not similar_title(r.role, title) or _levels(r.role) != _levels(title):
                    continue
                if applied_date is not None and first_seen is not None:
                    fs_date = first_seen.date() if hasattr(first_seen, "date") else first_seen
                    if abs((fs_date - applied_date).days) > FUZZY_MATCH_WINDOW_DAYS:
                        continue
                cands.append((first_seen, pid))
        kind = "exact" if job_id is not None else "fuzzy"   # the row's own job id is as precise as ours
        out.append((r, max(cands)[1], kind) if cands else (r, None, "none"))
    return out


def pg_rows(conn=None) -> list:
    """TrackerRow records for every tracker.application row. Section comes from the status table
    (active -> "Active", else "Closed"). Raises TrackerSourceError when Postgres cannot be read."""
    own = conn is None
    try:
        if own:
            from backend.tracker import db
            conn = db.connect()
        found = conn.execute("""SELECT a.posting_id, a.employer_job_id, a.company, a.role, a.status, a.date_applied,
                                       s.is_active
                                FROM tracker.application a JOIN tracker.status s USING (status)
                                ORDER BY a.app_id""").fetchall()
    except Exception as e:   # psycopg errors, missing module, bad credentials: never fall back to markdown
        raise TrackerSourceError(f"TRACKER_SOURCE=postgres but the tracker database could not be read: {e}") from e
    finally:
        if own and conn is not None:
            conn.close()
    return [TrackerRow("Active" if r["is_active"] else "Closed",
                       r["date_applied"].isoformat() if r["date_applied"] else "",
                       r["company"], r["role"], r["status"], r["posting_id"], r["employer_job_id"])
            for r in found]


def match_rows_pg(rows: list, postings: list) -> list:
    """(row, matched_posting_id, match_kind, ambiguous) per Postgres tracker row. Exact only, never fuzzy:
    the row's posting_id when it exists in `postings`; else its employer_job_id equal to a req_id (or standing
    alone inside the url) of a posting whose employer `company_matches`. More than one such posting matches
    none (ambiguous=True). match_kind is 'posting_id', 'req_id' or 'none'. `postings` as for `match_rows`."""
    known = {p[0] for p in postings}
    employers = defaultdict(list)    # employer -> [(posting_id, req_id, url)]
    for pid, employer, _title, _seen, *rest in postings:
        employers[employer].append((pid, str(rest[0]).strip().lower() if rest and rest[0] else None,
                                    str(rest[1]).lower() if len(rest) > 1 and rest[1] else ""))
    employer_keys = {e: norm_company(e) for e in employers}
    out = []
    for r in rows:
        if r.posting_id and r.posting_id in known:
            out.append((r, r.posting_id, "posting_id", False))
            continue
        job_id = (r.job_id or "").strip().lower()
        if not job_id:
            out.append((r, None, "none", False))
            continue
        keys = company_keys(r.company)
        url_re = re.compile(rf"(?<![0-9a-z]){re.escape(job_id)}(?![0-9a-z])")
        cands = {pid for employer, key in employer_keys.items() if company_matches(key, keys)
                 for pid, req, url in employers[employer] if req == job_id or url_re.search(url)}
        if len(cands) == 1:
            out.append((r, next(iter(cands)), "req_id", False))
        else:
            out.append((r, None, "none", len(cands) > 1))
    return out


def _apply(con, rows, matches, decide, counts, now):
    """Rebuild the `tracker` mirror and record decisions. `matches` = (row, matched, kind) triples."""
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM tracker")
        wanted = {}   # posting_id -> (decision, reason, section) of its most advanced tracker row
        for r, matched, kind in matches:
            con.execute("INSERT INTO tracker VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [r.section, r.date_applied, r.company, r.role, r.status, r.posting_id, matched, kind, now])
            if matched:
                decision, reason = decide(r)
                if matched not in wanted or _DECISION_RANK[decision] > _DECISION_RANK[wanted[matched][0]]:
                    wanted[matched] = (decision, reason, r.section)
        for pid, (decision, reason, section) in wanted.items():
            # Record a tracker decision when the tracker's call changes (a hold becomes a build once applied).
            # Only an application overrides a later ruling made elsewhere (`mark`, a sheet): a tracker hold or
            # pass never undoes one.
            prev = con.execute("""SELECT decision, decided_at FROM decisions WHERE posting_id = ? AND source = 'tracker'
                                  ORDER BY decided_at DESC LIMIT 1""", [pid]).fetchone()
            if prev and prev[0] == decision:
                continue
            if decision != "build" and con.execute(
                    "SELECT 1 FROM decisions WHERE posting_id = ? AND source != 'tracker' AND decided_at > ?",
                    [pid, prev[1] if prev else datetime.min]).fetchone():
                continue
            con.execute("INSERT INTO decisions VALUES (?, ?, ?, 'tracker', ?, ?)", [pid, decision, reason, section, now])
            counts["decisions_added"] += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def sync(con, vault_dir: str, verbose: bool = False, log=print, source: Optional[str] = None, pg_conn=None) -> dict:
    """Rebuilds `tracker` from the configured source and records a tracker decision for each matched posting.

    `source` (default: $TRACKER_SOURCE, else `markdown`): `markdown` reads the vault file (fuzzy matching);
    `postgres` reads tracker.application (exact matching only; raises TrackerSourceError if unreachable)."""
    source = (source or os.environ.get(TRACKER_SOURCE_ENV) or "markdown").strip().lower()
    if source == "postgres":
        return _sync_pg(con, verbose, log, pg_conn)
    if source != "markdown":
        raise TrackerSourceError(f"{TRACKER_SOURCE_ENV}={source!r}: expected 'markdown' or 'postgres'")
    path = job_search_dir(vault_dir) / TRACKER_FILE
    if not path.exists():
        log(f"Tracker sync: {path} not found; skipped.")
        return {"rows": 0, "exact": 0, "fuzzy": 0, "none": 0}
    rows = parse_tracker(path)
    postings = con.execute("SELECT posting_id, employer, title, first_seen_at, req_id, url FROM postings").fetchall()
    matches = match_rows(rows, postings)
    now = _now()
    counts = {"rows": len(rows), "exact": 0, "fuzzy": 0, "none": 0, "decisions_added": 0}
    for _r, _m, kind in matches:
        counts[kind] += 1
    _apply(con, rows, matches, decision_for, counts, now)
    if verbose:
        for r, _, kind in matches:
            if kind == "none":
                log(f"  unmatched: {r.section} | {r.date_applied} | {r.company} | {r.role}")
    return counts


def _sync_pg(con, verbose, log, pg_conn):
    rows = pg_rows(pg_conn)
    postings = con.execute("SELECT posting_id, employer, title, first_seen_at, req_id, url FROM postings").fetchall()
    matched = match_rows_pg(rows, postings)
    counts = {"source": "postgres", "rows": len(rows), "exact": 0, "fuzzy": 0, "none": 0, "posting_id": 0,
              "req_id": 0, "ambiguous": 0, "decisions_added": 0}
    for _r, _m, kind, ambiguous in matched:
        counts[kind] += 1
        if kind != "none":
            counts["exact"] += 1
        counts["ambiguous"] += ambiguous
    _apply(con, rows, [(r, m, k) for r, m, k, _a in matched], decision_for_pg, counts, _now())
    log(f"Tracker sync (postgres): {counts['rows']} rows, {counts['posting_id']} by posting_id, "
        f"{counts['req_id']} by req_id, {counts['none']} unmatched ({counts['ambiguous']} ambiguous).")
    if verbose:
        for r, _, kind, ambiguous in matched:
            if kind == "none":
                log(f"  unmatched{' (ambiguous req)' if ambiguous else ''}: {r.section} | {r.date_applied} | "
                    f"{r.company} | {r.role}")
    return counts
