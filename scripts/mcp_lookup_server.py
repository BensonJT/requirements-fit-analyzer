#!/usr/bin/env python3
"""MCP server (stdio) for Claude Desktop / Cowork: posting lookups plus the application tracker.

Posting tools (read-only, return lookup.py's own text output):
    lookup_posting(target, include_jd=false)                  -> lookup.py <target> [--jd]
    search_postings(regex, limit=25, include_closed=false)    -> lookup.py --search <regex> --limit N [--all]
  When DuckDB is write-locked by a sweep, both fall back to jobs.fit_snapshot in Postgres (role claude_ro)
  and say so, with the time of the snapshot.

Tracker tools (design section 11; the approved exception to "read-only by default", writes only through
the tracker library): tracker_list, tracker_find, tracker_show, tracker_set_status, tracker_note,
tracker_contact, tracker_edit, tracker_add. Each shells out to `python -m backend.tracker.cli` (trk) with
TRK_ACTOR=claude-desktop, so trk's validation, event log, re-render and exit codes are the single path.
  jobs_query(sql): ONE read-only SELECT over tracker.*, jobs.*, resume.*. Safety is layered: a keyword
  heuristic (quotes and comments stripped, one statement, must start with SELECT/WITH), then the real
  protection -- Postgres role claude_ro (SELECT only), a READ ONLY transaction, a 15 s statement_timeout,
  and a 200-row cap.

Why stdlib JSON-RPC instead of the `mcp` package: newline-delimited JSON on stdin/stdout needs no
framework, and the repo's requirements stay as they are.

Why a subprocess per call instead of importing lookup.py / the tracker: they print to stdout and call
sys.exit, and stdout here is the protocol channel. A child process keeps its output off the channel,
and it holds the read-only DuckDB connection only for the call -- a long-lived server holding it would
block a sweep from taking the write lock. (jobs_query and the snapshot fallback use psycopg in-process;
they print nothing.)

Claude Desktop launches it inside WSL (claude_desktop_config.json):
    "jobsearch": {"command": "wsl.exe", "args": ["-d", "Ubuntu-20.04", "--cd", "<repo>", "--exec",
                  ".venv/bin/python", "scripts/mcp_lookup_server.py"]}
By hand: .venv/bin/python scripts/mcp_lookup_server.py [--db PATH]   (one JSON-RPC message per line)
Env: TRACKER_PGDATABASE (default resume), JOBSEARCH_VAULT_DIR (where trk renders the view).
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOOKUP = REPO / "scripts" / "lookup.py"
SERVER_INFO = {"name": "jobsearch-lookup", "version": "2.0.0"}
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # newest first
TIMEOUT_S = 180  # a --search regex scans every description; the posting lookups take ~1 s
MAX_LIMIT = 200
ROW_CAP = 200
QUERY_TIMEOUT_MS = 15000
TRK_TIMEOUT_S = 60
ACTOR = "claude-desktop"
STATUSES = ("offer", "interview", "screen", "applied", "built", "accepted", "declined", "rejected",
            "assumed-rejected", "withdrawn", "passed", "closed-unsent")
CONTACT_ROLES = ("recruiter", "hiring_manager", "panelist", "referral", "staffing", "other")
SOURCES = ("pipeline", "linkedin", "recruiter", "referral", "board", "manual", "migrated", "sheet")

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
TOOLS = [
    {
        "name": "lookup_posting",
        "description": (
            "Look up one job posting in the jobsearch pipeline (read-only): identity, screen scores, latest "
            "judge and Jev reviews, requirement coverage, and any build/pass/hold decision. target is a "
            "posting_id (20 hex chars), a posting URL, or \"employer|title\" (fuzzy; if several postings "
            "match, they are listed with their ids -- call again with one id). include_jd adds the full "
            "job description."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "posting_id, posting URL, or \"employer|title\""},
                "include_jd": {"type": "boolean", "default": False, "description": "append the full JD"},
            },
            "required": ["target"],
        },
        "annotations": {"title": "Look up a job posting", **READ_ONLY},
    },
    {
        "name": "search_postings",
        "description": (
            "List job postings whose title or description matches a case-insensitive regex, newest first "
            "(read-only). Each line: posting_id, final score, verdict, status, employer | title. Use a "
            "posting_id with lookup_posting for the detail."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "regex": {"type": "string", "description": "case-insensitive regex over title + description"},
                "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": MAX_LIMIT},
                "include_closed": {"type": "boolean", "default": False,
                                   "description": "also list postings no longer on the employer's board"},
            },
            "required": ["regex"],
        },
        "annotations": {"title": "Search job postings", **READ_ONLY},
    },
]

WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
_STATUS_NOTE = ("Status is one of: " + ", ".join(STATUSES) + " (offer..built are active; the rest close the "
                "application).")
_WRITE_NOTE = (" Every write is recorded in the application's history as actor claude-desktop and re-renders "
               "the tracker markdown view in the vault.")
_APP = {"type": "integer", "description": "app_id (the ID column of tracker_list / tracker_find)"}


def _s(desc):
    return {"type": "string", "description": desc}


TRACKER_TOOLS = [
    {
        "name": "tracker_list",
        "description": (
            "List the job application tracker (read-only): ID, company, role, req, status, since, next action, "
            "follow-up (a trailing ! means overdue). Default scope is active applications. Start here to see "
            "where things stand or to find an app_id. " + _STATUS_NOTE),
        "inputSchema": {"type": "object", "properties": {
            "scope": {"type": "string", "enum": ["active", "closed", "all"], "default": "active"},
            "status": _s("optional: list only this status (overrides scope)")}},
        "annotations": {"title": "List tracked applications", **READ_ONLY},
    },
    {
        "name": "tracker_find",
        "description": ("Search tracked applications (read-only). Every term must match company, role, req id, "
                        "posting id or app_id (case-insensitive). Returns app_ids to use with the other tools."),
        "inputSchema": {"type": "object", "properties": {
            "terms": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "one or more search words, AND-ed"}}, "required": ["terms"]},
        "annotations": {"title": "Find tracked applications", **READ_ONLY},
    },
    {
        "name": "tracker_show",
        "description": ("One application in full (read-only): every field, its contacts and the complete "
                        "history of status changes, notes and edits."),
        "inputSchema": {"type": "object", "properties": {"app_id": _APP}, "required": ["app_id"]},
        "annotations": {"title": "Show one application", **READ_ONLY},
    },
    {
        "name": "tracker_set_status",
        "description": ("Change an application's status (WRITE). Use when something happened: applied, got a "
                        "screen, interview, offer, rejection, withdrew. " + _STATUS_NOTE + " date defaults to "
                        "today; pass the real date when it happened earlier." + _WRITE_NOTE),
        "inputSchema": {"type": "object", "properties": {
            "app_id": _APP, "status": {"type": "string", "enum": list(STATUSES)},
            "date": _s("YYYY-MM-DD, when the status took effect (default today)"),
            "note": _s("optional note recorded with the change")}, "required": ["app_id", "status"]},
        "annotations": {"title": "Set application status", **WRITE},
    },
    {
        "name": "tracker_note",
        "description": "Add a free-text note to an application's history (WRITE)." + _WRITE_NOTE,
        "inputSchema": {"type": "object", "properties": {"app_id": _APP, "text": _s("the note")},
                        "required": ["app_id", "text"]},
        "annotations": {"title": "Add a note", **WRITE},
    },
    {
        "name": "tracker_contact",
        "description": ("Record a person on an application (WRITE): recruiter, hiring manager, panelist. "
                        "Reuses an existing contact with the same name and company." + _WRITE_NOTE),
        "inputSchema": {"type": "object", "properties": {
            "app_id": _APP, "name": _s("full name"),
            "role": {"type": "string", "enum": list(CONTACT_ROLES), "default": "recruiter"},
            "email": _s("email"), "phone": _s("phone"), "linkedin": _s("LinkedIn URL"),
            "title": _s("their job title"), "company": _s("their company (e.g. a staffing firm)")},
            "required": ["app_id", "name"]},
        "annotations": {"title": "Add a contact", **WRITE},
    },
    {
        "name": "tracker_edit",
        "description": ("Edit fields of an application (WRITE); pass only what changes. An empty string clears "
                        "a field. Dates are YYYY-MM-DD. To change status use tracker_set_status." + _WRITE_NOTE),
        "inputSchema": {"type": "object", "properties": {
            "app_id": _APP, "next_action": _s("what to do next"), "follow_up": _s("follow-up due, YYYY-MM-DD"),
            "closes": _s("posting close date, YYYY-MM-DD"), "role": _s("title"), "company": _s("employer"),
            "req": _s("employer's req / job id"), "posting": _s("20-hex posting id, or 'none'"),
            "location": _s("location"), "pay": _s("pay as text"), "close_reason": _s("why it closed"),
            "applied": _s("date applied, YYYY-MM-DD"), "source": {"type": "string", "enum": list(SOURCES)},
            "folder": _s("folder path under Job_Search"), "notes": _s("the notes field")},
            "required": ["app_id"]},
        "annotations": {"title": "Edit application fields", **WRITE},
    },
    {
        "name": "tracker_add",
        "description": ("Add a new application (WRITE). company and role are required unless posting (a "
                        "pipeline posting id) is given, which fills blanks from the pipeline snapshot. "
                        "status defaults to built. folder: auto creates the application folder in the vault "
                        "(default when status is built), none skips it." + _WRITE_NOTE + " " + _STATUS_NOTE),
        "inputSchema": {"type": "object", "properties": {
            "company": _s("employer"), "role": _s("job title"), "req": _s("employer's req / job id"),
            "posting": _s("20-hex pipeline posting id, or 'none'"),
            "source": {"type": "string", "enum": list(SOURCES)},
            "status": {"type": "string", "enum": list(STATUSES), "default": "built"},
            "date": _s("status date YYYY-MM-DD (default today)"),
            "folder": {"type": "string", "enum": ["auto", "none"]},
            "location": _s("location"), "pay": _s("pay as text")}},
        "annotations": {"title": "Add an application", **WRITE},
    },
    {
        "name": "jobs_query",
        "description": (
            "Run ONE read-only SQL SELECT (or WITH ... SELECT) against the Postgres database (role claude_ro, "
            "READ ONLY, 15 s timeout, at most 200 rows). Schemas: tracker.* (application, contact, event, "
            "status, vw_applications), jobs.* (fit_snapshot: scored pipeline postings; decisions; snapshot_run), "
            "resume.* (the resume database). Use for joins and counts the other tools cannot do; use "
            "information_schema.columns to discover columns. Anything that is not a single SELECT is rejected."),
        "inputSchema": {"type": "object", "properties": {"sql": _s("a single SELECT statement")},
                        "required": ["sql"]},
        "annotations": {"title": "Query the job database", **READ_ONLY},
    },
]
TOOLS = TOOLS + TRACKER_TOOLS


class ToolInputError(ValueError):
    pass


def _bool(args, key):
    v = args.get(key, False)
    if not isinstance(v, bool):
        raise ToolInputError(f"{key} must be true or false")
    return v


def _text(args, key):
    v = args.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ToolInputError(f"{key} is required (a non-empty string)")
    return v.strip()


def lookup_argv(name, args, db):
    """lookup.py's argv for one tool call. Values never reach a shell; `--` / `--search=` keep a value
    that starts with '-' from being read as an option."""
    argv = [sys.executable, str(LOOKUP)]
    if db:
        argv += ["--db", db]
    if name == "lookup_posting":
        if _bool(args, "include_jd"):
            argv.append("--jd")
        return argv + ["--", _text(args, "target")]
    if name == "search_postings":
        limit = args.get("limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise ToolInputError(f"limit must be an integer from 1 to {MAX_LIMIT}")
        argv += [f"--search={_text(args, 'regex')}", "--limit", str(limit)]
        return argv + (["--all"] if _bool(args, "include_closed") else [])
    raise KeyError(name)


def _result(text, error=False):
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _py():
    """The repo's own venv python for children (the server itself may be launched any way)."""
    venv = REPO / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def _opt(args, key, flag, out, kind=str):
    v = args.get(key)
    if v is None:
        return
    if not isinstance(v, kind) or isinstance(v, bool):
        raise ToolInputError(f"{key} must be a {'string' if kind is str else 'number'}")
    out.append(f"{flag}={v}")   # `--flag=value` keeps a value that starts with '-' from being read as a flag


