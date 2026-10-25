#!/usr/bin/env python3
"""Job finder: screen the ATS corpus against the profile rules, write the Jobs_Found report,
and record decisions so nothing is shown twice.

Usage:
    .venv/bin/python finder.py screen                     # new / changed / version-changed rows
    .venv/bin/python finder.py rescreen-all               # every active row, prints the verdict diff
    .venv/bin/python finder.py facets [--discover]        # each Workday board's filter scope (country / partition)
    .venv/bin/python finder.py report --out /tmp/x.md     # Jobs_Found file (+ snapshots)
    .venv/bin/python finder.py lenses --out /tmp/l.md     # strong on both / process / technical, plus applied AI
    .venv/bin/python finder.py top --out /tmp/t.md        # END-of-pipeline list: run after judge import
    .venv/bin/python finder.py sync --verbose             # rebuild the tracker table from $TRACKER_SOURCE
    .venv/bin/python finder.py mark <posting_id|url|"employer|title"> pass --reason "travel"
    .venv/bin/python finder.py mark <target> pass --reason "requirement: active TS/SCI required" \
        --unmet "Active TS/SCI clearance required" --basis blind
    .venv/bin/python finder.py mark <target> build --grade bullseye --basis blind
    .venv/bin/python finder.py mark --from-file decisions.csv   # one row per reviewed posting
    .venv/bin/python finder.py shortlist --days 7 --n 30
    .venv/bin/python finder.py labels --report                # rebuild label_docs from the vault
    .venv/bin/python finder.py labels --reanchor [--dry-run]  # recover judge labels a re-fetched JD expired
                                                                # (judge batch files as evidence)
    .venv/bin/python finder.py train --report                 # TF-IDF + LR on the labels
    .venv/bin/python finder.py setup-check                    # personal files, optional dependencies, DB
    .venv/bin/python finder.py evidence --check               # evidence manifest: sources, units, samples
    .venv/bin/python finder.py evidence --rebuild             # embed new evidence units
    .venv/bin/python finder.py coverage [--all] [--limit N]   # requirement coverage for survivors
    .venv/bin/python finder.py coverage --calibrate           # thresholds vs hard negatives (§16.1)
    .venv/bin/python finder.py feedback --load CSV --agreement --export OUT  # golden-source load/check/re-export
    .venv/bin/python finder.py feedback sheet --n 20 [--seed S] [--out PATH]  # monthly blind sheet w/ decoys (§26)
    .venv/bin/python finder.py feedback sheet-import PATH                    # import it; basis='blind'
    .venv/bin/python finder.py feedback ingest --manifest gold_manifest.csv  # ingest every gold sheet at once
    .venv/bin/python finder.py feedback ingest PATH --basis seen [--dry-run] # one narrow/blind sheet
    .venv/bin/python finder.py train --lens ai                # train one lens's model (process|technical|ai)
    .venv/bin/python finder.py train --lens required          # the Required-block ranking model (NOT a lens)
    .venv/bin/python finder.py bullseye-backfill               # once, after train --lens bullseye passes its gate
    .venv/bin/python finder.py required-embed train|score [--all]  # second-layer Required model (NOT a lens)
    .venv/bin/python finder.py judge export [--limit N] [--dir D]  # write grading batch files (no API call)
    .venv/bin/python finder.py judge import [--dir D]          # load graded *.result.json into llm_labels
    .venv/bin/python finder.py retrain [--dry-run]             # weekly retrain, gated + ledgered (sprint plan §24)
    .venv/bin/python finder.py retrain --history               # print the model_runs ledger, newest first
    .venv/bin/python finder.py judge2 run --dry-run --show 3    # print payloads + prompt, call nothing (§25/§29)
    .venv/bin/python finder.py judge2 run --eval-set --i-have-approval   # judge the blind human-graded rows
    .venv/bin/python finder.py judge2 run --eval-set --rerun --run-tag round6 --i-have-approval  # noise floor (§29.4)
    .venv/bin/python finder.py judge2 eval                      # the §25 acceptance bar + §29.4 line-level report
    .venv/bin/python finder.py judge2 eval --compare <pv_a> <pv_b>       # §29.4: postings whose call differs
    .venv/bin/python finder.py judge2 rederive                  # §29.2/§29.3: re-derive required_fit from
                                                                   # stored judge2_lines, no API call
    .venv/bin/python finder.py judge2 status                    # counts, discard/downgrade rates, last eval
    .venv/bin/python finder.py jev run --eval-set --dry-run --show 1   # Jev tier: print what leaves the machine
    .venv/bin/python finder.py jev run --eval-set --i-have-approval    # Jev reviews the blind gold rows
    .venv/bin/python finder.py jev run --eval-set --run-tag r2 --i-have-approval   # a repeatability rerun
    .venv/bin/python finder.py jev run --gated --limit 50 --i-have-approval   # any lens model fit >= 0.70
    .venv/bin/python finder.py jev run --injection --i-have-approval   # the synthetic adversarial set (tag inj)
    .venv/bin/python finder.py jev sentinel --init                     # pin the sentinel set once
    .venv/bin/python finder.py jev run --sentinel --run-tag wk40 --i-have-approval   # rerun the pinned set
    .venv/bin/python finder.py jev eval [--sentinel-tag wk40]          # the MSA study (docs/JEV_PLAN.md §4)
    .venv/bin/python finder.py jev status                              # counts, drift, canary, tokens, bar
    .venv/bin/python finder.py jev rederive                            # re-derive required_fit, no API call
    .venv/bin/python finder.py bridge [--max-ring N] [--new-only]  # bridge (place-scoped retail) track's open list (§31.6)

Every subcommand takes --db (default $JOBSEARCH_DB, else db/jobsearch.duckdb) and --vault (default
$JOBSEARCH_VAULT_DIR; retrain ignores --vault and reads the variable). Every subcommand opens the DuckDB
read-write first: while a sweep holds the lock it stops with a message and exits 1. `<command> --help`
gives each command's own summary and switches.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
import duckdb  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(REPO, ".env"))
from backend.ats import store  # noqa: E402
from backend.finder import features, labels, pipeline, report, retrain as retrain_mod, rubric, tracker_sync, version  # noqa: E402

# 'requirement' and 'level' (sprint plan §22.3, Gap 2): a hard Required line not met, or a seat too senior /
# too junior. 'clearance' is accepted at the CLI as an alias for 'requirement' (see store.vw_decisions) --
# only 'requirement' and 'level' are real codes; 'clearance' is never stored as the code itself.
PASS_REASON_CODES = ("function", "nuance", "logistics", "comp", "other", "requirement", "level")
PASS_REASON_ALIASES = {"clearance": "requirement"}
MARK_BASIS_VALUES = ("blind", "seen")
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
    bullseye_model = None if a.no_model else features.load_bullseye_model(con)
    pipeline.screen(con, since=since, full=a.full, limit=a.limit, model=model, lens_models=lens_models,
                    required_model=required_model, bullseye_model=bullseye_model)


def cmd_rescreen_all(con, a):
    before = _latest_counts(con)
    pipeline.screen(con, full=True, model=None if a.no_model else features.load_latest(con),
                    lens_models={} if a.no_model else features.load_lens_models(con),
                    required_model=None if a.no_model else features.load_required_model(con),
                    bullseye_model=None if a.no_model else features.load_bullseye_model(con))
    after = _latest_counts(con)
    print(f"{'verdict':<10} {'before':>8} {'after':>8} {'diff':>8}")
    for v in sorted(set(before) | set(after)):
        print(f"{v:<10} {before.get(v, 0):>8} {after.get(v, 0):>8} {after.get(v, 0) - before.get(v, 0):>+8}")


def cmd_bullseye_backfill(con, a):
    row = features.latest_row(con, lens=features.BULLSEYE_MODEL)
    if row and json.loads(row[2] or "{}").get("auc_employer_grouped_passed") is False:
        sys.exit("bullseye-backfill: the latest `bullseye` model FAILED its employer-grouped acceptance gate "
                 "(see `finder.py train --lens bullseye` output / models.notes) -- retrain until it passes "
                 "before backfilling.")
    bullseye_model = features.load_bullseye_model(con)
    stats = pipeline.bullseye_backfill(con, bullseye_model)
    print(stats)


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


def cmd_bridge(con, a):
    """Prints the bridge (place-scoped retail) track's open list -- sprint plan §31.6. Advisory
    only: nothing is hidden, `--hide-voice-high` is a convenience filter the user must ask for.
    Sort: ring (ascending), then voice (low first, blank second, high last -- voice-heavy roles
    are a poor fit, per the user's ruling), then employer preference
    (registry/bridge_employer_order.csv, absent = no preference), then newest first.

    `--max-ring` (2026-09-21 user ruling, replaces the earlier boolean `--ring2`): rings are any
    positive integer now, not just 1|2 -- a row at ring > max_ring is left out of the list
    entirely, same as it was never pulled.

    `evergreen` places (2026-09-21 user ruling): a standing application pool (every site lists the
    same titles under one shared posted date) makes `days_open` and a NEW mark actively
    misleading. A posting matched ONLY by evergreen place(s) prints `pool` in place of days-open
    and is never marked NEW. This is pure display logic driven by the registry's `evergreen`
    column -- it is never inferred from the data itself.

    `--include-stale` (orchestrator audit 2026-09-21, letter E): by default, a posting whose
    `last_seen_at` has not moved in `backend.ats.bridge.BRIDGE_STALE_DAYS` days is hidden (see
    `vw_bridge_open` / `vw_bridge_open_all`) and a one-line summary reports how many were hidden.
    `--include-stale` shows them anyway, with no summary line (nothing was hidden).

    NEW-mark cutoff (orchestrator audit 2026-09-21, letter G; corrected in the re-audit): NEW means
    "first seen in the LATEST bridge sweep", so the cutoff is the START of the latest sweep -- one
    value per `board_runs.run_id` (the min `ran_at` across its board rows), not the per-board
    `ran_at` timestamps a sweep's boards finish at slightly different times. The start of the
    PREVIOUS sweep is the wrong cutoff: every posting first seen by that sweep's second and later
    boards sits after it, and would still print NEW one sweep later. On the very first bridge
    sweep ever run there is no earlier sweep to be new against, so nothing is marked NEW --
    documented here rather than left to look like a bug."""
    from backend.ats import bridge as B
    from backend.ats.registry import load_bridge_employer_order, load_bridge_places

    all_places = load_bridge_places(max_ring=None)  # every configured ring, for the ring/evergreen lookup
    place_ring = {p["place"]: p["ring"] for p in all_places}
    place_evergreen = {p["place"]: p["evergreen"] for p in all_places}
    employer_order = load_bridge_employer_order()

    run_starts = con.execute(
        "SELECT run_id, min(ran_at) AS started FROM board_runs WHERE track = 'bridge' "
        "GROUP BY run_id ORDER BY started DESC LIMIT 2").fetchall()
    new_cutoff = run_starts[0][1] if len(run_starts) > 1 else None

    cols = ("posting_id", "employer", "title", "location_primary", "bridge_place", "employment_type",
            "pay_min", "pay_max", "pay_interval", "url", "first_seen_at", "days_open")
    view = "vw_bridge_open_all" if a.include_stale else "vw_bridge_open"
    rows = con.execute(f"SELECT {', '.join(cols)} FROM {view}").fetchall()
    if not a.include_stale:
        hidden = con.execute("SELECT count(*) FROM vw_bridge_open_all").fetchone()[0] - len(rows)
        if hidden:
            print(f"bridge: {hidden} stale posting(s) hidden (no confirmed sighting in "
                  f"{B.BRIDGE_STALE_DAYS}+ days) -- rerun with --include-stale to see them")

    out = []
    for r in rows:
        d = dict(zip(cols, r))
        places = [p for p in (d["bridge_place"] or "").split("|") if p]
        ring = min((place_ring.get(p, 1) for p in places), default=1)
        if ring > a.max_ring:
            continue
        # A posting matched by SEVERAL places is a pool only when EVERY place it matched is
        # evergreen -- if any one of them is a real, dated place, days-open/NEW still mean
        # something for it.
        is_pool = bool(places) and all(place_evergreen.get(p, False) for p in places)
        voice = B.voice_flag(d["title"])
        if voice == "high" and a.hide_voice_high:
            continue
        is_new = (not is_pool) and new_cutoff is not None and d["first_seen_at"] >= new_cutoff
        if a.new_only and not is_new:
            continue
        emp_rank = employer_order.get((d["employer"] or "").lower(), len(employer_order) + 1)
        out.append((ring, B.VOICE_SORT_ORDER.get(voice, 1), emp_rank, d, voice, is_new, is_pool))

    out.sort(key=lambda t: (t[0], t[1], t[2], -(t[3]["first_seen_at"].timestamp())))

    if not out:
        print("bridge: nothing open" + (" (new-only)" if a.new_only else ""))
        return
    print(f"{'NEW':<4}{'ring':<5}{'voice':<6}{'employer':<24}{'title':<38}{'place':<22}{'pay':<16}{'days':<6}{'link'}")
    for ring, _vs, _rank, d, voice, is_new, is_pool in out:
        pay = ""
        if d["pay_min"] or d["pay_max"]:
            unit = f"/{d['pay_interval']}" if d["pay_interval"] else ""
            pay = f"{d['pay_min'] or '?'}-{d['pay_max'] or '?'}{unit}"
        time_type = {"full_time": "Full", "part_time": "Part"}.get(d["employment_type"], "")
        days = "pool" if is_pool else str(d["days_open"])
        print(f"{'NEW' if is_new else '':<4}{ring:<5}{voice:<6}{(d['employer'] or '')[:23]:<24}"
              f"{(d['title'] or '')[:37]:<38}{(d['location_primary'] or '')[:21]:<22}{pay:<16}"
              f"{days:<6}{d['url'] or ''}"
              f"{'  [' + time_type + ']' if time_type else ''}")


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
    """END-of-pipeline apply/review list, read off vw_selection -- run by hand after a judge import.
    `--jev` first sends the Apply / Review subset to Jev (stage 2, backend/finder/jev_gate.py; live only with
    --i-have-approval or JEV_LIVE_OK=1); the page is written either way, with the demotion pass applied from
    the stored reviews unless --no-jev-gate."""
    if not a.out and not a.vault and not a.stdout:
        sys.exit("top: set JOBSEARCH_VAULT_DIR, or pass --out or --stdout")
    if a.jev:
        from backend.finder import jev_cli, jev_gate
        try:
            facts = jev_cli.load_facts(a.background_file)
            jev_gate.run_over_top(con, facts=facts, live_ok=jev_cli.live_ok(a.i_have_approval),
                                  include_decided=a.include_decided, apply_cap=a.apply_cap, review_cap=a.review_cap)
            # Gemma second-reads ONLY what Jev demoted (user's call 2026-09-30); live gate = JUDGE2_LIVE_OK=1,
            # which launch.sh's judge_env exports for every jevtop preset.
            jev_gate.run_gemma_on_demoted(con, background_path=jev_cli.background_path(a.background_file),
                                          live_ok=bool(os.environ.get("JUDGE2_LIVE_OK")) or a.i_have_approval,
                                          include_decided=a.include_decided, apply_cap=a.apply_cap,
                                          review_cap=a.review_cap)
        except jev_cli.JevCliError as exc:
            print(f"top --jev: {exc}; writing the report from the stored reviews")
    if a.stdout:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = report.write_top_jobs(con, a.vault, out_path=tmp, apply_cap=a.apply_cap,
                                         review_cap=a.review_cap, include_decided=a.include_decided,
                                         jev_gate_on=not a.no_jev_gate)
            print(path.read_text(encoding="utf-8"))
        return
    path = report.write_top_jobs(con, a.vault, out_path=a.out, apply_cap=a.apply_cap, review_cap=a.review_cap,
                                 include_decided=a.include_decided, jev_gate_on=not a.no_jev_gate)
    print(path)


LOCK_NOTE = ("DuckDB lock: every finder.py command opens the pipeline DuckDB read-write first, even one that "
             "only reads. While a sweep or launch.sh run holds it, the command stops with a message, writes "
             "nothing and exits 1; run it again when that finishes.")

MARK_GUIDE = """\
targets:
  a 20-hex posting_id; a posting URL (falls back to a req id found inside the URL); or "employer|title"
  (fuzzy on both, active postings preferred). Several matches print the candidates and write nothing.

what each decision feeds:
  build   decided (leaves the lists); a positive training label for the fit models; records a human
          required_fit of 'meets' unless --unmet names a line that is not met
  pass    decided; a negative training label, except logistics and comp passes, which say nothing
          about fit and are left out of training
  hold    not decided: the posting keeps surfacing on the lists, marked held; not a training label
  The latest mark on a posting wins, so a second mark replaces the first.

pass reason codes (the word before the colon):
  function      wrong kind of work; also a calibration hard negative for requirement coverage
  requirement   a hard Required line is not met (alias: clearance); add --unmet "<line from the JD>";
                records a human required_fit of 'fails', which outranks the judge's Required call
  level         wrong seniority
  logistics     location, travel, schedule, commute (not a training label)
  comp          pay (not a training label)
  nuance        a judgment call the other codes do not cover
  other         anything else

needs the JD text:
  a requirement pass, a build, or --grade records a human call against the posting's description;
  a posting with no JD text yet is refused (a plain pass or hold still works).

examples:
  finder.py mark 0123456789abcdef0123 build --grade bullseye --basis blind
  finder.py mark "Acme|Senior Analyst" pass --reason "logistics: five days on site"
  finder.py mark <url> pass --reason "requirement: active TS/SCI" --unmet "Active TS/SCI clearance required"
  finder.py mark --from-file decisions.csv
                columns posting,decision,reason,unmet,grade,basis (several unmet lines joined with ' || ');
                every row is checked first, and one bad row writes nothing

DuckDB lock:
  mark writes to the pipeline DuckDB. While a sweep or launch.sh run holds it, mark stops with a
  message and writes nothing; run it again when the run finishes.
"""


def _mark_reason_code(reason: str):
    """The leading word of a pass --reason, normalized; 'clearance' reads as 'requirement'
    (PASS_REASON_ALIASES, store.vw_decisions does the same mapping for rows written directly to the
    `decisions` table). None if the reason doesn't start with a recognized word."""
    m = re.match(r"\s*(\w+)", reason or "")
    if not m:
        return None
    word = PASS_REASON_ALIASES.get(m.group(1).lower(), m.group(1).lower())
    return word if word in PASS_REASON_CODES else None


def _validate_unmet_usage(decision: str, reason_code, unmet: list):
    if unmet and not (decision == "build" or (decision == "pass" and reason_code == "requirement")):
        raise ValueError('--unmet only makes sense with "build" or a pass --reason starting "requirement" '
                         '(or "clearance")')


def _mark_required_fit(decision: str, reason_code, unmet: list):
    """The (required_fit, required_unmet) a mark implies, or (None, None) when it makes no Required-block
    claim (sprint plan §22.3, Gap 2). A `build` defaults to human required_fit = 'meets' "unless told
    otherwise" -- --unmet on a build IS being told otherwise. A `pass` only ever claims 'fails', and only
    when its reason is 'requirement' (which 'clearance' aliases to) -- never for logistics/comp/level/
    function/nuance/other, so those can never become a required_fit negative for any model (Gap 2, rule 4)."""
    lines = " ; ".join(unmet) if unmet else None
    if decision == "build":
        return ("fails", lines) if unmet else ("meets", None)
    if decision == "pass" and reason_code == "requirement":
        return "fails", (lines if lines is not None else "")
    return None, None


def _prepare_mark(con, pid: str, decision: str, reason, unmet: list, grade, basis) -> dict:
    """Validates one mark (single CLI call or one --from-file row) and works out everything it implies,
    without writing anything. Raises ValueError with a message naming what's wrong."""
    unmet = list(unmet or [])
    grade = grade or None
    basis = basis or "seen"
    if basis not in MARK_BASIS_VALUES:
        raise ValueError(f"basis {basis!r} must be one of {', '.join(MARK_BASIS_VALUES)}")
    if grade and grade not in rubric.GRADES:
        raise ValueError(f"grade {grade!r} must be one of {', '.join(rubric.GRADES)}")
    reason_code = None
    if decision == "pass":
        reason_code = _mark_reason_code(reason or "")
        if reason_code is None:
            raise ValueError(f"--reason must start with one of {', '.join(PASS_REASON_CODES)} or "
                             f"{', '.join(PASS_REASON_ALIASES)} "
                             f'(e.g. "function: clinical operations, not process work"); got {reason!r}')
    _validate_unmet_usage(decision, reason_code, unmet)
    required_fit, required_unmet = _mark_required_fit(decision, reason_code, unmet)
    description_hash = None
    if required_fit is not None or grade is not None:
        row = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [pid]).fetchone()
        description_hash = row[0] if row else None
        if not description_hash:
            raise ValueError(f"{pid} has no description_hash yet (no JD text) -- cannot record a "
                             "required-fit call or a human grade against it")
    return {"reason": reason or None, "reason_code": reason_code, "unmet": unmet, "grade": grade,
           "basis": basis, "required_fit": required_fit, "required_unmet": required_unmet,
           "description_hash": description_hash}


