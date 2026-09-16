"""Screening pipeline: pick the rows that need a (re)screen, score them, write `screens` and
`postings.screen_*`, and run the daily stage order inside the sweep.

`model` (Phase 2, from features.load_latest) adds fit_prob + top_terms. Requirement coverage (Phase 3a) runs after
the screen on new / changed survivors and is stored and shown only: it carries weight 0 in the score until step 3b.
"""
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from backend import profile as P

from . import report as report_mod
from . import rules, tracker_sync, version

BATCH = 500

# Columns the rules read (raw_json is left out: large and unused).
ROW_COLUMNS = ("posting_id", "employer", "platform", "req_id", "title", "url", "location_primary", "locations",
               "country", "workplace_type", "employment_type", "job_level", "pay_min", "pay_max", "pay_interval",
               "posted_at", "description_text", "first_seen_at", "description_fetched_at")

# New, JD changed since the last screen, or screened under another rules/model version.
RESCREEN_SQL = """
SELECT p.posting_id FROM postings p LEFT JOIN vw_screen_latest s USING (posting_id)
WHERE p.status = 'active' AND (s.posting_id IS NULL OR s.rules_version != ? OR s.model_version != ?
      OR s.screened_at < p.description_fetched_at)"""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _candidate_sql(rv: str, mv: str, since=None, limit=None, full: bool = False, columns: str = "p.posting_id"):
    """(sql, params) selecting `columns` for the rows to screen, newest first."""
    if full:
        sql, params = "SELECT p.posting_id FROM postings p WHERE p.status = 'active'", []
    else:
        sql, params = RESCREEN_SQL, [rv, mv]
    sql = sql.replace("SELECT p.posting_id", f"SELECT {columns}", 1)
    if since is not None:
        sql += " AND (p.first_seen_at >= ? OR p.description_fetched_at >= ?)"
        params += [since, since]
    sql += " ORDER BY p.first_seen_at DESC, p.posting_id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return sql, params


def candidate_ids(con, rv: str, mv: str, since=None, limit=None, full: bool = False) -> list:
    """Posting ids to screen, newest first. `since` keeps rows first seen or given a JD since then."""
    sql, params = _candidate_sql(rv, mv, since=since, limit=limit, full=full)
    return [r[0] for r in con.execute(sql, params).fetchall()]


def iter_candidate_batches(con, rv: str, mv: str, since=None, limit=None, full: bool = False, batch: int = BATCH):
    """Dict rows to screen, streamed in batches from a separate cursor. The cursor reads one snapshot,
    so the writes made between batches on `con` never shift what it returns."""
    sql, params = _candidate_sql(rv, mv, since=since, limit=limit, full=full,
                                 columns=", ".join(f"p.{c}" for c in ROW_COLUMNS))
    reader = con.cursor()
    try:
        reader.execute(sql, params)
        while rows := reader.fetchmany(batch):
            yield [dict(zip(ROW_COLUMNS, r)) for r in rows]
    finally:
        reader.close()


def fetch_rows(con, ids: list) -> list:
    """Postings rows as dicts, in the order of `ids`."""
    if not ids:
        return []
    cur = con.execute(f"SELECT {', '.join(ROW_COLUMNS)} FROM postings "
                      "WHERE posting_id IN (SELECT unnest(json_transform(?, '[\"VARCHAR\"]')))", [json.dumps(ids)])
    by_id = {r[0]: dict(zip(ROW_COLUMNS, r)) for r in cur.fetchall()}
    return [by_id[i] for i in ids if i in by_id]


def candidates(con, rv: str, mv: str, since=None, limit=None) -> list:
    """Dict rows needing a screen, ordered first_seen_at DESC."""
    return fetch_rows(con, candidate_ids(con, rv, mv, since=since, limit=limit))


def band_for(score: int) -> str:
    for threshold, name in P.SCORE_BANDS:
        if score >= threshold:
            return name
    return P.SCORE_BANDS[-1][1]