def _app_id(args):
    v = args.get("app_id")
    if isinstance(v, bool) or not isinstance(v, int):
        raise ToolInputError("app_id is required (an integer)")
    return str(v)


def trk_argv(name, args):
    """argv tail for `python -m backend.tracker.cli` per tracker tool. Values never reach a shell."""
    if name == "tracker_list":
        scope = args.get("scope", "active")
        if scope not in ("active", "closed", "all"):
            raise ToolInputError("scope must be active, closed or all")
        out = ["ls"]
        if args.get("status"):
            return out + [f"--status={args['status']}"]
        return out + ([] if scope == "active" else [f"--{scope}"])
    if name == "tracker_find":
        terms = args.get("terms")
        if not isinstance(terms, list) or not terms or not all(isinstance(t, str) and t.strip() for t in terms):
            raise ToolInputError("terms is required (a non-empty list of strings)")
        return ["find", "--"] + [t.strip() for t in terms]
    # Options first, then `--`, then positionals, so a value that starts with "-" cannot be read as a flag.
    if name == "tracker_show":
        return ["show", "--", _app_id(args)]
    if name == "tracker_set_status":
        out = ["set"]
        pos = [_app_id(args), _text(args, "status")]
        _opt(args, "date", "--date", out)
        _opt(args, "note", "--note", out)
        return out + ["--"] + pos
    if name == "tracker_note":
        return ["note", "--", _app_id(args), _text(args, "text")]
    if name == "tracker_contact":
        out = ["contact"]
        pos = [_app_id(args), _text(args, "name")]
        for k in ("role", "email", "phone", "linkedin", "title", "company"):
            _opt(args, k, f"--{k}", out)
        return out + ["--"] + pos
    if name == "tracker_edit":
        out = ["edit"]
        pos = [_app_id(args)]
        for k, flag in (("next_action", "--next-action"), ("follow_up", "--follow-up"), ("closes", "--closes"),
                        ("role", "--role"), ("company", "--company"), ("req", "--req"), ("posting", "--posting"),
                        ("location", "--location"), ("pay", "--pay"), ("close_reason", "--close-reason"),
                        ("applied", "--applied"), ("source", "--source"), ("folder", "--folder"),
                        ("notes", "--notes")):
            _opt(args, k, flag, out)
        if not out[1:]:
            raise ToolInputError("tracker_edit needs at least one field to change")
        return out + ["--"] + pos
    if name == "tracker_add":
        if args.get("folder") not in (None, "auto", "none"):
            raise ToolInputError("folder must be 'auto' or 'none' for tracker_add (set a path later with tracker_edit)")
        out = ["add"]
        for k in ("company", "role", "req", "posting", "source", "status", "date", "folder", "location", "pay"):
            _opt(args, k, f"--{k}", out)
        return out
    raise KeyError(name)


