"""scripts/mcp_lookup_server.py: the read-only MCP server over scripts/lookup.py. Every test builds its own
tmp DuckDB and drives the server as Claude Desktop does -- a subprocess, one JSON-RPC message per stdin line.
"""
import json
import os
import subprocess
import sys
from datetime import datetime

import duckdb
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import store  # noqa: E402

SERVER = os.path.join(ROOT, "scripts", "mcp_lookup_server.py")
NOW = datetime(2026, 9, 28, 12, 0)
ACTIVE = "a" * 20
CLOSED = "b" * 20
ACTIVE2 = "c" * 20


def _db(tmp_path):
    path = str(tmp_path / "jobs.duckdb")
    con = store.connect(path)
    for pid, title, status in ((ACTIVE, "Lead Business Analyst", "active"),
                               (ACTIVE2, "Principal Business Analyst", "active"),
                               (CLOSED, "Senior Business Analyst", "closed")):
        con.execute(
            "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary, status, "
            "description_hash, description_text, first_seen_at, last_seen_at) VALUES (?, 'Acme', 'greenhouse', "
            "?, ?, ?, 'Remote - USA', ?, 'h', ?, ?, ?)",
            [pid, pid, title, f"https://x/{pid}", status, f"## Requirements\nprocess mapping for {title}\n", NOW, NOW])
    con.close()
    return path


def _rpc(db, *msgs):
    """Send messages to a fresh server; return the responses keyed by id."""
    stdin = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in msgs)
    res = subprocess.run([sys.executable, SERVER, "--db", db], input=stdin, capture_output=True, text=True,
                         timeout=120, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    out = [json.loads(line) for line in lines]  # every stdout line is protocol: nothing else may be printed
    return {r["id"]: r for r in out}


def _call(mid, name, **arguments):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _text(resp):
    return resp["result"]["content"][0]["text"]


def test_initialize_and_tools_list_expose_only_the_two_read_only_tools(tmp_path):
    r = _rpc(_db(tmp_path),
             {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert set(r) == {1, 2, 3}  # the notification got no answer
    assert r[1]["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in r[1]["result"]["capabilities"]
    tools = r[2]["result"]["tools"]
    assert [t["name"] for t in tools] == ["lookup_posting", "search_postings"]
    assert all(t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"] for t in tools)
    assert r[3]["result"] == {}


def test_unknown_protocol_version_gets_the_newest_supported(tmp_path):
    r = _rpc(_db(tmp_path), {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "1999-01-01"}})
    assert r[1]["result"]["protocolVersion"] == "2025-11-25"


def test_lookup_posting_by_id_by_employer_title_and_with_jd(tmp_path):
    r = _rpc(_db(tmp_path), _call(1, "lookup_posting", target=ACTIVE),
             _call(2, "lookup_posting", target="Acme|Lead Business Analyst"),
             _call(3, "lookup_posting", target=ACTIVE, include_jd=True))
    for mid in (1, 2, 3):
        assert r[mid]["result"]["isError"] is False
        assert "Acme | Lead Business Analyst  [active]" in _text(r[mid])
    assert "process mapping for Lead" not in _text(r[1])
    assert "process mapping for Lead Business Analyst" in _text(r[3])


def test_lookup_several_matches_and_no_match_are_answers_not_errors(tmp_path):
    r = _rpc(_db(tmp_path), _call(1, "lookup_posting", target="Acme|Business Analyst"),
             _call(2, "lookup_posting", target="-h"))  # a leading dash is a target, never an option
    assert r[1]["result"]["isError"] is False
    assert "re-run with one posting_id" in _text(r[1]) and ACTIVE in _text(r[1]) and ACTIVE2 in _text(r[1])
    assert r[2]["result"]["isError"] is False
    assert "no posting matches '-h'" in _text(r[2])


def test_search_postings_active_only_by_default(tmp_path):
    r = _rpc(_db(tmp_path), _call(1, "search_postings", regex="business analyst"),
             _call(2, "search_postings", regex="business analyst", include_closed=True, limit=1),
             _call(3, "search_postings", regex="business analyst", include_closed=True))
    assert ACTIVE in _text(r[1]) and CLOSED not in _text(r[1])
    assert len(_text(r[2]).splitlines()) == 1
    assert ACTIVE in _text(r[3]) and CLOSED in _text(r[3])


def test_bad_arguments_are_tool_errors(tmp_path):
    r = _rpc(_db(tmp_path), _call(1, "lookup_posting"), _call(2, "search_postings", regex="x", limit=0),
             _call(3, "lookup_posting", target=ACTIVE, include_jd="yes"))
    for mid in (1, 2, 3):
        assert r[mid]["result"]["isError"] is True


def test_protocol_errors(tmp_path):
    r = _rpc(_db(tmp_path), "{not json", {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
             _call(2, "mark_posting", target=ACTIVE))
    assert r[None]["error"]["code"] == -32700
    assert r[1]["error"]["code"] == -32601
    assert r[2]["error"]["code"] == -32602  # there is no write tool to call


def test_locked_database_returns_the_retry_message(tmp_path):
    db = _db(tmp_path)
    writer = duckdb.connect(db)  # a running sweep holds the write lock
    try:
        r = _rpc(db, _call(1, "lookup_posting", target=ACTIVE))
    finally:
        writer.close()
    assert r[1]["result"]["isError"] is False
    assert "retry when it finishes" in _text(r[1])


def test_the_server_never_writes(tmp_path):
    db = _db(tmp_path)
    before = os.stat(db).st_mtime_ns
    _rpc(db, _call(1, "lookup_posting", target=ACTIVE), _call(2, "search_postings", regex="analyst"))
    assert os.stat(db).st_mtime_ns == before
    with duckdb.connect(db, read_only=True) as con:
        assert con.execute("SELECT count(*) FROM postings").fetchone()[0] == 3


@pytest.mark.parametrize("args, tail", [
    ({"target": "x"}, ["--", "x"]),
    ({"target": "x", "include_jd": True}, ["--jd", "--", "x"]),
    ({"regex": "-a", "limit": 5, "include_closed": True}, ["--search=-a", "--limit", "5", "--all"]),
])
def test_lookup_argv(args, tail):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import mcp_lookup_server as srv
    name = "lookup_posting" if "target" in args else "search_postings"
    argv = srv.lookup_argv(name, args, "db.duckdb")
    assert argv[2:4] == ["--db", "db.duckdb"] and argv[4:] == tail
