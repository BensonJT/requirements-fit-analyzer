#!/usr/bin/env python3
"""Feedback applet: record a build / pass / hold call on a posting without waiting for the DuckDB lock.

A small desktop window (tkinter). Paste a posting ID (20 hex), a tracker app ID, or a posting URL; the
window shows the employer and title, then queues the call (with optional Process / Technical / AI lens
grades, which train the lens models) in Postgres (jobs.decision_queue). Nothing here
touches the pipeline DuckDB: `finder.py apply-queue` writes queued calls into it at the next launch start,
just before the snapshot, or within the hour (scripts/inbox_watch.sh), with the same checks as
`finder.py mark`. A refused call shows its reason in the queue list; fix it and submit again.

For a posting with a tracker row, the Tracker box links a contact (name, role, title, company, email,
phone, LinkedIn URL) through `trk contact`, and the Tracker box changes the application's status through `trk set`, or,
with the status left blank, adds the comment as a note through `trk note` (immediate: the tracker is
Postgres already).

The contacts on file for the rows found are listed under the Tracker box (double-click a cell to copy it).
"Offload PDFs to Drive" runs the vault's media_offload.py finalize on the selected row's package folder (after
a confirmation listing the PDFs): copy to Drive, hash check, delete the local copies.
"Apply queue + refresh snapshot" runs scripts/refresh_now.sh (apply-queue, pg_snapshot, the report rebuild)
and stops cleanly when a pipeline run holds the database.

When the window gains focus and the clipboard holds a posting ID, it fills the ID box and looks it up.

    .venv/bin/python scripts/feedback_app.py
"""
import os
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.tracker import db  # noqa: E402
from backend.tracker.store import CONTACT_ROLES  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
# The vault is three levels above the Job_Search directory ($JOBSEARCH_VAULT_DIR); its media_offload.py moves finals
# to Google Drive. Tracker folders are relative to Job_Search, e.g. Applications/<Company_Role>.
JOB_SEARCH = Path(os.environ["JOBSEARCH_VAULT_DIR"]) if os.environ.get("JOBSEARCH_VAULT_DIR") else None
VAULT = JOB_SEARCH.parents[2] if JOB_SEARCH else None
OFFLOAD = VAULT / "System" / "Context" / "Skills" / "scripts" / "media_offload.py" if VAULT else None
SCRATCH = Path(os.environ.get("KEYSTONE_SCRATCH") or Path.home() / "share" / "work")

POSTING_RE = re.compile(r"^[0-9a-f]{20}$")
# finder.py PASS_REASON_CODES, most used first, with what each feeds (finder.py mark --help).
REASONS = [("logistics", "Location, commute, travel, schedule (not a training label)"),
           ("level", "Wrong seniority"),
           ("function", "Wrong kind of work"),
           ("requirement", "A hard Required line is not met"),
           ("comp", "Pay (not a training label)"),
           ("nuance", "A judgment call the others don't cover"),
           ("other", "Something else")]
NEEDS_COMMENT = {"nuance", "other"}
GRADES = ["", "bullseye", "adjacent", "stretch", "wrong"]
QUEUE_SQL = """
SELECT q.queue_id, to_char(q.submitted_at, 'MM-DD HH24:MI') AS sub,
       coalesce(f.employer || ' | ' || f.title, q.posting_id) AS role, q.decision || CASE WHEN q.basis = 'blind' THEN ' (blind)' ELSE '' END AS decision,
       concat_ws('  ·  ', q.reason, 'P ' || q.grade_process, 'T ' || q.grade_technical, 'AI ' || q.grade_ai) AS reason,
       CASE WHEN q.cancelled_at IS NOT NULL THEN 'undone'
            WHEN q.applied_at IS NOT NULL THEN 'applied ' || to_char(q.applied_at, 'MM-DD HH24:MI')
            WHEN q.apply_error IS NOT NULL THEN 'REFUSED: ' || q.apply_error
            ELSE 'pending' END AS state
FROM jobs.decision_queue q LEFT JOIN jobs.fit_snapshot f USING (posting_id)
ORDER BY q.queue_id DESC LIMIT 30"""


