"""`finder.py jev run|eval|status|rederive|sentinel` (docs/JEV_PLAN.md §5, WP4): the thin layer between the
argparse namespace and `jev.py` / `jev_eval.py` / `store.py`, kept out of finder.py so the CLI stays a wrapper.

Nothing here calls Jev directly: `run_cmd` goes through `jev.run`, which owns the live gate and the caps. The
CLI adds an up-front gate check only so a refused run exits non-zero (launch.sh reads the exit code). Output
names posting ids only, never a title or an employer.
"""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.ats import store

from . import jev, jev_eval, judge2
from . import jev_questions as Q
from .jev_types import Posting

REPO = Path(__file__).resolve().parents[2]
DEFAULT_BACKGROUND = REPO / "judge2_background.local.md"
SENTINEL_FILE_NAME = "jev_sentinel_ids.txt"
POPULATIONS = ("eval_set", "gated", "injection", "sentinel")


class JevCliError(RuntimeError):
    """A usage or setup problem the CLI reports in one line and exits 1 on."""


# ---------------------------------------------------------------- paths
def background_path(explicit: Optional[str] = None) -> str:
    """The fact sheet: `--background-file`, else `JUDGE2_BACKGROUND_FILE`, else judge2_background.local.md."""
    return explicit or os.environ.get(judge2.BACKGROUND_ENV) or str(DEFAULT_BACKGROUND)


def load_facts(explicit: Optional[str] = None) -> list:
    path = background_path(explicit)
    try:
        return jev.load_facts(path)
    except (FileNotFoundError, ValueError) as exc:
        raise JevCliError(f"jev: cannot read the fact sheet ({exc}); pass --background-file or copy "
                          f"judge2_background.example.md to judge2_background.local.md") from None


def sentinel_path(db_path: Optional[str]) -> Path:
    """The pinned sentinel id list lives beside its database (db/jev_sentinel_ids.txt for the default DB)."""
    return Path(db_path or store.DEFAULT_DB_PATH).expanduser().resolve().parent / SENTINEL_FILE_NAME


def read_sentinel_ids(path: Path) -> list:
    if not path.exists():
        raise JevCliError(f"jev: no pinned sentinel set at {path}; run `finder.py jev sentinel --init` once first")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- populations
def sentinel_postings(con, path: Path, *, log=print) -> list:
    """Postings for the pinned sentinel ids, in file order. An id no longer in `postings` is skipped and named."""
    ids = read_sentinel_ids(path)
    rows = con.execute(
        "SELECT posting_id, description_hash, title, employer, description_text FROM postings "
        "WHERE posting_id IN (SELECT unnest(?::VARCHAR[])) AND description_text IS NOT NULL", [ids]).fetchall()
    by_id = {r[0]: r for r in rows}
    missing = [pid for pid in ids if pid not in by_id]
    if missing:
        log(f"jev: {len(missing)} pinned sentinel id(s) no longer present, skipped: {', '.join(missing)}")
    return [Posting(posting_id=pid, description_hash=dh or "", title=title or "", employer=employer or "",
                    description_text=text or "")
            for pid, dh, title, employer, text in (by_id[pid] for pid in ids if pid in by_id)]


def chosen_population(a) -> str:
    chosen = [name for name in POPULATIONS if getattr(a, name, False)]
    if len(chosen) != 1:
        raise JevCliError("jev run: give exactly one of --eval-set, --gated, --injection, --sentinel")
    return chosen[0]


def population(con, a, db_path: Optional[str], *, log=print) -> tuple:
    """(postings, run_tag) for `jev run`, validating the flag combinations."""
    which = chosen_population(a)
    if a.limit is not None and which != "gated":
        raise JevCliError("jev run: --limit applies to --gated only")
    tag = a.run_tag
    if which == "eval_set":
        return jev.eval_set_postings(con), tag
    if which == "gated":
        return jev.gated_postings(con, limit=a.limit), tag
    if which == "injection":
        if tag and tag != jev_eval.INJECTION_TAG:
            raise JevCliError(f"jev run --injection always stores under run tag {jev_eval.INJECTION_TAG!r}")
        return jev_eval.injection_postings(con), jev_eval.INJECTION_TAG
    if not tag:
        raise JevCliError("jev run --sentinel needs --run-tag (a sentinel rerun is compared with the canonical run)")
    return sentinel_postings(con, sentinel_path(db_path), log=log), tag


