"""Tests for scripts/tracker_migrate.py. Throwaway database; skipped when Postgres is unreachable. Fake data only."""
import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
duckdb = pytest.importorskip("duckdb")
from backend.tracker import db  # noqa: E402

_spec = importlib.util.spec_from_file_location("tracker_migrate", Path(__file__).resolve().parents[1] / "scripts" / "tracker_migrate.py")
M = importlib.util.module_from_spec(_spec)
sys.modules["tracker_migrate"] = M
_spec.loader.exec_module(M)

TEST_DB = "resume_test_wp3"
PID_CELL = "aaaaaaaaaaaaaaaaaaaa"
PID_EXACT = "bbbbbbbbbbbbbbbbbbbb"
PID_REQ = "cccccccccccccccccccc"
PID_FUZZY = "dddddddddddddddddddd"
PID_DUP = "eeeeeeeeeeeeeeeeeeee"

TRACKER = """---
node_id: TEST:tracker
---

# Tracker

## Active

| Date Applied | Company | Role | Status | Status Date | Recruiter | Next Action | Follow-Up Due | | Posting ID |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| — | Acme Corp | Senior Analyst (Austin TX hybrid, req A100, $100,000-$120,000, closes 2026-10-30) | Package built, unsent | 2026-10-18 | | Folder: Acme_SeniorAnalyst | | | PID_CELL |
| 2026-10-10 | Beta Inc | Program Manager (Remote, req B200) | Applied | 2026-10-10 | Pat Smith, Recruiter, pat@beta.example | note one | 2026-10-20 | | |
| 2026-10-02 | Gamma LLC | Data Lead (Credit Group) | Passed initial screen (2026-10-05) | 2026-10-05 | Not a name at all | next \\| steps | | | |
| 2026-09-20 | Delta Co | Ops Manager | Panel done 9/30 | 2026-09-30 | | | | | none |

## On Hold

| Date Applied | Company | Role | Status | Status Date | Recruiter | Next Action | Follow-Up Due | Posting ID |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| | | | | | | | | --- |

## Consultant Networks

| Date Accepted | Platform | Status | Profile |
| --- | --- | --- | --- |
| 2026-03-04 | NetOne | Active — available | [Profile](https://net.example/me) |
| 2021-01-21 | NetTwo | Not accepted | — |
| | | | |

## Closed

| Date Applied | Company | Role | Status | Status Date | Recruiter | Close Reason | Posting ID |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-01 | Epsilon | Analyst | Rejected | 2026-08-20 | | form letter | |
| 2026-08-02 | **Zeta** | Director of Things | Assumed Rejected | 2026-10-20 | | no response | |
| 2026-08-03 | Eta | Strategy Lead | Declined | 2026-08-30 | | | |
| 2026-08-04 | Theta | Strategy Manager | Withdrew | 2026-08-30 | | | |
| 2026-08-05 | Iota | Planner | Not Pursuing | 2026-08-06 | | | |
| 2026-08-06 | Kappa | Planner II | Closed before submission | 2026-08-09 | | | |
| 2026-08-07 | Lambda | Planner III | Evaluating | 2026-08-09 | | | |
| 2026-08-08 | Mu | Planner IV | Something Unheard Of | 2026-08-09 | Lee Wong \\| Ann Poe | extra \\| pipe \\| here | |
| — | Nu | Role With No Date | Rejected | 2026-02-02 | | | |
| 2026-07-01 | Xylon | Req Match Role (req X300) | Rejected | 2026-07-02 | | | |
| 2026-07-02 | Omicron | Exact Role | Applied | 2026-07-03 | | | |
| 2026-07-03 | Pi | Fuzzy Role | Applied | 2026-07-03 | | | |
| 2026-07-04 | Rho | Shared Posting Old | Rejected | 2026-07-05 | | | PID_DUP |
| 2026-09-04 | Rho | Shared Posting New | Rejected | 2026-09-05 | | | PID_DUP |
| 2026-06-01 | Sigma | Folder Twin | Rejected | 2026-06-02 | | | |
| 2026-06-20 | Sigma | Folder Twin | Rejected | 2026-06-21 | | | |
| 2026-05-01 | Upsilon | Same Job Role | Applied | 2026-05-02 | Quinn Reed | n1 | |
| 2026-05-03 | Upsilon | Same Job Role | Rejected | 2026-05-10 | | n2 | |
| 2026-04-01 | Phi | Two Reqs Role (req P1000) | Rejected | 2026-04-02 | | | |
| 2026-04-01 | Phi | Two Reqs Role (req P2000) | Rejected | 2026-04-02 | | | |
| 2026-06-03 | Tau | Quarterly Review Analyst | Rejected | 2026-06-04 | | | |
"""