class App:
    def __init__(self, root):
        self.root, self.pg = root, db.connect()
        self.pid = self.app_ids = None
        self.last_clip = ""
        root.title("Job feedback")
        root.minsize(820, 860)
        f = ttk.Frame(root, padding=10)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Posting ID, tracker ID or URL").grid(row=0, column=0, sticky="w")
        self.ident = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.ident, font=("TkFixedFont", 11))
        e.grid(row=0, column=1, sticky="ew", padx=6)
        e.bind("<Return>", lambda _e: self.lookup())
        e.bind("<FocusOut>", lambda _e: self.lookup())
        ttk.Button(f, text="Look up", command=self.lookup).grid(row=0, column=2)
        self.found = tk.StringVar(value="Paste an ID, or copy one on the focus report and click here.")
        ttk.Label(f, textvariable=self.found, wraplength=700, foreground="#355").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 8))

        d = ttk.LabelFrame(f, text="Pipeline decision (queued; applied when the DuckDB is free)", padding=8)
        d.grid(row=2, column=0, columnspan=3, sticky="ew")
        d.columnconfigure(1, weight=1)
        self.decision = tk.StringVar(value="pass")
        rb = ttk.Frame(d)
        rb.grid(row=0, column=0, columnspan=2, sticky="w")
        for v, label in (("pass", "Pass"), ("build", "Build"), ("hold", "Hold"), ("grade", "Grades only")):
            ttk.Radiobutton(rb, text=label, value=v, variable=self.decision, command=self.sync_fields).pack(side="left", padx=(0, 14))
        # Blind = graded from the posting alone, before any score, rank, Jev call or opinion was seen. Blind grades
        # can measure the models; seen ones only train them. Blind mode hides the machine's calls in the lookup.
        self.basis = tk.StringVar(value="seen")
        ttk.Label(rb, text="   Basis:").pack(side="left")
        for v, label in (("seen", "Seen"), ("blind", "Blind")):
            ttk.Radiobutton(rb, text=label, value=v, variable=self.basis, command=self.render_found).pack(side="left", padx=(4, 6))
        ttk.Label(d, text="Reason").grid(row=1, column=0, sticky="w", pady=3)
        self.reason = ttk.Combobox(d, state="readonly", values=[""] + [f"{c}: {t}" for c, t in REASONS])
        self.reason.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(d, text="Comment").grid(row=2, column=0, sticky="w", pady=3)
        self.comment = tk.StringVar()
        ttk.Entry(d, textvariable=self.comment).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(d, text="Unmet JD line").grid(row=3, column=0, sticky="w", pady=3)
        self.unmet = tk.StringVar()
        self.unmet_entry = ttk.Entry(d, textvariable=self.unmet)
        self.unmet_entry.grid(row=3, column=1, sticky="ew", pady=(3, 0))
        self.unmet_hint = tk.StringVar()
        ttk.Label(d, textvariable=self.unmet_hint, foreground="#666", font=("TkDefaultFont", 9), wraplength=600).grid(
            row=4, column=1, sticky="w", pady=(0, 3))
        ttk.Label(d, text="Lens grades (optional)").grid(row=5, column=0, sticky="w", pady=3)
        lf = ttk.Frame(d)
        lf.grid(row=5, column=1, sticky="w", pady=3)
        self.lens = {}
        for k, label in (("process", "Process"), ("technical", "Technical"), ("ai", "AI")):
            ttk.Label(lf, text=label).pack(side="left", padx=(0, 4))
            self.lens[k] = ttk.Combobox(lf, state="readonly", values=GRADES, width=10)
            self.lens[k].pack(side="left", padx=(0, 14))
        self.reason.bind("<<ComboboxSelected>>", lambda _e: self.sync_fields())
        btns = ttk.Frame(d)
        btns.grid(row=6, column=1, sticky="e", pady=(6, 0))
        ttk.Button(btns, text="Clear", command=self.clear_form).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="Submit decision", command=self.submit).pack(side="left")

        t = ttk.LabelFrame(f, text="Tracker (immediate, through trk)", padding=8)
        t.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        t.columnconfigure(3, weight=1)
        ttk.Label(t, text="App ID").grid(row=0, column=0, sticky="w")
        self.app = ttk.Combobox(t, state="readonly", width=8)
        self.app.grid(row=0, column=1, padx=(4, 12))
        self.app.bind("<<ComboboxSelected>>", self.sync_company)
        statuses = [r["status"] for r in self.pg.execute("SELECT status FROM tracker.status ORDER BY sort_order")]
        self.status = ttk.Combobox(t, state="readonly", values=[""] + statuses, width=16)
        self.status.grid(row=0, column=2, padx=(0, 12))
        self.note = tk.StringVar()
        ttk.Entry(t, textvariable=self.note).grid(row=0, column=3, sticky="ew")
        ttk.Button(t, text="Save to tracker", command=self.set_status).grid(row=0, column=4, padx=(8, 0))
        self.tracker_box = t

        # Contact fields sit inside the Tracker box, under the status row (trk contact on the same app ID).
        ttk.Separator(t).grid(row=1, column=0, columnspan=5, sticky="ew", pady=(8, 4))
        c = ttk.Frame(t)
        c.grid(row=2, column=0, columnspan=5, sticky="ew")
        c.columnconfigure(1, weight=1)
        c.columnconfigure(3, weight=1)
        self.cv = {k: tk.StringVar() for k in ("name", "title", "company", "email", "phone", "linkedin")}
        for row, pair in enumerate(((("Name", "name"), ("Title", "title")), (("Email", "email"), ("Phone", "phone")))):
            for col, (label, key) in enumerate(pair):
                ttk.Label(c, text=label).grid(row=row, column=col * 2, sticky="w", padx=(0 if col == 0 else 10, 4), pady=2)
                ttk.Entry(c, textvariable=self.cv[key]).grid(row=row, column=col * 2 + 1, sticky="ew", pady=2)
        ttk.Label(c, text="Company").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(c, textvariable=self.cv["company"]).grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Label(c, text="Role").grid(row=2, column=2, sticky="w", padx=(10, 4), pady=2)
        self.c_role = ttk.Combobox(c, state="readonly", values=list(CONTACT_ROLES), width=16)
        self.c_role.set("hiring_manager")
        self.c_role.grid(row=2, column=3, sticky="w", pady=2)
        ttk.Label(c, text="LinkedIn URL").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(c, textvariable=self.cv["linkedin"]).grid(row=3, column=1, columnspan=2, sticky="ew", pady=2)
        ttk.Button(c, text="Add contact", command=self.add_contact).grid(row=3, column=3, sticky="e", pady=2)
        self.contact_box = c
        # After applying: move the package's PDFs (vault folder and its scratch twin) to the parallel Drive folder.
        o = ttk.Frame(t)
        o.grid(row=3, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        self.offload_btn = ttk.Button(o, text="Offload PDFs to Drive", command=self.offload_pdfs)
        self.offload_btn.pack(side="left")
        ttk.Label(o, text="after you've attached them: copies to Drive, checks the hash, then deletes the local copies",
                  foreground="#666").pack(side="left", padx=(8, 0))

        # Contacts already on file for the rows found: a read-only list; double-click a cell to copy it.
        k = ttk.LabelFrame(f, text="Contacts on file (double-click a cell to copy it)", padding=6)
        k.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ccols = ("id", "app", "name", "role", "title", "email", "phone", "linkedin")
        self.ctree = ttk.Treeview(k, columns=ccols, show="headings", height=4)
        for col, w in zip(ccols, (40, 50, 140, 100, 180, 190, 100, 220)):
            self.ctree.heading(col, text="#" if col == "id" else col.capitalize())
            self.ctree.column(col, width=w, stretch=col in ("title", "email", "linkedin"))
        self.ctree.pack(fill="x")
        self.ctree.bind("<Double-1>", self.copy_cell)
        self.fill_tracker()

        self.msg = tk.StringVar()
        ttk.Label(f, textvariable=self.msg, foreground="#933", wraplength=760).grid(row=5, column=0, columnspan=3, sticky="w", pady=6)

        q = ttk.LabelFrame(f, text="Recent queued decisions", padding=6)
        q.grid(row=6, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(6, weight=1)
        cols = ("id", "sub", "role", "decision", "reason", "state")
        self.tree = ttk.Treeview(q, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (50, 100, 260, 100, 220, 200)):
            self.tree.heading(c, text=c.capitalize() if c != "sub" else "Submitted")
            self.tree.column(c, width=w, stretch=c in ("role", "reason", "state"))
        self.tree.pack(fill="both", expand=True)
        bar = ttk.Frame(q)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="Undo selected (pending only)", command=self.undo).pack(side="left")
        self.refresh_btn = ttk.Button(bar, text="Apply queue + refresh snapshot", command=self.run_refresh)
        self.refresh_btn.pack(side="left", padx=(10, 0))
        self.on_top = tk.BooleanVar()
        ttk.Checkbutton(bar, text="Keep on top", variable=self.on_top,
                        command=lambda: root.attributes("-topmost", self.on_top.get())).pack(side="right")

        root.bind("<FocusIn>", self.from_clipboard)
        self.sync_fields()
        self.tick()

    # ---------------------------------------------------------------- lookups
    def from_clipboard(self, _e=None):
        try:
            clip = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if POSTING_RE.match(clip) and clip != self.last_clip:
            self.last_clip = clip
            self.ident.set(clip)
            self.lookup()

    def lookup(self):
        text = self.ident.get().strip()
        self.pid, self.app_ids, self.found_parts = None, [], None
        if not text:
            return
        pg = self.pg
        if POSTING_RE.match(text):
            self.pid = text
        elif text.isdigit():
            r = pg.execute("SELECT posting_id, company, role, status FROM tracker.application WHERE app_id = %s", [int(text)]).fetchone()
            if not r:
                return self.show(f"No tracker row {text}.")
            self.app_ids = [int(text)]
            if not r["posting_id"]:
                self.fill_tracker()
                return self.show(f"Tracker #{text}: {r['company']} | {r['role']} ({r['status']}). No posting linked, "
                                 "so only the Tracker box applies." + self.contacts_text())
            self.pid = r["posting_id"]
        elif text.startswith("http"):
            r = pg.execute("SELECT posting_id FROM jobs.fit_snapshot WHERE url = %s OR url = %s LIMIT 1",
                           [text, text.rstrip("/")]).fetchone()
            if not r:
                return self.show("That URL is not in the snapshot. Use the posting ID instead.")
            self.pid = r["posting_id"]
        else:
            return self.show("Not a posting ID (20 hex characters), a tracker ID (a number) or a URL.")
        f = pg.execute("SELECT employer, title, location_primary, workplace_type, latest_decision, jev_result "
                       "FROM jobs.fit_snapshot WHERE posting_id = %s", [self.pid]).fetchone()
        tracked = pg.execute("SELECT app_id, status FROM tracker.application WHERE posting_id = %s ORDER BY app_id",
                             [self.pid]).fetchall()
        self.app_ids = sorted(set(self.app_ids) | {r["app_id"] for r in tracked})
        pending = pg.execute("SELECT count(*) AS n FROM jobs.decision_queue WHERE posting_id = %s AND applied_at IS NULL "
                             "AND cancelled_at IS NULL AND apply_error IS NULL", [self.pid]).fetchone()["n"]
        self.fill_tracker()
        if f:
            base = [f"{f['employer']} | {f['title']}", f"{f['location_primary'] or '?'} ({f['workplace_type'] or 'not stated'})"]
            machine = [f"decision on record: {f['latest_decision'] or 'none'}", f"Jev: {f['jev_result'] or '-'}"]
        else:
            base = [f"{self.pid}: not in the Postgres snapshot (it may still be in the pipeline; apply-queue will check)"]
            machine = []
        if tracked:
            base.append("tracker: " + ", ".join(f"#{r['app_id']} {r['status']}" for r in tracked))
        if pending:
            base.append(f"{pending} call(s) already pending")
        if self.contacts_text():
            base.append(self.contacts_text().strip(" ·"))
        self.found_parts = (base, machine)
        self.render_found()

    def render_found(self):
        """The lookup line; in Blind mode the machine's calls (decision on record, Jev) stay hidden."""
        base, machine = getattr(self, "found_parts", None) or ([], [])
        if not base:
            return
        hidden = self.basis.get() == "blind"
        self.show("  ·  ".join(base + ([] if hidden else machine)) + ("  ·  (Blind: machine calls hidden)" if hidden and machine else ""))

    def contacts_text(self):
        """'  ·  contacts: Name (role), ...' for the rows found, or ''."""
        if not self.app_ids:
            return ""
        rows = self.pg.execute("SELECT DISTINCT c.name, ac.role FROM tracker.app_contact ac JOIN tracker.contact c "
                               "USING (contact_id) WHERE ac.app_id = ANY(%s) ORDER BY c.name", [list(self.app_ids)]).fetchall()
        return ("  ·  contacts: " + ", ".join(f"{r['name']} ({r['role']})" for r in rows)) if rows else ""

    def fill_tracker(self):
        ids = [str(i) for i in self.app_ids or []]
        self.app.configure(values=ids)
        self.app.set(ids[0] if ids else "")
        # No tracker row: grey the box (status row and contact fields) out so it doesn't look usable.
        def walk(w):
            for ch in w.winfo_children():
                try:
                    ch.configure(state=("readonly" if isinstance(ch, ttk.Combobox) else "normal") if ids else "disabled")
                except tk.TclError:
                    pass
                walk(ch)
        walk(self.tracker_box)
        if getattr(self, "ctree", None):
            self.fill_contacts()
        self.tracker_box.configure(text="Tracker: status, note and contacts (immediate, through trk)" if ids
                                   else "Tracker (no tracker row for this posting)")
        if getattr(self, "cv", None):
            self.sync_company()

    def sync_company(self, _e=None):
        """Company always follows the selected tracker row (a new lookup or another App ID), never the last entry."""
        app = self.app.get()
        r = self.pg.execute("SELECT company FROM tracker.application WHERE app_id = %s", [int(app)]).fetchone() if app else None
        self.cv["company"].set(r["company"] if r else "")

    def show(self, text, ok=False):
        self.found.set(text)
        self.msg.set("")

    def sync_fields(self):
        is_pass = self.decision.get() == "pass"   # a reason only for a pass; Grades only takes no reason or unmet
        if not is_pass:
            self.reason.set("")                    # a leftover pass reason must not ride along greyed out
        self.reason.configure(state="readonly" if is_pass else "disabled")
        code = self.reason.get().split(":")[0]
        wants_unmet = self.decision.get() == "build" or (is_pass and code == "requirement")
        if not wants_unmet:
            self.unmet.set("")
        self.unmet_entry.configure(state="normal" if wants_unmet else "disabled")
        decision = self.decision.get()
        if decision == "build":
            hint = ("Optional: a Required line you do NOT meet but are applying anyway, pasted word for word. "
                    "Blank means you meet every Required line. A partial weak spot goes in Comment. Several: join with ||")
        elif wants_unmet:
            hint = "The Required line you don't meet, pasted word for word from the posting. Several: join with ||"
        elif is_pass:
            hint = "Only for a pass with reason 'requirement'; other reasons say nothing about the Required block."
        else:
            hint = "Not used for Hold or Grades only: they make no Required-block claim."
        self.unmet_hint.set(hint)

    def clear_form(self):
        """Back to a fresh form: Pass, no reason, no comment, unmet or lens grades. Keeps the posting and Basis."""
        self.decision.set("pass")
        self.reason.set("")
        self.comment.set("")
        self.unmet.set("")
        for c in self.lens.values():
            c.set("")
        self.msg.set("")
        self.sync_fields()

    # ---------------------------------------------------------------- writes
    def submit(self):
        if not self.pid:
            self.lookup()
        if not self.pid:
            return self.msg.set("Look up a posting first.")
        decision, code, comment = self.decision.get(), self.reason.get().split(":")[0], self.comment.get().strip()
        reason = None
        if decision == "pass":
            if not code:
                return self.msg.set("A pass needs a reason.")
            if code in NEEDS_COMMENT and not comment:
                return self.msg.set(f"'{code}' needs a comment saying what it was.")
            reason = f"{code}: {comment}" if comment else code
        elif comment:
            reason = comment
        lens = [self.lens[k].get() or None for k in ("process", "technical", "ai")]
        if decision == "grade" and not any(lens):
            return self.msg.set("Grades only needs at least one lens grade.")
        unmet = self.unmet.get().strip() if str(self.unmet_entry.cget("state")) == "normal" else ""
        r = self.pg.execute("INSERT INTO jobs.decision_queue (posting_id, decision, reason, unmet, basis, grade_process, "
                            "grade_technical, grade_ai) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING queue_id",
                            [self.pid, decision, reason, unmet or None, self.basis.get(), *lens]).fetchone()
        self.msg.set(f"Queued #{r['queue_id']} ({self.basis.get()}): {decision} {self.pid}" + (f" ({reason})" if reason else ""))
        self.clear_form()
        self.msg.set(f"Queued #{r['queue_id']} ({self.basis.get()}): {decision} {self.pid}" + (f" ({reason})" if reason else ""))
        self.refresh()

    def set_status(self):
        app, status, note = self.app.get(), self.status.get(), self.note.get().strip()
        if not app:
            return self.msg.set("Pick an app ID.")
        if status:      # status change, with the comment logged on it
            cmd = [str(ROOT / "bin" / "trk"), "set", app, status] + (["--note", note] if note else [])
        elif note:      # blank status + a comment = a plain note on the application
            cmd = [str(ROOT / "bin" / "trk"), "note", app, note]
        else:
            return self.msg.set("Pick a status, or leave it blank and type a comment to add a note.")
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout + res.stderr).strip().splitlines()
        if res.returncode == 0:
            self.note.set("")
            self.status.set("")
            self.lookup()          # refresh the tracker line first: lookup clears the message
        self.msg.set(("trk: " + out[-1]) if out else f"trk exited {res.returncode}")

    def add_contact(self):
        app, v = self.app.get(), {k: s.get().strip() for k, s in self.cv.items()}
        if not app:
            return self.msg.set("Look up a tracked application first (the contact is linked to its app ID).")
        if not v["name"]:
            return self.msg.set("A contact needs a name.")
        if v["email"] and "@" not in v["email"]:
            return self.msg.set("That email has no @.")
        if v["linkedin"] and "linkedin.com/" not in v["linkedin"]:
            return self.msg.set("The LinkedIn URL should contain linkedin.com/ (paste the profile link).")
        cmd = [str(ROOT / "bin" / "trk"), "contact", app, v["name"], "--role", self.c_role.get() or "recruiter"]
        for k in ("title", "company", "email", "phone", "linkedin"):
            if v[k]:
                cmd += [f"--{k}", v[k]]
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout + res.stderr).strip().splitlines()
        if res.returncode == 0:
            for k in ("name", "title", "email", "phone", "linkedin"):
                self.cv[k].set("")
            self.lookup()        # shows the new contact in the lookup line; lookup clears the message
        self.msg.set(("trk: " + out[0]) if out else f"trk exited {res.returncode}")

    def offload_pdfs(self):
        """media_offload.py finalize on the selected row's package folder, in a background thread."""
        app = self.app.get()
        if not app:
            return self.msg.set("Look up a tracked application first.")
        if not OFFLOAD or not OFFLOAD.exists():
            return self.msg.set("Can't find media_offload.py: JOBSEARCH_VAULT_DIR in ~/jobsearch/.env must point at the vault's Job_Search folder.")
        r = self.pg.execute("SELECT company, role, folder FROM tracker.application WHERE app_id = %s", [int(app)]).fetchone()
        if not r or not r["folder"]:
            return self.msg.set(f"#{app} has no package folder.")
        rel = (JOB_SEARCH / r["folder"]).relative_to(VAULT).as_posix()
        pdfs = sorted({p.name: p for d in (VAULT / rel, SCRATCH / rel) if d.is_dir() for p in d.glob("*.pdf")}.values())
        if not pdfs:
            return self.msg.set(f"No PDFs in {r['folder']} or its scratch twin; nothing to offload.")
        names = "\n".join(f"  {p.name}  ({p.stat().st_size // 1024} KB)" for p in pdfs)
        if not messagebox.askyesno("Offload PDFs to Drive",
                                   f"#{app} {r['company']}: {r['role']}\n\n{names}\n\nCopy these to Google Drive, verify the hash, "
                                   "then delete the local copies?\n\nOnly do this after you've attached them to the application."):
            return
        import threading
        self.offload_btn.configure(state="disabled")
        self.msg.set(f"Offloading {len(pdfs)} PDF(s) to Drive...")
        self._offload_result = None

        def work():
            res = subprocess.run(["python3", str(OFFLOAD), "finalize", rel], cwd=str(VAULT), capture_output=True, text=True)
            self._offload_result = (res.returncode, [l for l in (res.stdout + res.stderr).splitlines() if l.strip()])
        threading.Thread(target=work, daemon=True).start()
        self.root.after(1000, self.poll_offload)

    def poll_offload(self):
        if self._offload_result is None:
            self.root.after(1000, self.poll_offload)
            return
        rc, lines = self._offload_result
        self.offload_btn.configure(state="normal")
        if rc == 0:
            done = next((l for l in lines if l.startswith("finals:")), lines[-1] if lines else "done")
            self.msg.set("Offloaded: " + done.replace("finals: ", ""))
        elif rc == 2:
            self.msg.set("Not now: " + (lines[-1] if lines else "another offload or backup holds the lock") + " Try again in a minute.")
        else:
            self.msg.set("Offload stopped, nothing deleted: " + (lines[-1] if lines else f"exit {rc}"))

    def undo(self):
        for item in self.tree.selection():
            qid = int(self.tree.item(item, "values")[0])
            n = self.pg.execute("UPDATE jobs.decision_queue SET cancelled_at = now() WHERE queue_id = %s "
                                "AND applied_at IS NULL AND cancelled_at IS NULL", [qid]).rowcount
            self.msg.set(f"Undid #{qid}." if n else f"#{qid} was already applied or undone; mark it again to change it.")
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for r in self.pg.execute(QUEUE_SQL):
            self.tree.insert("", "end", values=(r["queue_id"], r["sub"], r["role"], r["decision"], r["reason"], r["state"]))

    def fill_contacts(self):
        self.ctree.delete(*self.ctree.get_children())
        if not self.app_ids:
            return
        for r in self.pg.execute(
                "SELECT c.contact_id, ac.app_id, c.name, ac.role, c.title, c.email, c.phone, c.linkedin "
                "FROM tracker.app_contact ac JOIN tracker.contact c USING (contact_id) WHERE ac.app_id = ANY(%s) "
                "ORDER BY ac.app_id, c.name", [list(self.app_ids)]):
            self.ctree.insert("", "end", values=[" ".join(str(r[k] or "").split()) for k in
                                                 ("contact_id", "app_id", "name", "role", "title", "email", "phone", "linkedin")])

    def copy_cell(self, e):
        row, col = self.ctree.identify_row(e.y), self.ctree.identify_column(e.x)
        if not row or not col:
            return
        val = self.ctree.item(row, "values")[int(col[1:]) - 1]
        if val:
            self.root.clipboard_clear()
            self.root.clipboard_append(val)
            self.last_clip = val          # so the clipboard watcher doesn't treat it as a new posting ID
            self.msg.set(f"Copied: {val}")

    # ---------------------------------------------------------------- on-demand refresh
    def run_refresh(self):
        """scripts/refresh_now.sh in a background thread: apply the queue, snapshot, rebuild the report.
        It stops cleanly (exit 2) when a pipeline run holds the database."""
        import threading
        self.refresh_btn.configure(state="disabled")
        self.msg.set("Refreshing: applying the queue, taking a snapshot, rebuilding the report (a minute or two)...")
        self._refresh_result = None

        def work():
            res = subprocess.run([str(ROOT / "scripts" / "refresh_now.sh")], capture_output=True, text=True)
            lines = [l for l in (res.stdout + res.stderr).splitlines() if l.strip()]
            self._refresh_result = (res.returncode, lines)
        threading.Thread(target=work, daemon=True).start()
        self.root.after(1000, self.poll_refresh)

    def poll_refresh(self):
        if self._refresh_result is None:
            self.root.after(1000, self.poll_refresh)
            return
        rc, lines = self._refresh_result
        final = next((l for l in reversed(lines) if l.startswith(("DONE", "LOCKED", "FAILED"))), lines[-1] if lines else "")
        self.msg.set(final + ("" if rc in (0, 2) else "  (log: logs/refresh_now.log)"))
        self.refresh_btn.configure(state="normal")
        self.refresh()

    def tick(self):
        """Refresh the queue list every minute, so applied / refused states show up on their own."""
        self.refresh()
        self.root.after(60_000, self.tick)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
