"""The Google Sheet view of the tracker: a wider, filterable copy of the database, one way only.

`refresh(conn)` rewrites five tabs (Active, Closed, Contacts, History, Networks) of the sheet named
by $TRACKER_SHEET_ID, signing in with the service-account key at $TRACKER_SHEET_KEY. Row 1 of each
tab is a banner, row 2 the header (frozen, with a basic filter whose criteria survive a refresh).
Anything typed into the sheet is overwritten. Without TRACKER_SHEET_ID the refresh is skipped, so
tests and other users never touch Google. `spawn()` runs a refresh in the background after a write.
"""
import fcntl
import os
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_KEY = "~/.config/jobsearch/tracker_sheet_sa.json"
LOCK = Path("~/.cache/jobsearch/tracker_sheet.lock").expanduser()
LOG = Path(__file__).resolve().parents[2] / "logs" / "tracker_sheet.log"

# (header, width in pixels, kind); kind 'date' gets a date format, 'link' a HYPERLINK formula
ACTIVE = [("ID", 50, ""), ("Applied", 90, "date"), ("Company", 190, ""), ("Role", 280, ""), ("Req", 90, ""),
          ("Status", 95, ""), ("Since", 90, "date"), ("Days", 50, ""), ("Next action", 220, ""),
          ("Follow-up", 90, "date"), ("Closes", 90, "date"), ("Posting", 70, "link"), ("Folder", 220, ""),
          ("Contact", 150, ""), ("Email", 190, ""), ("Phone", 110, ""), ("LinkedIn", 90, "link"),
          ("Location", 150, ""), ("Pay", 130, ""), ("Source", 80, ""), ("Latest note", 300, ""),
          ("Notes", 300, "")]
CLOSED = [c for c in ACTIVE if c[0] not in ("Days", "Next action", "Follow-up", "Closes")]
CLOSED.insert(7, ("Reason", 220, ""))   # then Posting and Folder, as on Active
CONTACTS = [("Name", 170, ""), ("Title", 180, ""), ("Company", 190, ""), ("Email", 200, ""), ("Phone", 110, ""),
            ("LinkedIn", 90, "link"), ("Role", 110, ""), ("ID", 50, ""), ("Application", 320, ""),
            ("Status", 95, ""), ("Notes", 250, "")]
HISTORY = [("When", 140, ""), ("ID", 50, ""), ("Company", 190, ""), ("Role", 260, ""), ("Event", 80, ""),
           ("Status", 95, ""), ("Field", 100, ""), ("From", 140, ""), ("To", 140, ""), ("Note", 380, ""),
           ("Actor", 100, "")]
NETWORKS = [("Platform", 140, ""), ("Accepted", 90, "date"), ("Status", 200, ""), ("Profile", 90, "link"),
            ("Notes", 380, "")]
TABS = {"Active": ACTIVE, "Closed": CLOSED, "Contacts": CONTACTS, "History": HISTORY, "Networks": NETWORKS}

STATUS_COLORS = {  # status -> (red, green, blue) background
    "offer": (0.72, 0.88, 0.80), "interview": (0.80, 0.90, 0.75), "screen": (0.85, 0.93, 0.83),
    "applied": (0.85, 0.90, 0.98), "built": (1.0, 0.95, 0.80), "accepted": (0.72, 0.88, 0.80),
    "rejected": (0.96, 0.84, 0.84), "assumed-rejected": (0.95, 0.90, 0.90), "declined": (0.93, 0.93, 0.93),
    "withdrawn": (0.93, 0.93, 0.93), "passed": (0.93, 0.93, 0.93), "closed-unsent": (0.93, 0.93, 0.93)}


def configured():
    """A sheet is set, and the database is the real one (tests point TRACKER_PGDATABASE elsewhere)."""
    from backend.tracker.db import DEFAULT_DB
    return bool(os.environ.get("TRACKER_SHEET_ID")) and \
        (os.environ.get("TRACKER_PGDATABASE") or DEFAULT_DB) == DEFAULT_DB


