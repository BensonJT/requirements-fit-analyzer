"""Orchestrates one sweep: pull every registered board, upsert, close what went
missing, then spend a bounded budget on per-posting detail calls.

Concurrency model: boards are fetched in a thread pool (each board is its own host,
so parallel boards don't violate the ~1 req/s/host politeness rule — pages within a
board are still sequential with a small delay). ALL DuckDB writes happen on the main
thread as results arrive, because a DuckDB connection is not shared across threads.
"""
import os
import queue
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from . import adapters, store
from .prefilter import DETAIL_TITLE_PATTERN
from .registry import load_registry


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sweep_bridge(con, rows, max_ring=1, workers=8, max_pages=None, log=print):
    """Pulls every `track=bridge` board in `rows` with the places strategy (sprint plan §31.3/§31.8)
    -- NEVER a whole-board pull. A row with no places configured (bridge_places.csv missing or
    empty for the requested ring selection) is skipped LOUDLY, one line per row, rather than
    falling back to a full pull. Returns the run summary dict, same shape as `sweep`.

    `max_ring` (2026-09-21 user ruling, replaces the earlier `ring2` boolean): only places with
    `ring <= max_ring` are pulled (default 1). A place at a higher ring not attempted this run
    closes nothing it covers -- the close-pass rule (record_bridge_board) is unchanged by this."""
    from . import bridge as B
    from .registry import load_bridge_places

    run_id = str(uuid.uuid4())[:8]
    started, t0 = _now(), time.monotonic()
    stats = dict(run_id=run_id, attempted=len(rows), succeeded=0, failed=0, seen=0, new=0, reopened=0, closed=0)
    places = load_bridge_places(max_ring=max_ring, log=log)
    if not places:
        log(f"Bridge sweep: no places configured in bridge_places.csv (max_ring={max_ring}) -- "
            f"skipping all {len(rows)} bridge row(s). Nothing was pulled.")
        stats["failed"] = len(rows)
        stats["elapsed"], stats["started"] = time.monotonic() - t0, started
        return stats
    log(f"Sweeping {len(rows)} bridge board(s) against {len(places)} place(s) "
        f"(max_ring={max_ring}, {workers} workers, run {run_id})...")
    attempted_places = {p["place"] for p in places}
    scope = {"strategy": "places", "places": places}

    # Orchestrator audit 2026-09-21, letter A.2: a row on a platform with no places-strategy
    # adapter is skipped LOUDLY here, before any request -- never handed to the thread pool, so
    # `adapters.list_jobs`'s own guard (letter A.1) is only ever the last line of defense.
    pullable = [r for r in rows if r["platform"] in adapters.PLACES_PLATFORMS]
    unsupported = [r for r in rows if r["platform"] not in adapters.PLACES_PLATFORMS]
    for row in unsupported:
        now = _now()
        msg = (f"platform {row['platform']!r} has no places-scoped adapter "
               f"(supported: {sorted(adapters.PLACES_PLATFORMS)})")
        stats["failed"] += 1
        store.log_board(con, run_id, row["employer"], row["platform"], False, 0.0, now, error=msg, track="bridge")
        log(f"  {row['employer']} ({row['platform']}) [bridge]: SKIPPED -- {msg}")
    rows = pullable

    def pull(row):
        t = time.monotonic()
        try:
            jobs = adapters.list_jobs(row, max_pages=max_pages, scope=scope)
            return row, jobs, None, time.monotonic() - t
        except Exception as e:  # noqa: BLE001 — every failure is logged per board
            return row, None, e, time.monotonic() - t

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(pull, r) for r in rows]
        for fut in as_completed(futures):
            row, jobs, err, elapsed = fut.result()
            done += 1
            employer, platform, now = row["employer"], row["platform"], _now()
            if err is not None:
                stats["failed"] += 1
                store.log_board(con, run_id, employer, platform, False, elapsed, now, error=str(err), track="bridge")
                log(f"  [{done}/{len(rows)}] {employer} ({platform}) [bridge]: FAILED — {str(err)[:120]}")
                continue
            place_status = getattr(jobs, "place_status", {})
            try:
                seen, new, reopened, closed = store.record_bridge_board(
                    con, employer, platform, jobs, now, attempted_places=attempted_places,
                    place_status=place_status, log=log)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                store.log_board(con, run_id, employer, platform, False, elapsed, now, job_count=len(jobs),
                                error=f"db: {e}", track="bridge")
                log(f"  [{done}/{len(rows)}] {employer} ({platform}) [bridge]: DB WRITE FAILED — {str(e)[:120]}")
                continue
            stats["succeeded"] += 1
            for k, v in (("seen", seen), ("new", new), ("reopened", reopened), ("closed", closed)):
                stats[k] += v
            truncated = any(not ok for ok in place_status.values())
            store.log_board(con, run_id, employer, platform, True, elapsed, now, job_count=seen, new_count=new,
                            closed_count=closed, truncated=truncated, track="bridge")
            flag = "  [one or more places failed/capped]" if truncated else ""
            log(f"  [{done}/{len(rows)}] {employer} ({platform}) [bridge]: {seen} live, {new} new, "
                f"{reopened} reopened, {closed} taken down — {elapsed:.1f}s{flag}")

    stats["elapsed"] = time.monotonic() - t0
    stats["started"] = started
    return stats


