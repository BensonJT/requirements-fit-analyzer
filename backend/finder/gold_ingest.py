"""Ingests the user's hand-graded gold-standard sheets, which live outside this repo in several header
formats -- until this module, only one of them (`feedback.load_csv`'s own wide export) could be loaded.
Detects the format from the header, normalizes enum values through an explicit alias table (never silent),
resolves precedence when the same posting is graded more than once (across files in one run, or already in
the DB), and writes through the SAME paths `feedback.write_records` / `feedback.load_csv` already use.
Nothing here calls an LLM or touches the network.

Five formats, by header shape (see `detect_format`):
  F1  the golden-source wide CSV `feedback.load_csv` already reads -- delegated to unchanged, one row per
      report_feedback column, `basis` carried per row.
  F2  a narrow spot-check sheet: posting_id, employer, title, url, human_grade, level_fit, note.
  F3  the blind-sheet format `blind_sheet.import_sheet` reads: F2 plus location, required_fit, required_unmet.
  F4  a single-lens (Applied-AI) grade sheet: human_grade_ai, not an overall human_grade -- see `ingest_f4`
      for why this format is report-only here.
  F5  a derived train/frozen split (posting_id/half/baseline_judge_grade) -- never a grading sheet; refused.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import feedback, rubric
from .blind_sheet import REQUIRED_FIT_VALUES

# CLI-facing basis words match `finder.py mark --basis` (MARK_BASIS_VALUES) and `blind_sheet.import_sheet`'s
# own 'blind' -- report_feedback.basis is written with these values verbatim. (The schema comment says
# jd_read|metadata|rule_screen, but no code path has ever written those; `mark`'s default write-back and its
# own test (`test_mark_basis_default_seen_and_blind_view_filters`) assert the literal string 'seen'. Gold
# ingest follows the code that actually runs, not the stale comment.)
BASIS_VALUES = ("blind", "seen")

# level_fit spellings seen in real sheets that are not the enum's own words. Anything not listed here and
# not already a valid LEVEL_ORDER value is a rejected row, never guessed into one.
LEVEL_FIT_ALIASES = {"to_low": "too_low", "stretch": "stretch_up", "out_of_range": "out_of_reach"}

PROPOSED_MARKER = "proposal_confidence"

F5_MARKERS = {"half", "baseline_judge_grade"}
F1_MARKERS = {"description_hash", "basis", "assessor", "confirmed_by_user", "verdict"}
F4_MARKERS = {"human_grade_ai", "posting_status"}
F3_MARKERS = {"required_fit", "required_unmet"}
F2_REQUIRED = {"posting_id", "employer", "title", "url", "human_grade", "level_fit"}

F2_KNOWN = F2_REQUIRED | {"row", "note"}
F3_KNOWN = {"posting_id", "employer", "title", "location", "url", "human_grade", "required_fit",
            "required_unmet", "level_fit", "note"}
F4_KNOWN = {"row", "posting_id", "employer", "title", "url", "posting_status", "human_grade_ai",
            "level_fit", "confidence", "note"}
F1_KNOWN = {_h.strip().lower() for _h in feedback.FEEDBACK_CSV_COLUMNS} | {"row"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm_header(name) -> str:
    return (name or "").strip().lstrip("﻿").lower()


def _header_set(fieldnames) -> set:
    return {_norm_header(h) for h in (fieldnames or []) if h is not None}


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v or None


class UnknownFormat(ValueError):
    """Raised with the raw header text; the caller refuses that one file and moves on."""


def detect_format(fieldnames) -> str:
    """'f1' | 'f2' | 'f3' | 'f4' | 'f5' from the header alone (BOM/case/whitespace-insensitive, a leading
    `row` column ignored). F5's marker columns are checked first because a derived split must never be read
    as a gradeable sheet even if it happened to share other column names."""
    cols = _header_set(fieldnames)
    if F5_MARKERS <= cols:
        return "f5"
    if F1_MARKERS <= cols:
        return "f1"
    if F4_MARKERS <= cols:
        return "f4"
    if F3_MARKERS <= cols:
        return "f3"
    if F2_REQUIRED <= cols:
        return "f2"
    raise UnknownFormat(f"unrecognized header: {list(fieldnames or [])}")


def ignored_columns(fieldnames, fmt: str) -> list:
    known = {"f1": F1_KNOWN, "f2": F2_KNOWN, "f3": F3_KNOWN, "f4": F4_KNOWN}.get(fmt, set())
    return [h for h in (fieldnames or []) if h is not None and _norm_header(h) not in known]


def _normalize_enum(raw, aliases: dict, valid: tuple):
    """(normalized_value_or_None, alias_used_or_None, was_invalid). Blank input is simply "not assessed",
    never a rejection."""
    v = _clean(raw)
    if v is None:
        return None, None, False
    key = v.lower()
    mapped = aliases.get(key, key)
    if mapped not in valid:
        return None, None, True
    return mapped, (key if key in aliases else None), False


def _resolve_posting(con, pid, url):
    """(posting_id, description_hash, matched_by_url). Mirrors feedback.load_csv: a blank posting_id is
    looked up by url; a posting_id given directly or found by url that isn't in `postings` resolves to
    (None, None, matched_by_url) -- "skipped, no matching posting", never invented."""
    matched_by_url = False
    if not pid:
        if not url:
            return None, None, False
        hit = con.execute("SELECT posting_id, description_hash FROM postings WHERE url = ?", [url]).fetchone()
        if not hit:
            return None, None, False
        return hit[0], hit[1], True
    hit = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [pid]).fetchone()
    if not hit:
        return None, None, False
    return pid, hit[0], matched_by_url


class FileReport:
    """Per-file counters and messages for the ingest log / dry-run report."""

    def __init__(self, path):
        self.path = str(path)
        self.format = None
        self.refused = None            # a message, if the whole file was refused
        self.rows_total = 0
        self.would_write = 0
        self.skipped_blank_grade = 0
        self.skipped_no_posting = 0
        self.matched_by_url = 0
        self.rejected = []             # [{line, posting_id, field, value}]
        self.aliases_applied = []      # [{line, posting_id, field, from, to}]
        self.conflicts = []            # [{line, posting_id, kind, existing, incoming}]
        self.ignored_columns = []

    def as_dict(self) -> dict:
        return {
            "path": self.path, "format": self.format, "refused": self.refused,
            "rows_total": self.rows_total, "would_write": self.would_write,
            "skipped_blank_grade": self.skipped_blank_grade, "skipped_no_posting": self.skipped_no_posting,
            "matched_by_url": self.matched_by_url, "rejected": self.rejected,
            "aliases_applied": self.aliases_applied, "conflicts": self.conflicts,
            "ignored_columns": self.ignored_columns,
        }

    def log(self, log):
        if self.refused:
            log(f"gold ingest: REFUSED {self.path}: {self.refused}")
            return
        log(f"gold ingest: {self.path} ({self.format}): {self.rows_total} rows, "
            f"{self.would_write} would-write, {self.skipped_blank_grade} skipped (blank grade), "
            f"{self.skipped_no_posting} skipped (no posting, {self.matched_by_url} matched by url), "
            f"{len(self.rejected)} rejected, {len(self.aliases_applied)} aliases applied, "
            f"{len(self.conflicts)} conflicts")
        if self.ignored_columns:
            log(f"  ignored columns: {', '.join(self.ignored_columns)}")
        for r in self.rejected:
            log(f"  REJECTED line {r['line']} ({r['posting_id']}): {r['field']}={r['value']!r}")
        for a in self.aliases_applied:
            log(f"  alias line {a['line']} ({a['posting_id']}): {a['field']} {a['from']!r} -> {a['to']!r}")
        for c in self.conflicts:
            log(f"  CONFLICT line {c['line']} ({c['posting_id']}): basis {c['existing']!r} kept over "
                f"{c['incoming']!r}")


# ---------------------------------------------------------------- precedence merge


def _seed_from_db(con, posting_id, description_hash):
    row = con.execute("""
        SELECT basis, human_grade, level_fit, note, required_fit, required_unmet
        FROM report_feedback WHERE posting_id = ? AND description_hash = ? AND assessor = 'user'
    """, [posting_id, description_hash]).fetchone()
    if not row:
        return None
    basis, human_grade, level_fit, note, required_fit, required_unmet = row
    return {"posting_id": posting_id, "description_hash": description_hash, "basis": basis,
            "human_grade": human_grade, "level_fit": level_fit, "note": note,
            "required_fit": required_fit, "required_unmet": required_unmet}


def _resolve_basis(current, incoming):
    """(basis, conflict). Never lets a 'seen' row downgrade an existing 'blind' one, nor a 'blind' row
    upgrade an existing 'seen' one -- the DB / earlier-in-run row's basis always wins on a mismatch, and
    the mismatch is reported, never silently resolved."""
    if current is None:
        return incoming, False
    if current == incoming:
        return current, False
    return current, True


def _merge(existing, incoming):
    """Fold one normalized row into the posting's running state. A row that carries a required_fit is
    never overwritten by a later one that lacks it (the later row's grade/level/note still land if
    non-blank); a later row's own required_fit (if any) always wins outright, same as everything else."""
    if existing is None:
        return dict(incoming), False
    merged = dict(existing)
    basis, conflict = _resolve_basis(existing.get("basis"), incoming.get("basis"))
    merged["basis"] = basis
    if existing.get("required_fit") is not None and incoming.get("required_fit") is None:
        pass  # keep existing required_fit / required_unmet
    else:
        merged["required_fit"] = incoming.get("required_fit")
        merged["required_unmet"] = incoming.get("required_unmet")
    for field in ("human_grade", "level_fit", "note"):
        v = incoming.get(field)
        if v not in (None, ""):
            merged[field] = v
    return merged, conflict