def combine(rule_score: int, fit_prob: Optional[float], embed_sim: Optional[float], llm_score: Optional[int],
            calib: Optional[dict] = None, *, tier: Optional[int] = None, rejected: bool = False,
            flags: int = 0) -> tuple:
    """(final_score, band), content first (sprint plan §14).

    content = 100 * fit_prob, averaged with the calibrated embedding score when present; final = the content
    and profile (rule_score) scores weighted by SCORE_COMPONENT_WEIGHTS (calib `fit_weight` replaces the content
    weight under a low-data warning). No content (no JD or no model) = the profile score capped at NO_CONTENT_CAP.
    Then the tier cap, FLAG_PENALTY per flag, and the LLM blend. A hard reject is (0, 'none').
    """
    if rejected:
        return 0, "none"
    calib = calib or {}
    content = []
    if fit_prob is not None:
        content.append(100.0 * fit_prob)
    lo, hi = calib.get("embed_lo"), calib.get("embed_hi")
    if embed_sim is not None and lo is not None and hi is not None and hi > lo:
        content.append(100.0 * min(1.0, max(0.0, (embed_sim - lo) / (hi - lo))))
    profile_w = 1.0 - P.SCORE_COMPONENT_WEIGHTS["content"]
    content_w = calib["fit_weight"] if calib.get("fit_weight") is not None else P.SCORE_COMPONENT_WEIGHTS["content"]
    if content:
        final = (content_w * sum(content) / len(content) + profile_w * rule_score) / (content_w + profile_w)
    else:
        final = min(float(rule_score), float(P.NO_CONTENT_CAP))
    cap = P.TIER_CAP.get(tier)
    if cap is not None:
        final = min(final, cap)
    final = max(0.0, final - min(flags * P.FLAG_PENALTY, P.FLAG_PENALTY_CAP))
    if llm_score is not None:
        final = (1 - P.LLM_BLEND) * final + P.LLM_BLEND * llm_score
    final = int(final + 0.5)
    return final, band_for(final)


def penalized_flags(flags: list) -> int:
    """Flags that cost points: every flag except UNPENALIZED_FLAG_PATTERNS (already priced by a component)."""
    patterns = [re.compile(p, re.I) for p in P.UNPENALIZED_FLAG_PATTERNS]
    return sum(1 for f in flags if not any(rx.search(f) for rx in patterns))


TITLE_REASONS = ("off-function title", "off-lane title")


def apply_content_gate(rec, fit_prob: Optional[float]) -> None:
    """With a content score, the JD decides function fit and the title stops rejecting: title reasons are
    dropped (an off-lane title stays visible as a flag), fit < FIT_REJECT rejects, fit < FIT_REVIEW flags.
    Without one (no JD or no model) the title gate stands. Recomputes the verdict."""
    if fit_prob is None:
        return
    off_lane = [r for r in rec.reasons if r.startswith("off-lane title")]
    rec.reasons = [r for r in rec.reasons if not r.startswith(TITLE_REASONS)]
    rec.flags.extend(r for r in off_lane if r not in rec.flags)
    if fit_prob < P.FIT_REJECT:
        rec.reasons.append(f"content does not fit (fit {fit_prob:.2f})")
    elif fit_prob < P.FIT_REVIEW:
        rec.flags.append(f"content fit borderline (fit {fit_prob:.2f})")
    rec.verdict = "reject" if rec.reasons else ("review" if rec.flags else "candidate")


# Shape of one batch passed to DuckDB as a single JSON string. Binding Python lists as parameters
# costs ~1 ms per element; one string parameter expanded with json_transform costs almost nothing.
_BATCH_SHAPE = json.dumps([{"posting_id": "VARCHAR", "verdict": "VARCHAR", "tier": "INTEGER", "rule_score": "INTEGER",
                            "fit_prob": "DOUBLE", "fit_process": "DOUBLE", "fit_technical": "DOUBLE",
                            "final_score": "INTEGER", "band": "VARCHAR", "reasons": "JSON",
                            "flags": "JSON", "top_terms": "JSON", "joined": "VARCHAR"}])
_BATCH_KEYS = ("posting_id", "verdict", "tier", "rule_score", "fit_prob", "fit_process", "fit_technical",
               "final_score", "band", "reasons", "flags", "top_terms")


