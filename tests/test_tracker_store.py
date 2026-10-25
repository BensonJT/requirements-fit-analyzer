"""Tests for the PostgreSQL tracker (backend/tracker). Uses a throwaway database; skipped when
Postgres is unreachable. Fake data only."""
import datetime as dt

import pytest

psycopg = pytest.importorskip("psycopg")
from backend.tracker import db, store  # noqa: E402

TEST_DB = "resume_test_wp1"
PID = "0123456789abcdef0123"
PID2 = "fedcba9876543210fedc"


@pytest.fixture(scope="session")
def conn():
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


def _new(conn, **kw):
    kw.setdefault("company", "Acme Corp")
    kw.setdefault("role", "Sr Analyst")
    kw.setdefault("actor", "tester")
    return store.add_application(conn, **kw)


def _events(conn, app_id):
    return conn.execute("SELECT * FROM tracker.event WHERE app_id = %s ORDER BY event_id", [app_id]).fetchall()


def test_migrate_idempotent(conn):
    first = db.migrate(conn)
    second = db.migrate(conn)
    assert first == second == ["001_tracker.sql", "002_jobs.sql", "003_roles.sql", "004_jev_status.sql"]
    n = conn.execute("SELECT count(*) AS n FROM tracker.status").fetchone()["n"]
    assert n == 12
    assert conn.execute("SELECT count(*) AS n FROM pg_roles WHERE rolname = 'claude_ro'").fetchone()["n"] == 1
    assert conn.execute("SELECT to_regclass('jobs.fit_snapshot') AS t").fetchone()["t"]


def test_add_creates_one_created_event(conn):
    a = _new(conn, employer_job_id="R123", status_date="2026-01-05")
    ev = _events(conn, a["app_id"])
    assert [(e["kind"], e["actor"], e["status"]) for e in ev] == [("created", "tester", "built")]
    assert a["status"] == "built" and a["date_applied"] is None


def test_add_applied_sets_date_applied(conn):
    a = _new(conn, status="applied", status_date="2026-02-01")
    assert a["date_applied"] == dt.date(2026, 2, 1)


def test_set_status_event_and_date_applied(conn):
    a = _new(conn)
    b = store.set_status(conn, a["app_id"], "applied", "bot", status_date="2026-03-02", note="sent")
    assert b["status"] == "applied" and b["date_applied"] == dt.date(2026, 3, 2)
    assert b["updated_at"] >= a["updated_at"]
    c = store.set_status(conn, a["app_id"], "screen", "bot", status_date="2026-03-09")
    assert c["date_applied"] == dt.date(2026, 3, 2)          # not overwritten
    ev = _events(conn, a["app_id"])
    assert [e["kind"] for e in ev] == ["created", "status", "status"]
    assert ev[1]["actor"] == "bot" and ev[1]["old_value"] == "built" and ev[1]["note"] == "sent"


def test_invalid_status_rejected(conn):
    a = _new(conn)
    with pytest.raises(store.TrackerError) as e:
        store.set_status(conn, a["app_id"], "ghosted", "bot")
    assert "allowed" in str(e.value) and "assumed-rejected" in str(e.value)
    assert len(_events(conn, a["app_id"])) == 1
    with pytest.raises(store.TrackerError):
        _new(conn, status="nope")
    with pytest.raises(store.TrackerError):
        store.set_status(conn, 999999, "applied", "bot")


def test_posting_id_validation(conn):
    assert _new(conn, posting_id=PID)["posting_id"] == PID
    assert _new(conn, posting_id="none")["posting_id"] is None
    assert _new(conn, posting_id=None)["posting_id"] is None
    for bad in ("xyz", PID[:-1], PID.upper() + "0"):
        with pytest.raises(store.TrackerError):
            _new(conn, posting_id=bad)
    with pytest.raises(Exception):                            # UNIQUE
        _new(conn, posting_id=PID)
    a = _new(conn)
    store.edit_field(conn, a["app_id"], "posting_id", PID2, "bot")
    assert store.get(conn, a["app_id"])["posting_id"] == PID2
    store.edit_field(conn, a["app_id"], "posting_id", "none", "bot")
    assert store.get(conn, a["app_id"])["posting_id"] is None