# ---------------------------------------------------------------- per-format row parsing


def _parse_f2_f3(con, fmt, fr: FileReport, rows, basis, accumulator: dict) -> None:
    """Folds this file's rows directly into the RUN-WIDE `accumulator` (mutated in place), updating `fr`
    counters as it goes. `rows` is a list of (line_no, raw_dict). A posting is seeded from the DB at most
    once per run, the first time any file in the run touches it -- seeding it again per file would let a
    later file's DB-inherited required_fit (inherited only because that file never had a required_fit
    column at all) look like a fresh claim and override an earlier file's real one in the SAME run."""
    for line, raw in rows:
        fr.rows_total += 1
        pid_in = _clean(raw.get("posting_id"))
        url = _clean(raw.get("url"))
        pid, description_hash, matched_by_url = _resolve_posting(con, pid_in, url)
        if matched_by_url:
            fr.matched_by_url += 1
        if pid is None or description_hash is None:
            fr.skipped_no_posting += 1
            continue

        human_grade = _clean(raw.get("human_grade"))
        if human_grade is None:
            fr.skipped_blank_grade += 1
            continue
        if human_grade not in rubric.GRADES:
            fr.rejected.append({"line": line, "posting_id": pid, "field": "human_grade", "value": human_grade})
            continue

        level_fit, level_alias, level_invalid = _normalize_enum(raw.get("level_fit"), LEVEL_FIT_ALIASES,
                                                                 feedback.LEVEL_ORDER)
        if level_invalid:
            fr.rejected.append({"line": line, "posting_id": pid, "field": "level_fit",
                                "value": raw.get("level_fit")})
            continue
        if level_alias:
            fr.aliases_applied.append({"line": line, "posting_id": pid, "field": "level_fit",
                                       "from": level_alias, "to": level_fit})

        required_fit = required_unmet = None
        if fmt == "f3":
            required_fit, req_alias, req_invalid = _normalize_enum(raw.get("required_fit"), {}, REQUIRED_FIT_VALUES)
            if req_invalid:
                fr.rejected.append({"line": line, "posting_id": pid, "field": "required_fit",
                                    "value": raw.get("required_fit")})
                continue
            required_unmet = _clean(raw.get("required_unmet"))
            if required_fit is not None:
                required_unmet = required_unmet or ""

        row = {"posting_id": pid, "description_hash": description_hash, "human_grade": human_grade,
               "level_fit": level_fit, "note": _clean(raw.get("note")), "required_fit": required_fit,
               "required_unmet": required_unmet, "basis": basis}
        key = (pid, description_hash)
        if key not in accumulator:
            accumulator[key] = _seed_from_db(con, pid, description_hash)
        merged, conflict = _merge(accumulator[key], row)
        accumulator[key] = merged
        if conflict:
            fr.conflicts.append({"line": line, "posting_id": pid,
                                 "existing": merged.get("basis"), "incoming": basis})
        fr.would_write += 1


