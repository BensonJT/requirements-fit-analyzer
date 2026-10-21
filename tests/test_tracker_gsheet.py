"""The Google Sheet view: tab contents and the formatting requests (no network)."""
import datetime as dt

import psycopg
import pytest

from backend.tracker import db, gsheet, store

TEST_DB = "tracker_test_gsheet"


@pytest.fixture(scope="module")
def _db():
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


@pytest.fixture
def conn(_db):
    _db.execute("TRUNCATE tracker.application, tracker.contact, tracker.network, tracker.render_log "
                "RESTART IDENTITY CASCADE")
    return _db


def test_build_tabs_splits_active_closed_with_contacts_and_history(conn):
    a = store.add_application(conn, company="Acme", role="Analyst", actor="user", status="applied",
                              status_date="2026-10-01")
    b = store.add_application(conn, company="=Evil", role="Lead", actor="user", status="passed")
    store.add_contact(conn, a["app_id"], "Pat Lee", "user", email="pat@acme.com", linkedin="https://x/in/pat")
    store.add_note(conn, a["app_id"], "phone screen next week", "user")
    tabs = gsheet.build_tabs(conn, today=dt.date(2026, 10, 11))
    hdr, row = tabs["Active"][0], tabs["Active"][1]
    cell = dict(zip(hdr, row))
    assert cell["ID"] == a["app_id"] and cell["Days"] == 10 and cell["Contact"] == "Pat Lee"
    assert cell["Email"] == "pat@acme.com" and cell["LinkedIn"].startswith("=HYPERLINK(")
    assert cell["Latest note"].endswith("phone screen next week")
    closed = dict(zip(tabs["Closed"][0], tabs["Closed"][1]))
    assert closed["Company"] == "'=Evil" and "Reason" in tabs["Closed"][0]   # formula text is escaped
    assert len(tabs["Contacts"]) == 2 and tabs["Contacts"][1][0] == "Pat Lee"
    assert {r[4] for r in tabs["History"][1:]} >= {"created", "contact", "note"}
    assert all(len(r) == len(gsheet.TABS[t]) for t, rows in tabs.items() for r in rows)


def test_format_requests_keep_filter_criteria_and_replace_rules():
    old = {"filterSpecs": [{"columnIndex": 5, "filterCriteria": {"hiddenValues": ["passed"]}},
                           {"columnIndex": 99, "filterCriteria": {}}]}
    req = gsheet.format_requests(7, gsheet.ACTIVE, 10, old, n_cond=3)
    filt = next(r["setBasicFilter"]["filter"] for r in req if "setBasicFilter" in r)
    assert filt["range"]["startRowIndex"] == 1 and filt["range"]["endRowIndex"] == 11
    assert filt["filterSpecs"] == [old["filterSpecs"][0]]                  # out-of-range column dropped
    assert sum("deleteConditionalFormatRule" in r for r in req) == 3
    adds = sum("addConditionalFormatRule" in r for r in req)
    assert adds == len(gsheet.STATUS_COLORS) + 1                            # statuses + overdue follow-up


def test_not_configured_in_tests_and_off_the_real_db(monkeypatch):
    assert not gsheet.configured() and gsheet.spawn() is False
    monkeypatch.setenv("TRACKER_SHEET_ID", "abc")
    monkeypatch.setenv("TRACKER_PGDATABASE", "some_test_db")
    assert not gsheet.configured()
    monkeypatch.setenv("TRACKER_PGDATABASE", "resume")
    assert gsheet.configured()


def test_refresh_against_a_fake_api(conn):
    store.add_application(conn, company="Acme", role="Analyst", actor="user", status="applied")

    class Fake:
        def __init__(self):
            self.calls, self.titles = [], ["Sheet1"]
        def meta(self):
            return {"sheets": [{"properties": {"sheetId": i, "title": t, "gridProperties": {"rowCount": 10, "columnCount": 5}}}
                               for i, t in enumerate(self.titles)]}
        def batch(self, reqs):
            self.calls.append(("batch", reqs))
            for r in reqs:
                if "addSheet" in r: self.titles.append(r["addSheet"]["properties"]["title"])
                if "updateSheetProperties" in r and "title" in r["updateSheetProperties"]["properties"]:
                    self.titles[0] = r["updateSheetProperties"]["properties"]["title"]
        def clear(self, ranges): self.calls.append(("clear", ranges))
        def write(self, data): self.calls.append(("write", data))

    api = Fake()
    counts = gsheet.refresh(conn, api=api)
    assert counts["Active"] == 1 and sorted(api.titles) == sorted(gsheet.TABS)
    write = next(c[1] for c in api.calls if c[0] == "write")
    active = next(d for d in write if d["range"] == "'Active'!A1")
    assert active["values"][0][0].startswith("Generated from the tracker database") and active["values"][1][0] == "ID"