def _apply_mark(con, pid: str, decision: str, prep: dict, now) -> None:
    con.execute("INSERT INTO decisions VALUES (?, ?, ?, 'cli', NULL, ?)", [pid, decision, prep["reason"], now])
    if prep["required_fit"] is not None or prep["grade"] is not None:
        from backend.finder import feedback
        feedback.record_mark(con, pid, prep["description_hash"], decision, reason_code=prep["reason_code"],
                             reason_detail=prep["reason"], human_grade=prep["grade"],
                             required_fit=prep["required_fit"], required_unmet=prep["required_unmet"],
                             basis=prep["basis"], now=now)


def cmd_mark(con, a):
    if getattr(a, "from_file", None):
        return cmd_mark_from_file(con, a)
    if not a.target or not a.decision:
        sys.exit("mark: target and decision are required unless --from-file is given")
    hits = resolve_posting(con, a.target)
    if len(hits) != 1:
        print(f"mark: {len(hits)} postings match {a.target!r}; need exactly one.")
        for pid, employer, title, status in hits[:20]:
            print(f"  {pid}  {status:<6}  {employer} | {title}")
        sys.exit(1)
    pid, employer, title, _ = hits[0]
    try:
        prep = _prepare_mark(con, pid, a.decision, a.reason, getattr(a, "unmet", None),
                             getattr(a, "grade", None), getattr(a, "basis", None))
    except ValueError as e:
        sys.exit(f"mark: {e}")
    _apply_mark(con, pid, a.decision, prep, _utcnow())
    print(f"{a.decision}: {pid}  {employer} | {title}")


