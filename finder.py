#!/usr/bin/env python3
"""Job finder: screen the ATS corpus against the profile rules, write the Jobs_Found report,
and record decisions so nothing is shown twice.

Usage:
    .venv/bin/python finder.py screen                     # new / changed / version-changed rows
    .venv/bin/python finder.py rescreen-all               # every active row, prints the verdict diff
    .venv/bin/python finder.py facets [--discover]        # each board's filter scope (country / partition)
    .venv/bin/python finder.py report --out /tmp/x.md     # Jobs_Found file (+ snapshots)
    .venv/bin/python finder.py lenses --out /tmp/l.md     # strong on process / technical / both
    .venv/bin/python finder.py top --out /tmp/t.md        # END-of-pipeline list: run after judge import
    .venv/bin/python finder.py sync --verbose             # mirror Application_Tracker.md
    .venv/bin/python finder.py mark <posting_id|url|"employer|title"> pass --reason "travel"
    .venv/bin/python finder.py shortlist --days 7 --n 30
    .venv/bin/python finder.py labels --report                # rebuild label_docs from the vault
    .venv/bin/python finder.py labels --reanchor [--dry-run]  # recover llm_labels orphaned before the
                                                                # description_hash normalization fix (§ v11)
    .venv/bin/python finder.py train --report                 # TF-IDF + LR on the labels
    .venv/bin/python finder.py setup-check                    # personal files, optional dependencies, DB
    .venv/bin/python finder.py evidence --check               # evidence manifest: sources, units, samples
    .venv/bin/python finder.py evidence --rebuild             # embed new evidence units
    .venv/bin/python finder.py coverage [--all] [--limit N]   # requirement coverage for survivors
    .venv/bin/python finder.py coverage --calibrate           # thresholds vs hard negatives (§16.1)
    .venv/bin/python finder.py feedback --load CSV --agreement --export OUT  # golden-source load/check/re-export
    .venv/bin/python finder.py train --lens ai                # train one lens's model (process|technical|ai)
    .venv/bin/python finder.py train --lens required          # the Required-block ranking model (NOT a lens)

Every subcommand takes --db (default db/jobsearch.duckdb) and --vault (default $JOBSEARCH_VAULT_DIR).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO, ".env"))
from backend.ats import store  # noqa: E402
from backend.finder import features, labels, pipeline, report, tracker_sync, version  # noqa: E402

PASS_REASON_CODES = ("function", "nuance", "logistics", "comp", "other")
from backend.screen import company_keys, company_matches, norm_company, similar_title  # noqa: E402


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _latest_counts(con) -> dict:
    return dict(con.execute("""SELECT s.verdict, count(*) FROM vw_screen_latest s JOIN postings p USING (posting_id)
                               WHERE p.status = 'active' GROUP BY 1""").fetchall())


def cmd_screen(con, a):
    since = _utcnow() - timedelta(hours=a.since_hours) if a.since_hours else None
    model = None if a.no_model else features.load_latest(con)
    lens_models = {} if a.no_model else features.load_lens_models(con)
    required_model = None if a.no_model else features.load_required_model(con)
    pipeline.screen(con, since=since, full=a.full, limit=a.limit, model=model, lens_models=lens_models,
                    required_model=required_model)


def cmd_rescreen_all(con, a):
    before = _latest_counts(con)
    pipeline.screen(con, full=True, model=None if a.no_model else features.load_latest(con),
                    lens_models={} if a.no_model else features.load_lens_models(con),
                    required_model=None if a.no_model else features.load_required_model(con))
    after = _latest_counts(con)
    print(f"{'verdict':<10} {'before':>8} {'after':>8} {'diff':>8}")
    for v in sorted(set(before) | set(after)):
        print(f"{v:<10} {before.get(v, 0):>8} {after.get(v, 0):>8} {after.get(v, 0) - before.get(v, 0):>+8}")


def cmd_facets(con, a):
    """Ask each Workday board what it can filter by, resolve a scope, verify it live, store it."""
    from backend.ats import facets as facets_mod
    from backend.ats.registry import load_registry
    rows = [r for r in load_registry() if r["platform"] == "workday"]
    if a.employer:
        want = a.employer.lower()
        rows = [r for r in rows if want in r["employer"].lower()]
    if not rows:
        sys.exit("facets: no matching Workday board in the registry")
    if not a.discover:
        for r in con.execute("SELECT employer, strategy, facet_parameter, reported_total, clamped, note "
                             "FROM board_scope ORDER BY employer").fetchall():
            print(f"{r[0]:<28} {r[1]:<10} {str(r[2] or ''):<22} total {str(r[3] or '?'):>6} "
                  f"clamped {str(r[4]):<5} {r[5] or ''}")
        return
    print(f"Discovering facets for {len(rows)} Workday board(s)...")
    facets_mod.discover_and_record(con, rows)


def cmd_report(con, a):
    if not a.out and not a.vault:
        sys.exit("report: set JOBSEARCH_VAULT_DIR or pass --out")
    since = _utcnow() - timedelta(hours=a.since_hours)
    latest = con.execute("SELECT model_version FROM screens ORDER BY screened_at DESC LIMIT 1").fetchone()
    mv = latest[0] if latest else "none"
    funnel = con.execute("""SELECT s.verdict, count(*) FROM vw_screen_latest s JOIN postings p USING (posting_id)
                            WHERE p.status = 'active' AND p.first_seen_at >= ? GROUP BY 1""", [since]).fetchall()
    meta = {"since": since, "screen": None, "passed_since": since,
            "funnel": {"screened": sum(n for _, n in funnel), "verdict": dict(funnel)},
            "rules_version": version.rules_version(), "model_version": mv,
            "stages": {"model": mv != "none", "embed": False, "llm": False}}
    path = report.write_jobs_found(con, a.vault, meta, max_blocks=a.max_blocks, block_min_band=a.block_min_band,
                                   table_min=a.table_min, out_path=a.out)
    print(path)
    if not a.no_snapshots:
        for p in report.snapshots(con):
            print(p)


def resolve_posting(con, target: str) -> list:
    """(posting_id, employer, title, status) hits for a posting id, a URL, or 'employer|title'."""
    t = target.strip()
    cols = "posting_id, employer, title, status"
    if re.fullmatch(r"[0-9a-f]{20}", t):
        return con.execute(f"SELECT {cols} FROM postings WHERE posting_id = ?", [t]).fetchall()
    if t.startswith(("http://", "https://")):
        hits = con.execute(f"SELECT {cols} FROM postings WHERE url = ?", [t]).fetchall()
        return hits or con.execute(f"SELECT {cols} FROM postings WHERE length(req_id) >= 4 "
                                   "AND position(req_id IN ?) > 0", [t]).fetchall()
    if "|" in t:
        employer, title = (x.strip() for x in t.split("|", 1))
        keys = company_keys(employer)
        employers = [e for (e,) in con.execute("SELECT DISTINCT employer FROM postings").fetchall()
                     if company_matches(norm_company(e), keys)]
        rows = con.execute(f"SELECT {cols} FROM postings WHERE employer IN (SELECT unnest(?::VARCHAR[]))",
                           [employers]).fetchall()
        hits = [r for r in rows if similar_title(title, r[2] or "")]
        active = [r for r in hits if r[3] == "active"]
        return active or hits
    return []


def cmd_lenses(con, a):
    """The three lens lists: strong on process, strong on technical, strong on both."""
    if not a.out and not a.vault:
        sys.exit("lenses: set JOBSEARCH_VAULT_DIR or pass --out")
    path = report.write_lens_lists(con, a.vault, cap=a.cap, out_path=a.out)
    for bucket, heading, _sub in report.LENS_LISTS:
        n = con.execute("SELECT count(*) FROM vw_lens_fit WHERE lens_bucket = ? AND verdict != 'reject' "
                        "AND NOT decided AND NOT in_tracker", [bucket]).fetchone()[0]
        print(f"{heading:<26} {n:>6} actionable")
    # Applied-AI lens (21): reported beside the three buckets, never folded into lens_bucket.
    ai_n = con.execute("SELECT count(*) FROM vw_lens_fit WHERE ai_strong AND verdict != 'reject' "
                       "AND NOT decided AND NOT in_tracker").fetchone()[0]
    print(f"{'Strong on APPLIED AI':<26} {ai_n:>6} actionable")
    for src, n in con.execute("SELECT lens_source, count(*) FROM vw_lens_fit GROUP BY 1 ORDER BY 2 DESC").fetchall():
        print(f"  placed by {src:<12} {n:>6}")
    print(f"Wrote {path}")


def cmd_top(con, a):
    """END-of-pipeline apply/review list, read off vw_selection -- run by hand after a judge import."""
    if not a.out and not a.vault and not a.stdout:
        sys.exit("top: set JOBSEARCH_VAULT_DIR, or pass --out or --stdout")
    if a.stdout:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = report.write_top_jobs(con, a.vault, out_path=tmp, apply_cap=a.apply_cap,
                                         review_cap=a.review_cap, include_decided=a.include_decided)
            print(path.read_text(encoding="utf-8"))
        return
    path = report.write_top_jobs(con, a.vault, out_path=a.out, apply_cap=a.apply_cap, review_cap=a.review_cap,
                                 include_decided=a.include_decided)
    print(path)


def cmd_mark(con, a):
    hits = resolve_posting(con, a.target)
    if len(hits) != 1:
        print(f"mark: {len(hits)} postings match {a.target!r}; need exactly one.")
        for pid, employer, title, status in hits[:20]:
            print(f"  {pid}  {status:<6}  {employer} | {title}")
        sys.exit(1)
    pid, employer, title, _ = hits[0]
    if a.decision == "pass":
        code = re.match(r"\s*(\w+)", a.reason or "")
        if not code or code.group(1).lower() not in PASS_REASON_CODES:
            sys.exit(f"mark pass: --reason must start with one of {', '.join(PASS_REASON_CODES)} "
                     '(e.g. --reason "function: clinical operations, not process work")')
    con.execute("INSERT INTO decisions VALUES (?, ?, ?, 'cli', NULL, ?)", [pid, a.decision, a.reason, _utcnow()])
    print(f"{a.decision}: {pid}  {employer} | {title}")


def cmd_sync(con, a):
    if not a.vault:
        sys.exit("sync: set JOBSEARCH_VAULT_DIR or pass --vault")
    print(tracker_sync.sync(con, a.vault, verbose=a.verbose))


def cmd_shortlist(con, a):
    rows = con.execute("""SELECT final_score, band, tier, employer, title, location_primary, days_since_first_seen,
                                 posting_id, coverage_required, coverage_role FROM vw_scored_new(?) LIMIT ?""",
                       [a.days, a.n]).fetchall()
    cov = lambda x: "-" if x is None else f"{x:.0f}"  # noqa: E731
    print(f"{'score':>5} {'band':<11} {'t':>1} {'req':>3} {'role':>4} {'employer':<28} {'title':<60} {'location':<24} "
          f"{'age':>3} posting_id")
    for score, band, tier, employer, title, loc, age, pid, c_req, c_role in rows:
        print(f"{score:>5} {band:<11} {tier if tier is not None else '-':>1} {cov(c_req):>3} {cov(c_role):>4} "
              f"{(employer or '')[:28]:<28} {(title or '')[:60]:<60} {(loc or '')[:24]:<24} {age:>3} {pid}")


def cmd_labels(con, a):
    if a.reanchor:
        from backend.finder import reanchor as reanchor_mod
        import glob
        dirs = a.batch_dir or sorted(glob.glob(os.path.join(REPO, "db", "batches*")))
        reanchor_mod.reanchor(con, dirs, dry_run=a.dry_run)
        return
    if not a.vault:
        sys.exit("labels: set JOBSEARCH_VAULT_DIR or pass --vault")
    counts = labels.sync_labels(con, a.vault, n_pseudo=a.pseudo, seed=a.seed)
    if a.report:
        pos_ok, neg_ok = counts["pos_text"] >= 250, counts["pseudo_text"] >= features.LOW_DATA_MIN
        print(f"{'source':<22} {'label':>5} {'docs':>6} {'text':>6} {'matched':>7}")
        for (source, label), c in sorted(counts["by_source"].items()):
            print(f"{source:<22} {label:>5} {c['docs']:>6} {c['with_text']:>6} {c['matched']:>7}")
        print(f"positives with text {counts['pos_text']} ({'ok' if pos_ok else 'below 250'}) · pseudo-negatives "
              f"{counts['pseudo_text']} ({'ok' if neg_ok else 'below 150: low-data warning path'}) · context-only "
              f"(not trained) {counts['context_rows']} · after dedupe for training: "
              f"{len(features.training_set(con))} docs")


def cmd_train(con, a):
    result = features.train(con, C=a.C, cv=a.cv, lens=a.lens)
    for side in ("positive", "negative"):
        print(f"Most {side} terms: " + ", ".join(f"{t} {c:+.2f}" for t, c in result["coefficients"][side]))
    print("Highest held-out fit among the negatives (the rows the model still reads as fits — since the "
          "labeling run these are mostly graded `wrong`/`stretch`, not random postings):")
    for prob, source, label_id, company, title in features.hard_negatives(result):
        ref = con.execute("SELECT source_ref FROM label_docs WHERE label_id = ?", [label_id]).fetchone()
        print(f"  {prob:.2f}  {source:<20} {(company or '')[:28]:<28} {(title or '')[:60]:<60} "
              f"{(ref[0] if ref and ref[0] else label_id)[:60]}")
    if a.report:
        print("Signal AUCs over labeled rows that have a screen (fit = held-out probability):")
        for name, auc_all, _, n_all, _ in features.signal_report(con, result):
            print(f"  {name:<10} AUC {auc_all}  (n={n_all})")


def judge_batch_default() -> int:
    from backend.finder import judge
    return judge.BATCH_SIZE


def _manifest(a):
    from backend.finder import evidence
    path = evidence.manifest_path(a.manifest)
    if not path.exists():
        sys.exit(f"no evidence manifest at {path}: copy evidence.example.toml to evidence.local.toml "
                 "(see docs/SETUP_CONTEXT.md)")
    return evidence.load_manifest(str(path))


def cmd_required_embed(con, a):
    from backend.finder import required_embed
    if a.action == "train":
        result = required_embed.train(con)
        if a.report:
            print(json.dumps({k: v for k, v in result.items() if k != "oof_stack"}, indent=2, default=str))
    else:
        stats = required_embed.score(con, all_rows=a.all)
        print(stats)


def cmd_evidence(con, a):
    from backend.finder import embed, evidence
    manifest = _manifest(a)
    ok = evidence.check(manifest)
    if a.rebuild and ok:
        evidence.rebuild(con, manifest, embed.load_encoder(model_name=manifest.embed_model))
    if not ok:
        sys.exit(1)


def cmd_coverage(con, a):
    from backend.finder import coverage, embed
    manifest = _manifest(a)
    encoder = embed.load_encoder(model_name=manifest.embed_model)
    if a.calibrate:
        coverage.calibrate(con, manifest, encoder, n_pseudo=a.pseudo, hard_top=a.hard_top)
        return
    coverage.cover(con, manifest, encoder, posting_ids=a.posting or None, all_rows=a.all, limit=a.limit)


def cmd_judge(con, a):
    from backend.finder import judge
    if a.action == "export":
        pool = judge.pools(con, n_reject_content=a.reject_content, n_reject_logistics=a.reject_logistics,
                           n_reject_random=a.reject_random, exclude=judge.exported_ids(a.exclude_dir),
                           only=a.pools.split(",") if a.pools else None, platform=a.platform,
                           relabel=a.relabel)
        queue = judge.interleave(pool, limit=a.limit)
        print({k: len(v) for k, v in pool.items()}, "-> queued", len(queue))
        judge.write_batches(con, a.dir, queue, batch_size=a.batch_size)
    elif a.action == "import":
        judge.load_results(con, a.dir, scorer=a.scorer)
    elif a.action == "status":
        judge.status(a.dir)
    elif a.action == "exclude":
        if a.posting:
            for pid in a.posting:
                judge.exclude(con, pid, a.reason or "retired by the user")
        judge.exclude_thin(con)
    else:
        judge.agreement(con)
        if a.csv:
            judge.to_csv(con, a.csv)


def cmd_feedback(con, a):
    """Load the golden-source CSV, check the level rule against confirmed human rows, and/or re-export
    (any combination of --load/--agreement/--export, always in that order)."""
    from backend.finder import feedback
    if a.load:
        feedback.load_csv(con, a.load)
    if a.agreement:
        feedback.agreement(con)
    if a.export:
        print(feedback.export(con, a.export))


def cmd_setup_check(con, a):
    from backend.finder import setup_check
    sys.exit(0 if setup_check.run(con, manifest=a.manifest) else 1)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="override the DuckDB path")
    common.add_argument("--vault", default=os.getenv("JOBSEARCH_VAULT_DIR"), help="vault / Job_Search directory")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", parents=[common], help="screen rows that need it")
    s.add_argument("--full", action="store_true", help="re-screen every active row")
    s.add_argument("--since-hours", type=float, help="only rows first seen / given a JD in the last N hours")
    s.add_argument("--limit", type=int)
    s.add_argument("--no-model", action="store_true", help="rules only: skip the trained fit model")
    s.add_argument("--no-embed", action="store_true", help="skip embeddings (Phase 3; no-op until built)")
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("rescreen-all", parents=[common], help="screen --full, then the verdict-count diff")
    s.add_argument("--no-model", action="store_true", help="rules only: skip the trained fit model")
    s.set_defaults(func=cmd_rescreen_all)

    s = sub.add_parser("facets", parents=[common], help="show or discover each board's filter scope")
    s.add_argument("--discover", action="store_true", help="probe the boards live and rewrite board_scope")
    s.add_argument("--employer", help="limit discovery to boards whose name contains this")
    s.set_defaults(func=cmd_facets)

    s = sub.add_parser("report", parents=[common], help="write a Jobs_Found file")
    s.add_argument("--max-blocks", type=int, default=15)
    s.add_argument("--block-min-band", default="strong", help="lowest band that gets a JD block (default strong)")
    s.add_argument("--table-min", type=int, default=50)
    s.add_argument("--since-hours", type=float, default=24, help="window for the header and Passed rows (default 24)")
    s.add_argument("--out", help="output file or directory (default: the vault's Search_Results)")
    s.add_argument("--no-snapshots", action="store_true")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("lenses", parents=[common], help="the three lens lists (process / technical / both)")
    s.add_argument("--cap", type=int, default=report.LENS_LIST_CAP, help="rows per list (default 60)")
    s.add_argument("--out", help="output file or directory (default: the vault's Search_Results)")
    s.set_defaults(func=cmd_lenses)

    s = sub.add_parser("top", parents=[common],
                       help="END-of-pipeline apply/review list; run after `judge import`")
    s.add_argument("--apply-cap", type=int, default=report.TOP_APPLY_CAP)
    s.add_argument("--review-cap", type=int, default=report.TOP_REVIEW_CAP)
    s.add_argument("--include-decided", action="store_true", help="also show already-decided / in-tracker rows")
    s.add_argument("--out", help="output file or directory (default: the vault's Search_Results)")
    s.add_argument("--stdout", action="store_true", help="print the markdown instead of writing a file")
    s.set_defaults(func=cmd_top)

    s = sub.add_parser("mark", parents=[common], help="record a build / pass / hold decision")
    s.add_argument("target", help='posting_id, posting URL, or "employer|title"')
    s.add_argument("decision", choices=report.DECISIONS)
    s.add_argument("--reason", help="for pass, start with a code: " + " | ".join(PASS_REASON_CODES) +
                   ' (e.g. "function: clinical ops"); only function passes become calibration negatives')
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("sync", parents=[common], help="mirror Application_Tracker.md into the tracker table")
    s.add_argument("--verbose", action="store_true", help="print unmatched tracker rows")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("shortlist", parents=[common], help="top scored postings first seen in the last N days")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--n", type=int, default=30)
    s.set_defaults(func=cmd_shortlist)

    s = sub.add_parser("labels", parents=[common], help="rebuild label_docs from the vault")
    s.add_argument("--report", action="store_true", help="per-source table and the Phase 2 thresholds")
    s.add_argument("--pseudo", type=int, default=1500, help="pseudo-negatives to sample (default 1500)")
    s.add_argument("--seed", type=int, default=7)
    s.add_argument("--reanchor", action="store_true",
                   help="recover llm_labels orphaned before the description_hash normalization fix, using the "
                        "judge batch files under db/batches* as evidence the JD did not materially change")
    s.add_argument("--dry-run", action="store_true", help="with --reanchor: print counts only, write nothing")
    s.add_argument("--batch-dir", action="append", help="with --reanchor: a batch dir to search (repeatable; "
                                                        "default every db/batches* directory)")
    s.set_defaults(func=cmd_labels)

    s = sub.add_parser("train", parents=[common], help="train the TF-IDF + logistic regression fit model")
    s.add_argument("--cv", type=int, default=5)
    s.add_argument("--C", type=float, default=4.0)
    s.add_argument("--report", action="store_true", help="single-signal and blend AUCs")
    s.add_argument("--lens", choices=["process", "technical", "ai", "required"],
                   help="train one lens's model instead of the overall one; 'required' is NOT a lens -- it is "
                        "the Required-block ranking model (features.REQUIRED_MODEL), a ranking signal only")
    s.set_defaults(func=cmd_train)

    s = sub.add_parser("evidence", parents=[common], help="check / embed the evidence manifest")
    s.add_argument("--manifest", help="manifest path (default evidence.local.toml or $JOBSEARCH_EVIDENCE)")
    s.add_argument("--check", action="store_true", help="list sources, unit counts and samples (the default)")
    s.add_argument("--rebuild", action="store_true", help="embed new units and drop units no longer produced")
    s.set_defaults(func=cmd_evidence)

    s = sub.add_parser("coverage", parents=[common], help="requirement coverage for screen survivors")
    s.add_argument("--manifest", help="manifest path (default evidence.local.toml or $JOBSEARCH_EVIDENCE)")
    s.add_argument("--all", action="store_true", help="re-cover every survivor, not only new / changed ones")
    s.add_argument("--limit", type=int)
    s.add_argument("--posting", action="append", help="cover this posting id (repeatable)")
    s.add_argument("--calibrate", action="store_true", help="choose thresholds against hard negatives (§16.1)")
    s.add_argument("--pseudo", type=int, default=300, help="pseudo-negatives for the sanity AUC (default 300)")
    s.add_argument("--hard-top", type=int, default=200, help="highest-fit unlabeled postings used as hard negatives")
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser("required-embed", parents=[common],
                       help="the second-layer stacked model (backend/finder/required_embed.py): NOT a lens")
    s.add_argument("action", choices=["train", "score"])
    s.add_argument("--report", action="store_true", help="train: print the full metrics dict")
    s.add_argument("--all", action="store_true", help="score: re-score every population row, not only new ones")
    s.set_defaults(func=cmd_required_embed)

    s = sub.add_parser("judge", parents=[common], help="LLM labeling run: export batches, import graded labels")
    s.add_argument("action", choices=["export", "import", "status", "report", "exclude"])
    s.add_argument("--csv", help="report: write every judged posting to this CSV for eyeballing")
    s.add_argument("--dir", default="db/batches", help="batch directory (gitignored)")
    s.add_argument("--limit", type=int, help="stop the queue after N postings")
    s.add_argument("--batch-size", type=int, default=judge_batch_default())
    s.add_argument("--scorer", default="claude-sonnet-batch")
    s.add_argument("--reject-content", type=int, default=100, help="rejected on content: measures false negatives")
    s.add_argument("--reject-logistics", type=int, default=100, help="rejected on location/pay but fit >= 0.5")
    s.add_argument("--relabel", type=int, nargs="?", const=0, metavar="N",
                   help="re-grade postings that already carry a label (the rubric changed, not the JD): "
                        "bare = all of them, N = about N drawn evenly across the four grades")
    s.add_argument("--reject-random", type=int, default=50)
    s.add_argument("--reason", help="exclude: why this posting must never be trained on")
    s.add_argument("--posting", action="append", help="exclude: posting id to retire from training (repeatable)")
    s.add_argument("--exclude-dir", action="append", help="skip postings already queued in this batch dir (repeatable)")
    s.add_argument("--pools", help="comma-separated subset of high,low,reject")
    s.add_argument("--platform", help="export every active posting from one ATS platform (after an ingest fix)")
    s.set_defaults(func=cmd_judge)

    s = sub.add_parser("feedback", parents=[common],
                       help="load the report_feedback golden-source CSV, check level agreement, re-export")
    s.add_argument("--load", help="CSV path to upsert into report_feedback")
    s.add_argument("--agreement", action="store_true",
                   help="print the rule's level_fit vs confirmed human rows")
    s.add_argument("--export", help="re-export every report_feedback row with the rule's answer and needs_you")
    s.set_defaults(func=cmd_feedback)

    s = sub.add_parser("setup-check", parents=[common], help="personal files, dependencies, manifest, DB")
    s.add_argument("--manifest", help="manifest path (default evidence.local.toml or $JOBSEARCH_EVIDENCE)")
    s.set_defaults(func=cmd_setup_check)

    a = ap.parse_args()
    con = store.connect(a.db)
    try:
        a.func(con, a)
    finally:
        con.close()


if __name__ == "__main__":
    main()