def _text(v):
    """A cell value. Text that Sheets would read as a formula gets a leading apostrophe."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone().strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return v
    s = " ".join(str(v).split())
    return "'" + s if s[:1] in "=+-@" else s


def _link(url, label):
    if not url:
        return ""
    return f'=HYPERLINK("{str(url).replace(chr(34), "%22")}", "{label}")'


def _contacts(conn):
    """app_id -> list of contact rows (recruiters first)."""
    out = {}
    for r in conn.execute(
            """SELECT ac.app_id, ac.role, c.* FROM tracker.app_contact ac JOIN tracker.contact c USING (contact_id)
                ORDER BY ac.app_id, (ac.role = 'recruiter') DESC, c.contact_id"""):
        out.setdefault(r["app_id"], []).append(r)
    return out


def _latest_notes(conn):
    return {r["app_id"]: r for r in conn.execute(
        """SELECT DISTINCT ON (app_id) app_id, at, note FROM tracker.event
            WHERE kind = 'note' AND note IS NOT NULL ORDER BY app_id, at DESC, event_id DESC""")}


def build_tabs(conn, today=None):
    """{tab: list of rows (header row first)} from the database. Pure apart from the reads."""
    today = today or date.today()
    apps = conn.execute(
        """SELECT v.*, f.url AS posting_url FROM tracker.vw_applications v
             LEFT JOIN jobs.fit_snapshot f USING (posting_id)
            ORDER BY v.sort_order, v.status_date DESC, v.app_id DESC""").fetchall()
    contacts, notes = _contacts(conn), _latest_notes(conn)
    tabs = {name: [[h for h, _, _ in cols]] for name, cols in TABS.items()}
    for a in apps:
        cs = contacts.get(a["app_id"], [])
        c = cs[0] if cs else {}
        n = notes.get(a["app_id"])
        latest = f'{n["at"].date().isoformat()}: {n["note"]}' if n else ""
        cell = {"ID": a["app_id"], "Applied": a["date_applied"], "Company": a["company"], "Role": a["role"],
                "Req": a["employer_job_id"], "Status": a["status"], "Since": a["status_date"],
                "Days": (today - a["status_date"]).days if a["status_date"] else "",
                "Next action": a["next_action"], "Follow-up": a["follow_up_due"], "Closes": a["closes_on"],
                "Contact": "; ".join(x["name"] for x in cs), "Email": c.get("email"), "Phone": c.get("phone"),
                "LinkedIn": _link(c.get("linkedin"), "profile"), "Location": a["location"], "Pay": a["pay_text"],
                "Source": a["source"], "Posting": _link(a["posting_url"], "open"), "Folder": a["folder"],
                "Latest note": latest, "Notes": a["notes"], "Reason": a["close_reason"]}
        tab, cols = ("Active", ACTIVE) if a["is_active"] else ("Closed", CLOSED)
        tabs[tab].append([v if k in ("LinkedIn", "Posting") else _text(v) for k, v in
                          ((h, cell[h]) for h, _, _ in cols)])
    for r in conn.execute(
            """SELECT c.*, ac.role AS link_role, a.app_id, a.company AS app_company, a.role AS app_role, a.status
                 FROM tracker.contact c JOIN tracker.app_contact ac USING (contact_id)
                 JOIN tracker.application a USING (app_id) ORDER BY lower(c.name), a.app_id DESC"""):
        tabs["Contacts"].append([_text(r["name"]), _text(r["title"]), _text(r["company"]), _text(r["email"]),
                                 _text(r["phone"]), _link(r["linkedin"], "profile"), _text(r["link_role"]),
                                 r["app_id"], _text(f'{r["app_company"]} / {r["app_role"]}'), _text(r["status"]),
                                 _text(r["notes"])])
    for e in conn.execute(
            """SELECT e.*, a.company, a.role FROM tracker.event e JOIN tracker.application a USING (app_id)
                WHERE e.kind <> 'migrated' ORDER BY e.at DESC, e.event_id DESC"""):
        tabs["History"].append([_text(e["at"]), e["app_id"], _text(e["company"]), _text(e["role"]), _text(e["kind"]),
                                _text(e["status"]), _text(e["field"]), _text(e["old_value"]), _text(e["new_value"]),
                                _text(e["note"]), _text(e["actor"])])
    for n in conn.execute("SELECT * FROM tracker.network ORDER BY platform"):
        tabs["Networks"].append([_text(n["platform"]), _text(n["date_accepted"]), _text(n["status"]),
                                 _link(n["profile_url"], "profile"), _text(n["notes"])])
    return tabs


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _rgb(t):
    return {"red": t[0], "green": t[1], "blue": t[2]}


def format_requests(sheet_id, cols, nrows, old_filter, n_cond):
    """batchUpdate requests that (re)apply layout, filter and colors to one tab. `nrows` counts the
    header; `old_filter` is the tab's current basicFilter (criteria kept), `n_cond` its rule count."""
    ncols = len(cols)
    grid = lambda r0, r1, c0=0, c1=ncols: {"sheetId": sheet_id, "startRowIndex": r0, "endRowIndex": r1,
                                          "startColumnIndex": c0, "endColumnIndex": c1}
    req = [{"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
               "frozenRowCount": 2, "frozenColumnCount": 0}}, "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount"}},
           {"repeatCell": {"range": grid(0, 1), "cell": {"userEnteredFormat": {
               "textFormat": {"italic": True, "foregroundColor": _rgb((0.4, 0.4, 0.4))}}},
               "fields": "userEnteredFormat(textFormat)"}},
           {"repeatCell": {"range": grid(1, 2), "cell": {"userEnteredFormat": {
               "textFormat": {"bold": True}, "backgroundColor": _rgb((0.85, 0.88, 0.93)),
               "wrapStrategy": "WRAP", "verticalAlignment": "MIDDLE"}},
               "fields": "userEnteredFormat(textFormat,backgroundColor,wrapStrategy,verticalAlignment)"}},
           {"repeatCell": {"range": grid(2, max(nrows + 1, 3)), "cell": {"userEnteredFormat": {
               "wrapStrategy": "CLIP", "verticalAlignment": "TOP"}},
               "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}}]
    for i, (_, width, kind) in enumerate(cols):
        req.append({"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                    "startIndex": i, "endIndex": i + 1}, "properties": {"pixelSize": width}, "fields": "pixelSize"}})
        if kind == "date":
            req.append({"repeatCell": {"range": grid(2, max(nrows + 1, 3), i, i + 1), "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "DATE", "pattern": "yyyy-mm-dd"}}}, "fields": "userEnteredFormat.numberFormat"}})
    filt = {"range": grid(1, nrows + 1)}
    for key in ("sortSpecs", "filterSpecs"):
        if old_filter and old_filter.get(key):
            filt[key] = [s for s in old_filter[key] if s.get("columnIndex", 0) < ncols]
    req.append({"setBasicFilter": {"filter": filt}})
    req += [{"deleteConditionalFormatRule": {"sheetId": sheet_id, "index": 0}} for _ in range(n_cond)]
    names = [h for h, _, _ in cols]
    body = grid(2, max(nrows + 1, 3))
    if "Status" in names:
        col = _col(names.index("Status"))
        for status, color in STATUS_COLORS.items():
            req.append({"addConditionalFormatRule": {"rule": {"ranges": [grid(2, max(nrows + 1, 3), names.index("Status"),
                        names.index("Status") + 1)], "booleanRule": {"condition": {"type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": f'=${col}3="{status}"'}]}, "format": {"backgroundColor": _rgb(color)}}}}})
    if "Follow-up" in names:
        col = _col(names.index("Follow-up"))
        req.append({"addConditionalFormatRule": {"rule": {"ranges": [body], "booleanRule": {"condition": {
                    "type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f'=AND(${col}3<>"",${col}3<TODAY())'}]},
                    "format": {"textFormat": {"foregroundColor": _rgb((0.75, 0.1, 0.1)), "bold": True}}}}}})
    return req


class Sheets:
    """The few Sheets API calls the refresh needs (httpx + a service-account token)."""

    def __init__(self, sheet_id=None, key=None):
        import httpx
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
        self.id = sheet_id or os.environ["TRACKER_SHEET_ID"]
        key = Path(os.path.expanduser(key or os.environ.get("TRACKER_SHEET_KEY") or DEFAULT_KEY))
        creds = service_account.Credentials.from_service_account_file(str(key), scopes=SCOPES)
        creds.refresh(Request())
        self.http = httpx.Client(timeout=60, headers={"Authorization": f"Bearer {creds.token}"})

    def _ok(self, r):
        if r.status_code >= 400:
            raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def meta(self):
        return self._ok(self.http.get(f"{API}/{self.id}", params={
            "fields": "sheets(properties(sheetId,title,gridProperties),basicFilter,conditionalFormats)"}))

    def batch(self, requests):
        return self._ok(self.http.post(f"{API}/{self.id}:batchUpdate", json={"requests": requests})) if requests else {}

    def clear(self, ranges):
        return self._ok(self.http.post(f"{API}/{self.id}/values:batchClear", json={"ranges": ranges}))

    def write(self, data):
        return self._ok(self.http.post(f"{API}/{self.id}/values:batchUpdate", json={
            "valueInputOption": "USER_ENTERED", "data": data}))


def refresh(conn, api=None, now=None):
    """Rewrite every tab. Returns {tab: data rows written}."""
    now = now or datetime.now(timezone.utc)
    api = api or Sheets()
    tabs = build_tabs(conn)
    sheets = {s["properties"]["title"]: s for s in api.meta()["sheets"]}
    adds = []
    for name in TABS:
        if name not in sheets:
            if "Sheet1" in sheets and name == "Active":      # reuse the blank first tab of a new spreadsheet
                s = sheets.pop("Sheet1")
                adds.append({"updateSheetProperties": {"properties": {"sheetId": s["properties"]["sheetId"],
                             "title": name}, "fields": "title"}})
            else:
                adds.append({"addSheet": {"properties": {"title": name}}})
    if adds:
        api.batch(adds)
        sheets = {s["properties"]["title"]: s for s in api.meta()["sheets"]}
    grow = []
    for name, rows in tabs.items():
        g = sheets[name]["properties"]["gridProperties"]
        need_r, need_c = len(rows) + 50, len(TABS[name])
        if g.get("rowCount", 0) < need_r or g.get("columnCount", 0) < need_c:
            grow.append({"updateSheetProperties": {"properties": {"sheetId": sheets[name]["properties"]["sheetId"],
                         "gridProperties": {"rowCount": max(need_r, g.get("rowCount", 0)),
                                            "columnCount": max(need_c, g.get("columnCount", 0))}},
                         "fields": "gridProperties.rowCount,gridProperties.columnCount"}})
    api.batch(grow)
    api.clear([f"'{name}'!A1:ZZ" for name in tabs])
    stamp = now.astimezone().strftime("%Y-%m-%d %H:%M")
    banner = (f"Generated from the tracker database at {stamp}. Read-only view: edits here are overwritten "
              "on the next refresh. Change things with trk (or Claude).")
    api.write([{"range": f"'{name}'!A1", "values": [[banner]] + rows} for name, rows in tabs.items()])
    fmt = []
    for name, rows in tabs.items():
        s = sheets[name]
        fmt += format_requests(s["properties"]["sheetId"], TABS[name], len(rows), s.get("basicFilter"),
                               len(s.get("conditionalFormats", [])))
    api.batch(fmt)
    return {name: len(rows) - 1 for name, rows in tabs.items()}


def run_locked(conn):
    """refresh() under a file lock so overlapping background runs queue rather than interleave."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return refresh(conn)


def spawn():
    """Start a background refresh (returns at once). No-op when the sheet is not configured."""
    if not configured():
        return False
    LOG.parent.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    with open(LOG, "a") as log:
        subprocess.Popen([sys.executable, "-m", "backend.tracker.gsheet"], cwd=root, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    return True


def main():
    from backend.tracker import cli, db
    cli.load_env()
    if not configured():
        print("tracker sheet: TRACKER_SHEET_ID not set; skipped")
        return 0
    try:
        with db.connect() as conn:
            counts = run_locked(conn)
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} tracker sheet refreshed: "
              + ", ".join(f"{k} {v}" for k, v in counts.items()))
        return 0
    except Exception as e:  # noqa: BLE001 - background job: log and exit non-zero
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} tracker sheet refresh FAILED: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