def _parse_mark_csv_row(con, raw: dict) -> dict:
    """One --from-file row -> a fully validated, ready-to-apply dict, or raises ValueError."""
    ref = (raw.get("posting") or "").strip()
    if not ref:
        raise ValueError("empty posting reference")
    hits = resolve_posting(con, ref)
    if len(hits) != 1:
        raise ValueError(f"posting {ref!r} matched {len(hits)} postings, need exactly one")
    pid, employer, title, _ = hits[0]
    decision = (raw.get("decision") or "").strip()
    if decision not in report.DECISIONS:
        raise ValueError(f"decision {decision!r} must be one of {', '.join(report.DECISIONS)}")
    reason = (raw.get("reason") or "").strip() or None
    unmet = [u.strip() for u in (raw.get("unmet") or "").split("||") if u.strip()]
    grade = (raw.get("grade") or "").strip() or None
    basis = (raw.get("basis") or "").strip() or None
    prep = _prepare_mark(con, pid, decision, reason, unmet, grade, basis)
    return {"pid": pid, "employer": employer, "title": title, "decision": decision, **prep}


def cmd_mark_from_file(con, a):
    """`finder.py mark --from-file decisions.csv` (sprint plan §22.3, Gap 2): one row per reviewed posting,
    columns posting/decision/reason/unmet/grade/basis (`unmet` multi-valued, ' || '-joined). Validates every
    row FIRST and applies nothing until the whole file is clean; a single bad row aborts the entire file with
    a message naming it, and everything is applied in one transaction."""
    with open(a.from_file, newline="", encoding="utf-8-sig") as f:
        raw_rows = list(csv.DictReader(f))
    if not raw_rows:
        sys.exit(f"mark --from-file: {a.from_file} has no data rows")
    parsed = []
    for i, raw in enumerate(raw_rows, start=2):   # row 1 is the header
        try:
            parsed.append(_parse_mark_csv_row(con, raw))
        except ValueError as e:
            sys.exit(f"mark --from-file: row {i}: {e} -- nothing written")
    now = _utcnow()
    con.execute("BEGIN")
    for row in parsed:
        _apply_mark(con, row["pid"], row["decision"], row, now)
    con.execute("COMMIT")
    by_decision = Counter(row["decision"] for row in parsed)
    print(f"mark --from-file: applied {len(parsed)} decisions from {len(raw_rows)} rows "
         f"({', '.join(f'{k}={v}' for k, v in sorted(by_decision.items()))})")


