"""`--db` must work whether it's given BEFORE or AFTER a sub-action, for every command that has one --
today: `feedback`'s nested sheet/sheet-import/ingest subparsers (finder.py, ~line 740). Runs finder.py as a
real subprocess (mirrors tests/test_finder.py's own subprocess pattern) so this exercises argparse's actual
parent/child namespace behaviour, not a stand-in. `judge2` and `required-embed` take a plain `action` CHOICE
positional, not a nested subparser, so `--db` was never broken there -- covered here anyway per the task.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FINDER = os.path.join(ROOT, "finder.py")


def _run(args, cwd):
    return subprocess.run([sys.executable, FINDER] + args, cwd=cwd, capture_output=True, text=True, timeout=60)


def _db_used(res, expected_path):
    """The DB file at `expected_path` was created (store.connect always creates/migrates it)."""
    return os.path.exists(expected_path)


# ---------------------------------------------------------------- feedback (nested sub-subparsers)

def test_feedback_db_before_subaction(tmp_path):
    db = str(tmp_path / "before.duckdb")
    res = _run(["feedback", "--db", db, "ingest", "nofile.csv", "--basis", "seen", "--dry-run"], tmp_path)
    assert res.returncode in (0, 1)  # 1 = "not ok" (bad file), still ran against the right db
    assert os.path.exists(db)


def test_feedback_db_after_subaction(tmp_path):
    """The bug this fixes: --db given AFTER the sub-action used to be an argparse error."""
    db = str(tmp_path / "after.duckdb")
    res = _run(["feedback", "ingest", "nofile.csv", "--basis", "seen", "--dry-run", "--db", db], tmp_path)
    assert "unrecognized arguments" not in res.stderr
    assert res.returncode in (0, 1)
    assert os.path.exists(db)


def test_feedback_db_given_at_neither_falls_back_to_default(tmp_path):
    """No --db anywhere -- the default (db/jobsearch.duckdb under REPO) is used, not an error, and the
    inner sub-subparser's SUPPRESS default must not blow up when nothing was given at all."""
    res = _run(["feedback", "ingest", "nofile.csv", "--basis", "seen", "--dry-run"], tmp_path)
    assert "unrecognized arguments" not in res.stderr
    assert "error" not in res.stderr.lower() or "refused" in res.stdout.lower()


def test_feedback_db_given_at_outer_only_reaches_the_inner_command(tmp_path):
    """The argparse trap this guards against: an inner sub-subparser with a plain (non-SUPPRESS) --db default
    would silently overwrite an outer-given value with None once the inner parser's own defaults apply."""
    db = str(tmp_path / "outer_only.duckdb")
    res = _run(["feedback", "--db", db, "sheet-import", "nofile.csv"], tmp_path)
    assert "unrecognized arguments" not in res.stderr
    assert os.path.exists(db), "the inner sheet-import subparser must not have overwritten --db with None"


def test_feedback_sheet_subaction_accepts_db_in_both_positions(tmp_path):
    # --out pinned into tmp_path: blind_sheet.generate_sheet's own default output dir is anchored to the
    # REPO's db/ (store.DEFAULT_DB_PATH), not cwd or --db, so leaving --out off here would write a sheet
    # into the shared worktree instead of this test's own tmp_path.
    db1 = str(tmp_path / "sheet_before.duckdb")
    out1 = str(tmp_path / "sheet1.csv")
    res1 = _run(["feedback", "--db", db1, "sheet", "--n", "1", "--out", out1], tmp_path)
    assert "unrecognized arguments" not in res1.stderr
    assert os.path.exists(db1)

    db2 = str(tmp_path / "sheet_after.duckdb")
    out2 = str(tmp_path / "sheet2.csv")
    res2 = _run(["feedback", "sheet", "--n", "1", "--out", out2, "--db", db2], tmp_path)
    assert "unrecognized arguments" not in res2.stderr
    assert os.path.exists(db2)


# ---------------------------------------------------------------- judge2 (plain `action` choice, not nested)

def test_judge2_db_before_and_after_action(tmp_path):
    db1 = str(tmp_path / "j2_before.duckdb")
    res1 = _run(["judge2", "--db", db1, "status"], tmp_path)
    assert "unrecognized arguments" not in res1.stderr
    assert os.path.exists(db1)

    db2 = str(tmp_path / "j2_after.duckdb")
    res2 = _run(["judge2", "status", "--db", db2], tmp_path)
    assert "unrecognized arguments" not in res2.stderr
    assert os.path.exists(db2)


def test_judge2_db_given_at_neither_and_at_outer_only(tmp_path):
    res_neither = _run(["judge2", "status"], tmp_path)
    assert "unrecognized arguments" not in res_neither.stderr

    db = str(tmp_path / "j2_outer.duckdb")
    res_outer = _run(["judge2", "--db", db, "status"], tmp_path)
    assert "unrecognized arguments" not in res_outer.stderr
    assert os.path.exists(db)


def test_required_embed_db_before_and_after_action(tmp_path):
    """required-embed also takes a plain `action` choice; confirms --db works in both positions there too."""
    db1 = str(tmp_path / "re_before.duckdb")
    res1 = _run(["required-embed", "--db", db1, "score"], tmp_path)
    assert "unrecognized arguments" not in res1.stderr
    assert os.path.exists(db1)

    db2 = str(tmp_path / "re_after.duckdb")
    res2 = _run(["required-embed", "score", "--db", db2], tmp_path)
    assert "unrecognized arguments" not in res2.stderr
    assert os.path.exists(db2)
