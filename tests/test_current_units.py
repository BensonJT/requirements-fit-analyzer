"""requirements.current_units_sql: one splitter per posting at the current JD hash (2026-10-20). requirement_units
keeps every hash and every splitter a JD was ever split with; readers that filtered on the hash alone saw each line
twice. No network, temp DuckDB files, synthetic rows only."""
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import store  # noqa: E402
from backend.finder import required_embed  # noqa: E402
from backend.finder import requirements as R  # noqa: E402


def _posting(con, pid, dhash):
    now = datetime(2026, 10, 1)
    con.execute("""INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary,
        status, description_hash, first_seen_at, last_seen_at)
        VALUES (?, 'Acme', 'greenhouse', ?, 'Role', ?, 'Remote', 'active', ?, ?, ?)""",
                [pid, pid, f"https://x/{pid}", dhash, now, now])


def _unit(con, pid, dhash, splitter, ord_, text, when, grp="required"):
    con.execute("""INSERT INTO requirement_units (posting_id, description_hash, splitter, ord, unit_hash, text,
        section, grp, weight, klass, spec, model, vector, embedded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'work', NULL, 'm', NULL, ?)""",
                [pid, dhash, splitter, ord_, f"{splitter}{ord_}", text, grp, grp, when])


def _texts(con, pid):
    return [r[0] for r in con.execute(f"SELECT text FROM ({R.current_units_sql('u.posting_id = ?')}) ORDER BY ord",
                                      [pid]).fetchall()]


def test_current_units_reads_one_splitter_at_the_current_hash(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    cur, old = R.splitter_fingerprint(), "oldsplitter0"
    _posting(con, "a", "h2")
    _unit(con, "a", "h1", cur, 0, "old JD line", datetime(2026, 9, 1))          # a hash the JD no longer has
    _unit(con, "a", "h2", old, 0, "line one", datetime(2026, 9, 15))            # the same JD, an older split
    _unit(con, "a", "h2", old, 1, "line two", datetime(2026, 9, 15))
    _unit(con, "a", "h2", cur, 0, "line one", datetime(2026, 9, 30))
    _unit(con, "a", "h2", cur, 1, "line two", datetime(2026, 9, 30))
    assert _texts(con, "a") == ["line one", "line two"]
    con.close()


def test_current_units_falls_back_to_the_newest_stored_splitter(tmp_path):
    """A posting the current splitter never reached keeps its newest split rather than losing every line."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "b", "h")
    _unit(con, "b", "h", "oldest000000", 0, "from the oldest split", datetime(2026, 9, 1))
    _unit(con, "b", "h", "newer0000000", 0, "from the newer split", datetime(2026, 9, 20))
    assert _texts(con, "b") == ["from the newer split"]
    con.close()


def test_required_embed_seen_units_no_longer_double_counts(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    cur = R.splitter_fingerprint()
    _posting(con, "c", "h")
    for i in range(20):
        _unit(con, "c", "h", "oldsplitter0", i, f"line {i}", datetime(2026, 9, 15))
        _unit(con, "c", "h", cur, i, f"line {i}", datetime(2026, 9, 30))
    seen = required_embed._seen_units(con, ["c"])["c"]
    assert len(seen) == required_embed.UNITS_PER_POSTING
    assert len({u["text"] for u in seen}) == required_embed.UNITS_PER_POSTING   # 18 distinct lines, not 9 x 2
    con.close()
