"""scripts/db_maintenance.py: the compaction must keep every row, keep the schema store.connect
expects, and actually shrink a file that has free blocks."""
import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "db_maintenance", Path(__file__).resolve().parents[1] / "scripts" / "db_maintenance.py")
dbm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dbm)


def _bloated_db(path):
    """A DB with real rows plus a dropped bulk table, so the file carries free blocks."""
    con = store.connect(path)
    # Bulk FIRST, live rows AFTER, then drop the bulk: the freed blocks sit in the middle of the
    # file, where a checkpoint cannot truncate them away -- the shape of a week of rescreens.
    # random() is incompressible; repeat('x', N) compresses to nothing and frees no block at all.
    con.execute("CREATE TABLE bulk AS SELECT random() AS a, random() AS b, random() AS c FROM range(1000000)")
    con.execute("CHECKPOINT")
    jobs = [N.base(req_id=f"R{i}", title="Process Excellence Lead", description_text="lean " * 200) for i in range(300)]
    store.record_board(con, "Acme", "greenhouse", jobs, datetime(2026, 9, 23))
    con.execute("CHECKPOINT")
    con.execute("DROP TABLE bulk")
    con.execute("CHECKPOINT")
    con.close()


def test_compact_keeps_rows_and_shrinks(tmp_path):
    db = str(tmp_path / "t.duckdb")
    _bloated_db(db)
    before = dbm.report(db, log=lambda *_: None)
    assert before["free_fraction"] >= dbm.COMPACT_MIN_FREE_FRACTION, before
    size_before = os.path.getsize(db)

    lines = []
    did, saved = dbm.compact(db, log=lines.append)
    assert did and saved > 0
    assert os.path.getsize(db) < size_before
    assert not os.path.exists(db + ".precompact") and not os.path.exists(db + ".compacting")

    con = store.connect(db)  # views + schema version still fine on the swapped-in file
    assert con.execute("SELECT count(*) FROM postings").fetchone()[0] == 300
    assert con.execute("SELECT count(*) FROM vw_active").fetchone()[0] == 300
    assert con.execute("SELECT version FROM schema_info").fetchone()[0] == store.SCHEMA_VERSION
    con.close()
    after = dbm.report(db, log=lambda *_: None)
    assert after["counts"] == before["counts"]
    assert after["free_fraction"] < before["free_fraction"]


def test_compact_skips_under_threshold_unless_forced(tmp_path):
    db = str(tmp_path / "t.duckdb")
    con = store.connect(db)
    store.record_board(con, "Acme", "greenhouse", [N.base(req_id="A", title="Process Lead")], datetime(2026, 9, 23))
    con.close()
    lines = []
    did, _ = dbm.compact(db, log=lines.append)
    assert not did and any("skipped" in l for l in lines)
    did, _ = dbm.compact(db, force=True, keep_old=True, log=lines.append)
    assert did and os.path.exists(db + ".precompact")
    assert duckdb.connect(db, read_only=True).execute("SELECT count(*) FROM postings").fetchone()[0] == 1
