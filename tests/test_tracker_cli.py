"""Tests for trk: folders, the generated view, and the CLI. Throwaway database; skipped when
Postgres is unreachable. Fake data only."""
import datetime as dt

import pytest

psycopg = pytest.importorskip("psycopg")
from backend.tracker import cli, db, folders, render, store  # noqa: E402

TEST_DB = "resume_test_wp2"
PID = "0123456789abcdef0123"


@pytest.fixture(scope="session")
def _db():
    try:
        admin = db.connect("postgres")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres unreachable: {e}")
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
    c = db.connect(TEST_DB)
    db.migrate(c)
    yield c
    c.close()
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.close()


@pytest.fixture
def conn(_db):
    _db.execute("TRUNCATE tracker.application, tracker.contact, tracker.network, tracker.render_log "
                "RESTART IDENTITY CASCADE")
    _db.execute("TRUNCATE jobs.fit_snapshot")
    return _db


@pytest.fixture
def jd(tmp_path, monkeypatch):
    """A tmp Job_Search dir, wired as the vault dir (the tracker file name marks it as the dir)."""
    monkeypatch.setenv("JOBSEARCH_VAULT_DIR", str(tmp_path))
    monkeypatch.setenv("TRACKER_PGDATABASE", TEST_DB)
    monkeypatch.delenv("TRK_ACTOR", raising=False)
    # job_search_dir() falls back to the vault subpath when the tracker file is absent
    from backend.finder.tracker_sync import job_search_dir
    d = job_search_dir(str(tmp_path))
    d.mkdir(parents=True)
    return d


def _app(conn, **kw):
    kw.setdefault("company", "Acme Corp")
    kw.setdefault("role", "Sr Analyst")
    kw.setdefault("actor", "tester")
    return store.add_application(conn, **kw)


# ---- folders ----

def test_folder_name_with_req():
    assert folders.base_name("Acme Corp", "Sr. BI Analyst, Credit Card", "R-123", 7) == \
        "AcmeCorp_SrBIAnalystCreditCard_R-123"


def test_folder_name_no_req_uses_app_id():
    assert folders.base_name("Acme", "Analyst", None, 42) == "Acme_Analyst_A42"
    assert folders.base_name("Acme", "Analyst", "  / ", 42) == "Acme_Analyst_A42"


def test_folder_name_odd_characters_and_length():
    n = folders.base_name("Zoë & Sons, Inc.", "Sr/Lead (Data) Analyst — Reporting & Insight Platform Operations", "JR 1/2#3", 1)
    assert n.startswith("ZoeSonsInc_") and n.endswith("_JR123")
    role = n.split("_")[1]
    assert role.isalnum() and len(role) <= folders.ROLE_MAX


def test_create_folder_collision_and_index(tmp_path):
    kw = dict(app_id=5, company="Acme Corp", role="Analyst", employer_job_id="R123", posting_id=PID)
    a = folders.create_folder(tmp_path, **kw)
    (tmp_path / a / "keep.txt").write_text("x")
    b = folders.create_folder(tmp_path, **kw)
    c = folders.create_folder(tmp_path, **kw)
    assert (a, b, c) == ("Applications/AcmeCorp_Analyst_R123", "Applications/AcmeCorp_Analyst_R123_2",
                         "Applications/AcmeCorp_Analyst_R123_3")
    assert (tmp_path / a / "keep.txt").read_text() == "x"
    idx = (tmp_path / a / "index.md").read_text()
    assert "node_id: PRO:application-acmecorp-analyst-r123" in idx
    assert f"app_id: 5\nposting_id: {PID}" in idx and "# Acme Corp: Analyst (R123)" in idx
    assert "posting_id: none" in folders.index_text("x", 1, "A", "B", None, None)


def test_use_folder_rejects_escape(tmp_path):
    with pytest.raises(store.TrackerError):
        folders.use_folder(tmp_path, "../evil", app_id=1, company="A", role="B")
    with pytest.raises(store.TrackerError):
        folders.use_folder(tmp_path, "/abs", app_id=1, company="A", role="B")


# ---- render ----

NOW = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 1, 1)


