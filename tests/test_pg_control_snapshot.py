"""Tests for scripts/pg_control_snapshot.py: one point per judge version, cut-short handling, the verdict and run
transforms, the upsert, and (when the reporting SQL is reachable) the control-chart limit math in the views (p, Laney p' and XmR).
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


# The shape of the real history: versions that never passed, a version tested several times, an eleventh earlier
# passing version, a cut-short test inside a version that later passed, and an inherited result.
TESTS = [
    t("f1", "2026-09-29 05:20", False, (15, 31), reason="catch_rate=0.63, agree_rate=0.48", pv="v307"),
    t("f2", "2026-09-29 19:48", False, (15, 29), reason="catch_rate=0.89, agree_rate=0.52", pv="v4a6"),
    t("f3", "2026-09-29 22:56", False, (23, 28), reason="catch_rate=0.86, agree_rate=0.82", pv="v4a6"),
    t("f4", "2026-09-30 00:24", False, (20, 31), reason="catch_rate=0.87, agree_rate=0.65", pv="vd1d"),
    t("p0a", "2026-09-30 03:37", True, (26, 28), catch=(21, 24), pv="v5f7"),
    t("p0b", "2026-09-30 14:01", True, (26, 28), pv="v5f7"),
    t("p1", "2026-09-30 14:20", True, (26, 28), pv="v0f3"),
    t("p2", "2026-10-15 12:06", True, (26, 28), pv="v7fd"),
    t("p3", "2026-10-20 20:14", True, (25, 29), pv="vc81"),
    t("p4", "2026-10-21 04:01", True, (26, 29), pv="v1b3"),
    t("p5", "2026-10-24 04:32", True, (25, 29), pv="v0cc"),
    t("p6", "2026-10-27 02:04", True, (25, 29), pv="vc08"),
    t("cs", "2026-10-27 14:45", False, (0, 0), catch=(0, 0), reason=CUT, pv="v0e2"),
    t("p7", "2026-10-28 13:38", True, (25, 29), pv="v0e2"),
    t("p8", "2026-10-30 06:07", True, (27, 31), pv="v4c7"),
    t("p9", "2026-11-01 20:19", True, (28, 31), pv="vd85"),
    t("pa", "2026-11-02 12:07", True, (28, 31), pv="ve8b"),
    t("in", "2026-11-03 07:08", True, (28, 31), reason="inherited from ve8b (only the fact sheet changed)", pv="vcf1"),
]


def test_one_point_per_judge_version():
    st = snap.classify_tests(TESTS)
    passes = [x for x in TESTS if st[x["run_id"]] == "pass"]
    assert len(passes) == 11 and st["p0b"] == "pass" and st["p0a"] == "superseded"   # last test of the version
    ten = passes[-10:]                                                                 # the published ten
    assert [x["n_agree_hits"] for x in ten] == [26, 26, 25, 26, 25, 25, 25, 27, 28, 28]
    assert sum(x["n_agree_hits"] for x in ten) == 261 and sum(x["n_agree"] for x in ten) == 294
    assert st["f1"] == st["f3"] == st["f4"] == "fail" and st["f2"] == "superseded"     # never passed: last test
    assert st["cs"] == "cut short" and st["p7"] == "pass"                              # cut short is its own mark
    assert st["in"] == "inherited"


def test_cut_short_is_shown_never_scored():
    pts = {(p["check_id"], p["source_ref"]): p for p in snap.test_points(TESTS, "jev_required", {"ve8b": None})}
    cut = pts[("jev_required_agree", "cs")]
    assert cut["value"] is None and cut["scored"] is False and cut["status"] == "cut short"
    assert pts[("jev_required_catch", "cs")]["value"] is None
    assert pts[("jev_required_agree", "f3")]["scored"] is False                        # a failing version: no limits
    ok = pts[("jev_required_agree", "pa")]
    assert ok["scored"] and ok["value"] == round(28 / 31, 6) and ok["model"] is None
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
    need = ("control_schema.sql", "control_views.sql", "control_plan.csv", "control_plan.py")
    return p if p and all((p / n).exists() for n in need) else None


def _plan_sql(rep):
    s = importlib.util.spec_from_file_location("control_plan", rep / "control_plan.py")
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m.plan_sql(rep / "control_plan.csv")


@pytest.fixture()
def pg():
    psycopg = pytest.importorskip("psycopg")
    from backend.tracker import db
    rep = _reporting_dir()
    if rep is None:
        pytest.skip("control_schema.sql / control_views.sql / control_plan.csv not found (JOBSEARCH_VAULT_DIR unset)")
    try:
        admin = db.connect("postgres")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres unreachable: {e}")
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
    c = db.connect(TEST_DB)
    c.execute("CREATE SCHEMA reporting")
    c.execute((rep / "control_schema.sql").read_text(encoding="utf-8"))
    c.execute(_plan_sql(rep))
    c.execute((rep / "control_views.sql").read_text(encoding="utf-8"))
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
    assert len(rows) == 15                     # 11 passing versions + 3 that never passed + the cut-short test
    pbar = 287 / 322
    assert float(rows[0]["p_bar"]) == pytest.approx(pbar)
    at29 = next(r for r in rows if r["n"] == 29)
    assert float(at29["lcl"]) == pytest.approx(pbar - 3 * math.sqrt(pbar * (1 - pbar) / 29))   # ~0.718
    assert float(at29["ucl"]) == 1.0                                                          # clamped
    cut = next(r for r in rows if r["status"] == "cut short")
    assert cut["lcl"] is None and cut["ucl"] is None and cut["out_of_limits"] is None and cut["seq"] is not None
    st = pg.execute("SELECT * FROM reporting.control_status WHERE check_id = 'jev_required_agree'").fetchone()
    assert st["status"] == "in control" and st["scored_points"] == 11 and st["signals"] == 0
    assert st["last_signal_at"] is None


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


def test_laney_limits(pg):
    """verdict_meets is a Laney p' chart in the plan: limits = p-bar +/- 3 * sigma_p * sigma_z."""
    data = [(100, 30), (100, 50), (100, 20), (100, 40), (100, 35)]
    pts = [snap.point("verdict_meets", f"r{i}", dt.datetime(2026, 11, 1 + i), n=n, k=k, value=k / n)
           for i, (n, k) in enumerate(data)]
    snap.load(pg, pts)
    rows = pg.execute("SELECT * FROM reporting.control_p WHERE check_id = 'verdict_meets' ORDER BY seq").fetchall()
    pbar = 175 / 500
    sp = math.sqrt(pbar * (1 - pbar) / 100)
    z = [(k / n - pbar) / sp for n, k in data]
    sz = sum(abs(b - a) for a, b in zip(z, z[1:])) / 4 / 1.128
    assert rows[0]["chart_type"] == "laney_p" and float(rows[0]["sigma_z"]) == pytest.approx(sz)
    assert float(rows[0]["lcl"]) == pytest.approx(max(0, pbar - 3 * sp * sz))
    assert float(rows[0]["ucl"]) == pytest.approx(min(1, pbar + 3 * sp * sz))
    assert not any(r["out_of_limits"] for r in rows)          # a plain p-chart would flag 0.50 and 0.20
    assert pbar + 3 * sp < 0.50 and pbar - 3 * sp > 0.20


