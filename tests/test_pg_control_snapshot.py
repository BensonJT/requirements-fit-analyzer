"""Tests for scripts/pg_control_snapshot.py: the judge-test day rule and cut-short handling, the verdict and run
transforms, the upsert, and (when the reporting SQL is reachable) the control-chart limit math in the views.
Fake data only; Postgres tests use a throwaway database and skip when it is unreachable."""
import datetime as dt
import importlib.util
import math
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "pg_control_snapshot", Path(__file__).resolve().parents[1] / "scripts" / "pg_control_snapshot.py")
snap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snap)

TEST_DB = "resume_test_control"
PASS = "catch_rate(fails-or-partial)=0.88 (bar 0.7), agree_rate(meets-only)=0.90 (bar 0.85)"
CUT = "insufficient data: blind human Required calls judged n_catch=0, n_agree=0 (min 5), unjudged=121 of 121"


def t(run_id, when, passed, agree, catch=(20, 23), reason=PASS, pv="pv"):
    return dict(run_id=run_id, prompt_version=pv, evaluated_at=dt.datetime.fromisoformat(when), passed=passed,
                reason=reason, n_agree=agree[1], n_agree_hits=agree[0], n_catch=catch[1], n_catch_hits=catch[0])


# The shape of the real history: a failing day, a day with repeated passes, a cut-short test and an inherited one.
TESTS = [
    t("f1", "2026-09-29 20:00", False, (15, 29), reason="catch_rate=0.85, agree_rate=0.52"),
    t("f2", "2026-09-29 22:56", False, (23, 28), reason="catch_rate=0.86, agree_rate=0.82"),
    t("f3", "2026-09-30 00:24", False, (20, 31), reason="catch_rate=0.87, agree_rate=0.65"),
    t("p1", "2026-09-30 03:37", True, (26, 28)),
    t("p2", "2026-09-30 13:09", True, (26, 28)),
    t("p3", "2026-09-30 14:20", True, (26, 28)),
    t("p4", "2026-10-15 12:06", True, (26, 28)),
    t("p5", "2026-10-20 20:14", True, (25, 29)),
    t("p6", "2026-10-21 04:01", True, (26, 29)),
    t("p7", "2026-10-24 04:32", True, (25, 29)),
    t("p8", "2026-10-27 02:04", True, (25, 29)),
    t("cs", "2026-10-27 14:45", False, (0, 0), catch=(0, 0), reason=CUT),
    t("p9", "2026-10-28 13:38", True, (25, 29)),
    t("pa", "2026-10-30 06:07", True, (27, 31)),
    t("pb", "2026-11-01 20:19", True, (28, 31)),
    t("pc", "2026-11-02 12:07", True, (28, 31)),
    t("in", "2026-11-03 07:08", True, (28, 31), reason="inherited from pc (only the fact sheet changed)"),
]


def test_day_rule_reproduces_the_published_ten():
    st = snap.classify_tests(TESTS)
    passes = [x for x in TESTS if st[x["run_id"]] == "pass"]
    assert [x["n_agree_hits"] for x in passes] == [26, 26, 25, 26, 25, 25, 25, 27, 28, 28]
    assert sum(x["n_agree_hits"] for x in passes) == 261 and sum(x["n_agree"] for x in passes) == 294
    assert st["p3"] == "pass" and st["p1"] == st["p2"] == st["f3"] == "superseded"   # last pass of the day wins
    assert st["f2"] == "fail" and st["f1"] == "superseded"                            # no pass that day: last test
    assert st["cs"] == "cut short" and st["in"] == "inherited"


def test_cut_short_is_shown_never_scored():
    pts = {(p["check_id"], p["source_ref"]): p for p in snap.test_points(TESTS, "jev_required", {"pv": "m-1"})}
    cut = pts[("jev_required_agree", "cs")]
    assert cut["value"] is None and cut["scored"] is False and cut["status"] == "cut short"
    assert pts[("jev_required_catch", "cs")]["value"] is None
    assert pts[("jev_required_agree", "f2")]["scored"] is False                        # a failing test: no limits
    ok = pts[("jev_required_agree", "pc")]
    assert ok["scored"] and ok["value"] == round(28 / 31, 6) and ok["model"] == "m-1"
    assert ok["measured_at"].tzinfo == dt.timezone.utc


