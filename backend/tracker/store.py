"""The tracker's write/read library: the only code that changes `tracker.*` tables.

Every write runs in one transaction that also inserts the matching `tracker.event` row, so the
history can never disagree with the data. `actor` is passed by the caller ('user',
'claude-code', 'claude-desktop', 'pipeline', ...). Functions take an open connection from
`backend.tracker.db.connect()` and return plain dicts. Errors are `TrackerError` (a ValueError).
"""
import re
from datetime import date

_POSTING_ID = re.compile(r"^[0-9a-f]{20}$")

# Fields `edit_field` may change; status and its dates go through `set_status`.
EDITABLE_FIELDS = {
    "company": "text", "role": "text", "employer_job_id": "text", "posting_id": "posting",
    "location": "text", "pay_text": "text", "source": "text", "folder": "text",
    "date_applied": "date", "closes_on": "date", "follow_up_due": "date",
    "next_action": "text", "close_reason": "text", "notes": "text",
}
SOURCES = ("pipeline", "linkedin", "recruiter", "referral", "board", "manual", "migrated")
CONTACT_ROLES = ("recruiter", "hiring_manager", "panelist", "referral", "staffing", "other")
CONTACT_FIELDS = ("title", "company", "email", "phone", "linkedin", "notes")


class TrackerError(ValueError):
    """A rejected write (bad status, bad id, unknown field, missing application)."""


def _today():
    return date.today()


def _date(value, what):
    """Accept a date, an ISO string, or empty/None (-> None)."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise TrackerError(f"{what}: {value!r} is not a YYYY-MM-DD date") from None


def _posting_id(value):
    """A 20-hex posting id, or None / 'none' / '' -> None."""
    if value is None or str(value).strip().lower() in ("", "none"):
        return None
    value = str(value).strip().lower()
    if not _POSTING_ID.match(value):
        raise TrackerError(f"posting_id {value!r} must be 20 hex characters, or 'none'")
    return value


def _statuses(conn):
    return {r["status"]: r["is_active"] for r in
            conn.execute("SELECT status, is_active FROM tracker.status ORDER BY sort_order")}


def _check_status(conn, status):
    allowed = _statuses(conn)
    if status not in allowed:
        raise TrackerError(f"unknown status {status!r}; allowed: {', '.join(allowed)}")


def _text(value):
    return None if value is None or str(value).strip() == "" else str(value).strip()


def _event(conn, app_id, kind, actor, **cols):
    if not actor:
        raise TrackerError("actor is required")
    keys = ["app_id", "kind", "actor", *cols]
    conn.execute(
        f"INSERT INTO tracker.event ({', '.join(keys)}) VALUES ({', '.join(['%s'] * len(keys))})",
        [app_id, kind, actor, *cols.values()])


def _require_app(conn, app_id, lock=False):
    row = conn.execute(
        "SELECT * FROM tracker.application WHERE app_id = %s" + (" FOR UPDATE" if lock else ""),
        [app_id]).fetchone()
    if row is None:
        raise TrackerError(f"no application with app_id {app_id}")
    return row


def add_application(conn, *, company, role, actor, employer_job_id=None, posting_id=None,
                    location=None, pay_text=None, source="manual", folder=None, status="built",
                    status_date=None, date_applied=None, closes_on=None, next_action=None,
                    follow_up_due=None, notes=None):
    """Create an application plus its 'created' event. `date_applied` defaults to the status
    date when the starting status is 'applied'. Returns the new row."""
    company, role = _text(company), _text(role)
    if not company or not role:
        raise TrackerError("company and role are required")
    if source not in SOURCES:
        raise TrackerError(f"unknown source {source!r}; allowed: {', '.join(SOURCES)}")
    pid = _posting_id(posting_id)
    sdate = _date(status_date, "status_date") or _today()
    adate = _date(date_applied, "date_applied")
    if status == "applied" and adate is None:
        adate = sdate
    with conn.transaction():
        _check_status(conn, status)
        row = conn.execute(
            """INSERT INTO tracker.application
                 (posting_id, employer_job_id, company, role, location, pay_text, source, folder,
                  status, status_date, date_applied, closes_on, next_action, follow_up_due, notes)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            [pid, _text(employer_job_id), company, role, _text(location), _text(pay_text), source,
             _text(folder), status, sdate, adate, _date(closes_on, "closes_on"), _text(next_action),
             _date(follow_up_due, "follow_up_due"), _text(notes)]).fetchone()
        _event(conn, row["app_id"], "created", actor, status=status, status_date=sdate)
    return row