def ingest_f4(fr: FileReport, rows) -> list:
    """Parses and validates the single-lens (Applied-AI) sheet fully, but never writes it: `llm_labels`
    stamps whether a `scorer='user-adjudicated'` row's LENS grade is human/carried/placeholder with ONE
    column (`lens_grade_source`) for the WHOLE row, not one per lens (see store.py's `lens_label_source`
    macro and its 2026-09-19 audit-fix comment). Writing a human `grade_ai` here would need the row's overall
    `grade` and any existing `required_fit` to be preserved untouched while only `grade_ai`'s provenance
    changes -- which the current single-flag column cannot express without either (a) laundering an
    unasserted overall grade into 'user_adjudicated' status (the exact defect that comment describes), or
    (b) silently overwriting an existing user-adjudicated row's required_fit/other lens grades on a PK
    collision (posting_id, description_hash, rubric_version, scorer). Both are worse than not writing.
    A real fix needs a schema change (e.g. a per-lens `*_grade_source` column, or a separate table for a
    human per-lens grade) -- out of scope here (no schema bump allowed on this branch). Returns the parsed,
    validated rows for the report only."""
    out = []
    for line, raw in rows:
        fr.rows_total += 1
        pid = _clean(raw.get("posting_id"))
        human_grade_ai = _clean(raw.get("human_grade_ai"))
        if human_grade_ai is None:
            fr.skipped_blank_grade += 1
            continue
        if human_grade_ai not in rubric.GRADES:
            fr.rejected.append({"line": line, "posting_id": pid, "field": "human_grade_ai",
                                "value": human_grade_ai})
            continue
        level_fit, level_alias, level_invalid = _normalize_enum(raw.get("level_fit"), LEVEL_FIT_ALIASES,
                                                                 feedback.LEVEL_ORDER)
        if level_invalid:
            fr.rejected.append({"line": line, "posting_id": pid, "field": "level_fit",
                                "value": raw.get("level_fit")})
            continue
        if level_alias:
            fr.aliases_applied.append({"line": line, "posting_id": pid, "field": "level_fit",
                                       "from": level_alias, "to": level_fit})
        fr.would_write += 1  # "would write" if F4 ever gets a write path; today it never does
        out.append({"posting_id": pid, "human_grade_ai": human_grade_ai, "level_fit": level_fit,
                    "note": _clean(raw.get("note"))})
    fr.refused = ("F4 (single-lens Applied-AI grade) parses and validates but is never written -- "
                  "llm_labels.lens_grade_source is one flag for the whole row, not one per lens, so a human "
                  "grade_ai cannot be stored here without either laundering an unasserted overall grade as "
                  "human-adjudicated or overwriting an existing required_fit/other lens grade on the same "
                  "posting. Needs a schema change this branch may not make; see ingest_f4's docstring.")
    return out


