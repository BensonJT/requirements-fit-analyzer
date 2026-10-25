"""`trk`: the application tracker command line (design section 5).

Exit codes: 0 ok, 1 error (TrackerError, database or file problem, `check` found problems), 2 usage.
Actor for the event log: $TRK_ACTOR, default 'user'.
"""
import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from backend.tracker import db, folders, gsheet, render, store
from backend.tracker.store import TrackerError

EDIT_OPTIONS = {   # argparse dest -> (flag, store field)
    "next_action": ("--next-action", "next_action"), "follow_up": ("--follow-up", "follow_up_due"),
    "closes": ("--closes", "closes_on"), "role": ("--role", "role"), "company": ("--company", "company"),
    "req": ("--req", "employer_job_id"), "posting": ("--posting", "posting_id"),
    "location": ("--location", "location"), "pay": ("--pay", "pay_text"),
    "close_reason": ("--close-reason", "close_reason"), "applied": ("--applied", "date_applied"),
    "source": ("--source", "source"), "folder": ("--folder", "folder"), "notes": ("--notes", "notes"),
}


def actor():
    return os.environ.get("TRK_ACTOR") or "user"


def trunc(s, n):
    s = "" if s is None else " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "~"


def print_table(headers, rows):
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    for r in [headers, *rows]:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def list_table(rows, closed):
    today = date.today()
    out = []
    for r in rows:
        base = [r["app_id"], trunc(r["company"], 24), trunc(r["role"], 38), trunc(r["employer_job_id"], 14),
                r["status"], r["status_date"]]
        if closed:
            out.append(base + [trunc(r["close_reason"], 30)])
        else:
            fu = r["follow_up_due"]
            flag = "!" if fu and fu < today and r["is_active"] else ""
            out.append(base + [trunc(r["next_action"], 30), f"{fu}{flag}" if fu else ""])
    hdr = ["ID", "Company", "Role", "Req", "Status", "Since"]
    print_table(hdr + (["Reason"] if closed else ["Next action", "Follow-up"]), out)
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def get_job_dir():
    return render.job_dir_from_env()