def run_tracker(name, args):
    try:
        tail = trk_argv(name, args)
    except ToolInputError as exc:
        return _result(str(exc), True)
    env = {**os.environ, "TRK_ACTOR": ACTOR}
    try:
        res = subprocess.run([_py(), "-m", "backend.tracker.cli", *tail], cwd=REPO, env=env, capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=TRK_TIMEOUT_S,
                             stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return _result(f"trk did not finish in {TRK_TIMEOUT_S} s", True)
    text = "\n".join(x.strip("\n") for x in (res.stdout, res.stderr) if x.strip()) or "(no output)"
    if res.returncode == 0:
        return _result(text)  # stderr warnings (e.g. view not re-rendered) ride along in the text
    return _result(f"trk failed (exit {res.returncode}): {text}", True)


# ---- jobs_query -------------------------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|execute|merge|vacuum|"
    r"analyze|cluster|reindex|refresh|comment|lock|listen|notify|set|reset|begin|commit|rollback|savepoint|"
    r"declare|fetch|prepare|into|pg_sleep|dblink|lo_import|lo_export)\b", re.I)


def _strip_literals(sql):
    """Blank out '...' strings, "..." identifiers, $tag$..$tag$ bodies and comments, so keyword checks only
    see real SQL. Unterminated quotes are left in place (the check then fails closed or Postgres rejects it)."""
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"\$([A-Za-z_]*)\$.*?\$\1\$", "''", sql, flags=re.S)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return re.sub(r'"(?:[^"]|"")*"', '""', sql)


