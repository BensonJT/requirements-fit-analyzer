#!/usr/bin/env python3
"""Re-grade study: measure how repeatable the user's own blind Required-fit calls are (test-retest).

A measurement system analysis of the human gold labels (vault Professional/Areas/Articles/BACKLOG.md item 6, A2).
The user re-grades a stratified sample of his own blind gold rows, weeks later, without seeing his earlier call,
any score, rank or judge call. The re-grades live in jobs.regrade_study (db/pg/013) and NEVER touch
report_feedback or jobs.decision_queue, so the gold set is unchanged whatever he answers.

    .venv/bin/python scripts/regrade_study.py draw --study msa1 --meets 29 --fails 31 --min-age-days 21
    .venv/bin/python scripts/regrade_study.py gui --study msa1        # also: the applet's "Re-grade study" button
    .venv/bin/python scripts/regrade_study.py report --study msa1

`draw` reads the DuckDB read-only (once); grading and the report read Postgres only.
"""
import argparse
import os
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.finder import regrade_stats  # noqa: E402
from backend.tracker import db  # noqa: E402

DEFAULT_STUDY = "msa1"
DUCKDB = Path(os.environ.get("JOBSEARCH_DUCKDB") or ROOT / "db" / "jobsearch.duckdb")   # env: run from a worktree

HEADING_RE = re.compile(r"qualif|requir|must have|responsib|preferred|education|experience|skills|what you|about you|"
                        r"who you are|what we|you will|you'll|duties", re.I)

GOLD_SQL = """
    SELECT rf.posting_id, rf.description_hash, rf.required_fit AS original_fit, rf.assessed_at AS original_at,
           p.employer, p.title, p.description_text
    FROM vw_report_feedback_blind rf JOIN postings p USING (posting_id)
    WHERE rf.required_fit IN ('meets', 'fails') AND p.description_text IS NOT NULL"""


def gold_rows(duck_path=DUCKDB):
    import duckdb
    con = duckdb.connect(str(duck_path), read_only=True)
    try:
        cols = ["posting_id", "description_hash", "original_fit", "original_at", "employer", "title", "description_text"]
        return [dict(zip(cols, r)) for r in con.execute(GOLD_SQL).fetchall()]
    finally:
        con.close()


def cmd_draw(args):
    from datetime import datetime, timedelta, timezone
    pg = db.connect()
    if pg.execute("SELECT 1 FROM jobs.regrade_study WHERE study_id = %s LIMIT 1", [args.study]).fetchone():
        print(f"study {args.study} already drawn; pick a new --study name")
        return 1
    rows = gold_rows()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.min_age_days)
    def aware(t):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    old = [r for r in rows if aware(r["original_at"]) <= cutoff]
    chosen = regrade_stats.select_sample(old, args.meets, args.fails, args.seed)
    with pg.transaction():
        for r in chosen:
            pg.execute("INSERT INTO jobs.regrade_study (study_id, item_no, posting_id, description_hash, employer, title, "
                       "description_text, original_fit, original_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                       [args.study, r["item_no"], r["posting_id"], r["description_hash"], r["employer"], r["title"],
                        r["description_text"], r["original_fit"], r["original_at"]])
    pop = {k: sum(r["original_fit"] == k for r in rows) for k in regrade_stats.CATS}
    print(f"drew {len(chosen)} for study {args.study} (seed {args.seed}): {args.meets} meets + {args.fails} fails, "
          f"all graded at least {args.min_age_days} days ago; gold set now {pop['meets']} meets / {pop['fails']} fails")
    return 0


def population_share():
    """Meets / fails shares of the whole blind gold set (DuckDB, read-only); None if the DuckDB is busy."""
    try:
        rows = gold_rows()
    except Exception:
        return None
    n = len(rows)
    return {k: sum(r["original_fit"] == k for r in rows) / n for k in regrade_stats.CATS} if n else None


