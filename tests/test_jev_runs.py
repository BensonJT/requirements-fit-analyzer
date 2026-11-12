"""jev_runs: jev.run records one row per live run (store.record_jev_run), and scripts/backfill_jev_runs.py rebuilds
earlier runs from the launch logs. Fake data only; an in-memory-style temp DuckDB."""
import datetime as dt
import importlib.util
from pathlib import Path

from backend.ats import store
from backend.finder.jev_types import RunSummary

spec = importlib.util.spec_from_file_location(
    "backfill_jev_runs", Path(__file__).resolve().parents[1] / "scripts" / "backfill_jev_runs.py")
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

LOG = """launch: 2026-10-27 23:00:00  preset: sweep:|jevtop|maint
════ 23:10:00  .venv/bin/python -u finder.py sweep
════ 00:55:16  .venv/bin/python -u finder.py top --jev --background-file x.md
jev: abc failed (JevAPIError: status=402)
jev.run: RunSummary(considered=300, skipped_cached=0, reviewed=0, calls=300, input_tokens=0, errors=300, drift=0, canary_flags=0, stopped_by_cap=None, dry_run=False)
════ 02:00:00  .venv/bin/python -u finder.py jev run --eval-set --run-tag r2 --background-file x.md
jev.run: RunSummary(considered=150, skipped_cached=10, reviewed=100, reused=5, carried=0, calls=200, input_tokens=9, errors=0, drift=0, canary_flags=0, stopped_by_cap='daily_token_cap', dry_run=False, stopped_by_error=None)
"""


def test_parse_launch_log(tmp_path):
    (tmp_path / "launch_20261027_2300.log").write_text(LOG, encoding="utf-8")
    (tmp_path / "launch_now_x.out").write_text(LOG, encoding="utf-8")            # .out copies are ignored
    rows = bf.rows_from(tmp_path, dt.timezone.utc)
    assert len(rows) == 2
    a, b = rows
    assert a["started_at"] == dt.datetime(2026, 10, 28, 0, 55, 16, tzinfo=dt.timezone.utc)   # crossed midnight
    assert a["summary"].errors == 300 and a["summary"].not_reached == 0 and a["run_tag"] == ""
    assert b["run_tag"] == "r2" and b["to_review"] == 140 and b["summary"].not_reached == 35   # 140 - 100 - 5


def test_record_jev_run_replaces_its_own_row(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    s = RunSummary(considered=10, skipped_cached=2, reviewed=7, errors=1)
    t0 = dt.datetime(2026, 11, 12, 6, tzinfo=dt.timezone.utc)
    rid = store.record_jev_run(con, s, prompt_version="pv", run_tag="", to_review=8, started_at=t0,
                               finished_at=t0 + dt.timedelta(minutes=5))
    s.errors = 2
    store.record_jev_run(con, s, prompt_version="pv", run_tag="", to_review=8, started_at=t0, finished_at=None)
    got = con.execute("SELECT run_id, started_at, errors, source FROM jev_runs").fetchall()
    assert got == [(rid, dt.datetime(2026, 11, 12, 6), 2, "live")]
    con.close()