@pytest.fixture()
def env(tmp_path):
    jd = tmp_path / "Job_Search"
    apps = jd / "Applications"
    for n in ["Acme_SeniorAnalyst", "Beta_ProgramManager_B200", "Sigma_FolderTwin", "Tau_QuarterlyReviewAnalyst",
              "Tau_AnnualBudgetPlanner", "Xylon_Whatever"]:
        (apps / n).mkdir(parents=True)
        (apps / n / "index.md").write_text("---\nstatus: applied\n---\n# x\n", encoding="utf-8")
    (apps / "Xylon_Whatever" / "index.md").write_text("Req X300 for this role\n", encoding="utf-8")
    (jd / "Application_Tracker.md").write_text(
        TRACKER.replace("PID_CELL", PID_CELL).replace("PID_DUP", PID_DUP), encoding="utf-8")
    ddb = tmp_path / "t.duckdb"
    con = duckdb.connect(str(ddb))
    con.execute("CREATE TABLE tracker (section VARCHAR, date_applied VARCHAR, company VARCHAR, role VARCHAR, status VARCHAR,"
                " posting_id VARCHAR, matched_posting_id VARCHAR, match_kind VARCHAR, synced_at TIMESTAMP)")
    con.execute("CREATE TABLE postings (posting_id VARCHAR, employer VARCHAR, req_id VARCHAR, title VARCHAR, url VARCHAR)")
    con.execute("INSERT INTO tracker VALUES ('Closed','2026-07-02','Omicron','Exact Role','Applied',NULL,?, 'exact', now())", [PID_EXACT])
    con.execute("INSERT INTO tracker VALUES ('Closed','2026-07-03','Pi','Fuzzy Role','Applied',NULL,?, 'fuzzy', now())", [PID_FUZZY])
    con.execute("INSERT INTO postings VALUES (?, 'Xylon Holdings', 'X300', 'Req Match Role', NULL)", [PID_REQ])
    con.execute("INSERT INTO postings VALUES (?, 'Pi Group', 'P1', 'Fuzzy Role Plus', NULL)", [PID_FUZZY])
    con.close()
    return dict(tracker=jd / "Application_Tracker.md", duckdb=ddb, report=tmp_path / "report.md", dir=tmp_path)


@pytest.fixture(scope="module")
def admin():
    try:
        a = db.connect("postgres")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres unreachable: {e}")
    yield a
    a.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    a.close()


@pytest.fixture()
def conn(admin):
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
    c = db.connect(TEST_DB)
    db.migrate(c)
    yield c
    c.close()


def run(env, *extra):
    return M.main(["--tracker", str(env["tracker"]), "--duckdb", str(env["duckdb"]), "--report", str(env["report"]),
                   "--db", TEST_DB, *extra])


def app(conn, company, role=None):
    q = "SELECT * FROM tracker.application WHERE company = %s" + (" AND role = %s" if role else "") + " ORDER BY app_id"
    return conn.execute(q, [company, role] if role else [company]).fetchall()