def _write_batch(con, recs: list, rv: str, mv: str, now) -> None:
    """INSERT OR REPLACE the batch into screens and mirror it into postings.screen_* (one transaction)."""
    payload = json.dumps([{**{k: r.get(k) for k in _BATCH_KEYS}, "joined": "; ".join(r["reasons"] + r["flags"])}
                          for r in recs])
    con.execute("BEGIN")
    try:
        con.execute("CREATE OR REPLACE TEMP TABLE screen_batch AS "
                    "SELECT unnest(json_transform($1, $2), recursive := true)", [payload, _BATCH_SHAPE])
        con.execute("""
            INSERT OR REPLACE INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier,
                                            rule_score, fit_prob, fit_process, fit_technical, final_score, band,
                                            reasons, flags, top_terms)
            SELECT posting_id, $1, $2, $3, verdict, tier, rule_score, fit_prob, fit_process, fit_technical,
                   final_score, band, reasons, flags, top_terms
            FROM screen_batch""", [rv, mv, now])
        con.execute("""
            UPDATE postings SET screen_verdict = b.verdict, screen_score = b.final_score,
                                screen_reasons = b.joined, screened_at = $1
            FROM screen_batch b WHERE postings.posting_id = b.posting_id""", [now])
        con.execute("DROP TABLE screen_batch")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def model_scores(model: Optional[dict], rows: list) -> tuple:
    """(fit_probs, top_terms) per row; None for every row when there is no model or no JD text."""
    probs, terms = [None] * len(rows), [None] * len(rows)
    if model is None:
        return probs, terms
    from . import features
    idx = [i for i, r in enumerate(rows) if (r.get("description_text") or "").strip()]
    if idx:
        p, t = features.predict_with_terms(model, [features.doc_text(rows[i]["title"], rows[i]["description_text"],
                                                                     rows[i]["employer"]) for i in idx])
        for j, i in enumerate(idx):
            probs[i], terms[i] = p[j], t[j]
    return probs, terms


def lens_scores(lens_models: Optional[dict], rows: list) -> dict:
    """{lens: [prob per row]} for the per-lens fit models (sprint plan 18.8).

    These are a prediction, not a score input: nothing in `combine` reads them. They exist so every row in
    the corpus carries "which of his two lanes would call this strong", which is what the three report lists
    need for the rows the judge has never seen.

    Probabilities only -- no top terms. Explaining a score is the main model's job, and running the per-row
    term attribution for two more models triples the expensive half of the screen for output nobody reads.
    """
    out = {}
    if not lens_models:
        return out
    from . import features
    idx = [i for i, r in enumerate(rows) if (r.get("description_text") or "").strip()]
    texts = [features.doc_text(rows[i]["title"], rows[i]["description_text"], rows[i]["employer"]) for i in idx]
    for lens, model in lens_models.items():
        probs = [None] * len(rows)
        if idx:
            for j, prob in zip(idx, features.predict(model, texts)):
                probs[j] = prob
        out[lens] = probs
    return out


def screen(con, *, since=None, full: bool = False, limit=None, model=None, lens_models=None, encoder=None,
           log=print) -> dict:
    """Screens every row that needs it in 500-row transactions. Returns counts by verdict and band.

    `lens_models` ({lens: model}) adds fit_process / fit_technical alongside fit_prob; they are stored and
    reported only, and never move final_score."""
    t0, started = time.monotonic(), _now()
    rv, mv = version.rules_version(), (model or {}).get("version", "none")
    calib = {"fit_weight": (model or {}).get("fit_weight")}
    total = len(candidate_ids(con, rv, mv, since=since, limit=limit, full=full))
    verdicts, bands, done = Counter(), Counter(), 0
    for n_batch, rows in enumerate(iter_candidate_batches(con, rv, mv, since=since, limit=limit, full=full)):
        recs = []
        probs, terms = model_scores(model, rows)
        lens_probs = lens_scores(lens_models, rows)
        none_col = [None] * len(rows)
        for i, (row, fit_prob, top) in enumerate(zip(rows, probs, terms)):
            rec = rules.screen_row(row, rv)
            apply_content_gate(rec, fit_prob)
            final, band = combine(rec.rule_score, fit_prob, None, None, calib, tier=rec.tier,
                                  rejected=rec.verdict == "reject", flags=penalized_flags(rec.flags))
            recs.append({"posting_id": rec.posting_id, "verdict": rec.verdict, "tier": rec.tier,
                         "rule_score": rec.rule_score, "fit_prob": fit_prob,
                         "fit_process": lens_probs.get("process", none_col)[i],
                         "fit_technical": lens_probs.get("technical", none_col)[i],
                         "final_score": final, "band": band,
                         "reasons": rec.reasons, "flags": rec.flags, "top_terms": top})
            verdicts[rec.verdict] += 1
            bands[band] += 1
        if recs:
            _write_batch(con, recs, rv, mv, _now())
        done += len(rows)
        if n_batch % 20 == 19:
            log(f"  screened {done}/{total} ({time.monotonic() - t0:.0f}s)")
    stats = {"screened": done, "started": started, "seconds": round(time.monotonic() - t0, 1),
             "verdict": dict(verdicts), "band": dict(bands), "rules_version": rv, "model_version": mv,
             "lens_models": {k: v.get("version") for k, v in (lens_models or {}).items()}}
    log(f"Screen: {done} rows in {stats['seconds']}s — " +
        ", ".join(f"{k} {v}" for k, v in sorted(verdicts.items())) + " | bands " +
        ", ".join(f"{k} {v}" for k, v in sorted(bands.items())) + f" | rules {rv} · model {mv}")
    return stats