# ---------------------------------------------------------------- Phase 2 (2026-11-12)

def test_jev_run_points():
    pts = snap.jev_run_points_from([
        dict(run_id="s1", started_at=dt.datetime(2026, 10, 28, 4), jev_runs=1, to_review=300, errors=300,
             not_reached=0, caps=None, account_errors=None),
        dict(run_id="s2", started_at=dt.datetime(2026, 10, 30, 4), jev_runs=2, to_review=234, errors=0,
             not_reached=130, caps="daily_token_cap", account_errors=None),
        dict(run_id="s3", started_at=dt.datetime(2026, 10, 31, 4), jev_runs=1, to_review=0, errors=0,
             not_reached=0, caps=None, account_errors=None)])
    got = {(p["check_id"], p["source_ref"]): p for p in pts}
    assert len(pts) == 4                                                    # nothing to review: no point
    assert got[("run_jev_failed", "s1")]["value"] == 1.0
    assert got[("run_jev_cut_short", "s2")]["k"] == 130 and "daily token cap" in got[("run_jev_cut_short", "s2")]["note"]
    assert got[("run_jev_failed", "s2")]["value"] == 0.0


def test_verdict_limits_restart_at_each_judge_version(pg):
    """verdict_meets restarts at each prompt_version and draws limits only once a version has 5 runs."""
    data = [("a", 100, 30), ("a", 100, 34), ("a", 100, 28),                      # 3 runs: no limits
            ("b", 100, 60), ("b", 100, 55), ("b", 100, 65), ("b", 100, 58), ("b", 100, 62)]
    pts = [snap.point("verdict_meets", f"r{i}", dt.datetime(2026, 11, 1) + dt.timedelta(days=i), n=n, k=k,
                      value=k / n, prompt_version=pv) for i, (pv, n, k) in enumerate(data)]
    snap.load(pg, pts)
    rows = pg.execute("SELECT * FROM reporting.control_p WHERE check_id = 'verdict_meets' ORDER BY seq").fetchall()
    assert [r["phase"] for r in rows] == ["a"] * 3 + ["b"] * 5
    assert all(r["ucl"] is None and r["out_of_limits"] is None for r in rows[:3])
    assert float(rows[3]["p_bar"]) == pytest.approx(300 / 500)                 # its own center, not the pooled one
    assert all(r["lcl"] is not None for r in rows[3:])
    st = pg.execute("SELECT status FROM reporting.control_status WHERE check_id = 'verdict_meets'").fetchone()
    assert st["status"] == "in control"