def sweep(con, rows, workers=8, max_pages=None, log=print):
    """Pulls every board in `rows`, writes each as it lands. Returns the run summary dict."""
    run_id = str(uuid.uuid4())[:8]
    started, t0 = _now(), time.monotonic()
    stats = dict(run_id=run_id, attempted=len(rows), succeeded=0, failed=0, seen=0, new=0, reopened=0, closed=0)
    log(f"Sweeping {len(rows)} boards with {workers} workers (run {run_id})...")

    scopes = {(r[0], r[1]): {"strategy": r[2], "facet_parameter": r[3], "value_ids": r[4]}
              for r in con.execute("SELECT employer, platform, strategy, facet_parameter, value_ids "
                                   "FROM board_scope").fetchall()}

    def pull(row):
        t = time.monotonic()
        try:
            jobs = adapters.list_jobs(row, max_pages=max_pages,
                                      scope=scopes.get((row["employer"], row["platform"])))
            return row, jobs, None, time.monotonic() - t
        except Exception as e:  # noqa: BLE001 — every failure is logged per board
            return row, None, e, time.monotonic() - t

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(pull, r) for r in rows]
        for fut in as_completed(futures):
            row, jobs, err, elapsed = fut.result()
            done += 1
            employer, platform, now = row["employer"], row["platform"], _now()
            if err is not None:
                stats["failed"] += 1
                store.log_board(con, run_id, employer, platform, False, elapsed, now, error=str(err))
                log(f"  [{done}/{len(rows)}] {employer} ({platform}): FAILED — {str(err)[:120]}")
                continue
            truncated = getattr(jobs, "truncated", False)
            try:
                seen, new, reopened, closed = store.record_board(con, employer, platform, jobs, now, truncated=truncated)
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                store.log_board(con, run_id, employer, platform, False, elapsed, now, job_count=len(jobs), error=f"db: {e}")
                log(f"  [{done}/{len(rows)}] {employer} ({platform}): DB WRITE FAILED — {str(e)[:120]}")
                continue
            stats["succeeded"] += 1
            for k, v in (("seen", seen), ("new", new), ("reopened", reopened), ("closed", closed)):
                stats[k] += v
            store.log_board(con, run_id, employer, platform, True, elapsed, now, job_count=seen,
                            new_count=new, closed_count=closed, truncated=truncated)
            flag = "  [TRUNCATED, no close-pass]" if truncated else ""
            log(f"  [{done}/{len(rows)}] {employer} ({platform}): {seen} live, {new} new, "
                f"{reopened} reopened, {closed} taken down — {elapsed:.1f}s{flag}")

    expired = store.close_expired_postings(con, store.CLOSE_BY_DATE_PLATFORMS, _now())
    if expired:
        stats["closed"] += expired
        log(f"  Closed {expired} posting(s) past their own close date "
            f"({', '.join(sorted(store.CLOSE_BY_DATE_PLATFORMS))}).")

    stats["elapsed"] = time.monotonic() - t0
    stats["started"] = started
    return stats