def _read_csv(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = [(i, raw) for i, raw in enumerate(reader, start=2)]  # row 1 is the header
    return fieldnames, rows


# ---------------------------------------------------------------- manifest


def load_manifest(manifest_path) -> list:
    """[(path, basis, notes)] from a `path,basis,notes` CSV; `path` is resolved relative to the manifest's
    OWN directory. A row with a blank basis is passed through as `None` -- resolved (or refused) per-file
    against the detected format, same as an explicit CLI path with no `--basis`."""
    manifest_path = Path(manifest_path)
    base_dir = manifest_path.parent
    out = []
    with manifest_path.open(newline="", encoding="utf-8-sig") as f:
        for raw in csv.DictReader(f):
            rel = _clean(raw.get("path"))
            if not rel:
                continue
            basis = _clean(raw.get("basis"))
            out.append((base_dir / rel, basis, _clean(raw.get("notes"))))
    return out


# ---------------------------------------------------------------- top-level run


def ingest(con, targets: list, *, accept_proposed=False, dry_run=False, log=print) -> dict:
    """`targets` is [(path, basis)], in the order they should be applied (a later entry wins precedence
    ties for the same posting). Detects each file's format, normalizes and validates its rows, folds them
    into a run-wide precedence merge (seeded from the DB), and -- unless `dry_run` -- writes the result
    through `feedback.write_records` (F2/F3) or delegates whole-file to `feedback.load_csv` (F1). F4 is
    always report-only (see `ingest_f4`); F5 is always refused. Returns {"files": [...], "ok": bool}; `ok`
    is False if any file was refused outright or any row was rejected for a bad enum value."""
    file_reports = []
    accumulator: dict = {}
    ok = True

    for path, basis in targets:
        fr = FileReport(path)
        file_reports.append(fr)
        try:
            fieldnames, rows = _read_csv(path)
        except OSError as e:
            fr.refused = f"cannot read file: {e}"
            ok = False
            continue

        try:
            fmt = detect_format(fieldnames)
        except UnknownFormat as e:
            fr.refused = str(e)
            ok = False
            continue
        fr.format = fmt
        fr.ignored_columns = ignored_columns(fieldnames, fmt)

        if fmt == "f5":
            fr.refused = ("this is a derived train/frozen split (posting_id/half/baseline_judge_grade), "
                          "not a grading sheet -- it is never ingested")
            ok = False
            continue

        if PROPOSED_MARKER in _header_set(fieldnames) and not accept_proposed:
            fr.refused = (f"header carries {PROPOSED_MARKER!r} -- this is a machine-drafted proposal of the "
                          "user's calls, not a confirmed grading sheet; re-run with --accept-proposed once "
                          "the user has reviewed and confirmed the draft")
            ok = False
            continue

        if fmt == "f1":
            fr.rows_total = len(rows)
            if dry_run:
                _dry_run_f1(con, fr, rows)
            else:
                feedback.load_csv(con, path, log=lambda *_a, **_k: None)
                fr.would_write = fr.rows_total  # load_csv's own counts are logged separately by cmd
            continue

        if fmt == "f4":
            ingest_f4(fr, rows)
            ok = False  # F4 never writes; a run that touched one is not fully applied
            continue

        # f2 / f3: basis is required -- narrow sheets carry no basis column of their own, and a default
        # would silently blur blind vs seen (sprint plan §22.4, the whole point of the distinction).
        if basis is None:
            fr.refused = (f"no basis declared for a narrow sheet (format {fmt}) -- pass --basis blind|seen "
                          "or give it a non-blank basis column in the manifest")
            ok = False
            continue
        if basis not in BASIS_VALUES:
            fr.refused = f"basis {basis!r} must be one of {', '.join(BASIS_VALUES)}"
            ok = False
            continue

        _parse_f2_f3(con, fmt, fr, rows, basis, accumulator)
        if fr.rejected:
            ok = False

    if not dry_run and accumulator:
        now = _now()
        records = [{
            "posting_id": pid, "description_hash": description_hash, "human_grade": rec["human_grade"],
            "level_fit": rec["level_fit"], "verdict": "consider", "basis": rec["basis"], "assessor": "user",
            "confirmed_by_user": True, "note": rec["note"], "required_fit": rec["required_fit"],
            "required_unmet": rec["required_unmet"], "assessed_at": now,
        } for (pid, description_hash), rec in accumulator.items()]
        feedback.write_records(con, records, log=lambda *_a, **_k: None)

    for fr in file_reports:
        fr.log(log)
    log(f"gold ingest: {len(accumulator)} posting(s) resolved from narrow sheets "
        f"({'DRY RUN, nothing written' if dry_run else 'written'})")
    return {"files": [fr.as_dict() for fr in file_reports], "merged_postings": len(accumulator), "ok": ok}


def _dry_run_f1(con, fr: FileReport, rows) -> None:
    """Mirrors feedback.load_csv's own counting (blank posting_id -> url match; no match -> skipped) without
    writing anything, so `--dry-run` can report F1 counts too."""
    for _line, raw in rows:
        pid = _clean(raw.get("posting_id"))
        if not pid:
            url = _clean(raw.get("url"))
            hit = con.execute("SELECT posting_id FROM postings WHERE url = ?", [url]).fetchone() if url else None
            if not hit:
                fr.skipped_no_posting += 1
                continue
            fr.matched_by_url += 1
        fr.would_write += 1
