"""MCP tracker tools (design section 11): throwaway database, server driven through handle(). Skipped when
Postgres is unreachable. Fake data only."""
import json
import os
import sys

import pytest

psycopg = pytest.importorskip("psycopg")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from backend.tracker import db  # noqa: E402
import mcp_lookup_server as srv  # noqa: E402

TEST_DB = "resume_test_wp6"
PID = "0123456789abcdef0123"


@pytest.fixture(scope="module")
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
def env(_db, tmp_path, monkeypatch):
    _db.execute("TRUNCATE tracker.application, tracker.contact, tracker.network, tracker.render_log "
                "RESTART IDENTITY CASCADE")
    _db.execute("TRUNCATE jobs.fit_snapshot, jobs.snapshot_run")
    monkeypatch.setenv("TRACKER_PGDATABASE", TEST_DB)
    monkeypatch.setenv("JOBSEARCH_VAULT_DIR", str(tmp_path))
    monkeypatch.delenv("TRK_ACTOR", raising=False)
    from backend.finder.tracker_sync import job_search_dir
    job_search_dir(str(tmp_path)).mkdir(parents=True)
    try:
        c = srv.pg_connect_ro()
        c.close()
    except psycopg.OperationalError as e:
        pytest.skip(f"claude_ro cannot connect: {e}")
    return _db


def call(tool, **arguments):
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": arguments}}, None)
    r = resp["result"]
    return r["content"][0]["text"], r["isError"]


def test_tools_list_has_tracker_tools():
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, None)
    names = [t["name"] for t in resp["result"]["tools"]]
    for n in ("lookup_posting", "search_postings", "tracker_list", "tracker_find", "tracker_show",
              "tracker_set_status", "tracker_note", "tracker_contact", "tracker_edit", "tracker_add",
              "jobs_query"):
        assert n in names
    writes = {t["name"] for t in resp["result"]["tools"] if not t["annotations"]["readOnlyHint"]}
    assert writes == {"tracker_set_status", "tracker_note", "tracker_contact", "tracker_edit", "tracker_add"}


def test_round_trip_records_actor(env):
    text, err = call("tracker_add", company="Acme Corp", role="Sr Analyst", req="R1", status="built",
                     folder="none")
    assert not err, text
    assert "added #1" in text
    text, err = call("tracker_set_status", app_id=1, status="applied", date="2026-10-01", note="sent")
    assert not err, text
    assert call("tracker_note", app_id=1, text="recruiter called")[1] is False
    text, err = call("tracker_contact", app_id=1, name="Pat Example", role="recruiter", email="pat@example.com")
    assert not err, text
    text, _ = call("tracker_edit", app_id=1, next_action="Follow up", follow_up="2026-10-30")
    assert "next_action" in text
    text, _ = call("tracker_list")
    assert "Acme Corp" in text and "applied" in text
    text, _ = call("tracker_find", terms=["acme", "r1"])
    assert "(1 match)" in text
    text, _ = call("tracker_show", app_id=1)
    assert "Pat Example" in text and "recruiter called" in text
    assert call("tracker_list", scope="closed")[0].startswith("ID")
    actors = {r["actor"] for r in env.execute("SELECT actor FROM tracker.event")}
    assert actors == {"claude-desktop"}
    # the write re-rendered the view into the tmp vault
    from pathlib import Path
    assert any("Acme Corp" in f.read_text(encoding="utf-8") for f in Path(os.environ["JOBSEARCH_VAULT_DIR"]).rglob("*.md"))


def test_errors_are_error_results(env):
    call("tracker_add", company="Acme", role="Analyst", status="built", folder="none")
    text, err = call("tracker_set_status", app_id=1, status="bogus")
    assert err and "allowed" in text
    text, err = call("tracker_show", app_id=999)
    assert err and "no application" in text
    assert call("tracker_edit", app_id=1)[1] is True
    assert call("tracker_note", text="x")[1] is True  # missing app_id


def test_jobs_query_select_and_rejections(env):
    call("tracker_add", company="Acme", role="Analyst", status="built", folder="none")
    text, err = call("jobs_query", sql="SELECT app_id, company FROM tracker.application ORDER BY 1;")
    assert not err, text
    assert text.splitlines()[0] == "app_id\tcompany" and "1\tAcme" in text
    text, err = call("jobs_query", sql="WITH x AS (SELECT 1 AS n) SELECT n FROM x")
    assert not err and "(1 row)" in text
    text, err = call("jobs_query", sql="SELECT 'DELETE; DROP' AS s")  # keywords inside a literal are fine
    assert not err, text
    for bad in ("DELETE FROM tracker.application", "UPDATE tracker.application SET company='x'",
                "SELECT 1; SELECT 2", "SELECT 1; DROP TABLE tracker.application",
                "CREATE TABLE t (a int)", "DROP SCHEMA tracker CASCADE", "SELECT * INTO t FROM tracker.status",
                "", "EXPLAIN ANALYZE DELETE FROM tracker.application"):
        _, err = call("jobs_query", sql=bad)
        assert err, bad
    # the role is the real protection: bypass the heuristic and the database still refuses
    conn = srv.pg_connect_ro()
    with pytest.raises(psycopg.Error):
        conn.execute("DELETE FROM tracker.application")
    conn.close()
    assert env.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"] == 1