@pytest.mark.parametrize("text,section,dated,expected", [
    ("Package built, unsent", "Active", False, "built"),
    ("Applied (no letter)", "Active", True, "applied"),
    ("Passed initial screen (2026-10-13)", "Active", True, "screen"),
    ("Panel done 10/16", "Active", True, "interview"),
    ("Assumed Rejected", "Closed", True, "assumed-rejected"),
    ("Rejected - Poor Fit", "Closed", True, "rejected"),
    ("Declined", "Closed", True, "declined"),
    ("Declined", "Closed", False, "passed"),              # user ruling: declined with no apply date = declined to pursue
    ("Declined offer", "Active", False, "passed"),
    ("Withdrew", "Closed", True, "withdrawn"),
    ("Skip -  Not pursuing", "Closed", False, "passed"),
    ("Pass — Poor Fit", "Closed", False, "passed"),
    ("Closed before submission", "Closed", False, "closed-unsent"),
    ("No Longer Posted", "Closed", False, "closed-unsent"),
    ("Not Applied", "Closed", False, "passed"),
    ("On Hold", "Closed", False, "passed"),               # user ruling: status text On Hold = decided not to apply
    ("Evaluating", "Closed", False, "closed-unsent"),     # section tiebreaker: built text in Closed
    ("Applied", "Closed", True, "assumed-rejected"),
    ("Recruiter Screening", "Closed", True, "rejected"),
])
def test_status_rules(text, section, dated, expected):
    assert M.map_status(text, section, dated)[0] == expected


@pytest.mark.parametrize("text,section", [("Not Applied", "Closed"), ("Applied", "Closed"), ("Recruiter Call", "Closed"),
                                           ("RTR unsigned - holding lightly", "Active")])
def test_rulings_are_confident(text, section):
    assert M.map_status(text, section, True)[1] is True


def test_status_unmatched_defaults():
    assert M.map_status("Something Unheard Of", "Closed", True) == ("rejected", False, "unmatched; Closed default")
    assert M.map_status("Something Unheard Of", "Active", True)[0] == "applied"
    assert M.map_status("Something Unheard Of", "Active", False)[0] == "built"


def test_clean_role():
    c = M.clean_role("Senior Analyst (Austin TX hybrid, req A100, $100,000-$120,000, closes 2026-10-30)")
    assert (c["title"], c["location"], c["employer_job_id"], c["pay_text"], c["closes_on"]) == (
        "Senior Analyst", "Austin TX hybrid", "A100", "$100,000-$120,000", dt.date(2026, 10, 30))
    kept = M.clean_role("Data Lead (Credit Group)")
    assert kept["title"] == "Data Lead (Credit Group)" and not kept["clean"]
    assert M.clean_role("Roles (Remote), req B900, extra words")["employer_job_id"] == "B900"


def test_load_numbering_and_fields(env, conn):
    assert run(env) == 0
    n = conn.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"]
    assert n == 24
    order = [r["company"] for r in conn.execute("SELECT company FROM tracker.application ORDER BY app_id").fetchall()]
    assert order[0] == "Phi"
    dated = [r["date_applied"] for r in conn.execute("SELECT date_applied FROM tracker.application ORDER BY app_id").fetchall()]
    known = [d for d in dated if d]
    assert known == sorted(known) and dated[len(known):] == [None] * (len(dated) - len(known))
    # blank dates last, ordered by status date: Nu (2026-02-02) before Acme (2026-10-18)
    tail = [r["company"] for r in conn.execute("SELECT company FROM tracker.application WHERE date_applied IS NULL ORDER BY app_id").fetchall()]
    assert tail.index("Nu") < tail.index("Acme Corp")
    a = app(conn, "Acme Corp")[0]
    assert (a["role"], a["location"], a["employer_job_id"], a["status"], a["source"]) == (
        "Senior Analyst", "Austin TX hybrid", "A100", "built", "migrated")
    assert a["closes_on"] == dt.date(2026, 10, 30) and a["posting_id"] == PID_CELL
    assert a["folder"] == "Applications/Acme_SeniorAnalyst"
    assert app(conn, "Zeta")[0]["status"] == "assumed-rejected"
    assert app(conn, "Gamma LLC")[0]["status"] == "screen" and app(conn, "Delta Co")[0]["status"] == "interview"
    assert app(conn, "Mu")[0]["status"] == "rejected"
    assert app(conn, "Delta Co")[0]["posting_id"] is None


