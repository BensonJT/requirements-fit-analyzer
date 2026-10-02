#!/usr/bin/env python3
"""Read-only MCP server (stdio) over scripts/lookup.py, so Claude Desktop / Cowork can look up postings.

Two tools, both read-only, both returning lookup.py's own text output:
    lookup_posting(target, include_jd=false)                  -> lookup.py <target> [--jd]
    search_postings(regex, limit=25, include_closed=false)    -> lookup.py --search <regex> --limit N [--all]

Nothing here writes: no `finder.py mark`, no SQL, no sweeps. Decisions still go through Claude Code.

Why stdlib JSON-RPC instead of the `mcp` package: two tools over newline-delimited JSON on stdin/stdout
need no framework, and the repo's requirements stay as they are.

Why a subprocess per call instead of importing lookup.py: lookup.py prints to stdout and calls sys.exit
on errors, and stdout here is the protocol channel. A child process keeps its output off the channel,
and it holds the read-only DuckDB connection only for the call -- a long-lived server holding it would
block a sweep from taking the write lock. A sweep holding the lock comes back as lookup.py's
"retry when it finishes" message, as a normal tool result.

Claude Desktop launches it inside WSL (claude_desktop_config.json):
    "jobsearch": {"command": "wsl.exe", "args": ["-d", "Ubuntu-20.04", "--cd", "<repo>", "--exec",
                  ".venv/bin/python", "scripts/mcp_lookup_server.py"]}
By hand: .venv/bin/python scripts/mcp_lookup_server.py [--db PATH]   (one JSON-RPC message per line)
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOOKUP = REPO / "scripts" / "lookup.py"
SERVER_INFO = {"name": "jobsearch-lookup", "version": "1.0.0"}
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")  # newest first
TIMEOUT_S = 180  # a --search regex scans every description; the posting lookups take ~1 s
MAX_LIMIT = 200

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


def call_tool(name, args, db):
    """MCP tools/call result. lookup.py's own exits (no match, several matches, database locked) are
    answers, so they come back as text; only a crash or a timeout is isError."""
    try:
        argv = lookup_argv(name, args or {}, db)
    except ToolInputError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    try:
        res = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"content": [{"type": "text", "text": f"lookup.py did not finish in {TIMEOUT_S} s"}],
                "isError": True}
    text = "\n".join(s.strip("\n") for s in (res.stdout, res.stderr) if s.strip()) or "(no output)"
    crashed = res.returncode not in (0, 2, 3) and "Traceback" in res.stderr
    return {"content": [{"type": "text", "text": text}], "isError": crashed}


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
                   "instructions": "Read-only lookups into the jobsearch DuckDB. Recording decisions "
                                   "(build/pass/hold) is done in Claude Code with finder.py mark."})
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