def test_render_sections_and_content(conn, jd):
    a = _app(conn, employer_job_id="R123", folder="Applications/AcmeCorp_SrAnalyst_R123",
             next_action="Follow up | call", follow_up_due="2026-01-05", status_date="2026-01-02")
    store.add_contact(conn, a["app_id"], "Pat Example", "tester", company="Acme Corp")
    b = _app(conn, company="Beta LLC", status="applied", status_date="2026-01-03")
    store.set_status(conn, b["app_id"], "rejected", "tester", status_date="2026-01-09")
    conn.execute("INSERT INTO tracker.network (platform, status) VALUES ('Netco', 'accepted')")
    res = render.render(conn, jd, today=TODAY, now=NOW)
    assert res.status == "written" and res.path.name == "Application_Tracker_PREVIEW.md"
    t = res.path.read_text()
    assert "## Active (1)" in t and "## Consultant networks (1)" in t
    assert "## Closed" not in t and "Closed: 1 (" in t and "trk ls --closed" in t
    assert "node_id: PRO:job-application-tracker-preview" in t
    assert "1 built. Follow-ups due within 7 days: 1." in t and "Closed: 1 (1 rejected)." in t
    assert "Follow up \\| call" in t and "Pat Example (recruiter)" in t
    assert "[open](Applications/AcmeCorp_SrAnalyst_R123/index.md)" in t
    assert "| 2 |" not in t and "Beta LLC" not in t and "| Netco | " in t
    assert all(l.startswith("|") for l in t.splitlines() if "| Acme" in l or "| Beta" in l)


def test_folder_link_encodes_spaces():
    assert render._folder_cell("Applications/My Co_X_1") == "[open](Applications/My%20Co_X_1/index.md)"
    assert render._folder_cell(None) == ""


def test_render_deterministic(conn, jd):
    _app(conn)
    t1 = render.render_text(conn, preview=True, today=TODAY, now=NOW)
    t2 = render.render_text(conn, preview=True, today=TODAY, now=NOW)
    assert t1 == t2


def test_hand_edit_guard(conn, jd):
    _app(conn)
    r1 = render.render(conn, jd)
    assert r1.status == "written"
    assert render.render(conn, jd).status == "written"          # untouched file is overwritten
    r1.path.write_text(r1.path.read_text() + "\nmy hand edit\n")
    r2 = render.render(conn, jd)
    assert r2.status == "refused" and "my hand edit" in r2.diff
    assert "my hand edit" in r1.path.read_text()
    r3 = render.render(conn, jd, force=True)
    assert r3.status == "written" and "my hand edit" not in r1.path.read_text()
    n = conn.execute("SELECT count(*) AS n FROM tracker.render_log").fetchone()["n"]
    assert n == 3


def test_existing_unlogged_file_refused(conn, jd):
    (jd / render.PREVIEW_NAME).write_text("someone else's file")
    assert render.render(conn, jd).status == "refused"


def test_cutover_false_never_writes_live(conn, jd, monkeypatch):
    assert render.CUTOVER is False
    _app(conn)
    render.render(conn, jd, force=True)
    render.render(conn, jd, preview=False, force=True)
    assert not (jd / render.LIVE_NAME).exists()
    monkeypatch.setattr(render, "CUTOVER", True)
    res = render.render(conn, jd)
    assert res.path.name == render.LIVE_NAME and "job-application-tracker\n" in res.path.read_text()
    assert render.render(conn, jd, preview=True).path.name == render.PREVIEW_NAME


# ---- CLI end to end ----

def run(capsys, *argv):
    rc = cli.main(list(argv))
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_cli_add_set_note_contact_edit_show(conn, jd, capsys):
    rc, out, _ = run(capsys, "add", "--company", "Acme Corp", "--role", "Sr Analyst", "--req", "R123")
    assert rc == 0 and "added #1" in out
    assert (jd / "Applications/AcmeCorp_SrAnalyst_R123/index.md").exists()
    assert conn.execute("SELECT folder FROM tracker.application").fetchone()["folder"] == \
        "Applications/AcmeCorp_SrAnalyst_R123"
    assert (jd / render.PREVIEW_NAME).exists()
    assert run(capsys, "set", "1", "applied", "--date", "2026-03-01", "--note", "sent")[0] == 0
    assert run(capsys, "note", "1", "Recruiter replied")[0] == 0
    assert run(capsys, "contact", "1", "Pat Example", "--email", "pat@example.com")[0] == 0
    assert run(capsys, "edit", "1", "--next-action", "Follow up", "--follow-up", "2026-03-10")[0] == 0
    rc, out, _ = run(capsys, "show", "1")
    assert rc == 0 and "applied" in out and "Pat Example" in out and "Recruiter replied" in out
    assert "user" in out                                   # default actor
    assert "Follow up" in (jd / render.PREVIEW_NAME).read_text()
    rc, out, _ = run(capsys, "")  # unknown command -> usage
    assert rc == 2


def test_cli_ls_find_variants(conn, jd, capsys):
    _app(conn, employer_job_id="R123")
    b = _app(conn, company="Beta LLC", status="applied")
    store.set_status(conn, b["app_id"], "rejected", "t")
    rc, out, _ = run(capsys)                                   # default = ls
    assert rc == 0 and "Acme Corp" in out and "Beta" not in out
    assert "Beta LLC" in run(capsys, "ls", "--closed")[1]
    out = run(capsys, "ls", "--all")[1]
    assert "Acme Corp" in out and "Beta LLC" in out
    assert "Beta LLC" in run(capsys, "ls", "--status", "rejected")[1]
    out = run(capsys, "find", "acme", "R123")[1]
    assert "Acme Corp" in out and "(1 match)" in out


