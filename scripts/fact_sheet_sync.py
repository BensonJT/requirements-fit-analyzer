#!/usr/bin/env python3
"""Check (and optionally sync) the judge fact sheet between its source of truth and the copy this repo reads.

Usage (from the repo root):
    .venv/bin/python scripts/fact_sheet_sync.py            # report only: in sync, or which side is newer
    .venv/bin/python scripts/fact_sheet_sync.py --apply    # copy source -> local (refuses if local is newer)
    .venv/bin/python scripts/fact_sheet_sync.py --apply --pull   # local is newer: copy local -> source instead

Why it matters: the LLM second judge (judge2) and the Jev tier both read `judge2_background.local.md`
(`backend/finder/jev_cli.py` DEFAULT_BACKGROUND). The owner edits the source copy outside this repo, so the
two drift silently; a fact added to only one side is either invisible to the reviewers or lost on the next
copy. Jev also parses the sheet into at most 254 facts (jev.py enforces it), so the report counts facts with Jev's own parser.

Source path: $JUDGE2_BACKGROUND_SOURCE, else $JOBSEARCH_VAULT_DIR/Tools/judge2_background.md
(JOBSEARCH_VAULT_DIR is the job-search folder the reports are written to). Both may be set in .env.
Exit code: 0 in sync (or synced), 1 out of sync after a report-only run, 2 setup error.
"""
import filecmp
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCAL = REPO / "judge2_background.local.md"
sys.path.insert(0, str(REPO))
from backend.finder.jev import MAX_FACTS, parse_facts  # noqa: E402  (count facts exactly as Jev does)


def load_env():
    env = REPO / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))


def source_path():
    if os.environ.get("JUDGE2_BACKGROUND_SOURCE"):
        return Path(os.path.expanduser(os.environ["JUDGE2_BACKGROUND_SOURCE"]))
    vault = os.environ.get("JOBSEARCH_VAULT_DIR")
    if not vault:
        sys.exit("fact_sheet_sync: set JUDGE2_BACKGROUND_SOURCE or JOBSEARCH_VAULT_DIR (see docstring)")
    return Path(os.path.expanduser(vault)) / "Tools" / "judge2_background.md"


def fact_lines(path):
    return len(parse_facts(path.read_text(encoding="utf-8")))


def main(argv):
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    load_env()
    src = source_path()
    for p in (src, LOCAL):
        if not p.exists():
            print(f"fact_sheet_sync: missing {p}")
            return 2
    print(f"source {src}  ({fact_lines(src)} facts as Jev parses them)")
    print(f"local  {LOCAL}  ({fact_lines(LOCAL)} facts; Jev cap {MAX_FACTS})")
    if filecmp.cmp(src, LOCAL, shallow=False):
        print("in sync")
        return 0
    newer = "local" if LOCAL.stat().st_mtime > src.stat().st_mtime else "source"
    print(f"OUT OF SYNC; newer side: {newer}")
    if "--apply" not in argv:
        print("run with --apply (source -> local) or --apply --pull (local -> source) after reading the diff:")
        print(f"  diff '{src}' '{LOCAL}'")
        return 1
    if "--pull" in argv:
        shutil.copy2(LOCAL, src)
        print("copied local -> source")
    elif newer == "local":
        print("refusing: local is newer, so copying source -> local would drop its edits. Read the diff, then "
              "use --apply --pull if the local edits are wanted.")
        return 1
    else:
        shutil.copy2(src, LOCAL)
        print("copied source -> local")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