def set_status(conn, app_id, status, actor, status_date=None, note=None):
    """Move an application to `status` (validated against tracker.status). Sets date_applied when
    the new status is 'applied' and it is empty. Returns the updated row."""
    sdate = _date(status_date, "status_date") or _today()
    with conn.transaction():
        _check_status(conn, status)
        cur = _require_app(conn, app_id, lock=True)
        adate = cur["date_applied"]
        if status == "applied" and adate is None:
            adate = sdate
        row = conn.execute(
            """UPDATE tracker.application
                  SET status = %s, status_date = %s, date_applied = %s, updated_at = now()
                WHERE app_id = %s RETURNING *""", [status, sdate, adate, app_id]).fetchone()
        _event(conn, app_id, "status", actor, status=status, status_date=sdate,
               old_value=cur["status"], new_value=status, note=_text(note))
    return row


def add_note(conn, app_id, note, actor):
    """Append a free-text note event (and touch updated_at). Returns the event row."""
    note = _text(note)
    if not note:
        raise TrackerError("note is empty")
    with conn.transaction():
        _require_app(conn, app_id, lock=True)
        conn.execute("UPDATE tracker.application SET updated_at = now() WHERE app_id = %s", [app_id])
        _event(conn, app_id, "note", actor, note=note)
        return conn.execute("SELECT * FROM tracker.event WHERE app_id = %s ORDER BY event_id DESC LIMIT 1",
                            [app_id]).fetchone()


