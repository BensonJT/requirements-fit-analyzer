"""DuckDB maintenance for db/jobsearch.duckdb: report, compact, verify.

    .venv/bin/python scripts/db_maintenance.py report            # sizes, free blocks, row counts; writes nothing
    .venv/bin/python scripts/db_maintenance.py compact           # rewrite the file without its free blocks, verified, swapped in
    .venv/bin/python scripts/db_maintenance.py compact --force   # even when free space is under the threshold

Why a rewrite and not VACUUM. DuckDB has no CLUSTER and its VACUUM reclaims nothing: a
single-file database only ever grows, because freed blocks are reused but never returned to
the filesystem (2026-09-23: 2.1 GB on disk, 35% of blocks free after a week of nightly
rescreens). The one way to shrink it is to copy every table into a fresh file --
`COPY FROM DATABASE`, which keeps schema, constraints and indexes -- and swap the files. That
rewrite also lays each table out contiguously, which is as close to "cluster" as DuckDB gets.

Safety, in order: refuses while any other process holds the database (a live compaction
would corrupt the run holding it); CHECKPOINTs first so the WAL is folded in; copies into a
sibling `.compacting` file; verifies every table's row count and that `store.connect` opens
the copy (views, schema version); only then renames old -> `.precompact`, new -> live; the
`.precompact` file is deleted after the swap unless --keep-old, because disk space is the
whole point (the migration backups on the external drive remain the rollback).

`compact` is a no-op below COMPACT_MIN_FREE_FRACTION so the nightly hook (launch.sh `maint`
step) never rewrites 2 GB for a 2% gain.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb  # noqa: E402

from backend.ats import store  # noqa: E402

COMPACT_MIN_FREE_FRACTION = 0.10   # rewrite only when at least this share of blocks is free
DEFAULT_DB = store.DEFAULT_DB_PATH


def _fmt(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def holders(db_path):
    """Other processes that look like they hold this database (pgrep on the pipeline entry points)."""
    try:
        out = subprocess.run(["pgrep", "-af", r"sweep_ats\.py|finder\.py|judge2 run"], capture_output=True,
                             text=True).stdout
    except OSError:
        return []
    me = str(os.getpid())
    return [l for l in out.splitlines() if l.strip() and not l.startswith(me + " ") and "pgrep" not in l]


def database_size(con):
    """(file_bytes, block_size, total_blocks, used_blocks, free_blocks) from PRAGMA database_size."""
    name, db_size, block_size, total_blocks, used_blocks, free_blocks, wal, mem, mem_limit = \
        con.execute("PRAGMA database_size").fetchone()
    return block_size, total_blocks, used_blocks, free_blocks


def table_counts(con):
    names = [r[0] for r in con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE NOT internal ORDER BY 1").fetchall()]
    return {n: con.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}


def report(db_path=DEFAULT_DB, log=print):
    p = Path(db_path)
    if not p.exists():
        log(f"report: {db_path} does not exist")
        return None
    wal = Path(str(p) + ".wal")
    con = duckdb.connect(str(p), read_only=True)
    try:
        block_size, total, used, free = database_size(con)
        counts = table_counts(con)
    finally:
        con.close()
    frac = free / total if total else 0.0
    log(f"database : {p}  {_fmt(p.stat().st_size)} on disk"
        + (f"  (+ WAL {_fmt(wal.stat().st_size)})" if wal.exists() else ""))
    log(f"blocks   : {total} x {block_size // 1024} KB -- used {used}, free {free} ({frac:.0%} reclaimable, "
        f"~{_fmt(free * block_size)})")
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:6]
    log("rows     : " + ", ".join(f"{n} {c:,}" for n, c in top))
    return {"file_bytes": p.stat().st_size, "block_size": block_size, "total_blocks": total,
            "used_blocks": used, "free_blocks": free, "free_fraction": frac, "counts": counts}


def compact(db_path=DEFAULT_DB, force=False, keep_old=False, log=print):
    """Rewrite the database without its free blocks. Returns (did_compact, bytes_saved)."""
    p = Path(db_path)
    if not p.exists():
        log(f"compact: {db_path} does not exist")
        return False, 0
    busy = holders(db_path)
    if busy:
        log("compact: REFUSED -- another jobsearch process holds the database:")
        for l in busy:
            log("   " + l)
        return False, 0
    before = report(db_path, log=log)
    if before is None:
        return False, 0
    if before["free_fraction"] < COMPACT_MIN_FREE_FRACTION and not force:
        log(f"compact: skipped -- {before['free_fraction']:.0%} free is under the "
            f"{COMPACT_MIN_FREE_FRACTION:.0%} threshold (use --force)")
        return False, 0

    new = Path(str(p) + ".compacting")
    old = Path(str(p) + ".precompact")
    for f in (new, Path(str(new) + ".wal")):
        if f.exists():
            f.unlink()
    t0 = time.monotonic()
    # Exclusive open: this also fails loudly if something DuckDB-side still holds the file.
    con = duckdb.connect(str(p))
    try:
        con.execute("CHECKPOINT")
        catalog = con.execute("SELECT current_database()").fetchone()[0]  # the file's stem, not a fixed name
        con.execute(f"ATTACH '{new}' AS compacted")
        con.execute(f'COPY FROM DATABASE "{catalog}" TO compacted')
        con.execute("DETACH compacted")
    finally:
        con.close()
    log(f"compact  : copied into {new.name} in {time.monotonic() - t0:.0f}s")

    # Verify: same row count in every table, and the copy opens through store.connect (views + version).
    con = duckdb.connect(str(new), read_only=True)
    try:
        after_counts = table_counts(con)
    finally:
        con.close()
    diff = {n: (c, after_counts.get(n)) for n, c in before["counts"].items() if after_counts.get(n) != c}
    if diff:
        log(f"compact: ABORT -- row counts differ after the copy: {diff}; {new.name} left for inspection")
        return False, 0
    try:
        vc = store.connect(str(new))
        vc.execute("SELECT count(*) FROM vw_active").fetchone()
        vc.close()
    except Exception as e:  # noqa: BLE001 -- any failure means: do not swap
        log(f"compact: ABORT -- the copy does not open cleanly through store.connect: {e!r}")
        return False, 0
    for f in (Path(str(new) + ".wal"),):
        if f.exists():
            f.unlink()

    # Swap. Two renames on the same filesystem; the window with no live file is microseconds,
    # and nothing else may be holding the DB (checked above).
    old_bytes = p.stat().st_size
    if old.exists():
        old.unlink()
    p.rename(old)
    new.rename(p)
    new_bytes = p.stat().st_size
    saved = old_bytes - new_bytes
    log(f"compact  : {_fmt(old_bytes)} -> {_fmt(new_bytes)}  (saved {_fmt(saved)})")
    if keep_old:
        log(f"compact  : previous file kept as {old.name} (--keep-old)")
    else:
        old.unlink()
        log("compact  : previous file deleted (the external migration backups are the rollback)")
    return True, saved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["report", "compact"])
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--force", action="store_true", help="compact even under the free-block threshold")
    ap.add_argument("--keep-old", action="store_true", help="keep the pre-compaction file as .precompact")
    a = ap.parse_args(argv)
    if a.cmd == "report":
        return 0 if report(a.db) is not None else 1
    did, _ = compact(a.db, force=a.force, keep_old=a.keep_old)
    return 0


if __name__ == "__main__":
    sys.exit(main())