def cmd_report(args):
    pg = db.connect()
    rows = pg.execute("SELECT original_fit, regrade_fit, changes FROM jobs.regrade_study WHERE study_id = %s",
                      [args.study]).fetchall()
    done = [(r["original_fit"], r["regrade_fit"]) for r in rows if r["regrade_fit"]]
    print(f"study {args.study}: {len(done)} of {len(rows)} re-graded; answers changed with Back: "
          f"{sum(r['changes'] for r in rows)}")
    if not done:
        return 0
    share = population_share()
    s = regrade_stats.summary(done, share)
    lo, hi = s["agreement_ci"]
    print(f"percent agreement  {s['percent_agreement']:.1%}  ({s['agree']} of {s['n']}; 95% CI {lo:.0%}-{hi:.0%})")
    print(f"Cohen's kappa      {s['kappa']:.2f}  (95% CI {s['kappa_ci'][0]:.2f} to {s['kappa_ci'][1]:.2f})")
    print(f"Gwet's AC1         {s['ac1']:.2f}  (95% CI {s['ac1_ci'][0]:.2f} to {s['ac1_ci'][1]:.2f})")
    for k, (a, n) in s["by_original"].items():
        print(f"  originally {k:5s}: kept {a} of {n}")
    c = s["confusion"]
    print(f"then meets -> now fails: {c[('meets', 'fails')]}   then fails -> now meets: {c[('fails', 'meets')]}")
    if share:
        print(f"weighted to the gold set's mix ({share['meets']:.0%} meets): agreement {s['weighted_agreement']:.1%}")
    return 0