def check_select(sql):
    """Heuristic guard; returns an error message or None. The real protection is the claude_ro role."""
    if not isinstance(sql, str) or not sql.strip():
        return "sql is required (a single SELECT statement)"
    if "\\" in sql or re.search(r"(?<![A-Za-z0-9_$])[eE]\s*'", sql):
        return "backslashes and E'...' string prefixes are not allowed"  # E-strings fool _strip_literals
    bare = _strip_literals(sql).strip().rstrip(";").strip()
    if ";" in bare:
        return "only one statement is allowed"
    if not re.match(r"(select|with)\b", bare, re.I):
        return "only a SELECT (or WITH ... SELECT) statement is allowed"
    bad = _FORBIDDEN.search(bare)
    if bad:
        return f"keyword not allowed in a read-only query: {bad.group(0).upper()}"
    return None


def pg_connect_ro():
    import psycopg
    from psycopg.rows import dict_row
    conn = psycopg.connect(host=os.environ.get("PGHOST", "localhost"), user="claude_ro",
                           dbname=os.environ.get("TRACKER_PGDATABASE") or "resume", row_factory=dict_row,
                           connect_timeout=10,
                           options=f"-c default_transaction_read_only=on -c statement_timeout={QUERY_TIMEOUT_MS} "
                                   f"-c idle_in_transaction_session_timeout=5000 -c lock_timeout=5000")
    conn.read_only = True  # BEGIN READ ONLY; autocommit stays off, so the whole call is one such transaction
    return conn