# ---------------------------------------------------------------- run
def live_ok(i_have_approval: bool) -> bool:
    """`--i-have-approval`, or `JEV_LIVE_OK=1` (set by the launch.sh Jev presets)."""
    return bool(i_have_approval) or os.environ.get(Q.LIVE_OK_ENV) == "1"


def run_cmd(con, a, db_path: Optional[str], *, log=print):
    """`jev run`. A dry run needs no key and no approval. Returns the RunSummary."""
    facts = load_facts(a.background_file)
    rows, tag = population(con, a, db_path, log=log)
    ok = live_ok(a.i_have_approval)
    if not a.dry_run:
        endpoint = jev.endpoint_from_env()
        if not ok:
            raise JevCliError("jev run refused: no live call is authorized. Run --dry-run first, then pass "
                              f"--i-have-approval or set {Q.LIVE_OK_ENV}=1")
        if not jev.api_key(endpoint):
            raise JevCliError(f"jev run refused: {Q.KEY_ENV[endpoint]} is not set (see .env.template)")
    summary = jev.run(con, rows, live_ok=ok, dry_run=a.dry_run, show=a.show, force=a.force, run_tag=tag,
                      facts=facts, log=log)
    if a.dry_run:
        log(f"jev.run: {summary}")   # a live run logs its own summary
    return summary


# ---------------------------------------------------------------- eval
def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("jev-%Y%m%dT%H%M%S%fZ")


def eval_cmd(con, a, db_path: Optional[str], *, log=print) -> dict:
    """`jev eval`: every family with data under the CURRENT base prompt_version, then the text report."""
    facts = load_facts(a.background_file)
    pv = jev.prompt_version(facts, jev.endpoint_from_env())
    tags = tuple(t.strip() for t in (a.tags or "").split(",") if t.strip())
    if a.sentinel_tag:
        path = sentinel_path(db_path)
        if path.exists():
            pinned = read_sentinel_ids(path)
            current = [p.posting_id for p in jev_eval.sentinel_postings(con, len(pinned))]
            if pinned != current:
                log(f"jev eval: WARNING the pinned sentinel set ({path.name}) differs from the first "
                    f"{len(pinned)} gold postings that evaluate_sentinel compares; its result may be partial")
    results = jev_eval.evaluate_all(con, pv, run_id=new_run_id(), judge2_pv=a.judge2_pv, tags=tags,
                                    sentinel_tag=a.sentinel_tag, write=not a.no_write)
    log(f"jev eval: base prompt_version {pv}" + (" (not written)" if a.no_write else ""))
    log(jev_eval.format_report(results))
    return results


# ---------------------------------------------------------------- status
_STATUS_SQL = """
    SELECT prompt_version, run_tag, count(*) AS n,
           count(*) FILTER (WHERE version_drift) AS drift,
           count(*) FILTER (WHERE injection_p >= ?) AS canary,
           count(*) FILTER (WHERE required_fit = 'meets') AS meets,
           count(*) FILTER (WHERE required_fit = 'partial') AS partial,
           count(*) FILTER (WHERE required_fit = 'fails') AS fails,
           count(*) FILTER (WHERE required_fit IS NULL) AS no_call,
           max(reviewed_at) AS last_reviewed
    FROM jev_reviews GROUP BY prompt_version, run_tag ORDER BY last_reviewed DESC, prompt_version, run_tag
"""