def test_run_rules(pg):
    """run_pull_minutes uses the Western Electric rules: 8 in a row above the center signals with no point
    outside the limits; run_boards_failed ('limits') never flags a run."""
    base = [25, 5, 25, 5, 25, 5, 25, 5, 25, 5]                                  # wide moving ranges; ends below
    tail = [16, 17, 16, 17, 16, 17, 16, 17]                                     # 8 just above the center
    vals = base + tail
    pts = [snap.point("run_pull_minutes", f"r{i:02d}", dt.datetime(2026, 10, 1) + dt.timedelta(days=i), value=v)
           for i, v in enumerate(vals)]
    pts += [snap.point("run_boards_failed", f"r{i:02d}", dt.datetime(2026, 10, 1) + dt.timedelta(days=i), n=100,
                       k=0 if i < 17 else 1, value=0.0 if i < 17 else 0.01) for i in range(18)]
    snap.load(pg, pts)
    rules = pg.execute("SELECT * FROM reporting.control_rules WHERE check_id = 'run_pull_minutes' ORDER BY measured_at").fetchall()
    assert not any(r["rule1"] for r in rules)
    assert rules[-1]["rule4"] and rules[-1]["signal"] and "8 points in a row" in rules[-1]["rules"]
    assert not any(r["rule4"] for r in rules[:-1])
    st = pg.execute("SELECT * FROM reporting.control_status WHERE check_id = 'run_pull_minutes'").fetchone()
    assert st["status"] == "signal" and st["outside_limits"] == 0 and st["signals"] == 1
    bf = pg.execute("SELECT count(*) FILTER (WHERE rule4) AS r4 FROM reporting.control_rules "
                    "WHERE check_id = 'run_boards_failed'").fetchone()
    assert bf["r4"] == 0                                                        # 17 zeros below the center: no flag


def test_restudy_due(pg):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)
    snap.load(pg, [
        snap.point("regrade_agreement", "msa1", old, n=60, k=55, value=55 / 60),
        snap.point("jev_required_agree", "t1", dt.datetime(2026, 11, 1), n=31, k=28, value=28 / 31, model="m-1",
                   status="pass"),
        snap.point("verdict_meets", "r1", dt.datetime(2026, 11, 2), n=100, k=30, value=0.3, model="m-2")])
    due = {r["check_id"]: r for r in pg.execute("SELECT * FROM reporting.control_restudy").fetchall()}
    assert due["regrade_agreement"]["due"] and "180 days" in due["regrade_agreement"]["due_reason"]
    assert due["jev_required_agree"]["due"] and "m-2" in due["jev_required_agree"]["due_reason"]