def test_jobs_query_caps_rows(env):
    text, err = call("jobs_query", sql="SELECT g FROM generate_series(1, 500) g")
    assert not err
    assert text.endswith("(200 rows; capped at 200, add LIMIT/WHERE for the rest)")


def test_duckdb_lock_falls_back_to_snapshot(env, tmp_path):
    import duckdb
    env.execute("INSERT INTO jobs.fit_snapshot (posting_id, employer, title, url, status, final_score, verdict, "
                "description_text) VALUES (%s, 'Acme', 'Lead Analyst', 'https://x/1', 'active', 80, 'strong', "
                "'process mapping')", [PID])
    env.execute("INSERT INTO jobs.snapshot_run (rows, decisions, seconds, source) VALUES (1, 0, 1, 'test')")
    path = str(tmp_path / "locked.duckdb")
    holder = duckdb.connect(path)  # this process holds the write lock; the lookup child cannot open it
    try:
        resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "lookup_posting", "arguments": {"target": PID}}}, path)
        text = resp["result"]["content"][0]["text"]
        assert "snapshot taken at" in text and "Lead Analyst" in text and not resp["result"]["isError"]
        resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                           "params": {"name": "search_postings", "arguments": {"regex": "process"}}}, path)
        text = resp["result"]["content"][0]["text"]
        assert "snapshot taken at" in text and PID in text
    finally:
        holder.close()


def test_jobs_query_estring_bypass_and_multi_statement(env, monkeypatch):
    payload = "SELECT E'\\'' ; COMMIT; START TRANSACTION READ WRITE; DELETE FROM tracker.application; SELECT 1"
    text, err = call("jobs_query", sql=payload)
    assert err
    assert call("jobs_query", sql="SELECT e'x'")[1] is True
    assert call("jobs_query", sql="SELECT 'a\\b'")[1] is True
    # even if the heuristic is bypassed, the extended protocol refuses several statements
    monkeypatch.setattr(srv, "check_select", lambda sql: None)
    call("tracker_add", company="Acme", role="Analyst", status="built", folder="none")
    for sql in ("SELECT 1; SELECT 2", payload, "SELECT 1; DELETE FROM tracker.application"):
        text, err = call("jobs_query", sql=sql)
        assert err and "query failed" in text, sql
    assert env.execute("SELECT count(*) AS n FROM tracker.application").fetchone()["n"] == 1


def test_jobs_query_like_still_works(env):
    call("tracker_add", company="Acme", role="Analyst", status="built", folder="none")
    text, err = call("jobs_query", sql="SELECT company FROM tracker.application WHERE company LIKE '%cm%'")
    assert not err and "Acme" in text


def test_jobs_query_connection_timeouts(env):
    conn = srv.pg_connect_ro()
    row = conn.execute("SELECT current_setting('lock_timeout') AS l, "
                       "current_setting('idle_in_transaction_session_timeout') AS i").fetchone()
    conn.close()
    assert row["l"] == "5s" and row["i"] == "5s"


def test_trk_argv_options_first_then_positionals():
    argv = srv.trk_argv("tracker_note", {"app_id": 3, "text": "-starts with dash"})
    assert argv == ["note", "--", "3", "-starts with dash"]
    argv = srv.trk_argv("tracker_set_status", {"app_id": 3, "status": "applied", "note": "-x"})
    assert argv == ["set", "--note=-x", "--", "3", "applied"]
    argv = srv.trk_argv("tracker_contact", {"app_id": 3, "name": "-Pat", "email": "-a@example.com"})
    assert argv == ["contact", "--email=-a@example.com", "--", "3", "-Pat"]


def test_dash_values_round_trip(env):
    assert not call("tracker_add", company="-Acme", role="-Analyst", folder="none")[1]
    text, err = call("tracker_note", app_id=1, text="-starts with dash")
    assert not err, text
    text, err = call("tracker_set_status", app_id=1, status="applied", note="-x")
    assert not err, text
    text, err = call("tracker_contact", app_id=1, name="-Pat", email="-p@example.com")
    assert not err, text
    text, err = call("tracker_edit", app_id=1, next_action="-call", notes="-n")
    assert not err, text
    notes = [r["note"] for r in env.execute("SELECT note FROM tracker.event WHERE note IS NOT NULL")]
    assert "-starts with dash" in notes and "-x" in notes


def test_tracker_add_folder_only_auto_or_none(env):
    text, err = call("tracker_add", company="Acme", role="A", folder="Applications/Evil")
    assert err and "auto" in text
    assert call("tracker_add", company="Acme", role="A", folder="none")[1] is False


def test_tracker_edit_bad_source_changes_nothing(env):
    call("tracker_add", company="Acme", role="Keep", folder="none")
    text, err = call("tracker_edit", app_id=1, source="bogus", role="Changed")
    assert err
    assert env.execute("SELECT role FROM tracker.application").fetchone()["role"] == "Keep"
    assert call("tracker_edit", app_id=1, folder="../x")[1] is True