def test_events_and_contacts(env, conn):
    run(env)
    a = app(conn, "Acme Corp")[0]
    ev = conn.execute("SELECT * FROM tracker.event WHERE app_id = %s ORDER BY event_id", [a["app_id"]]).fetchall()
    assert [(e["kind"], e["actor"]) for e in ev] == [("migrated", "migration"), ("status", "migration")]
    assert "Acme Corp" in ev[0]["note"] and "req A100" in ev[0]["note"]
    assert ev[1]["status"] == "built" and ev[1]["status_date"] == dt.date(2026, 10, 18)
    b = app(conn, "Beta Inc")[0]
    c = conn.execute("SELECT c.* FROM tracker.contact c JOIN tracker.app_contact ac USING (contact_id) WHERE ac.app_id = %s",
                     [b["app_id"]]).fetchall()
    assert [(x["name"], x["email"]) for x in c] == [("Pat Smith", "pat@beta.example")]
    g = app(conn, "Gamma LLC")[0]
    assert "Not a name at all" in g["notes"]
    assert conn.execute("SELECT count(*) AS n FROM tracker.app_contact WHERE app_id = %s", [app(conn, "Mu")[0]["app_id"]]).fetchone()["n"] == 2
    nets = conn.execute("SELECT * FROM tracker.network ORDER BY network_id").fetchall()
    assert [(n["platform"], n["profile_url"]) for n in nets] == [("NetOne", "https://net.example/me"), ("NetTwo", None)]
    assert app(conn, "Mu")[0]["close_reason"].count("pipe") == 1     # stray pipes kept in the free-text cell


def test_posting_id_exact_vs_fuzzy(env, conn):
    run(env)
    assert app(conn, "Omicron")[0]["posting_id"] == PID_EXACT          # DuckDB exact
    assert app(conn, "Pi")[0]["posting_id"] is None                    # fuzzy never filled
    assert app(conn, "Xylon")[0]["posting_id"] == PID_REQ                 # unique employer req match
    text = env["report"].read_text(encoding="utf-8")
    assert PID_FUZZY in text and "Pi Group" in text                    # ... but listed as a candidate
    assert text.startswith("---\nnode_id: SYS:tracker-migration-review-20261021")


def test_posting_id_duplicate_keeps_most_recent(env, conn):
    run(env)
    assert app(conn, "Rho", "Shared Posting New")[0]["posting_id"] == PID_DUP
    assert app(conn, "Rho", "Shared Posting Old")[0]["posting_id"] is None
    assert "also on app" in env["report"].read_text(encoding="utf-8")


def test_folder_matching_and_collision(env, conn):
    run(env)
    assert app(conn, "Beta Inc")[0]["folder"] == "Applications/Beta_ProgramManager_B200"      # req in name
    assert app(conn, "Tau")[0]["folder"] == "Applications/Tau_QuarterlyReviewAnalyst"          # tokens
    assert app(conn, "Xylon")[0]["folder"] == "Applications/Xylon_Whatever"                         # index.md names the req
    twins = [r["folder"] for r in app(conn, "Sigma")]
    assert sorted(f is None for f in twins) == [False, True]                                  # never two claims
    assert app(conn, "Delta Co")[0]["folder"] is None
    folders = [r["folder"] for r in conn.execute("SELECT folder FROM tracker.application WHERE folder IS NOT NULL").fetchall()]
    assert len(folders) == len(set(folders))
    assert "collision" in env["report"].read_text(encoding="utf-8")


def test_rerun_is_idempotent(env, conn):
    run(env)
    first = conn.execute("SELECT app_id, company, role, status, posting_id, folder FROM tracker.application ORDER BY app_id").fetchall()
    run(env)
    second = conn.execute("SELECT app_id, company, role, status, posting_id, folder FROM tracker.application ORDER BY app_id").fetchall()
    assert first == second
    assert conn.execute("SELECT count(*) AS n FROM tracker.event").fetchone()["n"] == 2 * len(first) + 1      # +1: the merged-away duplicate row
    assert conn.execute("SELECT min(app_id) AS m FROM tracker.application").fetchone()["m"] == 1


