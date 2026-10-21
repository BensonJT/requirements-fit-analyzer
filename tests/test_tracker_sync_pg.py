"""Tests for the PostgreSQL source of tracker_sync (exact matching only). Throwaway database
resume_test_wp5, skipped when Postgres is unreachable. Fake data only."""
import pytest

psycopg = pytest.importorskip("psycopg")
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store as ats_store  # noqa: E402
from backend.finder import tracker_sync as T  # noqa: E402
from backend.tracker import db, store  # noqa: E402

TEST_DB = "resume_test_wp5"
WHEN = __import__("datetime").datetime(2026, 9, 1)


def _quiet(*_a, **_k):
    pass


@pytest.fixture(scope="module")
def pgc():
    try:
        admin = db.connect("postgres")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres unreachable: {e}")
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
    c = db.connect(TEST_DB)
    db.migrate(c)
    yield c
    c.close()
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.close()


@pytest.fixture(autouse=True)
def clean(pgc):
    pgc.execute("DELETE FROM tracker.application")


@pytest.fixture
def duck(tmp_path):
    con = ats_store.connect(str(tmp_path / "t.duckdb"))
    jobs = [N.base(req_id="R100", title="Operations Lead"), N.base(req_id="R200", title="Operations Lead"),
            N.base(req_id="DUP", title="Analyst A"), N.base(req_id="DUP", title="Analyst B", url="https://x/other")]
    ats_store.record_board(con, "Acme", "greenhouse", jobs[:2], WHEN)
    ats_store.record_board(con, "Acme", "lever", jobs[2:3], WHEN)
    ats_store.record_board(con, "Acme Corp", "workday", jobs[3:], WHEN)
    ids = {j["req_id"] + str(i): ats_store.posting_id(e, p, j["req_id"])
           for i, (e, p, j) in enumerate([("Acme", "greenhouse", jobs[0]), ("Acme", "greenhouse", jobs[1])])}
    return con, {"R100": ids["R1000"], "R200": ids["R2001"]}


def _add(pgc, **kw):
    kw.setdefault("company", "Acme")
    kw.setdefault("role", "Operations Lead")
    return store.add_application(pgc, actor="tester", **kw)


def _mirror(con):
    return {r[0]: r[1:] for r in con.execute(
        "SELECT role, section, status, matched_posting_id, match_kind FROM tracker").fetchall()}


def test_posting_id_and_req_id_match(pgc, duck):
    con, ids = duck
    _add(pgc, role="By Pid", posting_id=ids["R100"], status="applied", status_date="2026-09-02")
    _add(pgc, role="By Req", employer_job_id="r200", status="built")
    _add(pgc, role="Stale Pid", posting_id="f" * 20, employer_job_id="R100", status="screen", status_date="2026-09-03")
    counts = T.sync(con, "unused", log=_quiet, source="postgres", pg_conn=pgc)
    assert counts["source"] == "postgres" and (counts["rows"], counts["posting_id"], counts["req_id"]) == (3, 1, 2)
    m = _mirror(con)
    assert m["By Pid"] == ("Active", "applied", ids["R100"], "posting_id")
    assert m["By Req"] == ("Active", "built", ids["R200"], "req_id")
    assert m["Stale Pid"][2:] == (ids["R100"], "req_id")


def test_ambiguous_req_matches_none(pgc, duck):
    con, _ = duck
    _add(pgc, role="Dup", employer_job_id="DUP")
    counts = T.sync(con, "unused", log=_quiet, source="postgres", pg_conn=pgc)
    assert (counts["none"], counts["ambiguous"], counts["decisions_added"]) == (1, 1, 0)
    assert _mirror(con)["Dup"][2:] == (None, "none")


def test_no_fuzzy_title_match_and_wrong_company_never_matches(pgc, duck):
    con, _ = duck
    _add(pgc, role="Operations Lead")                                    # identical title, no ids
    _add(pgc, role="Other Co", company="Globex", employer_job_id="R100")  # id exists, other employer
    counts = T.sync(con, "unused", log=_quiet, source="postgres", pg_conn=pgc)
    assert counts["none"] == 2 and counts["fuzzy"] == 0
    assert {v[3] for v in _mirror(con).values()} == {"none"}


@pytest.mark.parametrize("status,applied,decision", [
    ("built", None, "hold"), ("passed", None, "pass"), ("closed-unsent", None, "pass"),
    ("withdrawn", None, "pass"), ("withdrawn", "2026-09-05", "build"),
    ("declined", None, "pass"), ("declined", "2026-09-05", "build"), ("applied", "2026-09-05", "build"),
    ("rejected", "2026-09-05", "build"), ("assumed-rejected", "2026-09-05", "build"),
    ("interview", "2026-09-05", "build"), ("offer", "2026-09-05", "build"), ("passed", "2026-09-05", "pass")])
def test_decision_for_pg(status, applied, decision):
    row = T.TrackerRow("Active", applied or "", "Acme", "R", status)
    assert T.decision_for_pg(row)[0] == decision


def test_sync_records_decision_section_and_best_row_wins(pgc, duck):
    con, ids = duck
    _add(pgc, role="Held", employer_job_id="R100", status="built")
    _add(pgc, role="Closed", employer_job_id="R200", status="rejected", date_applied="2026-09-01",
         status_date="2026-09-10")
    counts = T.sync(con, "unused", log=_quiet, source="postgres", pg_conn=pgc)
    assert counts["decisions_added"] == 2
    d = dict(con.execute("SELECT posting_id, decision FROM decisions WHERE source = 'tracker'").fetchall())
    assert d == {ids["R100"]: "hold", ids["R200"]: "build"}
    assert _mirror(con)["Closed"][0] == "Closed"
    # a second row for the same posting that is applied outranks the hold
    pgc.execute("UPDATE tracker.application SET employer_job_id = 'R100' WHERE role = 'Closed'")
    T.sync(con, "unused", log=_quiet, source="postgres", pg_conn=pgc)
    assert dict(con.execute("SELECT posting_id, decision FROM decisions WHERE source = 'tracker' "
                            "ORDER BY decided_at").fetchall())[ids["R100"]] == "build"


def test_env_switch_and_unreachable_raises(monkeypatch, duck):
    con, _ = duck
    monkeypatch.setenv("TRACKER_SOURCE", "postgres")
    monkeypatch.setenv("TRACKER_PGDATABASE", "resume_test_wp5_missing_db")
    monkeypatch.setenv("PGPORT", "1")
    with pytest.raises(T.TrackerSourceError):
        T.sync(con, "unused", log=_quiet)
    monkeypatch.setenv("TRACKER_SOURCE", "bogus")
    with pytest.raises(T.TrackerSourceError):
        T.sync(con, "unused", log=_quiet)
    assert con.execute("SELECT count(*) FROM tracker").fetchone()[0] == 0


def test_unset_env_uses_markdown(tmp_path, duck, monkeypatch):
    con, _ = duck
    monkeypatch.delenv("TRACKER_SOURCE", raising=False)
    counts = T.sync(con, str(tmp_path), log=_quiet)       # no markdown file: the old skipped result
    assert counts == {"rows": 0, "exact": 0, "fuzzy": 0, "none": 0}
