"""DuckDB maintenance for db/jobsearch.duckdb: report, compact, verify.

    .venv/bin/python scripts/db_maintenance.py report            # sizes, free blocks, row counts; writes nothing
    .venv/bin/python scripts/db_maintenance.py compact           # rewrite the file without its free blocks, verified, swapped in
    .venv/bin/python scripts/db_maintenance.py compact --force   # even when free space is under the threshold
    .venv/bin/python scripts/db_maintenance.py snapshot --out F  # consistent compressed copy of the DB (db_maintenance.sh --backup)
    .venv/bin/python scripts/db_maintenance.py snapshot --remote R  # stream the same copy to an rclone path; nothing written locally

Why a rewrite and not VACUUM. DuckDB has no CLUSTER and its VACUUM reclaims nothing: a
single-file database only ever grows, because freed blocks are reused but never returned to
the filesystem (2026-09-23: 2.1 GB on disk, 35% of blocks free after a week of nightly
rescreens). The one way to shrink it is to copy every table into a fresh file --
`COPY FROM DATABASE`, which keeps schema, constraints and indexes -- and swap the files. That
rewrite also lays each table out contiguously, which is as close to "cluster" as DuckDB gets.

Safety, in order: refuses while a sweep_ats.py, finder.py or judge2 run is running (pgrep; a live
compaction would corrupt the run holding it). Any other holder of the DuckDB lock (lookup.py,
pg_snapshot.py, a DuckDB shell) is not caught by that check, but the open then fails: `report` says the
database is locked (exit 1), and `compact` and `snapshot` stop with exit 2. It CHECKPOINTs first so the
WAL is folded in; copies into a sibling `.compacting` file; verifies every table's row count and that `store.connect` opens
the copy (views, schema version); only then renames old -> `.precompact`, new -> live; the
`.precompact` file is deleted after the swap unless --keep-old, because disk space is the
whole point (the migration backups on the external drive remain the rollback).

`compact` is a no-op below COMPACT_MIN_FREE_FRACTION (10% of blocks free) so the nightly hook
(launch.sh `maint` step) never rewrites 2 GB for a 2% gain.

Exit: report 0, or 1 if the file is missing or locked; compact 0 compacted or skipped under the threshold,
1 failed (missing file, ABORT), 2 database busy (REFUSED); snapshot 0 ok, 1 failed, 2 database busy
(skipped), 3 upload failed (--remote).
"""
import argparse
import hashlib
import json
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
    try:
        con = duckdb.connect(str(p), read_only=True)
    except duckdb.IOException as e:   # a writer the pgrep check does not know about holds the lock
        log(f"report: database is locked by another process ({str(e).splitlines()[0]})")
        return None
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
    """Rewrite the database without its free blocks. Returns (did_compact, bytes_saved, exit_code): exit 0
    compacted or skipped under the threshold, 1 failed (missing file, ABORT), 2 database busy (REFUSED)."""
    p = Path(db_path)
    if not p.exists():
        log(f"compact: {db_path} does not exist")
        return False, 0, 1
    busy = holders(db_path)
    if busy:
        log("compact: REFUSED -- another jobsearch process holds the database:")
        for l in busy:
            log("   " + l)
        return False, 0, 2
    before = report(db_path, log=log)
    if before is None:   # the file exists, so report could not open it: locked by a process pgrep missed
        log("compact: REFUSED -- the database is locked")
        return False, 0, 2
    if before["free_fraction"] < COMPACT_MIN_FREE_FRACTION and not force:
        log(f"compact: skipped -- {before['free_fraction']:.0%} free is under the "
            f"{COMPACT_MIN_FREE_FRACTION:.0%} threshold (use --force)")
        return False, 0, 0

    new = Path(str(p) + ".compacting")
    old = Path(str(p) + ".precompact")
    for f in (new, Path(str(new) + ".wal")):
        if f.exists():
            f.unlink()
    t0 = time.monotonic()
    # Exclusive open: this also fails if something DuckDB-side still holds the file.
    try:
        con = duckdb.connect(str(p))
    except duckdb.IOException as e:
        log(f"compact: REFUSED -- the database is locked ({str(e).splitlines()[0]})")
        return False, 0, 2
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
        return False, 0, 1
    try:
        vc = store.connect(str(new))
        vc.execute("SELECT count(*) FROM vw_active").fetchone()
        vc.close()
    except Exception as e:  # noqa: BLE001 -- any failure means: do not swap
        log(f"compact: ABORT -- the copy does not open cleanly through store.connect: {e!r}")
        return False, 0, 1
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
    return True, saved, 0


def _codec():
    """(compress argv, decompress argv): zstd when installed, else gzip. `.zst` / `.gz` is chosen by the caller."""
    if shutil.which("zstd"):
        return ["zstd", "-T0", "-3", "-q", "-c"], ["zstd", "-dc", "-q"], ".zst"
    return ["gzip", "-c"], ["gzip", "-dc"], ".gz"


