"""Loads the employer registry: which ATS each employer uses, and its identifiers.

Where the CSVs live is configuration, not code. Set JOBSEARCH_REGISTRY_DIR (in the
environment or in the repo's gitignored .env) to a directory holding:
- ats_registry.csv             the registry (one file; a former candidates file was merged in 2026-09-15)
- bridge_places.csv            PRIVATE, never in the repo (sprint plan §31.2): the named places a
                                `track=bridge` row is pulled for -- place, ring (1|2), search_text,
                                state. See registry/bridge_places.example.csv for the shape (invented
                                places -- this repo is public).
- bridge_employer_order.csv    PRIVATE, optional: employer, rank -- sort preference only for
                                `finder.py bridge`'s output. Absent means no preference.
Unset, it falls back to ./registry/, where ats_registry.example.csv is used if no
ats_registry.csv exists — so a fresh clone runs against a handful of example boards.

`employer` is the dedup key, case-insensitive; the first row for a name wins. Rows whose `source` is "unresolved" are
skipped, as are rows on platforms without an adapter. Lines starting with # are comments.

`track` (optional column, blank/absent = `fit`) marks a row as the bridge track (sprint plan §31): a
place-scoped pull against bridge_places.csv rather than a whole-board pull. One employer may carry BOTH a
`fit` row (its corporate board, whole or job-family-scoped) and a `bridge` row (its store/retail board,
place-scoped) -- see backend/ats/store.py's posting_id / record_bridge_board for how the two never collide.
"""
import csv
import os

from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(REPO_ROOT, ".env"))

REGISTRY_DIR = os.path.expanduser(os.environ.get("JOBSEARCH_REGISTRY_DIR") or os.path.join(REPO_ROOT, "registry"))
REGISTRY_CSV = os.path.join(REGISTRY_DIR, "ats_registry.csv")
if not os.path.exists(REGISTRY_CSV):
    REGISTRY_CSV = os.path.join(REGISTRY_DIR, "ats_registry.example.csv")

BRIDGE_PLACES_CSV = os.path.join(REGISTRY_DIR, "bridge_places.csv")
if not os.path.exists(BRIDGE_PLACES_CSV):
    BRIDGE_PLACES_CSV = os.path.join(REGISTRY_DIR, "bridge_places.example.csv")
BRIDGE_EMPLOYER_ORDER_CSV = os.path.join(REGISTRY_DIR, "bridge_employer_order.csv")

from .adapters import IMPLEMENTED_PLATFORMS  # the platforms that have a working adapter

# Sources that mean "we don't actually have a working identifier yet" — skip these,
# don't waste a request finding out.
SKIP_SOURCES = {"unresolved"}


def _read_csv_rows(path):
    if not os.path.exists(path):
        return []
    rows = []
    # utf-8-sig: a BOM'd header (Excel-saved CSV) would otherwise make the first column name
    # come back as "﻿employer", which never matches row.get("employer") below (orchestrator
    # audit 2026-09-21, letter B).
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            employer = (row.get("employer") or "").strip()
            if not employer or employer.startswith("#"):
                continue  # section-comment lines in ats_registry.csv
            rows.append(row)
    return rows


def load_registry(implemented_only=True, include_unresolved=False):
    """Returns a deduped list of registry row dicts, normalized to one schema."""
    seen = {}
    ordered = []

    for path in (REGISTRY_CSV,):
        for row in _read_csv_rows(path):
            key = row["employer"].strip().lower()
            if key in seen:
                continue
            source = (row.get("source") or "").strip().lower()
            if not include_unresolved and source in SKIP_SOURCES:
                continue
            platform = (row.get("platform") or "").strip().lower()
            if implemented_only and platform not in IMPLEMENTED_PLATFORMS:
                continue
            normalized = {
                "employer": row["employer"].strip(),
                "platform": platform,
                "identifier_1": (row.get("identifier_1") or "").strip(),
                "identifier_2": (row.get("identifier_2") or "").strip(),
                "identifier_3": (row.get("identifier_3") or "").strip(),
                "source": source,
                "notes": (row.get("notes") or "").strip(),
                "track": (row.get("track") or "").strip().lower() or "fit",
            }
            seen[key] = normalized
            ordered.append(normalized)

    return ordered


