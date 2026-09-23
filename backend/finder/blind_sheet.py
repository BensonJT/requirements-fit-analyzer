"""The monthly blind sheet with decoys (sprint plan §26, follows §22.4).

Review feedback (`finder.py mark`) is top-heavy: only postings that already ranked high get looked at, so
it can measure precision at the top and cannot measure MISSES (good jobs buried lower, or wrongly rejected
by a rule). `generate_sheet()` draws a small stratified, shuffled, score-free CSV once a month; the user
grades it GRADE FIRST -- no Rank, no verdict, no reason is on the page -- and `import_sheet()` writes the
grades back through the exact same `report_feedback` path `feedback.load_csv()` uses, with `basis='blind'`,
so the sheet is a genuine held-out check on the rank, not another anchored review.
"""
import csv
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.ats import store
from backend.screen import norm_company

from . import feedback, rubric
from .judge import _title_key

# Sheet columns: identifying columns only, then the empty columns the user fills in by hand. No rank, no
# verdict, no reason, no flag -- GRADE FIRST, REVEAL SECOND (sprint plan §22.4).
SHEET_COLUMNS = ("posting_id", "employer", "title", "location", "url",
                  "human_grade", "required_fit", "required_unmet", "level_fit", "note")

# The three reject reasons a blind sheet decoy is drawn from (sprint plan §26), matched against
# screens.reasons text -- see backend/screen.py's reasons.append() call sites for the exact phrases.
REJECT_BUCKETS = ("location", "clearance", "title")
_BUCKET_PATTERNS = {
    "location": re.compile(r"remote and outside the commute area|remote restricted to:|far commute:", re.I),
    "clearance": re.compile(r"clearance must already be held", re.I),
    "title": re.compile(r"off-function title|off-lane title", re.I),
}

REQUIRED_FIT_VALUES = ("meets", "fails")

DEFAULT_DIR = os.path.join(os.path.dirname(store.DEFAULT_DB_PATH), "blind_sheets")
DEFAULT_HISTORY_PATH = os.path.join(os.path.dirname(store.DEFAULT_DB_PATH), "blind_sheet_history.jsonl")

# The exclusion the sheet must honor: a posting the user has already graded (report_feedback, assessor
# 'user') or already decided (any `mark`/tracker row in `decisions`) teaches nothing blind a second time.
_EXCLUDED_SQL = """
    SELECT posting_id FROM decisions
    UNION SELECT posting_id FROM report_feedback WHERE assessor = 'user'
"""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rows(cur) -> list:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _dedup_key(employer, title) -> tuple:
    return (norm_company(employer or ""), _title_key(title or ""))


def _split_counts(total: int, k: int) -> list:
    """`total` split into `k` non-negative ints as evenly as possible, the remainder going to the first
    buckets -- e.g. (7, 3) -> [3, 2, 2]."""
    base, extra = divmod(max(total, 0), k)
    return [base + (1 if i < extra else 0) for i in range(k)]