def rerender(conn, args):
    """Re-render after a write. Never raises: a refusal or file error only warns, the write stays."""
    if getattr(args, "no_render", False):
        return
    try:
        jd = get_job_dir()
        if jd is None:
            print("warning: JOBSEARCH_VAULT_DIR not set; tracker view not re-rendered", file=sys.stderr)
            return
        res = render.render(conn, jd)
        gsheet.spawn()
        if res.status == "refused":
            print(f"warning: {res.path.name} was edited by hand ({res.notes[0]}); the database change "
                  "is saved but the view was NOT updated.\nApply your edit with trk, then run "
                  "`trk render --force` to overwrite the file.", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - the DB write already succeeded
        print(f"warning: could not render the tracker view: {e}", file=sys.stderr)


def cmd_sheet(conn, a):
    """Refresh the Google Sheet view now (in the foreground)."""
    if not gsheet.configured():
        raise UsageError("TRACKER_SHEET_ID is not set in .env (or TRACKER_PGDATABASE is not the real database)")
    counts = gsheet.run_locked(conn)
    print("refreshed the tracker sheet: " + ", ".join(f"{k} {v}" for k, v in counts.items()))


def cmd_ls(conn, a):
    if a.status:
        rows = store.list_apps(conn, active=None, status=a.status)
        active_rows = [r for r in rows if r["is_active"]]
        closed_rows = [r for r in rows if not r["is_active"]]
        if active_rows:
            list_table(active_rows, False)
        if closed_rows:
            list_table(closed_rows, True)
        if not rows:
            print("(0 rows)")
    elif a.all:
        list_table(store.list_apps(conn, active=True), False)
        print()
        list_table(store.list_apps(conn, active=False), True)
    else:
        list_table(store.list_apps(conn, active=not a.closed), a.closed)


def cmd_find(conn, a):
    rows = store.find(conn, a.terms)
    print_table(["ID", "Company", "Role", "Req", "Status", "Since", "Folder"],
                [[r["app_id"], trunc(r["company"], 24), trunc(r["role"], 38), trunc(r["employer_job_id"], 14),
                  r["status"], r["status_date"], r["folder"] or ""] for r in rows])
    print(f"({len(rows)} match{'es' if len(rows) != 1 else ''})")


def cmd_show(conn, a):
    row = store.get(conn, a.app_id)
    if row is None:
        raise TrackerError(f"no application with app_id {a.app_id}")
    skip = {"contacts", "events", "is_active", "sort_order"}
    w = max(len(k) for k in row if k not in skip)
    for k, v in row.items():
        if k not in skip:
            print(f"{k.ljust(w)}  {'' if v is None else v}")
    print("\nContacts")
    for c in row["contacts"] or []:
        bits = [c["name"], f"({c['role']})"] + [str(c[k]) for k in ("title", "company", "email", "phone", "linkedin") if c[k]]
        print("  " + "  ".join(bits))
    if not row["contacts"]:
        print("  (none)")
    print("\nHistory")
    for e in row["events"]:
        detail = e["note"] or ""
        if e["kind"] == "status":
            detail = f"{e['old_value'] or ''} -> {e['new_value']} ({e['status_date']}) {detail}".strip()
        elif e["kind"] == "field":
            detail = f"{e['field']}: {e['old_value'] or ''} -> {e['new_value'] or ''}"
        elif e["kind"] == "created":
            detail = f"status {e['status']} ({e['status_date']})"
        print(f"  {e['at']:%Y-%m-%d %H:%M}  {e['kind']:<8} {e['actor']:<14} {detail}")


def cmd_set(conn, a):
    r = store.set_status(conn, a.app_id, a.status, actor(), status_date=a.date, note=a.note)
    print(f"#{r['app_id']} {r['company']}: {r['status']} ({r['status_date']})")
    rerender(conn, a)


def cmd_note(conn, a):
    store.add_note(conn, a.app_id, a.text, actor())
    print(f"note added to #{a.app_id}")
    rerender(conn, a)


def cmd_contact(conn, a):
    extra = {k: getattr(a, k) for k in ("title", "email", "phone", "linkedin") if getattr(a, k)}
    if a.company:
        extra["company"] = a.company
    c = store.add_contact(conn, a.app_id, a.name, actor(), role=a.role, **extra)
    print(f"{'added' if c['created'] else 'linked existing'} contact {c['name']} ({c['role']}) on #{a.app_id}")
    rerender(conn, a)


def cmd_edit(conn, a):
    changes = [(field, getattr(a, dest)) for dest, (_, field) in EDIT_OPTIONS.items()
               if getattr(a, dest) is not None]
    if not changes:
        raise UsageError("edit needs at least one option (see trk edit --help)")
    store.edit_fields(conn, a.app_id, dict(changes), actor())   # all or nothing
    for field, value in changes:
        print(f"#{a.app_id} {field} = {value!r}")
    pid = store._posting_id(dict(changes).get("posting_id"))
    if pid and not snapshot_row(conn, pid):   # same as add: the sheet's "open" link works the same day
        ensure_snapshot(conn, pid)
    rerender(conn, a)


def _fmt_pay(lo, hi, interval):
    if lo is None and hi is None:
        return None
    txt = f"${lo:,}-${hi:,}" if lo is not None and hi is not None else f"${(lo if lo is not None else hi):,}"
    if interval and str(interval).lower() not in ("year", "yearly", "annual", "annually"):
        txt += f" per {interval}"
    return txt


def snapshot_row(conn, posting_id):
    return conn.execute("SELECT employer, title, req_id, location_primary, pay_min, pay_max, pay_interval "
                        "FROM jobs.fit_snapshot WHERE posting_id = %s", [posting_id]).fetchone()


def ensure_snapshot(conn, posting_id):
    """Pull one posting from the jobsearch DuckDB into jobs.fit_snapshot when the nightly snapshot left it
    out (2026-10-23), so the sheet's "open" link and the field fill-in work the same day. Never fatal: a
    locked or missing DuckDB (a sweep running) just leaves the row for the nightly snapshot. Called after the
    tracker write has committed, so a failure here never undoes the add or edit."""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.environ.get("JOBSEARCH_DUCKDB", os.path.join(root, "db", "jobsearch.duckdb"))
    try:
        import duckdb
        spec = importlib.util.spec_from_file_location("pg_snapshot", os.path.join(root, "scripts", "pg_snapshot.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        duck = duckdb.connect(path, read_only=True)
        try:
            if mod.snapshot_one(conn, duck, posting_id):
                print(f"posting {posting_id} copied into jobs.fit_snapshot (was not in the nightly snapshot)")
        finally:
            duck.close()
    except Exception as e:  # noqa: BLE001 -- best effort; the nightly snapshot picks it up
        print(f"note: could not copy posting {posting_id} into jobs.fit_snapshot now ({type(e).__name__}); "
              f"the nightly snapshot will add it")


def cmd_add(conn, a):
    company, role, req, loc, pay = a.company, a.role, a.req, a.location, a.pay
    pid = store._posting_id(a.posting)
    if pid and not snapshot_row(conn, pid):
        ensure_snapshot(conn, pid)
    if pid:
        snap = snapshot_row(conn, pid)
        if snap:
            company = company or snap["employer"]
            role = role or snap["title"]
            req = req or snap["req_id"]
            loc = loc or snap["location_primary"]
            pay = pay or _fmt_pay(snap["pay_min"], snap["pay_max"], snap["pay_interval"])
    if not company or not role:
        raise TrackerError("add needs --company and --role"
                           + (f" (posting {pid} is not in jobs.fit_snapshot)" if pid else ""))
    mode = (a.folder or ("auto" if a.status == "built" else "none")).strip()
    if mode.lower() in ("none", ""):
        mode = "none"
    jd = None
    if mode != "none":
        jd = get_job_dir()
        if jd is None:
            raise TrackerError("JOBSEARCH_VAULT_DIR is not set; cannot create the folder (use --folder none)")
        if mode != "auto":   # an explicit folder another application already uses: refuse before inserting
            want = store.normalize_folder(mode)
            other = store.folder_in_use(conn, want) if want else None
            if other is not None:
                raise TrackerError(f"folder {want!r} is already used by application #{other}; nothing was added")
    row = store.add_application(
        conn, company=company, role=role, actor=actor(), employer_job_id=req, posting_id=pid,
        location=loc, pay_text=pay, source=a.source or ("pipeline" if pid else "manual"),
        status=a.status, status_date=a.date)
    app_id = row["app_id"]
    print(f"added #{app_id} {row['company']}: {row['role']} [{row['status']}]")
    if mode != "none":
        try:
            kw = dict(app_id=app_id, company=row["company"], role=row["role"],
                      employer_job_id=row["employer_job_id"], posting_id=row["posting_id"])
            rel = (folders.create_folder(jd, taken=lambda r: store.folder_in_use(conn, r) is not None, **kw)
                   if mode == "auto" else folders.use_folder(jd, mode, **kw))
            store.edit_field(conn, app_id, "folder", rel, actor())
            print(f"folder {rel}")
        except Exception as e:  # noqa: BLE001 - the application is committed; a retry must not duplicate it
            print(f"warning: #{app_id} was added but the folder step failed ({type(e).__name__}: {e}); "
                  f"set it with `trk edit {app_id} --folder <path>`", file=sys.stderr)
    rerender(conn, a)


def cmd_render(conn, a):
    jd = get_job_dir()
    if jd is None:
        raise TrackerError("JOBSEARCH_VAULT_DIR is not set")
    res = render.render(conn, jd, preview=a.preview, force=a.force)
    if not a.preview:
        gsheet.spawn()
    if res.status == "refused":
        print(f"refused: {res.path} {res.notes[0]}; not overwritten.", file=sys.stderr)
        lines = res.diff.splitlines()
        print("\n".join(lines[:200]), file=sys.stderr)
        if len(lines) > 200:
            print(f"... ({len(lines) - 200} more diff lines)", file=sys.stderr)
        print("Apply the change with trk, or rerun with --force to overwrite.", file=sys.stderr)
        return 1
    print(f"wrote {res.path}")
    return 0


def cmd_closed_report(conn, a):
    """On demand only: nothing re-renders this file."""
    jd = get_job_dir()
    if jd is None:
        raise TrackerError("JOBSEARCH_VAULT_DIR is not set")
    if a.since:
        try:
            date.fromisoformat(a.since)
        except ValueError:
            raise TrackerError(f"--since {a.since!r} is not a YYYY-MM-DD date") from None
    path, n = render.write_closed_report(conn, jd, status=a.status, since=a.since)
    print(f"wrote {path} ({n} closed row{'s' if n != 1 else ''})")
    return 0


def cmd_check(conn, a):
    problems, notes = [], []
    rows = conn.execute("SELECT * FROM tracker.vw_applications ORDER BY app_id").fetchall()
    jd = get_job_dir()
    if jd is None:
        notes.append("folder check skipped: JOBSEARCH_VAULT_DIR not set")
    else:
        for r in rows:
            if r["folder"] and not (jd / r["folder"]).is_dir():
                problems.append(f"#{r['app_id']} {r['company']}: folder missing on disk: {r['folder']}")
    if conn.execute("SELECT count(*) AS n FROM jobs.fit_snapshot").fetchone()["n"] == 0:
        notes.append("posting check skipped: jobs.fit_snapshot is empty")
    else:
        for r in conn.execute(
                """SELECT a.app_id, a.company, a.posting_id FROM tracker.application a
                    WHERE a.posting_id IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM jobs.fit_snapshot f WHERE f.posting_id = a.posting_id)
                    ORDER BY a.app_id"""):
            problems.append(f"#{r['app_id']} {r['company']}: posting {r['posting_id']} not in jobs.fit_snapshot")
    for r in conn.execute(
            """SELECT lower(company) AS c, lower(employer_job_id) AS j, array_agg(app_id ORDER BY app_id) AS ids
                 FROM tracker.application WHERE employer_job_id IS NOT NULL
                GROUP BY 1, 2 HAVING count(*) > 1"""):
        problems.append(f"duplicate (company, req) {r['c']} / {r['j']}: apps {', '.join(map(str, r['ids']))}")
    today = date.today()
    for r in rows:
        if r["is_active"] and r["follow_up_due"] and r["follow_up_due"] < today:
            problems.append(f"#{r['app_id']} {r['company']}: follow-up overdue since {r['follow_up_due']}")
    for n in notes:
        print(f"note: {n}")
    for p in problems:
        print(f"problem: {p}")
    print(f"{len(rows)} applications checked, {len(problems)} problem{'s' if len(problems) != 1 else ''}")
    return 1 if problems else 0


def cmd_sweep(conn, a):
    """Quarterly housekeeping (user ruling 2026-10-20): `applied` rows with no status change for
    --days (default 90) become `assumed-rejected`. A dry run unless --apply; real news later
    (`trk set <id> rejected` or `screen`) overrides it."""
    cutoff = date.today() - timedelta(days=a.days)
    rows = conn.execute("""SELECT * FROM tracker.vw_applications
                            WHERE status = 'applied' AND status_date <= %s
                            ORDER BY status_date, app_id""", [cutoff]).fetchall()
    list_table(rows, False)
    if not a.apply:
        print(f"dry run: {len(rows)} applied row(s) unchanged since {cutoff} or earlier; "
              "rerun with --apply to mark them assumed-rejected")
        return 0
    note = f"quarterly sweep: no response in {a.days}+ days"
    for r in rows:
        store.set_status(conn, r["app_id"], "assumed-rejected", actor(), note=note)
    print(f"marked {len(rows)} row(s) assumed-rejected")
    if rows:
        rerender(conn, a)
    return 0


class UsageError(Exception):
    pass


GUIDE = """\
statuses:
  active   built -> applied -> screen -> interview -> offer
  closed   accepted, declined, rejected, assumed-rejected, withdrawn, passed, closed-unsent
  An official rejection is 'rejected'; 'assumed-rejected' means never heard back (see sweep).
  If a closed employer gets back in touch, trk set <id> screen moves the row back to active.

everyday use:
  trk ls                               open applications (--closed, --all, --status S)
  trk find capital one                 search; every term must match (company, role, req, posting, id)
  trk show 330                         one application: fields, contacts, full history
  trk add --company Acme --role "Sr BI Analyst" --req R123 --posting <20-hex id>
                                       new row; status built makes Applications/Company_RoleSlug_Req/
  trk add --company Acme --role X --status passed --folder none
                                       record a pass without a folder
  trk set 330 applied                  change status (--date YYYY-MM-DD, --note "...")
  trk set 330 rejected --note "email from recruiter"
  trk note 330 "phone screen booked for Friday"
  trk contact 330 "Pat Lee" --role recruiter --email pat@acme.com
                                       roles: recruiter, hiring_manager, panelist, referral, staffing, other
  trk edit 330 --follow-up 2026-11-03 --next-action "email recruiter"
                                       fields: --next-action --follow-up --closes --role --company --req
                                       --posting --location --pay --close-reason --applied --source
                                       --folder --notes   ('' clears a field; --posting none unlinks)
  trk edit 330 --posting <20-hex id>   link a posting found later (a new employer, a recruiter lead);
                                       like add, it copies the posting so the sheet's open link works now

views and upkeep:
  trk render [--preview] [--force]     rewrite the markdown view (writes re-render it automatically;
                                       --no-render skips that). A hand-edited view is refused unless --force.
  trk closed-report [--status S] [--since YYYY-MM-DD]
                                       write Application_Tracker_Closed.md, a one-time snapshot
  trk sheet                            refresh the Google Sheet view (wide columns, filters, contacts,
                                       history). Every write and render also refreshes it in the
                                       background; the log is logs/tracker_sheet.log. One way only.
  trk check                            folders, posting ids, duplicates, overdue follow-ups
  trk sweep [--days 90]                list applied rows silent that long; --apply moves them to assumed-rejected

posting copies:
  add --posting and edit --posting copy a posting missing from jobs.fit_snapshot out of the pipeline's
  DuckDB. If a sweep or launch.sh run has DuckDB locked, the row is still saved, a note prints, and the
  nightly snapshot adds the posting (and the sheet's open link) after the next run.

environment (from the repo .env):
  JOBSEARCH_VAULT_DIR   locates the Job_Search folder
  TRACKER_PGDATABASE    the Postgres database (default resume)
  TRACKER_SHEET_ID      the Google Sheet view; unset means trk sheet refuses and writes skip the refresh
  JOBSEARCH_DUCKDB      the pipeline DuckDB that posting copies read (default db/jobsearch.duckdb)
  TRK_ACTOR             who made each change (default 'user'). Every write is logged as an event (trk show).

exit codes: 0 ok, 1 error (or trk check found problems), 2 usage.
Help for one command: trk <command> --help
"""


def build_parser():
    ap = argparse.ArgumentParser(prog="trk", description="Application tracker (PostgreSQL).", epilog=GUIDE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", metavar="command")
    nr = argparse.ArgumentParser(add_help=False)
    nr.add_argument("--no-render", action="store_true", help="do not re-render the markdown view")

    p = sub.add_parser("ls", help="list applications (default: active)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--closed", action="store_true", help="closed rows instead")
    g.add_argument("--all", action="store_true", help="open rows, then closed rows")
    g.add_argument("--status", metavar="STATUS", help="only rows with this status, open or closed")
    p.set_defaults(fn=cmd_ls)

    p = sub.add_parser("find", help="search company / role / req / posting id / app id (AND-ed)")
    p.add_argument("terms", nargs="+", help="words that must all match (company, role, req, posting id, app id); "
                                            "open and closed rows alike")
    p.set_defaults(fn=cmd_find)

    p = sub.add_parser("show", help="one application: fields, contacts, history")
    p.add_argument("app_id", type=int, help="the tracker row's ID (trk ls / trk find)")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("set", parents=[nr], help="change status")
    p.add_argument("app_id", type=int, help="the tracker row's ID (trk ls / trk find)")
    p.add_argument("status", help="new status (the list is in trk --help); a closed row set to an active "
                                  "status reopens")
    p.add_argument("--date", help="status date YYYY-MM-DD (default today)")
    p.add_argument("--note", help="a note logged with the status change")
    p.set_defaults(fn=cmd_set)

    p = sub.add_parser("note", parents=[nr], help="add a note")
    p.add_argument("app_id", type=int, help="the tracker row's ID (trk ls / trk find)")
    p.add_argument("text", help="the note (quote it)")
    p.set_defaults(fn=cmd_note)

    p = sub.add_parser("contact", parents=[nr], help="link a contact")
    p.add_argument("app_id", type=int, help="the tracker row's ID (trk ls / trk find)")
    p.add_argument("name", help="the contact's name; an existing contact with the same name and --company is "
                                  "reused (new details only fill its empty fields)")
    p.add_argument("--role", default="recruiter", choices=store.CONTACT_ROLES,
                   help="their part in this application (default recruiter)")
    for k in ("email", "phone", "linkedin", "title", "company"):
        p.add_argument(f"--{k}", help=f"the contact's {k}")
    p.set_defaults(fn=cmd_contact)

    p = sub.add_parser("edit", parents=[nr], help="edit fields (empty string clears; --posting none)")
    p.add_argument("app_id", type=int, help="the tracker row's ID (trk ls / trk find)")
    edit_help = {
        "next_action": "what happens next, shown on the views", "follow_up": "follow-up due date YYYY-MM-DD",
        "closes": "posting close date YYYY-MM-DD", "role": "role title", "company": "company name",
        "req": "the employer's req / job id",
        "posting": "20-hex posting id or 'none'; copies a posting missing from jobs.fit_snapshot so "
                   "the sheet links it now (a locked DuckDB leaves it for the nightly snapshot)",
        "location": "location text", "pay": "pay text as posted, e.g. '$150K-$175K'",
        "close_reason": "why a closed row closed", "applied": "date applied YYYY-MM-DD",
        "source": "where the lead came from: " + ", ".join(store.SOURCES),
        "folder": "package folder under Job_Search, or 'none'", "notes": "free-text notes field (replaces it)",
    }
    for dest, (flag, _) in EDIT_OPTIONS.items():
        p.add_argument(flag, dest=dest, default=None, help=edit_help.get(dest))
    p.set_defaults(fn=cmd_edit)

    p = sub.add_parser("add", parents=[nr], help="add an application (and its folder)")
    p.add_argument("--company", help="company name (required unless --posting fills it)")
    p.add_argument("--role", help="role title (required unless --posting fills it)")
    p.add_argument("--req", help="the employer's req / job id; names the folder")
    p.add_argument("--posting", help="20-hex posting id or 'none'; fills blank company, role, req, location "
                                     "and pay from jobs.fit_snapshot, first copying a posting it is missing "
                                     "(a locked DuckDB leaves that for the nightly snapshot)")
    p.add_argument("--source", choices=store.SOURCES, help="where the lead came from (default pipeline "
                                                            "with --posting, else manual)")
    p.add_argument("--status", default="built", help="starting status (default built)")
    p.add_argument("--date", help="status date YYYY-MM-DD (default today)")
    p.add_argument("--folder", help="auto (default when status is built) | none | <path under Job_Search>")
    p.add_argument("--location", help="location text")
    p.add_argument("--pay", help="pay text as posted, e.g. '$150K-$175K'")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("render", help="write the markdown view")
    p.add_argument("--preview", action="store_true", help="write the PREVIEW file")
    p.add_argument("--force", action="store_true", help="overwrite even if the file was edited by hand")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("closed-report", help="write Application_Tracker_Closed.md (on-demand snapshot of closed rows)")
    p.add_argument("--status", metavar="STATUS", help="only this closed status")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only rows whose status date is on or after this")
    p.set_defaults(fn=cmd_closed_report)

    p = sub.add_parser("sweep", parents=[nr], help="applied rows silent for --days (90) -> assumed-rejected")
    p.add_argument("--days", type=int, default=90, help="days with no status change (default 90)")
    p.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    p.set_defaults(fn=cmd_sweep)

    p = sub.add_parser("sheet", help="refresh the Google Sheet view now (writes also refresh it in the background)")
    p.set_defaults(fn=cmd_sheet)

    p = sub.add_parser("check", help="validate folders, posting ids, duplicates, follow-ups")
    p.set_defaults(fn=cmd_check)
    return ap


def load_env(path=None):
    """Load <repo>/.env (JOBSEARCH_VAULT_DIR and friends) without overriding real environment variables."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path or Path(__file__).resolve().parents[2] / ".env", override=False)
    except Exception:  # noqa: BLE001 - a missing or unreadable .env must never stop trk
        pass


def main(argv=None):
    load_env()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["ls"]
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    if not getattr(args, "fn", None):
        ap.print_usage(sys.stderr)
        return 2
    try:
        with db.connect() as conn:
            rc = args.fn(conn, args)
        return rc or 0
    except UsageError as e:
        print(f"trk: usage: {e}", file=sys.stderr)
        return 2
    except TrackerError as e:
        print(f"trk: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - database / file problems: message, no traceback
        print(f"trk: error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