def _cell(v):
    return "" if v is None else str(v).replace("\t", " ").replace("\n", " ")


def format_rows(cols, rows, truncated=False):
    lines = ["\t".join(cols)] + ["\t".join(_cell(v) for v in r) for r in rows]
    lines.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''}"
                 + (f"; capped at {ROW_CAP}, add LIMIT/WHERE for the rest" if truncated else "") + ")")
    return "\n".join(lines)


def run_jobs_query(args):
    sql = args.get("sql")
    problem = check_select(sql)
    if problem:
        return _result(problem, True)
    from psycopg.rows import tuple_row
    try:
        with pg_connect_ro() as conn:
            cur = conn.cursor(row_factory=tuple_row)
            cur.execute(sql, prepare=True)  # extended protocol: Postgres refuses more than one statement
            if cur.description is None:
                return _result("the statement returned no result set", True)
            cols = [d.name for d in cur.description]
            rows = cur.fetchmany(ROW_CAP + 1)
    except Exception as exc:  # noqa: BLE001 - surface the database's own message
        return _result(f"query failed: {type(exc).__name__}: {str(exc).strip()}", True)
    truncated = len(rows) > ROW_CAP
    return _result(format_rows(cols, rows[:ROW_CAP], truncated))


# ---- DuckDB-locked fallback to jobs.fit_snapshot ----------------------------------------------------------

_FALLBACK_COLS = ("posting_id, final_score, verdict, status, employer, title, location_primary, url, "
                  "latest_decision, in_tracker")


def _snapshot_note(conn):
    row = conn.execute("SELECT max(run_at) AS t FROM jobs.snapshot_run").fetchone()
    when = row["t"] if row else None
    return (f"[DuckDB is locked by a sweep; this answer comes from the Postgres snapshot taken at {when}"
            " and may be a little behind.]")


