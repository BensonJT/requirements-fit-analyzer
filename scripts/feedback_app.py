#!/usr/bin/env python3
"""Feedback applet: record a build / pass / hold call on a posting without waiting for the DuckDB lock.

A small desktop window (tkinter). Paste a posting ID (20 hex), a tracker app ID, or a posting URL; the
window shows the employer and title, then queues the call in Postgres (jobs.decision_queue). Nothing here
touches the pipeline DuckDB: `finder.py apply-queue` writes queued calls into it at the next launch start,
just before the snapshot, or within the hour (scripts/inbox_watch.sh), with the same checks as
`finder.py mark`. A refused call shows its reason in the queue list; fix it and submit again.

For a posting with a tracker row, the Tracker box changes the application's status through `trk set`
(immediate: the tracker is Postgres already).

When the window gains focus and the clipboard holds a posting ID, it fills the ID box and looks it up.

    .venv/bin/python scripts/feedback_app.py
"""
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.tracker import db  # noqa: E402

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
       coalesce(f.employer || ' | ' || f.title, q.posting_id) AS role, q.decision, coalesce(q.reason, '') AS reason,
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
        root.minsize(760, 560)
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
        for v, label in (("pass", "Pass"), ("build", "Build"), ("hold", "Hold")):
            ttk.Radiobutton(rb, text=label, value=v, variable=self.decision, command=self.sync_fields).pack(side="left", padx=(0, 14))
        ttk.Label(d, text="Reason").grid(row=1, column=0, sticky="w", pady=3)
        self.reason = ttk.Combobox(d, state="readonly", values=[f"{c}: {t}" for c, t in REASONS])
        self.reason.grid(row=1, column=1, sticky="ew", pady=3)
        ttk.Label(d, text="Comment").grid(row=2, column=0, sticky="w", pady=3)
        self.comment = tk.StringVar()
        ttk.Entry(d, textvariable=self.comment).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(d, text="Unmet JD line").grid(row=3, column=0, sticky="w", pady=3)
        self.unmet = tk.StringVar()
        self.unmet_entry = ttk.Entry(d, textvariable=self.unmet)
        self.unmet_entry.grid(row=3, column=1, sticky="ew", pady=3)
        ttk.Label(d, text="Lane grade (optional)").grid(row=4, column=0, sticky="w", pady=3)
        self.grade = ttk.Combobox(d, state="readonly", values=GRADES, width=12)
        self.grade.grid(row=4, column=1, sticky="w", pady=3)
        self.reason.bind("<<ComboboxSelected>>", lambda _e: self.sync_fields())
        ttk.Button(d, text="Submit decision", command=self.submit).grid(row=5, column=1, sticky="e", pady=(6, 0))

        t = ttk.LabelFrame(f, text="Tracker (immediate, through trk)", padding=8)
        t.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        t.columnconfigure(3, weight=1)
        ttk.Label(t, text="App ID").grid(row=0, column=0, sticky="w")
        self.app = ttk.Combobox(t, state="readonly", width=8)
        self.app.grid(row=0, column=1, padx=(4, 12))
        statuses = [r["status"] for r in self.pg.execute("SELECT status FROM tracker.status ORDER BY sort_order")]
        self.status = ttk.Combobox(t, state="readonly", values=statuses, width=16)
        self.status.grid(row=0, column=2, padx=(0, 12))
        self.note = tk.StringVar()
        ttk.Entry(t, textvariable=self.note).grid(row=0, column=3, sticky="ew")
        ttk.Button(t, text="Set status", command=self.set_status).grid(row=0, column=4, padx=(8, 0))
        self.tracker_box = t

        self.msg = tk.StringVar()
        ttk.Label(f, textvariable=self.msg, foreground="#933", wraplength=700).grid(row=4, column=0, columnspan=3, sticky="w", pady=6)

        q = ttk.LabelFrame(f, text="Recent queued decisions", padding=6)
        q.grid(row=5, column=0, columnspan=3, sticky="nsew")
        f.rowconfigure(5, weight=1)
        cols = ("id", "sub", "role", "decision", "reason", "state")
        self.tree = ttk.Treeview(q, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (50, 90, 260, 60, 220, 200)):
            self.tree.heading(c, text=c.capitalize() if c != "sub" else "Submitted")
            self.tree.column(c, width=w, stretch=c in ("role", "reason", "state"))
        self.tree.pack(fill="both", expand=True)
        bar = ttk.Frame(q)
        bar.pack(fill="x", pady=(4, 0))
        ttk.Button(bar, text="Undo selected (pending only)", command=self.undo).pack(side="left")
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
        self.pid, self.app_ids = None, []
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
                                 "so only the Tracker box applies.")
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
            parts = [f"{f['employer']} | {f['title']}", f"{f['location_primary'] or '?'} ({f['workplace_type'] or 'not stated'})",
                     f"decision on record: {f['latest_decision'] or 'none'}", f"Jev: {f['jev_result'] or '-'}"]
        else:
            parts = [f"{self.pid}: not in the Postgres snapshot (it may still be in the pipeline; apply-queue will check)"]
        if tracked:
            parts.append("tracker: " + ", ".join(f"#{r['app_id']} {r['status']}" for r in tracked))
        if pending:
            parts.append(f"{pending} call(s) already pending")
        self.show("  ·  ".join(parts), ok=True)

    def fill_tracker(self):
        ids = [str(i) for i in self.app_ids or []]
        self.app.configure(values=ids)
        self.app.set(ids[0] if ids else "")

    def show(self, text, ok=False):
        self.found.set(text)
        self.msg.set("")

    def sync_fields(self):
        is_pass = self.decision.get() == "pass"
        self.reason.configure(state="readonly" if is_pass else "disabled")
        code = self.reason.get().split(":")[0]
        wants_unmet = self.decision.get() == "build" or (is_pass and code == "requirement")
        self.unmet_entry.configure(state="normal" if wants_unmet else "disabled")

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
        unmet = self.unmet.get().strip() if str(self.unmet_entry.cget("state")) == "normal" else ""
        r = self.pg.execute("INSERT INTO jobs.decision_queue (posting_id, decision, reason, unmet, grade) "
                            "VALUES (%s, %s, %s, %s, %s) RETURNING queue_id",
                            [self.pid, decision, reason, unmet or None, self.grade.get() or None]).fetchone()
        self.msg.set(f"Queued #{r['queue_id']}: {decision} {self.pid}" + (f" ({reason})" if reason else ""))
        self.comment.set("")
        self.unmet.set("")
        self.grade.set("")
        self.refresh()

    def set_status(self):
        app, status = self.app.get(), self.status.get()
        if not app or not status:
            return self.msg.set("Pick an app ID and a status.")
        cmd = [str(ROOT / "bin" / "trk"), "set", app, status] + (["--note", self.note.get().strip()] if self.note.get().strip() else [])
        res = subprocess.run(cmd, capture_output=True, text=True)
        out = (res.stdout + res.stderr).strip().splitlines()
        self.msg.set(("trk: " + out[-1]) if out else f"trk exited {res.returncode}")
        if res.returncode == 0:
            self.note.set("")
            self.lookup()

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