def quota(n: int) -> tuple:
    """(top, mid, reject) counts for a sheet of size `n`, proportional to the 8/6/6-of-20 mix (sprint plan
    §26), via largest-remainder rounding so the three always sum to exactly `n`."""
    raw = [n * 0.4, n * 0.3, n * 0.3]
    counts = [int(x) for x in raw]
    remainder = n - sum(counts)
    order = sorted(range(3), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in order[:remainder]:
        counts[i] += 1
    return tuple(counts)


def _rank_pool(con, start: int, end: int) -> list:
    """Active, not-rejected, not-already-graded/decided postings ranked by `vw_lens_fit.rank_score`, rows
    `start`..`end` (1-based, inclusive) of that ranking."""
    cur = con.execute(f"""
        WITH excluded AS ({_EXCLUDED_SQL}),
        ranked AS (
            SELECT posting_id, employer, title, location_primary, url, rank_score, verdict,
                   row_number() OVER (ORDER BY rank_score DESC, posting_id) AS rn
            FROM vw_lens_fit
            WHERE verdict != 'reject' AND posting_id NOT IN (SELECT posting_id FROM excluded)
        )
        SELECT posting_id, employer, title, location_primary, url, rank_score, verdict
        FROM ranked WHERE rn BETWEEN ? AND ? ORDER BY rn
    """, [start, end])
    return _rows(cur)


def _reject_pool(con) -> list:
    """Screen-rejected postings that carry a JD, not already graded/decided, bucketed by reject reason."""
    cur = con.execute(f"""
        WITH excluded AS ({_EXCLUDED_SQL})
        SELECT f.posting_id, f.employer, f.title, f.location_primary, f.url, f.rank_score, f.verdict,
               f.reasons
        FROM vw_lens_fit f JOIN postings p USING (posting_id)
        WHERE f.verdict = 'reject' AND p.description_hash IS NOT NULL
          AND f.posting_id NOT IN (SELECT posting_id FROM excluded)
        ORDER BY f.posting_id
    """)
    return _rows(cur)


def _reasons_text(reasons) -> str:
    if reasons is None:
        return ""
    if isinstance(reasons, str):
        try:
            reasons = json.loads(reasons)
        except ValueError:
            return reasons
    return " ; ".join(reasons or [])


def bucket_for_reasons(reasons) -> Optional[str]:
    text = _reasons_text(reasons)
    for bucket in REJECT_BUCKETS:
        if _BUCKET_PATTERNS[bucket].search(text):
            return bucket
    return None


def _pick(pool: list, k: int, stratum: str, rng: random.Random, used_keys: set, used_pids: set) -> list:
    """Up to `k` rows from `pool`, deduped against everything already chosen (this call and earlier ones)
    by posting_id AND by (employer, normalized title) -- so a requisition duplicate never fills two seats
    on the same sheet. Marks its picks into `used_keys`/`used_pids` before returning."""
    seen_local = set()
    eligible = []
    for row in pool:
        if row["posting_id"] in used_pids:
            continue
        key = _dedup_key(row["employer"], row["title"])
        if key in used_keys or key in seen_local:
            continue
        seen_local.add(key)
        eligible.append((key, row))
    if k <= 0 or not eligible:
        return []
    chosen = rng.sample(eligible, k) if len(eligible) > k else eligible
    picked = []
    for key, row in chosen:
        used_keys.add(key)
        used_pids.add(row["posting_id"])
        picked.append(dict(row, stratum=stratum))
    return picked


def _write_csv(out_path: Path, rows: list) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({"posting_id": r["posting_id"], "employer": r["employer"], "title": r["title"],
                        "location": r.get("location_primary"), "url": r["url"],
                        "human_grade": "", "required_fit": "", "required_unmet": "", "level_fit": "",
                        "note": ""})


def sidecar_path_for(csv_path) -> Path:
    csv_path = Path(csv_path)
    return csv_path.with_name(csv_path.stem + ".sidecar.json")


def _write_sidecar(path: Path, rows: list) -> None:
    data = {r["posting_id"]: {"stratum": r["stratum"], "rank_score": r.get("rank_score"),
                              "verdict": r.get("verdict")} for r in rows}
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def generate_sheet(con, n: int = 20, seed: Optional[int] = None, out_path=None, *, top_n: int = 50,
                    mid_start: int = 200, mid_end: int = 600, log=print) -> dict:
    """Writes the blind sheet CSV (no scores/ranks/verdicts) plus its sidecar (posting_id -> stratum +
    the rank/verdict at generation time, which the CSV itself must never carry). Returns
    {"csv": path, "sidecar": path, "counts": {stratum: n}}.
    """
    rng = random.Random(seed)
    used_keys, used_pids = set(), set()
    rows = []

    top_k, mid_k, reject_k = quota(n)
    loc_k, clr_k, ttl_k = _split_counts(reject_k, 3)

    rows += _pick(_rank_pool(con, 1, top_n), top_k, "top50", rng, used_keys, used_pids)
    rows += _pick(_rank_pool(con, mid_start, mid_end), mid_k, "mid200_600", rng, used_keys, used_pids)

    reject_pool = _reject_pool(con)
    bucketed = {b: [] for b in REJECT_BUCKETS}
    for row in reject_pool:
        b = bucket_for_reasons(row.get("reasons"))
        if b:
            bucketed[b].append(row)

    shortfall = 0
    for bucket, k in zip(REJECT_BUCKETS, (loc_k, clr_k, ttl_k)):
        picked = _pick(bucketed[bucket], k, f"reject_{bucket}", rng, used_keys, used_pids)
        rows += picked
        shortfall += k - len(picked)
    if shortfall > 0:
        # Fall back to ANY reject with a JD, regardless of bucket (sprint plan §26: "fall back to any
        # reject with a JD when a bucket is short").
        rows += _pick(reject_pool, shortfall, "reject_any", rng, used_keys, used_pids)

    rng.shuffle(rows)   # position must not leak the stratum

    if out_path is None:
        os.makedirs(DEFAULT_DIR, exist_ok=True)
        out_path = os.path.join(DEFAULT_DIR, f"blind_sheet_{_now():%Y%m%d}.csv")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_path_for(out_path)
    _write_csv(out_path, rows)
    _write_sidecar(sidecar, rows)

    counts = {}
    for r in rows:
        counts[r["stratum"]] = counts.get(r["stratum"], 0) + 1
    log(f"blind sheet: {len(rows)}/{n} rows -> {out_path} (sidecar {sidecar.name}) -- "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return {"csv": out_path, "sidecar": sidecar, "counts": counts}


def _clean(v) -> Optional[str]:
    if v is None:
        return None
    v = v.strip()
    return v or None


def _validate_graded_row(i: int, raw: dict) -> dict:
    pid = _clean(raw.get("posting_id"))
    if not pid:
        raise ValueError(f"row {i}: empty posting_id")
    human_grade = _clean(raw.get("human_grade"))
    required_fit = _clean(raw.get("required_fit"))
    level_fit = _clean(raw.get("level_fit"))
    if human_grade is not None and human_grade not in rubric.GRADES:
        raise ValueError(f"row {i} ({pid}): human_grade {human_grade!r} must be one of {', '.join(rubric.GRADES)}")
    if required_fit is not None and required_fit not in REQUIRED_FIT_VALUES:
        raise ValueError(f"row {i} ({pid}): required_fit {required_fit!r} must be one of "
                         f"{', '.join(REQUIRED_FIT_VALUES)}")
    if level_fit is not None and level_fit not in feedback.LEVEL_ORDER:
        raise ValueError(f"row {i} ({pid}): level_fit {level_fit!r} must be one of {', '.join(feedback.LEVEL_ORDER)}")
    return {"posting_id": pid, "human_grade": human_grade, "required_fit": required_fit,
           "required_unmet": _clean(raw.get("required_unmet")), "level_fit": level_fit,
           "note": _clean(raw.get("note"))}


def import_sheet(con, path, *, history_path=None, log=print, now=None) -> dict:
    """Imports a graded blind sheet through `feedback.load_csv()` -- the SAME path the golden-source CSV
    import uses -- with `basis='blind'`, `assessor='user'`, `confirmed_by_user=True` forced regardless of
    what (if anything) the user wrote in those columns. A `required_fit` value additionally goes through
    `feedback.bridge_required_label()`, the same human-Required bridge `finder.py mark` uses, with
    `lens_grade_source='human'` because the sheet's `human_grade` IS the user's own lane call.

    Prints, and returns, three numbers read off the sidecar: precision at the top, the miss rate in the
    200-600 band, and the false-reject rate per reject-reason bucket. Appends them, timestamped, to a
    JSON-lines history file. Rows with no `human_grade` are skipped and counted, never guessed at.
    """
    now = now or _now()
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as f:
        raw_rows = list(csv.DictReader(f))
    if not raw_rows:
        raise ValueError(f"{path} has no data rows")
    sidecar_file = sidecar_path_for(path)
    sidecar = json.loads(sidecar_file.read_text(encoding="utf-8")) if sidecar_file.exists() else {}

    graded, skipped = [], 0
    for i, raw in enumerate(raw_rows, start=2):   # row 1 is the header
        parsed = _validate_graded_row(i, raw)
        if parsed["human_grade"] is None:
            skipped += 1
            continue
        graded.append(parsed)

    records = []
    for g in graded:
        hit = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?",
                          [g["posting_id"]]).fetchone()
        description_hash = hit[0] if hit else None
        if not description_hash:
            raise ValueError(f"{g['posting_id']} has no description_hash yet -- cannot import its grade")
        required_unmet = g["required_unmet"] if g["required_fit"] == "fails" else (g["required_unmet"] or "")
        records.append({
            "posting_id": g["posting_id"], "description_hash": description_hash, "human_grade": g["human_grade"],
            "level_fit": g["level_fit"], "basis": "blind", "assessor": "user", "confirmed_by_user": True,
            # A blind-sheet row is a grade, not a build/pass/hold decision -- report_feedback.verdict is
            # NOT NULL, so it is filled with the neutral 'consider' rather than invented as 'build'/'pass'.
            "verdict": "consider",
            "note": g["note"], "required_fit": g["required_fit"], "required_unmet": required_unmet,
            "assessed_at": now,
        })

    if records:
        # Goes through feedback.write_records -- the same INSERT and the same required_fit -> llm_labels
        # bridge load_csv's golden-source import uses -- instead of round-tripping through a temp CSV file.
        feedback.write_records(con, records, log=lambda *_a, **_k: None)

    stats = _score_against_sidecar({g["posting_id"]: g for g in graded}, sidecar)
    log(f"blind sheet import: {len(graded)} graded, {skipped} ungraded skipped (of {len(raw_rows)} rows)")
    log(f"  precision at top: {_pct(stats['precision_top'])} (n={stats['n_top']})")
    log(f"  miss rate 200-600: {_pct(stats['miss_rate_mid'])} (n={stats['n_mid']})")
    for bucket in REJECT_BUCKETS:
        log(f"  false-reject rate ({bucket}): {_pct(stats['false_reject_by_bucket'][bucket])} "
            f"(n={stats['n_by_bucket'][bucket]})")

    history_path = history_path or DEFAULT_HISTORY_PATH
    entry = {"imported_at": now.isoformat(), "csv": str(path), "n_graded": len(graded),
             "n_skipped_ungraded": skipped, **stats}
    os.makedirs(os.path.dirname(history_path) or ".", exist_ok=True)
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def _pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _score_against_sidecar(graded_by_pid: dict, sidecar: dict) -> dict:
    """The three §26 numbers, read off the sidecar's per-row stratum against the CSV's per-row grade.
    A row missing from the sidecar (a hand-added row, or a sheet graded without one) simply cannot be
    scored and is left out of every denominator -- never guessed into a bucket."""
    top_total = top_hit = 0
    mid_total = mid_bullseye = 0
    bucket_totals = {b: 0 for b in REJECT_BUCKETS}
    bucket_hits = {b: 0 for b in REJECT_BUCKETS}

    for pid, info in sidecar.items():
        g = graded_by_pid.get(pid)
        if g is None:
            continue
        good = g["human_grade"] in ("bullseye", "adjacent")
        stratum = info.get("stratum")
        if stratum == "top50":
            top_total += 1
            if good and g["required_fit"] != "fails":
                top_hit += 1
        elif stratum == "mid200_600":
            mid_total += 1
            if g["human_grade"] == "bullseye":
                mid_bullseye += 1
        elif stratum in (f"reject_{b}" for b in REJECT_BUCKETS):
            bucket = stratum[len("reject_"):]
            bucket_totals[bucket] += 1
            if good:
                bucket_hits[bucket] += 1
        # "reject_any" fallback rows are not attributable to one rule, so they feed no bucket rate.

    return {
        "precision_top": (top_hit / top_total) if top_total else None, "n_top": top_total,
        "miss_rate_mid": (mid_bullseye / mid_total) if mid_total else None, "n_mid": mid_total,
        "false_reject_by_bucket": {b: (bucket_hits[b] / bucket_totals[b] if bucket_totals[b] else None)
                                   for b in REJECT_BUCKETS},
        "n_by_bucket": bucket_totals,
    }