def snapshot(db_path, out_path, log=print):
    """Write a consistent, compressed, verified copy of the database to `out_path`.

    Consistency: the copy is taken while this process holds the database's exclusive lock, right after
    a CHECKPOINT folded the WAL into the main file -- the same exclusive open `compact` uses. Nothing else
    can write (or even open) the file until the copy is done, so the bytes are one committed state.
    Verification: the source is hashed as it is fed to the compressor, then the compressed file is
    decompressed and hashed again; the two sha256 must match. Written to `<out>.part` and renamed, so a
    half-written file never has the final name. Returns 0 ok, 2 database busy (skip), 1 failed.
    """
    p, out = Path(db_path), Path(out_path)
    if not p.exists():
        log(f"snapshot: {db_path} does not exist")
        return 1
    busy = holders(db_path)
    if busy:
        log("snapshot: skipped -- another jobsearch process holds the database:")
        for l in busy:
            log("   " + l)
        return 2
    comp, decomp, _ = _codec()
    part = Path(str(out) + ".part")
    part.unlink(missing_ok=True)
    t0 = time.monotonic()
    try:
        con = duckdb.connect(str(p))   # exclusive: fails if anything still holds the file
    except duckdb.Error as e:
        log(f"snapshot: skipped -- database is locked ({e})")
        return 2
    try:
        con.execute("CHECKPOINT")
        wal = Path(str(p) + ".wal")
        if wal.exists() and wal.stat().st_size > 0:
            log(f"snapshot: ABORT -- WAL still has {wal.stat().st_size} bytes after CHECKPOINT")
            return 1
        src = hashlib.sha256()
        n = 0
        with open(part, "wb") as fo, open(p, "rb") as fi:
            proc = subprocess.Popen(comp, stdin=subprocess.PIPE, stdout=fo)
            while True:
                chunk = fi.read(8 * 1024 * 1024)
                if not chunk:
                    break
                src.update(chunk)
                n += len(chunk)
                proc.stdin.write(chunk)
            proc.stdin.close()
            rc = proc.wait()
        if rc != 0 or n != p.stat().st_size:
            log(f"snapshot: ABORT -- compressor rc={rc}, read {n} of {p.stat().st_size} bytes")
            part.unlink(missing_ok=True)
            return 1
    finally:
        con.close()
    # Verify outside the lock: decompress what was written and compare to the hash of what was read.
    chk = hashlib.sha256()
    m = 0
    with open(part, "rb") as fi:
        proc = subprocess.Popen(decomp, stdin=fi, stdout=subprocess.PIPE)
        while True:
            chunk = proc.stdout.read(8 * 1024 * 1024)
            if not chunk:
                break
            chk.update(chunk)
            m += len(chunk)
        rc = proc.wait()
    if rc != 0 or m != n or chk.digest() != src.digest():
        log(f"snapshot: ABORT -- verification failed (rc={rc}, {m} vs {n} bytes, hash match {chk.digest() == src.digest()})")
        part.unlink(missing_ok=True)
        return 1
    part.rename(out)
    log(f"snapshot : {_fmt(n)} -> {out.name} {_fmt(out.stat().st_size)} (verified sha256 {src.hexdigest()[:12]}) in {time.monotonic() - t0:.0f}s")
    return 0


def _rclone(*args, **kw):
    return subprocess.run(["rclone", *args, *os.environ.get("RCLONE_NET", "").split()], **kw)