def cmd_sync(con, a):
    source = (os.environ.get(tracker_sync.TRACKER_SOURCE_ENV) or "markdown").strip().lower()
    if not a.vault and source != "postgres":   # only the markdown source reads the vault
        sys.exit("sync: set JOBSEARCH_VAULT_DIR or pass --vault (TRACKER_SOURCE=markdown reads the vault file)")
    try:
        print(tracker_sync.sync(con, a.vault or "", verbose=a.verbose))
    except tracker_sync.TrackerSourceError as e:   # read before any write, so nothing changed
        sys.exit(f"sync: {e}; nothing was written")


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


BULLSEYE_GATE_AUC = 0.78   # acceptance gate: employer-grouped OOF AUC must clear this or the model is not wired


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
    if a.lens == features.BULLSEYE_MODEL:
        _bullseye_gate(con, a, result)


def _bullseye_gate(con, a, result):
    """Employer-grouped OOF AUC (GroupKFold on normalized employer, via required_embed._employer_folds) plus
    a shuffled-label sanity check (expect ~0.5). Stored on the model's `models.notes` row so a later reader
    (bullseye-backfill, the STATUS doc) sees the gate without re-training. ACCEPTANCE GATE: >= BULLSEYE_GATE_AUC
    or the model is reported but not wired into the rank feed (no backfill run automatically)."""
    rows = result["rows"]
    texts = [features.doc_text(r["title"], r["text"], r["company"]) for r in rows]
    y = [int(r["label"]) for r in rows]
    weights = [float(r["weight"] or 1.0) for r in rows]
    n_pos, n_neg = sum(y), len(y) - sum(y)
    kwargs = dict(C=a.C, min_df=3, ngram=(1, 2), max_features=50_000, max_df=0.5, log=lambda *_a, **_k: None)
    emp_auc = features.employer_grouped_auc(con, rows, texts, y, weights, **kwargs)
    shuf_auc = features.employer_grouped_auc(con, rows, texts, y, weights, shuffle_labels=True, **kwargs)
    passed = emp_auc is not None and emp_auc >= BULLSEYE_GATE_AUC
    print(f"\nBULLSEYE ACCEPTANCE GATE: employer-grouped OOF AUC {emp_auc if emp_auc is None else round(emp_auc, 3)} "
          f"(need >= {BULLSEYE_GATE_AUC}) — {'PASS' if passed else 'FAIL'} · shuffled-label sanity "
          f"{shuf_auc if shuf_auc is None else round(shuf_auc, 3)} (expect ~0.5) · n_pos={n_pos} n_neg={n_neg}")
    notes = json.loads(con.execute("SELECT notes FROM models WHERE model_version = ?",
                                   [result["model_version"]]).fetchone()[0])
    notes["auc_employer_grouped"] = emp_auc
    notes["auc_employer_grouped_shuffled"] = shuf_auc
    notes["auc_employer_grouped_gate"] = BULLSEYE_GATE_AUC
    notes["auc_employer_grouped_passed"] = passed
    con.execute("UPDATE models SET notes = ? WHERE model_version = ?", [json.dumps(notes), result["model_version"]])
    if not passed:
        print("GATE FAILED: this model is recorded as failing, so screen skips it and keeps using the newest "
              "bullseye model that did not fail (or none). `bullseye-backfill` refuses until a passing model "
              "replaces it.")


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
        if a.expired:   # exclude only what was already queued at the posting's CURRENT text
            current = dict(con.execute("SELECT posting_id, coalesce(description_hash, '') FROM postings").fetchall())
            exclude = {pid for pid, h in judge.exported_pairs(a.exclude_dir) if current.get(pid) == h}
        else:
            exclude = judge.exported_ids(a.exclude_dir)
        pool = judge.pools(con, n_reject_content=a.reject_content, n_reject_logistics=a.reject_logistics,
                           n_reject_random=a.reject_random, exclude=exclude,
                           only=a.pools.split(",") if a.pools else None, platform=a.platform,
                           relabel=a.relabel, expired=a.expired)
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
    (any combination of --load/--agreement/--export, always in that order) -- or, with a nested
    `sheet` / `sheet-import` subcommand, the monthly blind sheet with decoys (sprint plan §26)."""
    action = getattr(a, "feedback_action", None)
    if action == "ingest":
        from backend.finder import gold_ingest
        targets = []
        if a.manifest:
            for path, basis, _notes in gold_ingest.load_manifest(a.manifest):
                targets.append((path, basis or a.basis))
        for p in a.paths or []:
            targets.append((p, a.basis))
        if not targets:
            print("feedback ingest: nothing to do -- give a PATH or --manifest")
            sys.exit(1)
        result = gold_ingest.ingest(con, targets, accept_proposed=a.accept_proposed, dry_run=a.dry_run)
        sys.exit(0 if result["ok"] else 1)
    if action == "sheet":
        from backend.finder import blind_sheet
        blind_sheet.generate_sheet(con, n=a.n, seed=a.seed, out_path=a.out, jev_n=a.jev)
        return
    if action == "sheet-import":
        from backend.finder import blind_sheet
        blind_sheet.import_sheet(con, a.path, history_path=a.history)
        return
    from backend.finder import feedback
    if a.load:
        feedback.load_csv(con, a.load)
    if a.agreement:
        feedback.agreement(con)
    if a.export:
        print(feedback.export(con, a.export))


def cmd_retrain(con, a):
    """The weekly retrain, as one named command (sprint plan §24): backend/finder/retrain.py owns the logic
    (candidate-vs-live artifacts, the promotion gate, the `model_runs` ledger); this is a thin CLI wrapper."""
    if a.history:
        retrain_mod.print_history(con)
        return
    retrain_mod.run(con, dry_run=a.dry_run)


def cmd_judge2(con, a):
    """The LLM second judge (sprint plan §25/§29, backend/finder/judge2.py). `run` refuses to make a live
    call without JUDGE2_LIVE_OK=1 or --i-have-approval; `--dry-run` never needs either."""
    from backend.finder import jev_cli, judge2
    if a.action == "status":
        judge2.status(con, background=a.background, background_path=a.background_file)
        return
    if a.action == "rederive":
        judge2.rederive(con)
        return
    if a.action == "eval":
        if a.compare:
            judge2.compare(con, a.compare[0], a.compare[1])
        else:
            judge2.evaluate(con, background=a.background, background_path=a.background_file,
                            prompt_version_override=a.prompt_version)
            judge2.line_level_report(con, background=a.background, background_path=a.background_file,
                                     prompt_version_override=a.prompt_version)
        return
    # action == "run"
    try:
        result = judge2.run(con, top_n=a.top, dry_run=a.dry_run, force=a.force, show=a.show,
                            background=a.background, background_path=a.background_file,
                            only_blind=a.eval_set, i_have_approval=a.i_have_approval,
                            rerun=a.rerun, run_tag=a.run_tag,
                            posting_ids=jev_cli.posting_ids_arg(a))
    except RuntimeError as exc:
        print(exc)
        sys.exit(1)
    print(result)


def cmd_jev(con, a):
    """The Jev typed-decision tier (docs/JEV_PLAN.md, backend/finder/jev.py). `run` refuses to make a live call
    without JEV_LIVE_OK=1 or --i-have-approval; `--dry-run` never needs either, nor a key. Reported only: no
    Jev output moves the rank until the user rules on stage 2."""
    from backend.finder import jev_cli
    db_path = a.db or os.environ.get("JOBSEARCH_DB") or None
    try:
        if a.action == "run":
            jev_cli.run_cmd(con, a, db_path)
        elif a.action == "eval":
            jev_cli.eval_cmd(con, a, db_path)
        elif a.action == "status":
            jev_cli.status(con, a)
        elif a.action == "rederive":
            jev_cli.rederive(con)
        elif a.action == "sentinel":
            if not a.init:
                raise jev_cli.JevCliError("jev sentinel: pass --init to pin the set (runs use `jev run --sentinel`)")
            jev_cli.sentinel_init(con, db_path, n=a.n, force=a.force)
    except jev_cli.JevCliError as exc:
        print(exc)
        sys.exit(1)


def cmd_setup_check(con, a):
    from backend.finder import setup_check
    sys.exit(0 if setup_check.run(con, manifest=a.manifest) else 1)


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="DuckDB path (default $JOBSEARCH_DB, else db/jobsearch.duckdb)")
    common.add_argument("--vault", default=os.getenv("JOBSEARCH_VAULT_DIR"), help="vault / Job_Search directory")
    # For a command with its OWN nested sub-subparsers (today: `feedback`'s sheet/sheet-import/ingest), the
    # outer `--db`/`--vault` (parents=[common]) is parsed against the `feedback` parser, but argparse then
    # parses the sub-subparser's OWN arguments into the SAME namespace -- so `--db` must also be accepted at
    # that inner level for `finder.py feedback ingest --db X ...` (given AFTER the sub-action) to work at all,
    # not just `finder.py feedback --db X ingest ...` (given before). The trap: a plain `parents=[common]` at
    # the inner level would carry `common`'s own defaults (None / $JOBSEARCH_VAULT_DIR), and argparse APPLIES
    # every parser's defaults into the namespace even when that parser's own arguments were not given on the
    # command line -- so the inner parser's default would silently overwrite a value the outer `--db` already
    # set. `default=argparse.SUPPRESS` makes the inner parser set nothing at all when its own `--db`/`--vault`
    # is absent, leaving whatever the outer parser (or its own default) already put in the namespace alone.
    common_inner = argparse.ArgumentParser(add_help=False)
    common_inner.add_argument("--db", default=argparse.SUPPRESS,
                              help="DuckDB path (default $JOBSEARCH_DB, else db/jobsearch.duckdb)")
    common_inner.add_argument("--vault", default=argparse.SUPPRESS, help="vault / Job_Search directory")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", parents=[common],
                       help="screen active fit-track rows that are new, have a re-fetched JD, or were screened "
                            "under another rules or overall-model version; writes screens rows (a retrained "
                            "lens model alone does not make rows due: use --full)")
    s.add_argument("--full", action="store_true", help="re-screen every active fit-track row (bridge rows are never screened)")
    s.add_argument("--since-hours", type=float,
                   help="only rows first seen or given a JD in the last N hours, plus any row never screened")
    s.add_argument("--limit", type=int, help="screen at most N candidate rows, newest first (default: all)")
    s.add_argument("--no-model", action="store_true",
                   help="rules only: skip every trained model; rows screened with a model become due, and the "
                        "next normal screen redoes them")
    s.add_argument("--no-embed", action="store_true", help="ignored: screen computes no embeddings (coverage and "
                                                           "required-embed do)")
    s.set_defaults(func=cmd_screen)

    s = sub.add_parser("rescreen-all", parents=[common],
                       help="re-screen every active fit-track row (writes screens rows), then print the verdict-count diff")
    s.add_argument("--no-model", action="store_true",
                   help="rules only: skip every trained model; the next normal screen then re-screens every row")
    s.set_defaults(func=cmd_rescreen_all)

    s = sub.add_parser("facets", parents=[common], help="show or discover each Workday board's filter scope")
    s.add_argument("--discover", action="store_true",
                   help="probe each Workday board live (network) and rewrite its board_facets and board_scope "
                        "rows; a board that fails is logged and keeps its old rows")
    s.add_argument("--employer", help="with --discover: probe only Workday boards whose employer contains this "
                                      "(case-insensitive); the listing ignores it")
    s.set_defaults(func=cmd_facets)

    s = sub.add_parser("bridge", parents=[common],
                       help="the bridge (place-scoped retail) track's open list -- sprint plan §31.6")
    s.add_argument("--max-ring", type=int, default=1,
                   help="highest bridge_places.csv ring to include (default 1 = ring-1 only)")
    s.add_argument("--new-only", action="store_true",
                   help="only rows first seen by the latest bridge sweep (pool rows, and every row after a "
                        "first-ever sweep, are never new)")
    s.add_argument("--hide-voice-high", action="store_true",
                   help="hide rows whose title is flagged voice-heavy (off by default; the flag is advisory)")
    s.add_argument("--include-stale", action="store_true",
                   help="also show postings not re-confirmed in 10+ days (BRIDGE_STALE_DAYS); hidden by default")
    s.set_defaults(func=cmd_bridge)

    s = sub.add_parser("report", parents=[common],
                       help="write a Jobs_Found file (never overwrites), record its blocks in surfaced, and export snapshots")
    s.add_argument("--max-blocks", type=int, default=15,
                   help="most JD blocks to write (default 15); block rows are recorded in the surfaced table")
    s.add_argument("--block-min-band", default="strong", choices=["very_strong", "strong", "partial", "weak", "none"],
                   help="lowest band that gets a JD block (default strong)")
    s.add_argument("--table-min", type=int, default=50,
                   help="Summary table: rows scoring at least N, plus every block row, up to 150 (default 50)")
    s.add_argument("--since-hours", type=float, default=24, help="window for the header and Passed rows (default 24)")
    s.add_argument("--out", help="output file or directory (default: the vault's Search_Results)")
    s.add_argument("--no-snapshots", action="store_true",
                   help="skip the Parquet / CSV exports to db/snapshots/ (written beside the default DB, even with --db)")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("lenses", parents=[common], help="the lens lists (both / process / technical, plus applied AI); writes Lens_Lists_<stamp>.md")
    s.add_argument("--cap", type=int, default=report.LENS_LIST_CAP, help="rows per list, the applied-AI list included (default 60)")
    s.add_argument("--out", help="output file or directory (default: the vault's Search_Results)")
    s.set_defaults(func=cmd_lenses)

    s = sub.add_parser("top", parents=[common],
                       help="END-of-pipeline apply/review list (writes Top_Jobs_<date>.md, never overwrites); "
                            "run after `judge import`")
    s.add_argument("--apply-cap", type=int, default=report.TOP_APPLY_CAP,
                   help="most Apply rows on the page (default 150); with --jev, also the Apply rows Jev can demote")
    s.add_argument("--review-cap", type=int, default=report.TOP_REVIEW_CAP,
                   help="most Review rows on the page (default 200); with --jev, also the Review rows Jev can demote")
    s.add_argument("--include-decided", action="store_true", help="also show already-decided / in-tracker rows")
    s.add_argument("--out", help="output file or directory (default: the vault's Search_Results)")
    s.add_argument("--stdout", action="store_true", help="print the markdown instead of writing a file")
    s.add_argument("--jev", action="store_true",
                   help="first have Jev review the Top Jobs rows and other lens-strong postings (at most 300 new "
                        "reviews), then Gemma second-reads the demotions; live only with --i-have-approval or "
                        "JEV_LIVE_OK=1; an error is logged and the page uses stored reviews")
    s.add_argument("--i-have-approval", action="store_true",
                   help="--jev: authorize the live Jev and Gemma calls (or JEV_LIVE_OK=1 and JUDGE2_LIVE_OK=1)")
    s.add_argument("--background-file", help="--jev: the fact sheet for Jev and Gemma (default "
                                             "$JUDGE2_BACKGROUND_FILE, else judge2_background.local.md)")
    s.add_argument("--no-jev-gate", action="store_true",
                   help="write the page without the Jev demotion pass (stored reviews are still shown in J3)")
    s.set_defaults(func=cmd_top)

    s = sub.add_parser("mark", parents=[common], help="record a build / pass / hold decision",
                       epilog=MARK_GUIDE, formatter_class=argparse.RawDescriptionHelpFormatter)
    s.add_argument("target", nargs="?", help='posting_id, posting URL, or "employer|title" '
                                             "(omit when using --from-file); must match exactly one posting")
    s.add_argument("decision", nargs="?", choices=report.DECISIONS, default=None,
                   help="build (pursuing it) | pass (not pursuing; needs --reason) | hold (not yet; "
                        "it keeps surfacing, marked held)")
    s.add_argument("--reason", help="required for pass, and must start with a code: " + " | ".join(PASS_REASON_CODES) +
                   " | clearance (alias for requirement)"
                   ' (e.g. "function: clinical ops"); see the reason codes below')
    s.add_argument("--unmet", action="append",
                   help="a Required line, quoted from the JD, that the human call says is not met "
                        "(repeatable); only valid with a build or a pass --reason requirement/clearance")
    s.add_argument("--grade", choices=rubric.GRADES,
                   help="a human lane grade, recorded the same way the golden feedback CSV import is")
    s.add_argument("--basis", choices=MARK_BASIS_VALUES, default="seen",
                   help="'blind' if this grade was given before any machine score was on screen (default seen)")
    s.add_argument("--from-file", help="apply a whole review session at once -- CSV columns: "
                                       "posting,decision,reason,unmet,grade,basis (unmet ' || '-joined)")
    s.set_defaults(func=cmd_mark)

    s = sub.add_parser("sync", parents=[common],
                       help="rebuild the DuckDB tracker table from $TRACKER_SOURCE (postgres: tracker.application, "
                            "exact posting or req id matches; markdown, the default: Application_Tracker.md, needs "
                            "--vault) and record tracker decisions. Postgres unreachable: exits 1, nothing written "
                            "(the nightly pipeline logs a warning and continues)")
    s.add_argument("--verbose", action="store_true", help="print unmatched tracker rows")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("shortlist", parents=[common],
                       help="print the top scored undecided postings first seen in the last N days (held rows "
                            "included); writes nothing")
    s.add_argument("--days", type=int, default=7, help="window in days by first-seen date (default 7)")
    s.add_argument("--n", type=int, default=30, help="rows to print (default 30)")
    s.set_defaults(func=cmd_shortlist)

    s = sub.add_parser("labels", parents=[common], help="rebuild label_docs from the vault (replaces the vault-sourced rows)")
    s.add_argument("--report", action="store_true",
                   help="also print the per-source table and the training bars (250 positives, 150 pseudo-negatives)")
    s.add_argument("--pseudo", type=int, default=1500, help="pseudo-negatives to sample (default 1500)")
    s.add_argument("--seed", type=int, default=7, help="seed for the pseudo-negative sample (default 7)")
    s.add_argument("--reanchor", action="store_true",
                   help="recover llm_labels a re-fetched JD expired, using the judge batch files under db/batches* "
                        "as evidence the JD did not materially change; moves their report_feedback rows too (also runs "
                        "after every sweep and before every non-dry-run retrain)")
    s.add_argument("--dry-run", action="store_true",
                   help="with --reanchor: print counts only, write nothing (ignored without --reanchor)")
    s.add_argument("--batch-dir", action="append", help="with --reanchor: a batch dir to search (repeatable; "
                                                        "default every db/batches* directory)")
    s.set_defaults(func=cmd_labels)

    s = sub.add_parser("train", parents=[common],
                       help="train a TF-IDF + logistic regression model; it goes live for the next screen at once, "
                            "with no gate (retrain is the gated path)")
    s.add_argument("--cv", type=int, default=5,
                   help="cross-validation folds (default 5); needs at least that many positives and negatives")
    s.add_argument("--C", type=float, default=4.0,
                   help="logistic-regression C, inverse regularization; higher fits harder (default 4.0)")
    s.add_argument("--report", action="store_true", help="single-signal and blend AUCs")
    s.add_argument("--lens", choices=["process", "technical", "ai", "required", "bullseye"],
                   help="train one lens's model instead of the overall one; 'required' and 'bullseye' are NOT "
                        "lenses -- see features.REQUIRED_MODEL / features.BULLSEYE_MODEL, ranking signals only; bullseye "
                        "also runs its acceptance gate (employer-grouped AUC >= 0.78), and screen never loads "
                        "a bullseye model that failed it")
    s.set_defaults(func=cmd_train)

    s = sub.add_parser("bullseye-backfill", parents=[common],
                       help="one-time fit_bullseye UPDATE on the latest screens row of non-rejected postings with a "
                            "lens fit of 0.5 or more and JD text (no rescreen); run once after `train --lens "
                            "bullseye` passes its gate; exits 1 if the latest bullseye model failed it")
    s.set_defaults(func=cmd_bullseye_backfill)

    s = sub.add_parser("evidence", parents=[common],
                       help="check the evidence manifest (exit 1 if a source is missing) and, with --rebuild, re-embed it")
    s.add_argument("--manifest", help="manifest path (default $JOBSEARCH_EVIDENCE, else evidence.local.toml)")
    s.add_argument("--check", action="store_true", help="list sources, unit counts and samples; always runs, so this flag is optional")
    s.add_argument("--rebuild", action="store_true",
                   help="after a clean check, embed new units into evidence_units and delete ones no longer "
                        "produced; refuses if a source with stored units now yields none")
    s.set_defaults(func=cmd_evidence)

    s = sub.add_parser("coverage", parents=[common],
                       help="requirement coverage for screen survivors; writes coverage and requirement_units, "
                            "re-syncs evidence first; needs `evidence --rebuild` once")
    s.add_argument("--manifest", help="manifest path (default $JOBSEARCH_EVIDENCE, else evidence.local.toml)")
    s.add_argument("--all", action="store_true", help="re-cover every survivor, not only new / changed ones")
    s.add_argument("--limit", type=int, help="cover at most N survivors, best final_score first (default all); "
                                             "ignored with --posting or --calibrate")
    s.add_argument("--posting", action="append", help="cover this posting id (repeatable)")
    s.add_argument("--calibrate", action="store_true",
                   help="choose thresholds against hard negatives (§16.1); rewrites hard_negatives and stores a "
                        "coverage calibration row; ignores --all / --limit / --posting")
    s.add_argument("--pseudo", type=int, default=300, help="--calibrate: pseudo-negatives for the sanity AUC (default 300)")
    s.add_argument("--hard-top", type=int, default=200,
                   help="--calibrate: highest-fit unlabeled postings added as hard negatives (default 200)")
    s.set_defaults(func=cmd_coverage)

    s = sub.add_parser("required-embed", parents=[common],
                       help="the second-layer stacked Required model (NOT a lens): train it or score postings; "
                            "writes db/models and the required_embed table")
    s.add_argument("action", choices=["train", "score"],
                   help="train = fit the stacked model, saved only if its gate passes; score = write required_embed "
                        "rows for the lens-surfaced population (no model: logs one line, exits 0)")
    s.add_argument("--report", action="store_true", help="train: print the full metrics dict")
    s.add_argument("--all", action="store_true", help="score: re-score every population row, not only new ones")
    s.set_defaults(func=cmd_required_embed)

    s = sub.add_parser("judge", parents=[common],
                       help="LLM labeling run (no API call): export batch files, import graded labels, status, "
                            "report, exclude")
    s.add_argument("action", choices=["export", "import", "status", "report", "exclude"],
                   help="export = write batch files to --dir; import = load graded *.result.json into llm_labels; "
                        "status = batches done / pending; report = judge vs human agreement (+ --csv); "
                        "exclude = retire --posting ids from training, then auto-exclude thin JDs")
    s.add_argument("--csv", help="report: write every judged posting to this CSV for eyeballing")
    s.add_argument("--dir", default="db/batches",
                   help="batch directory for export / import / status (default db/batches, gitignored); export "
                        "overwrites its manifest, so use a fresh dir per export")
    s.add_argument("--limit", type=int, help="export: stop the queue after N postings")
    s.add_argument("--batch-size", type=int, default=judge_batch_default(),
                   help="export: postings per batch file (default 20)")
    s.add_argument("--scorer", default="claude-sonnet-batch",
                   help="import: scorer name stored on each label (default claude-sonnet-batch)")
    s.add_argument("--reject-content", type=int, default=100,
                   help="export: postings rejected on content, to measure false negatives (default 100)")
    s.add_argument("--reject-logistics", type=int, default=100,
                   help="export: postings rejected on location/pay with fit >= 0.5 (default 100)")
    s.add_argument("--relabel", type=int, nargs="?", const=0, metavar="N",
                   help="re-grade postings that already carry a label (the rubric changed, not the JD): "
                        "bare = all of them, N = about N drawn evenly across the four grades")
    s.add_argument("--reject-random", type=int, default=50,
                   help="export: randomly sampled rejected postings to include (default 50)")
    s.add_argument("--expired", action="store_true",
                   help="export: re-grade postings whose judge grade expired when the JD changed (open or closed); "
                        "run `labels --reanchor` first so only real rewrites are queued")
    s.add_argument("--reason", help="exclude: why this posting must never be trained on")
    s.add_argument("--posting", action="append", help="exclude: posting id to retire from training (repeatable)")
    s.add_argument("--exclude-dir", action="append",
                   help="export: skip postings already queued in this batch dir (repeatable)")
    s.add_argument("--pools", help="export: keep only these comma-separated pools: high, low, reject, ai_title "
                                   "(default all; not validated)")
    s.add_argument("--platform", help="export every active posting from one ATS platform (after an ingest fix)")
    s.set_defaults(func=cmd_judge)

    s = sub.add_parser("feedback", parents=[common],
                       help="golden-source feedback: --load, --agreement, --export; or a nested action: sheet, "
                            "sheet-import, ingest (a nested action ignores the three flags)")
    s.add_argument("--load", help="golden-source CSV to upsert into report_feedback; a human required_fit "
                                  "(meets / fails) with a description_hash is also bridged into llm_labels")
    s.add_argument("--agreement", action="store_true",
                   help="print the rule's level_fit vs confirmed human rows")
    s.add_argument("--export", help="re-export every report_feedback row with the rule's answer and needs_you")
    s.set_defaults(func=cmd_feedback, feedback_action=None)
    fsub = s.add_subparsers(dest="feedback_action")

    fs = fsub.add_parser("sheet", parents=[common_inner], help="write a blind grading CSV with decoys (sprint plan §26)")
    fs.add_argument("--n", type=int, default=20, help="rows in the top / mid / reject mix, before the Jev decoys (default 20)")
    fs.add_argument("--seed", type=int, help="seeds the sample/shuffle for a reproducible sheet")
    fs.add_argument("--out", help="CSV output path, plus a .sidecar.json beside it (default "
                                  "db/blind_sheets/blind_sheet_<date>.csv; a same-day rerun overwrites both)")
    fs.add_argument("--jev", type=int, default=6, help="Jev-demoted decoys added on top of --n (default 6; 0 = none)")
    fs.set_defaults(func=cmd_feedback, feedback_action="sheet")

    fs = fsub.add_parser("sheet-import", parents=[common_inner],
                         help="import a graded blind sheet (basis='blind'): writes report_feedback and appends the "
                              "history file; a bad value stops it before any write")
    fs.add_argument("path", help="the graded sheet CSV; its .sidecar.json beside it supplies the strata for the scores")
    fs.add_argument("--history", help="history JSONL path (default db/blind_sheet_history.jsonl)")
    fs.set_defaults(func=cmd_feedback, feedback_action="sheet-import")

    fs = fsub.add_parser("ingest", parents=[common_inner],
                         help="ingest gold-standard sheets in any of the vault's header formats; a refused file is "
                              "skipped, the rest are written, exit 1")
    fs.add_argument("paths", nargs="*", metavar="PATH", help="one or more gold-sheet CSVs")
    fs.add_argument("--manifest", help="a path,basis,notes CSV of files to ingest in one run; paths are relative "
                                       "to the manifest's folder")
    fs.add_argument("--basis", choices=MARK_BASIS_VALUES,
                    help="required for a narrow sheet (F2, F3, F4) with no basis column; a manifest row's own "
                         "basis wins")
    fs.add_argument("--accept-proposed", action="store_true",
                    help="ingest a machine-drafted 'proposed required calls' file (header carries "
                         "proposal_confidence) once the user has reviewed and confirmed it")
    fs.add_argument("--dry-run", action="store_true",
                    help="detect, normalize and resolve precedence against the DB; write nothing")
    fs.set_defaults(func=cmd_feedback, feedback_action="ingest")

    s = sub.add_parser("retrain", parents=[common],
                       help="the weekly retrain (sprint plan §24): labels, reanchor, five gated models plus "
                            "required-embed, ledger in model_runs; a promotion triggers rescreen, coverage and "
                            "required-embed score. Labels come from $JOBSEARCH_VAULT_DIR; --vault is ignored")
    s.add_argument("--dry-run", action="store_true",
                   help="train and gate the five TF-IDF models, write ledger rows (promoted=false, reason "
                        "'dry-run'), promote and rescreen nothing; still rebuilds label_docs, skips required-embed")
    s.add_argument("--history", action="store_true", help="print the model_runs ledger, newest first")
    s.set_defaults(func=cmd_retrain)

    s = sub.add_parser("judge2", parents=[common],
                       help="LLM second judge, line-by-line (sprint plan §25/§29); never live without a go")
    s.add_argument("action", choices=["run", "eval", "status", "rederive"],
                   help="run = review postings with the LLM (live only with approval); eval = the acceptance bar "
                        "and line report, or --compare; status = counts and rates; rederive = recompute "
                        "required_fit from stored lines, no API call")
    s.add_argument("--top", type=int, default=150, help="run: top N by rank_score (default 150)")
    s.add_argument("--dry-run", action="store_true", help="run: build and print payloads + prompt, call nothing")
    s.add_argument("--show", type=int, default=1, help="run --dry-run: how many full payloads to print")
    s.add_argument("--force", action="store_true", help="run: re-review even if already reviewed at this hash")
    s.add_argument("--eval-set", action="store_true",
                   help="run: target exactly the blind human-graded rows (vw_report_feedback_blind), "
                        "ignoring the top-N/decided/screen filters, so evaluate() has something to score")
    s.add_argument("--rerun", action="store_true",
                   help="run: force a re-ask under the SAME prompt (implies --force); combine with --run-tag "
                        "to keep the previous run's rows instead of overwriting them (sprint plan §29.4)")
    s.add_argument("--run-tag", help="run: joins the storage/cache key (base_prompt_version:TAG) so a repeat "
                                     "run is stored separately for `judge2 eval --compare`; default: none, "
                                     "production caching unchanged")
    s.add_argument("--posting", action="append",
                   help="run: only this posting id (repeatable); narrows the chosen population")
    s.add_argument("--postings-file", help="run: only the posting ids in this file (one per line)")
    s.add_argument("--background", choices=["public", "file"], default="public",
                   help="what background is sent: public = the neutral lens text (default); file = "
                        "--background-file or $JUDGE2_BACKGROUND_FILE; changes the prompt_version eval looks up")
    s.add_argument("--background-file", help="path for --background file (default $JUDGE2_BACKGROUND_FILE)")
    s.add_argument("--i-have-approval", action="store_true",
                   help="run: authorize the live call (or JUDGE2_LIVE_OK=1; either one is enough)")
    s.add_argument("--prompt-version", help="eval: evaluate this stored prompt_version (e.g. a --run-tag "
                                            "repeat, base_pv:TAG) instead of the one the current flags hash to")
    s.add_argument("--compare", nargs=2, metavar=("PROMPT_VERSION_A", "PROMPT_VERSION_B"),
                   help="eval: print postings whose required_fit differs between two stored prompt_versions "
                        "(sprint plan §29.4), instead of running the posting-level bar")
    s.set_defaults(func=cmd_judge2)

    s = sub.add_parser("jev", parents=[common],
                       help="Jev typed-decision tier (docs/JEV_PLAN.md); never live without a go. Stored reviews "
                            "can demote rows on the top page; rank_score never changes")
    s.add_argument("action", choices=["run", "eval", "status", "rederive", "sentinel"],
                   help="run = review a population (live only with approval); eval = the MSA study, stores "
                        "jev_evals unless --no-write; status = counts, drift, canary, tokens today vs cap; "
                        "rederive = recompute from stored lines, no API call; sentinel = pin the sentinel set (--init)")
    pop = s.add_mutually_exclusive_group()
    pop.add_argument("--eval-set", action="store_true",
                     help="run: every blind human-graded gold row (judge2.EVAL_SET_SQL)")
    pop.add_argument("--gated", action="store_true",
                     help="run: active undecided postings with any lens model fit >= 0.70, in rank order")
    pop.add_argument("--injection", action="store_true",
                     help="run: the synthetic adversarial set (jev_eval.injection_postings), run tag 'inj'")
    pop.add_argument("--lens-set", action="store_true",
                     help="run: every posting with a blind human lens grade (for the lens check)")
    pop.add_argument("--sentinel", action="store_true",
                     help="run: the pinned sentinel set (see `jev sentinel --init`); needs --run-tag")
    s.add_argument("--posting", action="append",
                   help="run: only this posting id (repeatable); narrows --eval-set or --gated only")
    s.add_argument("--postings-file", help="run: only the posting ids in this file (one per line)")
    s.add_argument("--limit", type=int, help="run --gated: at most N postings")
    s.add_argument("--dry-run", action="store_true", help="run: print request bodies and an estimate, call nothing")
    s.add_argument("--show", type=int, default=1, help="run --dry-run: how many postings' requests to print")
    s.add_argument("--force", action="store_true",
                   help="run: re-review even when cached; sentinel --init: replace the pinned set")
    s.add_argument("--run-tag", help="run: store a repeat run beside the canonical one (<pv>:TAG), e.g. r2; "
                                     "required with --sentinel; --injection always uses inj")
    s.add_argument("--i-have-approval", action="store_true",
                   help="run: the live-call approval (or JEV_LIVE_OK=1, which the launch.sh Jev preset sets); also "
                        "needs the endpoint's API key; stops at JEV_MAX_CALLS_PER_RUN / JEV_DAILY_TOKEN_CAP")
    s.add_argument("--background-file",
                   help="the fact sheet (default $JUDGE2_BACKGROUND_FILE, else judge2_background.local.md)")
    s.add_argument("--judge2-pv", help="eval: the judge2 prompt_version to compare with (default: the best)")
    s.add_argument("--prompt-version", help="eval: evaluate this stored Jev prompt_version instead of the current one")
    s.add_argument("--tags", default="r2,r3", help="eval: comma-separated repeat run tags (default r2,r3)")
    s.add_argument("--sentinel-tag", help="eval: also compare this sentinel rerun tag with the canonical run")
    s.add_argument("--no-write", action="store_true", help="eval: print the report, store no jev_evals rows")
    s.add_argument("--init", action="store_true", help="sentinel: pin the sentinel posting ids beside the DB")
    s.add_argument("--n", type=int, default=10, help="sentinel --init: how many gold postings to pin (default 10)")
    s.set_defaults(func=cmd_jev)

    s = sub.add_parser("setup-check", parents=[common],
                       help="check .env, keys, local files, optional packages, the evidence manifest and gitignore; "
                            "exit 1 on a blocking issue. Opens (and creates or migrates) the DuckDB first")
    s.add_argument("--manifest", help="manifest path (default $JOBSEARCH_EVIDENCE, else evidence.local.toml)")
    s.set_defaults(func=cmd_setup_check)

    # argparse shows a subcommand's help= only in the top-level list; copy it into the subcommand's own
    # --help as its description, and add the DuckDB-lock note every command shares (mark has its own epilog).
    for subs in (sub, fsub):
        summaries = {c.dest: c.help for c in subs._choices_actions}
        for name, p in subs.choices.items():
            p.description = p.description or summaries.get(name)
            if p.epilog is None:
                p.epilog = LOCK_NOTE
    a = ap.parse_args()
    try:
        con = store.connect(a.db or os.environ.get("JOBSEARCH_DB") or None)   # JOBSEARCH_DB: default-path override (tests)
    except duckdb.IOException as e:   # another process (a sweep, launch.sh) holds the write lock
        sys.exit(f"{a.cmd}: the DuckDB is locked by another process (a sweep or launch.sh run?); nothing was "
                 f"written. Run it again when that finishes.\n  ({str(e).splitlines()[0]})")
    try:
        a.func(con, a)
    finally:
        con.close()


if __name__ == "__main__":
    main()