def snapshot_fallback(name, args):
    """Answer lookup_posting / search_postings from jobs.fit_snapshot. Returns an MCP result."""
    try:
        with pg_connect_ro() as conn:
            note = _snapshot_note(conn)
            if name == "lookup_posting":
                t = _text(args, "target")
                if re.fullmatch(r"[0-9a-f]{20}", t):
                    rows = conn.execute("SELECT * FROM jobs.fit_snapshot WHERE posting_id = %s", [t]).fetchall()
                elif t.lower().startswith("http"):
                    rows = conn.execute("SELECT * FROM jobs.fit_snapshot WHERE url = %s", [t]).fetchall()
                else:
                    emp, _, title = t.partition("|")
                    rows = conn.execute(
                        "SELECT * FROM jobs.fit_snapshot WHERE employer ILIKE %s AND title ILIKE %s "
                        "ORDER BY final_score DESC NULLS LAST LIMIT 10",
                        [f"%{emp.strip()}%", f"%{title.strip()}%"]).fetchall()
                if not rows:
                    return _result(f"{note}\nno posting in the snapshot matches {t!r}")
                if len(rows) > 1:
                    body = "\n".join(f"{r['posting_id']}  {r['final_score']}  {r['verdict']}  {r['status']}  "
                                     f"{r['employer']} | {r['title']}" for r in rows)
                    return _result(f"{note}\nseveral postings match; call again with one id:\n{body}")
                r = rows[0]
                skip = set() if _bool(args, "include_jd") else {"description_text"}
                body = "\n".join(f"{k}: {_cell(v)}" for k, v in r.items() if k not in skip and v is not None)
                return _result(f"{note}\n{body}")
            limit = args.get("limit", 25)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
                raise ToolInputError(f"limit must be an integer from 1 to {MAX_LIMIT}")
            rx = _text(args, "regex")
            closed = "" if _bool(args, "include_closed") else "AND status = 'active' "
            rows = conn.execute(
                f"SELECT {_FALLBACK_COLS} FROM jobs.fit_snapshot WHERE (title ~* %s OR description_text ~* %s) "
                f"{closed}ORDER BY coalesce(posted_at, first_seen_at::date) DESC NULLS LAST LIMIT %s",
                [rx, rx, limit]).fetchall()
            body = "\n".join(f"{r['posting_id']}  {r['final_score']}  {r['verdict']}  {r['status']}  "
                             f"{r['employer']} | {r['title']}" for r in rows) or "(no matches)"
            return _result(f"{note}\n{body}\n({len(rows)} shown)")
    except ToolInputError as exc:
        return _result(str(exc), True)
    except Exception as exc:  # noqa: BLE001
        return _result(f"DuckDB is locked and the Postgres snapshot is unavailable ({type(exc).__name__}: "
                       f"{str(exc).strip()[:200]}). A sweep holds the write lock; retry when it finishes.")


def call_tool(name, args, db):
    """MCP tools/call result. lookup.py's own exits (no match, several matches) are answers, so they come
    back as text; only a crash or a timeout is isError. A DuckDB lock falls back to the Postgres snapshot."""
    if name in {t["name"] for t in TRACKER_TOOLS}:
        return run_jobs_query(args or {}) if name == "jobs_query" else run_tracker(name, args or {})
    try:
        argv = lookup_argv(name, args or {}, db)
    except ToolInputError as exc:
        return _result(str(exc), True)
    try:
        res = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return _result(f"lookup.py did not finish in {TIMEOUT_S} s", True)
    text = "\n".join(s.strip("\n") for s in (res.stdout, res.stderr) if s.strip()) or "(no output)"
    if res.returncode != 0 and "write lock" in res.stderr:
        return snapshot_fallback(name, args or {})
    crashed = res.returncode not in (0, 2, 3) and "Traceback" in res.stderr
    return _result(text, crashed)


def handle(msg, db):
    """One JSON-RPC message -> the response dict, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "invalid request"}}
    if "id" not in msg:
        return None  # notifications/initialized, notifications/cancelled: nothing to answer
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER_INFO,
                   "instructions": "Posting lookups (read-only) and the application tracker. Tracker "
                                   "writes are logged as claude-desktop. Pipeline decisions "
                                   "(build/pass/hold) are recorded in Claude Code with finder.py mark."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return err(-32602, f"unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return err(-32602, "arguments must be an object")
        return ok(call_tool(name, args, db))
    return err(-32601, f"method not found: {method}")


def serve(stdin, stdout, db=None):
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(msg, db)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="database path passed to lookup.py (default: lookup.py's own)")
    a = ap.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    serve(sys.stdin, sys.stdout, a.db)


if __name__ == "__main__":
    main()