def test_verdict_and_run_transforms():
    v = snap.verdict_points_from([
        dict(run_id="r1", started_at=dt.datetime(2026, 11, 1, 5), n=10, meets=3, partial=2, fails=5, unanswered=1,
             drifted=0, model="m-1", prompt_version="pv", prompt_versions=2),
        dict(run_id="r2", started_at=dt.datetime(2026, 11, 2, 5), n=0, meets=0, partial=0, fails=0, unanswered=4,
             drifted=0, model=None, prompt_version=None, prompt_versions=0)])
    assert [p["check_id"] for p in v] == ["verdict_meets", "verdict_partial", "verdict_fails"]   # empty run dropped
    assert v[0]["value"] == 0.3 and v[0]["n"] == 10 and "2 prompt versions" in v[0]["note"]
    r = snap.run_points_from([
        dict(run_id="a", started_at=dt.datetime(2026, 11, 1), finished_at=dt.datetime(2026, 11, 1, 1),
             boards_attempted=200, boards_failed=4, elapsed_seconds=900),
        dict(run_id="b", started_at=dt.datetime(2026, 11, 2), finished_at=None,
             boards_attempted=200, boards_failed=0, elapsed_seconds=None)])
    assert [(p["check_id"], p["value"]) for p in r] == [("run_boards_failed", 0.02), ("run_pull_minutes", 15.0)]


# ---------------------------------------------------------------- Postgres: upsert and the view math

def _reporting_dir():
    d = os.environ.get("JOBSEARCH_VAULT_DIR")
    if not d:
        env = Path(__file__).resolve().parents[1] / ".env"
        for line in env.read_text().splitlines() if env.exists() else []:
            if line.startswith("JOBSEARCH_VAULT_DIR="):
                d = line.split("=", 1)[1].strip().strip('"')
    p = Path(d) / "Tools" / "reporting" if d else None
    return p if p and (p / "control_schema.sql").exists() and (p / "control_views.sql").exists() else None


@pytest.fixture()
def pg():
    psycopg = pytest.importorskip("psycopg")
    from backend.tracker import db
    rep = _reporting_dir()
    if rep is None:
        pytest.skip("control_schema.sql / control_views.sql not found (JOBSEARCH_VAULT_DIR unset)")
    try:
        admin = db.connect("postgres")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres unreachable: {e}")
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
    c = db.connect(TEST_DB)
    c.execute("CREATE SCHEMA reporting")
    for name in ("control_schema.sql", "control_views.sql"):
        c.execute((rep / name).read_text(encoding="utf-8"))
    yield c
    c.close()
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.close()


def test_upsert_is_idempotent_and_p_limits_match_the_article(pg):
    pts = snap.test_points(TESTS, "jev_required")
    assert snap.load(pg, pts) == (len(pts), 0, 0)
    assert snap.load(pg, pts) == (0, 0, len(pts))
    pts[-3]["note"] = "changed"
    assert snap.load(pg, pts) == (0, 1, len(pts) - 1)

    rows = pg.execute("SELECT * FROM reporting.control_p WHERE check_id = 'jev_required_agree' AND shown "
                      "ORDER BY seq").fetchall()
    assert len(rows) == 12                                          # 10 passes + the failing day + the cut-short test
    pbar = 261 / 294
    assert float(rows[0]["p_bar"]) == pytest.approx(pbar)
    at29 = next(r for r in rows if r["n"] == 29)
    assert float(at29["lcl"]) == pytest.approx(pbar - 3 * math.sqrt(pbar * (1 - pbar) / 29))   # ~0.712
    assert float(at29["ucl"]) == 1.0                                                          # clamped
    cut = next(r for r in rows if r["status"] == "cut short")
    assert cut["lcl"] is None and cut["ucl"] is None and cut["out_of_limits"] is None and cut["seq"] is not None
    st = pg.execute("SELECT * FROM reporting.control_status WHERE check_id = 'jev_required_agree'").fetchone()
    assert st["status"] == "in control" and st["scored_points"] == 10 and st["signals"] == 0


def test_xmr_limits(pg):
    vals = [10, 12, 11, 30, 11]
    pts = [snap.point("run_pull_minutes", f"r{i}", dt.datetime(2026, 11, 1 + i), value=v) for i, v in enumerate(vals)]
    snap.load(pg, pts)
    rows = pg.execute("SELECT * FROM reporting.control_xmr WHERE check_id = 'run_pull_minutes' ORDER BY seq").fetchall()
    mean, mr = sum(vals) / 5, (2 + 1 + 19 + 19) / 4
    assert float(rows[0]["mean"]) == pytest.approx(mean) and float(rows[0]["mr_bar"]) == pytest.approx(mr)
    assert float(rows[0]["ucl"]) == pytest.approx(mean + 2.66 * mr)
    assert float(rows[0]["lcl"]) == 0                                   # mean - 2.66 * MR-bar < 0, clamped
    st = pg.execute("SELECT status FROM reporting.control_status WHERE check_id = 'run_pull_minutes'").fetchone()
    assert st["status"] == "in control"
