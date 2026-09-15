"""Loads the employer registry: which ATS each employer uses, and its identifiers.

Where the CSVs live is configuration, not code. Set JOBSEARCH_REGISTRY_DIR (in the
environment or in the repo's gitignored .env) to a directory holding:
- ats_registry.csv             the registry (one file; a former candidates file was merged in 2026-09-15)
Unset, it falls back to ./registry/, where ats_registry.example.csv is used if no
ats_registry.csv exists — so a fresh clone runs against a handful of example boards.

`employer` is the dedup key, case-insensitive; the first row for a name wins. Rows whose `source` is "unresolved" are
skipped, as are rows on platforms without an adapter. Lines starting with # are comments.
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

from .adapters import IMPLEMENTED_PLATFORMS  # the platforms that have a working adapter

# Sources that mean "we don't actually have a working identifier yet" — skip these,
# don't waste a request finding out.
SKIP_SOURCES = {"unresolved"}


def _read_csv_rows(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
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
            }
            seen[key] = normalized
            ordered.append(normalized)

    return ordered


if __name__ == "__main__":
    rows = load_registry()
    from collections import Counter
    counts = Counter(r["platform"] for r in rows)
    print(f"{len(rows)} employers ready to sweep (implemented platforms only)")
    for platform, n in counts.most_common():
        print(f"  {platform:14s} {n}")