def test_refuses_after_real_edit_unless_force(env, conn):
    run(env)
    a = app(conn, "Beta Inc")[0]
    conn.execute("INSERT INTO tracker.event (app_id, kind, actor, note) VALUES (%s, 'note', 'user', 'hand edit')", [a["app_id"]])
    with pytest.raises(SystemExit) as e:
        run(env)
    assert "refusing" in str(e.value)
    assert conn.execute("SELECT count(*) AS n FROM tracker.event WHERE actor = 'user'").fetchone()["n"] == 1
    assert run(env, "--force") == 0
    assert conn.execute("SELECT count(*) AS n FROM tracker.event WHERE actor = 'user'").fetchone()["n"] == 0


def test_dry_run_touches_nothing(env, conn):
    run(env)
    before = conn.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"]
    conn.execute("INSERT INTO tracker.event (app_id, kind, actor, note) VALUES (1, 'note', 'user', 'hand edit')")
    assert run(env, "--dry-run") == 0                      # would refuse if it touched the database
    assert conn.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"] == before
    assert conn.execute("SELECT count(*) AS n FROM tracker.event WHERE actor = 'user'").fetchone()["n"] == 1
    assert "dry run" in env["report"].read_text(encoding="utf-8")


def test_dry_run_needs_no_database(env, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("connected")))
    assert run(env, "--dry-run") == 0


def test_merge_same_job_duplicates(env, conn):
    run(env)
    ups = app(conn, "Upsilon")
    assert len(ups) == 1 and ups[0]["status"] == "rejected"           # latest status date wins
    assert ups[0]["date_applied"] == dt.date(2026, 5, 1) or ups[0]["date_applied"] == dt.date(2026, 5, 3)
    ev = conn.execute("SELECT note FROM tracker.event WHERE app_id = %s AND kind = 'migrated' ORDER BY event_id", [ups[0]["app_id"]]).fetchall()
    assert len(ev) == 2 and "[merged duplicate]" in ev[1]["note"] and "Applied" in ev[1]["note"]
    assert conn.execute("SELECT count(*) AS n FROM tracker.app_contact WHERE app_id = %s", [ups[0]["app_id"]]).fetchone()["n"] == 1
    text = env["report"].read_text(encoding="utf-8")
    assert "## Merged duplicates" in text and "no req id; date applied within 3 days" in text


def test_different_req_ids_never_merge(env, conn):
    run(env)
    assert sorted(r["employer_job_id"] for r in app(conn, "Phi")) == ["P1000", "P2000"]


def test_far_apart_dates_not_merged(env, conn):
    run(env)
    assert len(app(conn, "Sigma")) == 2


def test_req_platform_needs_a_digit_and_loc_state_is_uppercase():
    assert M.clean_role("Analyst (Deel ATS)")["employer_job_id"] is None
    assert M.clean_role("Analyst (Workday 12345)")["employer_job_id"] == "12345"
    assert M.clean_role("Analyst (Austin TX)")["location"] == "Austin TX"
    hrs = M.clean_role("Analyst (20 hrs/wk)")
    assert hrs["location"] is None and not hrs["clean"]


def test_rerun_guard_covers_application_bullet(env, conn):
    run(env)
    a = app(conn, "Beta Inc")[0]
    conn.execute("INSERT INTO tracker.application_bullet (app_id, bullet_id, position) VALUES (%s, 1, 1)", [a["app_id"]])
    with pytest.raises(SystemExit) as e:
        run(env)
    assert "application_bullet" in str(e.value)
    assert conn.execute("SELECT count(*) AS n FROM tracker.application_bullet").fetchone()["n"] == 1
    assert run(env, "--force") == 0
    assert conn.execute("SELECT count(*) AS n FROM tracker.application_bullet").fetchone()["n"] == 0