def test_failed_write_leaves_no_row(conn):
    before = conn.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"]
    with pytest.raises(Exception):
        _new(conn, posting_id=PID)                            # duplicate from earlier test
    assert conn.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"] == before
    orphans = conn.execute("SELECT count(*) AS n FROM tracker.event e LEFT JOIN tracker.application a "
                           "USING (app_id) WHERE a.app_id IS NULL").fetchone()["n"]
    assert orphans == 0


def test_source_values(conn):
    for src in store.SOURCES:
        assert _new(conn, source=src)["source"] == src
    assert "sheet" in store.SOURCES
    with pytest.raises(store.TrackerError):
        _new(conn, source="psychic")
    a = _new(conn)
    assert store.edit_field(conn, a["app_id"], "source", "sheet", "owner")["source"] == "sheet"
    with pytest.raises(psycopg.errors.CheckViolation):          # the DB constraint itself
        conn.execute("UPDATE tracker.application SET source = 'psychic' WHERE app_id = %s", [a["app_id"]])


def test_note_event(conn):
    a = _new(conn)
    store.add_note(conn, a["app_id"], "HM review next week", "owner")
    ev = _events(conn, a["app_id"])
    assert ev[-1]["kind"] == "note" and ev[-1]["actor"] == "owner" and ev[-1]["note"] == "HM review next week"
    with pytest.raises(store.TrackerError):
        store.add_note(conn, a["app_id"], "  ", "owner")


def test_contact_reuse(conn):
    a, b = _new(conn), _new(conn, role="Lead Analyst")
    c1 = store.add_contact(conn, a["app_id"], "Pat Example", "bot", company="Acme Corp", email="pat@example.com")
    c2 = store.add_contact(conn, b["app_id"], "pat example", "bot", company="acme corp", phone="555-0100")
    assert c1["created"] and not c2["created"] and c1["contact_id"] == c2["contact_id"]
    assert c2["email"] == "pat@example.com" and c2["phone"] == "555-0100"   # empty columns filled
    assert [e["kind"] for e in _events(conn, a["app_id"])] == ["created", "contact"]
    store.add_contact(conn, a["app_id"], "Pat Example", "bot", company="Acme Corp")   # same link: no-op
    assert len(_events(conn, a["app_id"])) == 2
    store.add_contact(conn, a["app_id"], "Pat Example", "bot", role="panelist", company="Acme Corp")
    got = store.get(conn, a["app_id"])
    assert sorted(c["role"] for c in got["contacts"]) == ["panelist", "recruiter"]
    with pytest.raises(store.TrackerError):
        store.add_contact(conn, a["app_id"], "X", "bot", role="boss")
    with pytest.raises(store.TrackerError):
        store.add_contact(conn, a["app_id"], "X", "bot", shoe_size="9")


def test_edit_field_whitelist_and_values(conn):
    a = _new(conn)
    r = store.edit_field(conn, a["app_id"], "next_action", "Follow up", "owner")
    assert r["next_action"] == "Follow up"
    store.edit_field(conn, a["app_id"], "follow_up_due", "2026-10-30", "owner")
    store.edit_field(conn, a["app_id"], "follow_up_due", "2026-11-02", "owner")
    ev = _events(conn, a["app_id"])
    assert ev[-1]["kind"] == "field" and ev[-1]["field"] == "follow_up_due"
    assert (ev[-1]["old_value"], ev[-1]["new_value"]) == ("2026-10-30", "2026-11-02")
    store.edit_field(conn, a["app_id"], "next_action", "", "owner")           # clears
    assert store.get(conn, a["app_id"])["next_action"] is None
    n = len(_events(conn, a["app_id"]))
    for bad in ("status", "status_date", "app_id", "created_at", "bogus"):
        with pytest.raises(store.TrackerError):
            store.edit_field(conn, a["app_id"], bad, "x", "owner")
    with pytest.raises(store.TrackerError):
        store.edit_field(conn, a["app_id"], "company", "", "owner")
    with pytest.raises(store.TrackerError):
        store.edit_field(conn, a["app_id"], "follow_up_due", "next tuesday", "owner")
    with pytest.raises(store.TrackerError):
        store.edit_field(conn, a["app_id"], "source", "psychic", "owner")
    assert len(_events(conn, a["app_id"])) == n