def load_bridge_places(max_ring=1, log=None):
    """Places for the bridge track (sprint plan §31.2/§31.3), from the PRIVATE bridge_places.csv
    beside the registry (never in the repo -- see the module docstring). Columns: place, ring
    (any positive integer -- the user may keep as many rings as they like, e.g. a ring 3 of
    hard-commute places; NOT capped at 1|2), search_text (defaults to `place` when blank),
    state (defaults to the state parsed out of `place`'s "City, ST" shape when blank -- see
    backend.ats.bridge.parse_place), evergreen (optional, truthy -- see below), allow_no_state
    (optional, truthy -- orchestrator audit 2026-09-21, letter C.3: opts a place into matching a
    city-only location string with no state at all, e.g. a per-store facet board's
    "Springfield (0350)"; default off, since that shape is otherwise too easy to false-match).

    Only rows with `ring <= max_ring` are included (default 1: ring-1 only; 2026-09-21 user
    ruling replaced the earlier `ring2` boolean flag with this so a THIRD ring, or more, needs no
    code change -- just a bigger `--max-ring`). `max_ring=None` returns every ring (used by
    `finder.py bridge` to build a place -> ring lookup regardless of which rings the CURRENT list
    call is displaying). Returns [] when the file is missing or empty -- the
    caller (a bridge sweep) must treat that as "nothing to pull for this employer", loudly, never
    as license to fall back to a whole-board pull (sprint plan §31, item 8).

    `log` (orchestrator audit 2026-09-21, letter B): when the file has rows but NONE of them has a
    usable `place` value, this calls `log(...)` with the specific cause (a header mismatch --
    'place' column missing or misspelled) rather than leaving the caller to print the generic "no
    places configured" line, which is also true of a genuinely-empty/missing file but means
    something different to fix.

    `evergreen` (2026-09-21 user ruling): some employers post standing application pools -- every
    site lists the same titles under one shared posted date, including generic titles like "Any
    Position" -- where a NEW mark or a days-open count is actively misleading. A place row's
    `evergreen` column is truthy/blank; PURE DISPLAY LOGIC ONLY (finder.py cmd_bridge), never
    detected heuristically -- the user names which rows are pools."""
    from . import bridge as B

    raw_rows = _read_bridge_csv_rows(BRIDGE_PLACES_CSV)
    rows, any_place = [], False
    for row in raw_rows:
        place = (row.get("place") or "").strip()
        if not place:
            continue
        any_place = True
        try:
            ring = int((row.get("ring") or "1").strip() or "1")
        except ValueError:
            ring = 1
        if ring < 1:
            ring = 1
        if max_ring is not None and ring > max_ring:
            continue
        search_text = (row.get("search_text") or "").strip() or place
        state = (row.get("state") or "").strip()
        if not state:
            _, state = B.parse_place(place)
        evergreen = (row.get("evergreen") or "").strip().lower() in ("1", "true", "yes", "y")
        allow_no_state = (row.get("allow_no_state") or "").strip().lower() in ("1", "true", "yes", "y")
        rows.append({"place": place, "ring": ring, "search_text": search_text, "state": state,
                     "evergreen": evergreen, "allow_no_state": allow_no_state})
    if not any_place and raw_rows and log:
        log(f"bridge_places.csv has {len(raw_rows)} row(s) but none has a usable 'place' value -- "
            f"check the header spelling (expected column 'place') in {BRIDGE_PLACES_CSV}")
    return rows


def load_bridge_employer_order():
    """{employer_lower: rank} sort preference for `finder.py bridge` (sprint plan §31.2, private,
    optional). An employer not listed sorts after every listed one, in registry order among
    themselves (stable sort) -- absent file means no preference at all."""
    order = {}
    for i, row in enumerate(_read_bridge_csv_rows(BRIDGE_EMPLOYER_ORDER_CSV)):
        employer = (row.get("employer") or "").strip().lower()
        if not employer:
            continue
        try:
            rank = int((row.get("rank") or "").strip())
        except ValueError:
            rank = i
        order[employer] = rank
    return order


def _read_bridge_csv_rows(path):
    """Rows from a private bridge CSV (bridge_places.csv / bridge_employer_order.csv), skipping
    blank rows and lines whose FIRST field starts with '#' -- same comment convention
    _read_csv_rows uses for ats_registry.csv (orchestrator audit 2026-09-21, letter B). Opened
    utf-8-sig for the same BOM'd-header reason as _read_csv_rows."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not any((v or "").strip() for v in row.values()):
                continue
            first = next(iter(row.values()), "")
            if (first or "").strip().startswith("#"):
                continue
            rows.append(row)
    return rows


if __name__ == "__main__":
    rows = load_registry()
    from collections import Counter
    counts = Counter(r["platform"] for r in rows)
    print(f"{len(rows)} employers ready to sweep (implemented platforms only)")
    for platform, n in counts.most_common():
        print(f"  {platform:14s} {n}")
