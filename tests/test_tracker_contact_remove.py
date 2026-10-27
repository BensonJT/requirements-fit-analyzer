"""trk contact-remove (store.remove_contact). Throwaway database resume_test_contact_rm, skipped when Postgres is
unreachable. Fake data only."""
import pytest

psycopg = pytest.importorskip("psycopg")
from backend.tracker import db, store  # noqa: E402

TEST_DB = "resume_test_contact_rm"


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


def _app(pgc, company):
    return store.add_application(pgc, company=company, role="Lead", actor="test", status="applied")["app_id"]


def test_remove_unlinks_logs_and_deletes_an_orphan(pgc):
    a = _app(pgc, "Acme")
    c = store.add_contact(pgc, a, "Messy\nName", "test", company="Acme")
    r = store.remove_contact(pgc, a, c["contact_id"], "test")
    assert r == {"name": "Messy Name", "roles": ["recruiter"], "deleted": True}
    assert pgc.execute("SELECT count(*) AS n FROM tracker.contact WHERE contact_id = %s", [c["contact_id"]]).fetchone()["n"] == 0
    ev = pgc.execute("SELECT kind, old_value, note FROM tracker.event WHERE app_id = %s ORDER BY event_id DESC LIMIT 1", [a]).fetchone()
    assert ev["kind"] == "contact" and ev["old_value"] == "Messy Name" and ev["note"].startswith("removed recruiter")


def test_contact_shared_with_another_application_is_kept(pgc):
    a1, a2 = _app(pgc, "Beta"), _app(pgc, "Beta")
    c = store.add_contact(pgc, a1, "Pat", "test", company="Beta")
    store.add_contact(pgc, a2, "Pat", "test", company="Beta")
    r = store.remove_contact(pgc, a1, c["contact_id"], "test")
    assert r["deleted"] is False
    assert pgc.execute("SELECT count(*) AS n FROM tracker.app_contact WHERE contact_id = %s", [c["contact_id"]]).fetchone()["n"] == 1


def test_removing_a_contact_not_on_the_row_is_refused(pgc):
    a = _app(pgc, "Gamma")
    c = store.add_contact(pgc, _app(pgc, "Gamma"), "Lee", "test", company="Gamma")
    with pytest.raises(store.TrackerError, match="not linked"):
        store.remove_contact(pgc, a, c["contact_id"], "test")