def coverage_stage(con, *, since=None, log=print) -> Optional[dict]:
    """Coverage for survivors missing it, when an embedding library, the evidence manifest and built evidence exist;
    otherwise one log line saying why it was skipped. Never raises: the sweep must finish without it."""
    from . import embed, evidence
    t = time.monotonic()
    path = evidence.manifest_path()
    if not path.exists():
        log(f"Coverage: skipped (no evidence manifest at {path.name})")
        return None
    if embed.available() is None:
        log("Coverage: skipped (no embedding library installed)")
        return None
    try:
        manifest = evidence.load_manifest(str(path))
        if evidence.stored_version(con, manifest.embed_model) is None:
            log("Coverage: skipped (evidence not built; run `finder.py evidence --rebuild`)")
            return None
        from . import coverage
        stats = coverage.cover(con, manifest, embed.load_encoder(), since=since, log=log)
        log(f"Coverage stage: {stats['covered']} postings ({time.monotonic() - t:.1f}s)")
        return stats
    except Exception as exc:  # logged, never fatal to the sweep
        log(f"Coverage: failed ({type(exc).__name__}: {exc}); continuing without it")
        return None


def daily(con, *, since, vault_dir: Optional[str], llm_top: int = 0, report: bool = True, full: bool = False,
          use_model: bool = True, use_coverage: bool = True, log=print) -> dict:
    """tracker sync -> decision read-back -> screen -> (LLM) -> Jobs_Found -> snapshots, one log line per stage.
    The newest trained fit model is used when one exists and scikit-learn is installed."""
    out = {}
    vault_dir = os.path.expanduser(vault_dir) if vault_dir else None
    if vault_dir:
        t = time.monotonic()
        out["tracker"] = tracker_sync.sync(con, vault_dir, log=log)
        log(f"Tracker sync: {out['tracker']} ({time.monotonic() - t:.1f}s)")
        t = time.monotonic()
        out["read_back"] = report_mod.read_back(con, vault_dir)
        log(f"Decision read-back: {out['read_back']} new decisions ({time.monotonic() - t:.1f}s)")
    model, lens_models = None, {}
    if use_model:
        from . import features
        model = features.load_latest(con, log=log)
        lens_models = features.load_lens_models(con, log=log)
    out["screen"] = screen(con, since=since, full=full, model=model, lens_models=lens_models, log=log)
    if use_coverage:
        out["coverage"] = coverage_stage(con, since=None if full else since, log=log)
    if llm_top:
        log("LLM stage: not built yet (Phase 4); skipped.")
    if report and vault_dir:
        t = time.monotonic()
        meta = {"since": since, "screen": out["screen"], "tracker": out.get("tracker"),
                "read_back": out.get("read_back"), "stages": {"model": model is not None, "embed": bool(out.get("coverage")), "llm": False}}
        out["report"] = str(report_mod.write_jobs_found(con, vault_dir, meta))
        log(f"Report: {out['report']} ({time.monotonic() - t:.1f}s)")
    t = time.monotonic()
    out["snapshots"] = [str(p) for p in report_mod.snapshots(con)]
    log(f"Snapshots: {len(out['snapshots'])} files ({time.monotonic() - t:.1f}s)")
    return out