def fetch_details(con, registry_rows, budget=300, title_pattern=DETAIL_TITLE_PATTERN, workers=6, log=print,
                  since=None, label="Detail stage"):
    """Fills description_text (and locations/dates/pay) for postings that still lack
    one, newest first, up to `budget` requests. A 404 closes the posting.
    `since` limits it to postings first seen at or after that time (this run's new ones)."""
    by_employer = {(r["employer"], r["platform"]): r for r in registry_rows}
    cands = store.detail_candidates(con, adapters.DETAIL_PLATFORMS, title_pattern, budget,
                                    employers=[r["employer"] for r in registry_rows], since=since)
    if not cands:
        log(f"{label}: nothing to fetch.")
        return 0, 0
    log(f"{label}: fetching {len(cands)} JDs ({workers} workers)...")
    cols = ("posting_id", "employer", "platform", "req_id", "url", "location_primary", "locations",
            "workplace_type", "job_level", "posted_at", "posting_end_at")

    # Group by board so one host is hit sequentially; boards run in parallel.
    groups = {}
    for c in cands:
        p = dict(zip(cols, c))
        groups.setdefault((p["employer"], p["platform"]), []).append(p)

    results_q = queue.Queue()
    CHUNK = 100  # commit every 100 JDs per board, so a crash hours in loses minutes, not hours

    def work(key, postings):
        row = by_employer.get(key)
        buf = []
        for p in postings:
            t = time.monotonic()
            try:
                if row is None:
                    raise ValueError("board no longer in registry")
                buf.append((p["posting_id"], adapters.fetch_detail(row, p), None))
            except adapters.Gone:
                buf.append((p["posting_id"], None, "gone"))
            except Exception as e:  # noqa: BLE001
                buf.append((p["posting_id"], None, str(e)[:120]))
            if len(buf) >= CHUNK:
                results_q.put((key, buf))
                buf = []
            time.sleep(max(0.0, adapters.PAGE_DELAY * 4 - (time.monotonic() - t)))
        if buf:
            results_q.put((key, buf))

    fetched = closed = errors = 0
    per_board = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(work, k, v) for k, v in groups.items()]
        while True:
            try:
                key, results = results_q.get(timeout=2)
            except queue.Empty:
                if all(f.done() for f in futures) and results_q.empty():
                    break
                continue
            now = _now()
            con.execute("BEGIN")
            for pid, fields, err in results:
                if fields is not None:
                    store.apply_detail(con, pid, fields, now)
                    fetched += 1
                elif err == "gone":
                    store.close_posting(con, pid, now)
                    closed += 1
                else:
                    store.record_detail_error(con, pid, now)  # ages out after DETAIL_MAX_ATTEMPTS
                    errors += 1
            con.execute("COMMIT")
            b = per_board.setdefault(key, [0, len(groups[key])])
            b[0] += len(results)
            log(f"  {key[0]}: {b[0]}/{b[1]} done  (run total {fetched} JDs, {closed} gone, {errors} errors)")
        for f in futures:
            f.result()  # surface any unexpected worker crash
    log(f"{label} done: {fetched} fetched, {closed} closed as gone, {errors} errors.")
    return fetched, closed