def test_cli_actor_env(conn, jd, capsys, monkeypatch):
    monkeypatch.setenv("TRK_ACTOR", "claude-code")
    run(capsys, "add", "--company", "Acme Corp", "--role", "Analyst", "--folder", "none")
    assert conn.execute("SELECT actor FROM tracker.event").fetchone()["actor"] == "claude-code"


def test_cli_add_posting_from_snapshot(conn, jd, capsys):
    conn.execute("INSERT INTO jobs.fit_snapshot (posting_id, employer, title, req_id, location_primary, "
                 "pay_min, pay_max) VALUES (%s, 'Snapco', 'Data Analyst', 'S-9', 'Remote', 1000, 2000)", [PID])
    rc, out, _ = run(capsys, "add", "--posting", PID)
    assert rc == 0
    row = conn.execute("SELECT * FROM tracker.application").fetchone()
    assert (row["company"], row["role"], row["employer_job_id"], row["location"], row["pay_text"], row["source"]) == \
        ("Snapco", "Data Analyst", "S-9", "Remote", "$1,000-$2,000", "pipeline")
    assert row["folder"] == "Applications/Snapco_DataAnalyst_S-9"


def test_cli_add_posting_missing_requires_fields(conn, jd, capsys):
    rc, _, err = run(capsys, "add", "--posting", PID)
    assert rc == 1 and "--company" in err and "Traceback" not in err
    assert conn.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"] == 0


def test_cli_folder_none_and_non_built(conn, jd, capsys):
    run(capsys, "add", "--company", "Acme Corp", "--role", "A", "--folder", "none")
    run(capsys, "add", "--company", "Acme Corp", "--role", "B", "--status", "applied")
    assert not (jd / "Applications").exists()
    run(capsys, "add", "--company", "Acme Corp", "--role", "C", "--status", "applied", "--folder", "Applications/Custom_C")
    assert (jd / "Applications/Custom_C/index.md").exists()


def test_cli_render_refusal_keeps_db_write(conn, jd, capsys):
    run(capsys, "add", "--company", "Acme Corp", "--role", "A", "--folder", "none")
    p = jd / render.PREVIEW_NAME
    p.write_text(p.read_text() + "\nZZMARK\n")
    rc, out, err = run(capsys, "set", "1", "applied")
    assert rc == 0 and "edited by hand" in err
    assert conn.execute("SELECT status FROM tracker.application").fetchone()["status"] == "applied"
    assert "ZZMARK" in p.read_text()
    rc, out, err = run(capsys, "render")
    assert rc == 1 and "ZZMARK" in err
    assert run(capsys, "render", "--force")[0] == 0
    assert "ZZMARK" not in p.read_text()


def test_cli_no_render(conn, jd, capsys):
    run(capsys, "add", "--company", "Acme Corp", "--role", "A", "--folder", "none", "--no-render")
    assert not (jd / render.PREVIEW_NAME).exists()


def test_cli_check(conn, jd, capsys):
    a = _app(conn, employer_job_id="R1", folder="Applications/Gone", follow_up_due="2020-01-01")
    _app(conn, employer_job_id="r1")
    rc, out, _ = run(capsys, "check")
    assert rc == 1
    assert "folder missing" in out and "duplicate" in out and "overdue" in out
    assert "posting check skipped" in out
    (jd / "Applications/Gone").mkdir(parents=True)
    store.edit_field(conn, a["app_id"], "follow_up_due", "", "t")
    store.edit_field(conn, 2, "employer_job_id", "R2", "t")
    rc, out, _ = run(capsys, "check")
    assert rc == 0 and "0 problems" in out
    conn.execute("INSERT INTO jobs.fit_snapshot (posting_id) VALUES ('ffffffffffffffffffff')")
    store.edit_field(conn, 1, "posting_id", PID, "t")
    rc, out, _ = run(capsys, "check")
    assert rc == 1 and "not in jobs.fit_snapshot" in out


def test_cli_errors(conn, jd, capsys):
    rc, _, err = run(capsys, "set", "99", "applied")
    assert rc == 1 and "no application" in err and "Traceback" not in err
    _app(conn)
    rc, _, err = run(capsys, "set", "1", "bogus")
    assert rc == 1 and "allowed" in err
    assert run(capsys, "edit", "1")[0] == 2                    # no options
    assert run(capsys, "set", "x")[0] == 2                     # argparse usage
    assert run(capsys, "nonsense")[0] == 2