def add_contact(conn, app_id, name, actor, role="recruiter", **fields):
    """Link a contact to an application, reusing an existing contact with the same name + company
    (case-insensitive; a missing company matches a missing company). `fields` may hold title,
    company, email, phone, linkedin, notes; for a reused contact they only fill empty columns.
    Linking the same contact in the same role twice is a no-op (no second event).
    Returns the contact row plus 'role' and 'created' (False when reused)."""
    name = _text(name)
    if not name:
        raise TrackerError("contact name is required")
    if role not in CONTACT_ROLES:
        raise TrackerError(f"unknown contact role {role!r}; allowed: {', '.join(CONTACT_ROLES)}")
    extra = set(fields) - set(CONTACT_FIELDS)
    if extra:
        raise TrackerError(f"unknown contact field(s) {sorted(extra)}; allowed: {', '.join(CONTACT_FIELDS)}")
    vals = {k: _text(fields.get(k)) for k in CONTACT_FIELDS}
    with conn.transaction():
        _require_app(conn, app_id, lock=True)
        c = conn.execute(
            """SELECT * FROM tracker.contact
                WHERE lower(name) = lower(%s) AND lower(coalesce(company, '')) = lower(coalesce(%s, ''))
                ORDER BY contact_id LIMIT 1""", [name, vals["company"]]).fetchone()
        created = c is None
        if created:
            c = conn.execute(
                """INSERT INTO tracker.contact (name, title, company, email, phone, linkedin, notes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                [name, *(vals[k] for k in CONTACT_FIELDS)]).fetchone()
        else:
            c = conn.execute(
                """UPDATE tracker.contact SET title = coalesce(title, %s), email = coalesce(email, %s),
                          phone = coalesce(phone, %s), linkedin = coalesce(linkedin, %s),
                          notes = coalesce(notes, %s)
                    WHERE contact_id = %s RETURNING *""",
                [vals["title"], vals["email"], vals["phone"], vals["linkedin"], vals["notes"],
                 c["contact_id"]]).fetchone()
        linked = conn.execute(
            """INSERT INTO tracker.app_contact (app_id, contact_id, role) VALUES (%s,%s,%s)
               ON CONFLICT DO NOTHING RETURNING 1""", [app_id, c["contact_id"], role]).fetchone()
        if linked:
            conn.execute("UPDATE tracker.application SET updated_at = now() WHERE app_id = %s", [app_id])
            _event(conn, app_id, "contact", actor, new_value=name, note=f"{role}: {name}")
    return {**c, "role": role, "created": created}


def edit_field(conn, app_id, field, value, actor):
    """Set one whitelisted field (see EDITABLE_FIELDS), recording old and new values.
    Empty / None clears it (company and role cannot be cleared). Returns the updated row."""
    kind = EDITABLE_FIELDS.get(field)
    if kind is None:
        raise TrackerError(f"field {field!r} is not editable; allowed: {', '.join(sorted(EDITABLE_FIELDS))}"
                           + ("  (use set_status for status)" if field.startswith("status") else ""))
    if kind == "date":
        new = _date(value, field)
    elif kind == "posting":
        new = _posting_id(value)
    else:
        new = _text(value)
    if new is None and field in ("company", "role"):
        raise TrackerError(f"{field} cannot be empty")
    if field == "source" and new not in SOURCES:
        raise TrackerError(f"unknown source {new!r}; allowed: {', '.join(SOURCES)}")
    with conn.transaction():
        cur = _require_app(conn, app_id, lock=True)
        row = conn.execute(f"UPDATE tracker.application SET {field} = %s, updated_at = now() "
                           "WHERE app_id = %s RETURNING *", [new, app_id]).fetchone()
        old = cur[field]
        _event(conn, app_id, "field", actor, field=field,
               old_value=None if old is None else str(old), new_value=None if new is None else str(new))
    return row


def get(conn, app_id):
    """One application with 'contacts' (name, role, ...) and 'events' (oldest first); None if absent."""
    row = conn.execute("SELECT * FROM tracker.vw_applications WHERE app_id = %s", [app_id]).fetchone()
    if row is None:
        return None
    row["contacts"] = conn.execute(
        """SELECT c.*, ac.role FROM tracker.app_contact ac JOIN tracker.contact c USING (contact_id)
            WHERE ac.app_id = %s ORDER BY c.name, ac.role""", [app_id]).fetchall()
    row["events"] = conn.execute(
        "SELECT * FROM tracker.event WHERE app_id = %s ORDER BY at, event_id", [app_id]).fetchall()
    return row


def find(conn, terms):
    """Applications matching every term (AND). A term matches company, role, employer_job_id or
    posting_id (substring, case-insensitive), or app_id when it is all digits. Newest first."""
    if isinstance(terms, str):
        terms = terms.split()
    where, params = [], []
    for t in terms:
        t = str(t).strip()
        if not t:
            continue
        like = f"%{t}%"
        clause = "(company ILIKE %s OR role ILIKE %s OR employer_job_id ILIKE %s OR posting_id ILIKE %s"
        params += [like] * 4
        if t.isdigit():
            clause += " OR app_id = %s"
            params.append(int(t))
        where.append(clause + ")")
    if not where:
        raise TrackerError("find needs at least one term")
    return conn.execute("SELECT * FROM tracker.vw_applications WHERE " + " AND ".join(where)
                        + " ORDER BY app_id DESC", params).fetchall()


def list_apps(conn, active=True, status=None):
    """Rows from vw_applications ordered by status sort then status date (newest first).
    active=True -> active statuses, False -> closed, None -> all; `status` narrows further."""
    where, params = [], []
    if active is not None:
        where.append("is_active = %s")
        params.append(bool(active))
    if status:
        _check_status(conn, status)
        where.append("status = %s")
        params.append(status)
    sql = "SELECT * FROM tracker.vw_applications"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY sort_order, status_date DESC, app_id DESC", params).fetchall()