def run(db_path=None, platform=None, limit=None, employer=None, workers=8, max_pages=None,
        detail_budget=300, detail_all=False, skip_sweep=False, new_detail_cap=5000,
        screen=True, report=True, llm_top=0, full_screen=False, track="all", max_ring=1, log=print):
    """One sweep plus two detail passes, then the finder stage:
    1. NEW postings from this run get their JD fetched automatically, every title, no
       prefilter, up to `new_detail_cap` (a safety cap for a board's first-ever sweep,
       where every posting counts as new). Anything over the cap falls to the backlog.
    2. BACKLOG: up to `detail_budget` older postings still missing a JD, newest first,
       title-prefiltered unless `detail_all`.
    3. FINDER (`screen=True`): tracker sync, decision read-back, screen, Jobs_Found report
       (only when JOBSEARCH_VAULT_DIR is set and `report`), snapshots. A finder failure is
       logged and never costs the sweep its run log.

    `track` (sprint plan §31.8): 'fit' | 'bridge' | 'all' (default) -- which registry rows to
    sweep. A bridge row is ALWAYS pulled with the places strategy (sweep_bridge), never a whole-
    board pull, and never gets a JD detail fetch (detail_candidates is unconditionally `fit`-only)
    -- see the module docstring on sweep_bridge. `max_ring` is passed straight through to it.
    """
    all_rows = load_registry()
    if platform:
        all_rows = [r for r in all_rows if r["platform"] == platform]
    if employer:
        all_rows = [r for r in all_rows if employer.lower() in r["employer"].lower()]
    # Orchestrator audit 2026-09-21, letter I: `--limit` is applied AFTER the `--track` filter, so
    # `--track bridge --limit 5` means the first five BRIDGE rows -- applying it to the combined
    # fit+bridge list first (the old order) could hand `--limit 5` a slice with zero bridge rows
    # in it, even though bridge rows exist further down the registry.
    if track == "fit":
        all_rows = [r for r in all_rows if r.get("track", "fit") == "fit"]
    elif track == "bridge":
        all_rows = [r for r in all_rows if r.get("track", "fit") == "bridge"]
    if limit:
        all_rows = all_rows[:limit]
    fit_rows = [r for r in all_rows if r.get("track", "fit") == "fit"]
    bridge_rows = [r for r in all_rows if r.get("track", "fit") == "bridge"]
    con = store.connect(db_path)
    try:
        stats = bridge_stats = None
        details = 0
        if not skip_sweep:
            if track in ("fit", "all") and fit_rows:
                stats = sweep(con, fit_rows, workers=workers, max_pages=max_pages, log=log)
                if new_detail_cap:
                    n, _ = fetch_details(con, fit_rows, budget=new_detail_cap, title_pattern=None,
                                         since=stats["started"], label="New-posting details")
                    details += n
            if track in ("bridge", "all") and bridge_rows:
                bridge_stats = sweep_bridge(con, bridge_rows, max_ring=max_ring, workers=workers,
                                            max_pages=max_pages, log=log)
        if detail_budget and track in ("fit", "all"):
            n, _ = fetch_details(con, fit_rows, budget=detail_budget,
                                 title_pattern=None if detail_all else DETAIL_TITLE_PATTERN,
                                 label="Backlog details")
            details += n
        if screen:
            _run_finder(con, stats, report=report, llm_top=llm_top, full_screen=full_screen, log=log)
        if stats:
            store.log_run(con, stats["run_id"], stats["started"], _now(), stats["attempted"], stats["succeeded"],
                          stats["failed"], stats["seen"], stats["new"], stats["reopened"], stats["closed"],
                          details, stats["elapsed"])
            log(f"\nRun {stats['run_id']} done in {stats['elapsed'] / 60:.1f} min: "
                f"{stats['succeeded']} boards ok, {stats['failed']} failed, {stats['seen']} live postings, "
                f"{stats['new']} new, {stats['reopened']} reopened, {stats['closed']} taken down, {details} JDs fetched")
        if bridge_stats:
            # Bridge runs get their own run_id row too (0 JDs -- detail fetch never touches bridge).
            store.log_run(con, bridge_stats["run_id"], bridge_stats["started"], _now(), bridge_stats["attempted"],
                          bridge_stats["succeeded"], bridge_stats["failed"], bridge_stats["seen"],
                          bridge_stats["new"], bridge_stats["reopened"], bridge_stats["closed"],
                          0, bridge_stats["elapsed"])
            log(f"\nBridge run {bridge_stats['run_id']} done in {bridge_stats['elapsed'] / 60:.1f} min: "
                f"{bridge_stats['succeeded']} boards ok, {bridge_stats['failed']} failed, "
                f"{bridge_stats['seen']} live postings, {bridge_stats['new']} new, "
                f"{bridge_stats['reopened']} reopened, {bridge_stats['closed']} taken down")
        return stats
    finally:
        con.close()


def _run_finder(con, stats, report=True, llm_top=0, full_screen=False, log=print):
    """The finder stage on the sweep's connection; imported lazily so the sweep never needs it."""
    try:
        from backend.finder import pipeline
        vault_dir = os.getenv("JOBSEARCH_VAULT_DIR") or None
        pipeline.daily(con, since=stats["started"] if stats else None, vault_dir=vault_dir, llm_top=llm_top,
                       report=report and bool(vault_dir), full=full_screen, log=log)
    except Exception as e:  # noqa: BLE001 — the sweep's own results must still be logged
        try:
            con.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 — no open transaction
            pass
        log(f"Finder stage FAILED: {type(e).__name__}: {e}")
    try:
        from backend.finder import retrain
        retrain.staleness_reminder(con, log=log)
    except Exception as e:  # noqa: BLE001 — the reminder must never fail the sweep (sprint plan §24)
        log(f"Retrain staleness check FAILED: {type(e).__name__}: {e}")