def _today_utc_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def status(con, a, *, log=print) -> dict:
    """`jev status`: review counts, drift, canary flags and the required_fit spread per prompt_version and
    run tag; tokens used today against the cap; the bar row per prompt_version."""
    try:
        facts = load_facts(a.background_file)
        current: Optional[str] = jev.prompt_version(facts, jev.endpoint_from_env())
    except (JevCliError, ValueError) as exc:
        current = None
        log(f"jev status: current prompt_version unknown ({exc})")
    rows = con.execute(_STATUS_SQL, [Q.CANARY_FLAG_AT]).fetchall()
    tokens_today = store.jev_tokens_since(con, _today_utc_iso())
    cap = jev.caps_from_env().daily_token_cap
    bars = con.execute("SELECT prompt_version, required_passed, lens_passed, repeatability_passed, "
                       "jev_bar_passed FROM vw_jev_bar ORDER BY prompt_version").fetchall()
    flagged = [pid for (pid,) in con.execute(
        "SELECT posting_id FROM vw_jev_latest WHERE injection_p >= ? ORDER BY posting_id",
        [Q.CANARY_FLAG_AT]).fetchall()]

    log(f"jev status: current prompt_version {current or '?'}; tokens today {tokens_today:,} of "
        f"{cap:,} ({Q.DAILY_CAP_ENV})")
    if not rows:
        log("  no Jev reviews stored")
    for pv, tag, n, drift, canary, meets, partial, fails, no_call, last in rows:
        log(f"  {pv} tag={tag or '-'}: {n} review(s), drift {drift}, canary {canary}, "
            f"required meets {meets} / partial {partial} / fails {fails} / no call {no_call}, last {last}")
    for pv, req, lens, rep, passed in bars:
        log(f"  bar {pv}: required={req} lens={lens} repeatability={rep} -> "
            f"{'PASSED' if passed else 'NOT PASSED'}")
    if flagged:
        log(f"  canary-flagged (canonical) posting ids: {', '.join(flagged)}")
    return {"current_prompt_version": current, "rows": rows, "tokens_today": tokens_today, "bars": bars,
            "canary_flagged": flagged}


# ---------------------------------------------------------------- rederive
def rederive(con, *, log=print) -> dict:
    """`jev rederive`: recomputes required_fit / derive_why / lines_fit / shape_fit / shape_score for every
    stored review from its OWN stored jev_lines with judge2's derivation, the same call `jev.review_posting`
    makes. No API call. An injection posting ("<pid>#inj<i>") takes its title from the posting it copies."""
    keys = con.execute("""
        SELECT r.posting_id, r.description_hash, r.prompt_version, r.run_tag, coalesce(p.title, '')
        FROM jev_reviews r LEFT JOIN postings p ON p.posting_id = split_part(r.posting_id, '#', 1)
        ORDER BY r.posting_id, r.prompt_version, r.run_tag
    """).fetchall()
    updated = changed = 0
    for pid, dh, pv, tag, title in keys:
        rec = store.load_jev_review(con, pid, dh, pv, tag or None)
        if rec is None:
            continue
        fit, why = judge2.derive_required_fit(rec.lines, title=title)
        lfit, _lwhy = judge2.lines_fit(rec.lines, title=title)
        sfit, sscore, _counts = judge2.shape_fit(rec.lines)
        if (fit, why, lfit, sfit, sscore) != (rec.required_fit, rec.derive_why, rec.lines_fit, rec.shape_fit,
                                              rec.shape_score):
            changed += 1
        updated += store.update_jev_derivation(con, pid, dh, pv, tag or None, required_fit=fit, derive_why=why,
                                               lines_fit=lfit, shape_fit=sfit, shape_score=sscore)
    log(f"jev rederive: {updated} review(s) recomputed from stored lines (no API call), {changed} changed")
    return {"updated": updated, "changed": changed}


# ---------------------------------------------------------------- sentinel
def sentinel_init(con, db_path: Optional[str], *, n: int = jev_eval.SENTINEL_N, force: bool = False,
                  log=print) -> Path:
    """Pins the sentinel set once: jev_eval.sentinel_postings(con, n) ids, one per line, beside the DB."""
    if n < 1:
        raise JevCliError("jev sentinel --init: --n must be at least 1")
    path = sentinel_path(db_path)
    if path.exists() and not force:
        raise JevCliError(f"jev sentinel: {path} already pins a set; pass --force to replace it")
    ids = [p.posting_id for p in jev_eval.sentinel_postings(con, n)]
    if not ids:
        raise JevCliError("jev sentinel: no blind gold postings to pin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ids) + "\n", encoding="utf-8")
    log(f"jev sentinel: pinned {len(ids)} posting id(s) to {path}")
    return path