def snapshot_remote(db_path, remote_file, log=print):
    """Stream a consistent, compressed copy straight to an rclone path (Drive): nothing is written locally.

    Same consistency as `snapshot` (exclusive open + CHECKPOINT, held while the copy streams). The source is hashed
    on its way into the compressor and the compressed stream on its way into `rclone rcat`. Verified twice: Drive's
    size + sha256 must equal the stream's, then the Drive copy is streamed back, decompressed and hashed, and must
    equal the source. Any mismatch deletes the Drive file. Returns 0 ok, 2 database busy, 3 upload failed (network:
    skip, retry next run), 1 verification failed.
    """
    import threading
    p = Path(db_path)
    if not p.exists():
        log(f"snapshot: {db_path} does not exist")
        return 1
    busy = holders(db_path)
    if busy:
        log("snapshot: skipped -- another jobsearch process holds the database:")
        for l in busy:
            log("   " + l)
        return 2
    comp, decomp, _ = _codec()
    t0 = time.monotonic()
    try:
        con = duckdb.connect(str(p))   # exclusive: fails if anything still holds the file
    except duckdb.Error as e:
        log(f"snapshot: skipped -- database is locked ({e})")
        return 2
    src, zsum, n, zn, feed_err = hashlib.sha256(), hashlib.sha256(), 0, 0, []
    try:
        con.execute("CHECKPOINT")
        wal = Path(str(p) + ".wal")
        if wal.exists() and wal.stat().st_size > 0:
            log(f"snapshot: ABORT -- WAL still has {wal.stat().st_size} bytes after CHECKPOINT")
            return 1
        zproc = subprocess.Popen(comp, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        up = subprocess.Popen(["rclone", "rcat", remote_file, *os.environ.get("RCLONE_NET", "").split()],
                              stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def feed():   # file -> compressor, hashing the source (a thread, so the pipes never deadlock)
            nonlocal n
            try:
                with open(p, "rb") as fi:
                    while chunk := fi.read(8 * 1024 * 1024):
                        src.update(chunk)
                        n += len(chunk)
                        zproc.stdin.write(chunk)
            except Exception as e:   # noqa: BLE001
                feed_err.append(e)
            finally:
                zproc.stdin.close()
        th = threading.Thread(target=feed)
        th.start()
        try:
            while chunk := zproc.stdout.read(8 * 1024 * 1024):   # compressor -> rclone, hashing the stream
                zsum.update(chunk)
                zn += len(chunk)
                up.stdin.write(chunk)
        except BrokenPipeError:
            pass
        th.join()
        up.stdin.close()
        zrc, urc = zproc.wait(), up.wait()
        uerr = up.stderr.read().decode(errors="replace").strip().splitlines()
    finally:
        con.close()
    if urc != 0:
        log(f"snapshot: upload to {remote_file} failed (network?): {uerr[-1][:200] if uerr else 'rclone exit ' + str(urc)}")
        _rclone("deletefile", remote_file, capture_output=True)
        return 3
    if feed_err or zrc != 0 or n != p.stat().st_size:
        log(f"snapshot: ABORT -- compressor rc={zrc}, read {n} of {p.stat().st_size} bytes {feed_err or ''}")
        _rclone("deletefile", remote_file, capture_output=True)
        return 1
    # Check 1: Drive's own size + sha256 equal the stream that was sent.
    meta = _rclone("lsjson", "--hash", "--hash-type", "sha256", remote_file, capture_output=True, text=True)
    try:
        d = json.loads(meta.stdout)[0]
        rsize, rsum = d["Size"], (d.get("Hashes") or {}).get("sha256", "")
    except (ValueError, IndexError, KeyError):
        rsize, rsum = None, ""
    if rsize != zn or rsum != zsum.hexdigest():
        log(f"snapshot: VERIFY FAILED -- Drive has size {rsize} sha256 {rsum[:12]}, sent {zn} {zsum.hexdigest()[:12]}; deleted")
        _rclone("deletefile", remote_file, capture_output=True)
        return 1
    # Check 2: stream the Drive copy back, decompress, and hash: must equal the live database's bytes.
    cat = subprocess.Popen(["rclone", "cat", remote_file, *os.environ.get("RCLONE_NET", "").split()],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    dproc = subprocess.Popen(decomp, stdin=cat.stdout, stdout=subprocess.PIPE)
    cat.stdout.close()
    chk, m = hashlib.sha256(), 0
    while chunk := dproc.stdout.read(8 * 1024 * 1024):
        chk.update(chunk)
        m += len(chunk)
    drc, crc = dproc.wait(), cat.wait()
    if drc or crc or m != n or chk.digest() != src.digest():
        log(f"snapshot: VERIFY FAILED -- round trip from Drive gave {m} of {n} bytes (rc {crc}/{drc}); deleted")
        _rclone("deletefile", remote_file, capture_output=True)
        return 1
    log(f"snapshot : {_fmt(n)} -> {remote_file} {_fmt(zn)} (verified on Drive: sha256 {zsum.hexdigest()[:12]}; "
        f"round trip = source {src.hexdigest()[:12]}) in {time.monotonic() - t0:.0f}s")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["report", "compact", "snapshot"],
                    help="report: sizes, free blocks, row counts (read-only); compact: rewrite the file without "
                         "free blocks (exclusive lock); snapshot: compressed, verified copy to --out or --remote")
    ap.add_argument("--db", default=DEFAULT_DB, help="DuckDB file (default: the repo's db/jobsearch.duckdb)")
    ap.add_argument("--out", help="snapshot: destination file (a compressed copy, e.g. x.duckdb.zst)")
    ap.add_argument("--remote", help="snapshot: stream to this rclone path instead (Drive; nothing written locally)")
    ap.add_argument("--force", action="store_true", help="compact even when under 10%% of blocks are free")
    ap.add_argument("--keep-old", action="store_true", help="keep the pre-compaction file as .precompact")
    a = ap.parse_args(argv)
    if a.cmd == "report":
        return 0 if report(a.db) is not None else 1
    if a.cmd == "snapshot":
        if a.remote:
            return snapshot_remote(a.db, a.remote)
        if not a.out:
            ap.error("snapshot needs --out or --remote")
        return snapshot(a.db, a.out)
    _, _, rc = compact(a.db, force=a.force, keep_old=a.keep_old)
    return rc


if __name__ == "__main__":
    sys.exit(main())