class RegradeWindow:
    """One posting at a time: employer, title and the posting text, then Meets or Fails. Nothing else is shown."""

    def __init__(self, master, study=DEFAULT_STUDY, pg=None):
        self.pg = pg or db.connect()
        self.study = study
        self.win = tk.Toplevel(master) if master is not None else tk.Tk()
        self.win.title(f"Re-grade study ({study})")
        self.win.geometry("980x760+60+60")
        f = ttk.Frame(self.win, padding=10)
        f.pack(fill="both", expand=True)
        intro = ("Read the posting and make the Required-fit call as you normally would: do you meet its Required "
                 "block? Your earlier call, the scores and the judges are hidden. Each answer saves at once; close "
                 "the window any time and you'll pick up where you left off. Keys: M = meets, F = fails.")
        ttk.Label(f, text=intro, wraplength=940, foreground="#355").pack(anchor="w")
        self.progress = tk.StringVar()
        ttk.Label(f, textvariable=self.progress, font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(8, 2))
        self.head = tk.StringVar()
        ttk.Label(f, textvariable=self.head, font=("TkDefaultFont", 12, "bold"), wraplength=940).pack(anchor="w", pady=(0, 6))
        tf = ttk.Frame(f)
        tf.pack(fill="both", expand=True)
        self.text = tk.Text(tf, wrap="word", font=("TkDefaultFont", 11), padx=8, pady=6)
        sb = ttk.Scrollbar(tf, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.tag_configure("heading", font=("TkDefaultFont", 11, "bold"), foreground="#1f4e79")
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        nf = ttk.Frame(f)
        nf.pack(fill="x", pady=(8, 0))
        ttk.Label(nf, text="Note (optional)").pack(side="left")
        self.note = tk.StringVar()
        self.note_entry = ttk.Entry(nf, textvariable=self.note)
        self.note_entry.pack(side="left", fill="x", expand=True, padx=(6, 0))
        bf = ttk.Frame(f)
        bf.pack(fill="x", pady=(8, 0))
        ttk.Button(bf, text="< Back (change my last answer)", command=self.back).pack(side="left")
        self.fails_btn = ttk.Button(bf, text="Fails  (F)", command=lambda: self.answer("fails"))
        self.fails_btn.pack(side="right")
        self.meets_btn = ttk.Button(bf, text="Meets  (M)", command=lambda: self.answer("meets"))
        self.meets_btn.pack(side="right", padx=(0, 10))
        self.msg = tk.StringVar()
        ttk.Label(f, textvariable=self.msg, foreground="#933").pack(anchor="w", pady=(6, 0))
        for key, val in (("m", "meets"), ("f", "fails")):
            self.win.bind(f"<KeyPress-{key}>", lambda e, v=val: self._key(v))
        self.item = None
        self.load_next()

    def _key(self, val):
        if self.win.focus_get() is self.note_entry:   # typing a note, not answering
            return
        self.answer(val)

    def counts(self):
        r = self.pg.execute("SELECT count(*) AS n, count(regrade_fit) AS d FROM jobs.regrade_study WHERE study_id = %s",
                            [self.study]).fetchone()
        return r["d"], r["n"]

    def show(self, row):
        self.item = row["item_no"] if row else None
        done, total = self.counts()
        if total == 0:
            self.progress.set(f"No study named {self.study} has been drawn yet.")
            return self._disable()
        if row is None:
            self.progress.set(f"All {total} done. Thank you. Claude will score the study (regrade_study.py report).")
            self.head.set("")
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
            return self._disable()
        again = f"  (changing your answer: {row['regrade_fit']})" if row["regrade_fit"] else ""
        self.progress.set(f"Posting {row['item_no']} of {total}   ·   {done} answered{again}")
        self.head.set(f"{row['employer']}  |  {row['title']}")
        self.note.set(row["note"] or "")
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", row["description_text"] or "(no text stored)")
        self._mark_headings()
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)
        self.meets_btn.configure(state="normal")
        self.fails_btn.configure(state="normal")

    def _mark_headings(self):
        """Bold the short heading-like lines (Required, Qualifications, Responsibilities...) so the Required block is
        quick to find; the posting text itself is shown exactly as stored."""
        for i, line in enumerate(self.text.get("1.0", "end").splitlines(), 1):
            t = line.strip()
            if t and len(t) <= 70 and HEADING_RE.search(t):
                self.text.tag_add("heading", f"{i}.0", f"{i}.end")

    def _disable(self):
        self.meets_btn.configure(state="disabled")
        self.fails_btn.configure(state="disabled")

    def load_next(self):
        row = self.pg.execute("SELECT item_no, employer, title, description_text, regrade_fit, note FROM jobs.regrade_study "
                              "WHERE study_id = %s AND regrade_fit IS NULL ORDER BY item_no LIMIT 1", [self.study]).fetchone()
        self.show(row)

    def answer(self, val):
        if self.item is None:
            return
        n = self.pg.execute("UPDATE jobs.regrade_study SET changes = changes + CASE WHEN regrade_fit IS NOT NULL "
                            "AND regrade_fit <> %s THEN 1 ELSE 0 END, regrade_fit = %s, regraded_at = now(), note = %s "
                            "WHERE study_id = %s AND item_no = %s",
                            [val, val, self.note.get().strip() or None, self.study, self.item]).rowcount
        self.msg.set(f"Saved posting {self.item}: {val}." if n else "Not saved: the row is gone; tell Claude.")
        self.load_next()

    def back(self):
        row = self.pg.execute("SELECT item_no, employer, title, description_text, regrade_fit, note FROM jobs.regrade_study "
                              "WHERE study_id = %s AND regrade_fit IS NOT NULL ORDER BY regraded_at DESC LIMIT 1",
                              [self.study]).fetchone()
        if row is None:
            return self.msg.set("Nothing answered yet.")
        self.msg.set("")
        self.show(row)


def cmd_gui(args):
    w = RegradeWindow(None, args.study)
    w.win.mainloop()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draw", help="draw the stratified sample once (reads the DuckDB read-only)")
    d.add_argument("--study", default=DEFAULT_STUDY)
    d.add_argument("--meets", type=int, default=29)
    d.add_argument("--fails", type=int, default=31)
    d.add_argument("--min-age-days", type=int, default=21)
    d.add_argument("--seed", type=int, default=20261111)
    for name in ("gui", "report"):
        s = sub.add_parser(name)
        s.add_argument("--study", default=os.environ.get("REGRADE_STUDY", DEFAULT_STUDY))
    args = ap.parse_args(argv)
    return {"draw": cmd_draw, "gui": cmd_gui, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