def test_get(conn):
    a = _new(conn)
    store.add_note(conn, a["app_id"], "n", "owner")
    g = store.get(conn, a["app_id"])
    assert g["company"] == "Acme Corp" and g["is_active"] is True
    assert [e["kind"] for e in g["events"]] == ["created", "note"]
    assert store.get(conn, 999999) is None


def test_find_and_list(conn):
    a = _new(conn, company="Zenith Widgets", role="Process Lead", employer_job_id="ZW-77", posting_id="a" * 20)
    b = _new(conn, company="Zenith Widgets", role="Data Lead", employer_job_id="ZW-78", status="applied")
    ids = lambda rows: {r["app_id"] for r in rows}
    assert ids(store.find(conn, ["zenith"])) >= {a["app_id"], b["app_id"]}
    assert ids(store.find(conn, ["zenith", "process"])) == {a["app_id"]}
    assert ids(store.find(conn, "ZW-78")) == {b["app_id"]}
    assert ids(store.find(conn, [str(a["app_id"])])) >= {a["app_id"]}
    assert ids(store.find(conn, ["a" * 20])) == {a["app_id"]}
    assert store.find(conn, ["zenith", "nomatch"]) == []
    with pytest.raises(store.TrackerError):
        store.find(conn, [])
    store.set_status(conn, a["app_id"], "rejected", "bot")
    act, closed, allr = (ids(store.list_apps(conn, x)) for x in (True, False, None))
    assert b["app_id"] in act and a["app_id"] not in act
    assert a["app_id"] in closed and b["app_id"] not in closed
    assert {a["app_id"], b["app_id"]} <= allr
    assert ids(store.list_apps(conn, None, status="applied")) >= {b["app_id"]}
    assert a["app_id"] not in ids(store.list_apps(conn, None, status="applied"))
    with pytest.raises(store.TrackerError):
        store.list_apps(conn, status="bogus")
    rows = store.list_apps(conn, True)                         # ordered by status sort order
    assert [r["sort_order"] for r in rows] == sorted(r["sort_order"] for r in rows)


# ---- review fixes: edit_fields, folder validation ----

def test_edit_fields_atomic_and_one_event_per_field(conn):
    a = _new(conn, role="Old Role")
    before = len(_events(conn, a["app_id"]))
    with pytest.raises(store.TrackerError):
        store.edit_fields(conn, a["app_id"], {"role": "New Role", "source": "bogus"}, "t")
    assert store.get(conn, a["app_id"])["role"] == "Old Role"
    assert len(_events(conn, a["app_id"])) == before
    row = store.edit_fields(conn, a["app_id"], {"role": "New Role", "location": "Remote", "notes": ""}, "t")
    assert (row["role"], row["location"]) == ("New Role", "Remote")
    evs = [e for e in _events(conn, a["app_id"]) if e["kind"] == "field"]
    assert sorted(e["field"] for e in evs) == ["location", "role"]       # notes was already empty: no event


def test_edit_fields_unknown_field_changes_nothing(conn):
    a = _new(conn, role="Keep")
    with pytest.raises(store.TrackerError):
        store.edit_fields(conn, a["app_id"], {"role": "Changed", "status": "applied"}, "t")
    assert store.get(conn, a["app_id"])["role"] == "Keep"


def test_folder_validation_and_uniqueness(conn):
    a, b = _new(conn), _new(conn)
    for bad in ("../x", "/abs", "a/../../b", "C:/x"):
        with pytest.raises(store.TrackerError):
            store.edit_field(conn, a["app_id"], "folder", bad, "t")
    assert store.edit_field(conn, a["app_id"], "folder", "Applications\\Fix_F1", "t")["folder"] == "Applications/Fix_F1"
    with pytest.raises(store.TrackerError, match="already used"):
        store.edit_field(conn, b["app_id"], "folder", "Applications/Fix_F1", "t")
    assert store.edit_field(conn, a["app_id"], "folder", "none", "t")["folder"] is None
