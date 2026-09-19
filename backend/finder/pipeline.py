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
WHERE p.status = 'active' AND (s.posting_id IS NULL OR s.rules_version IS DISTINCT FROM ?
      OR s.model_version IS DISTINCT FROM ? OR s.screened_at < p.description_fetched_at)"""


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


def content_fit(fit_prob: Optional[float], lens_fits) -> Optional[float]:
    """The ONE content score that decides what is shown: the BEST of the lens models, never an average.

    The main model trains on the averaged process / technical grade, so a posting that is `bullseye` on one
    lens and `wrong` on the other is a NEGATIVE to it -- yet that is exactly the single-lens role the lenses
    exist to surface, and an applied-AI fit of 0.80 is worth reading whatever the other two say. Gating and
    ranking on the main model alone rejected such rows before the judge ever saw them (measured 2026-09-18:
    261 active postings with a lens fit >= 0.50 were rejected on main fit < FIT_REJECT, and of the 182 already
    judged, 60% sat in the apply or review tier against ~23% overall). With no lens score at all (no lens
    models, or none for this row) the main model stands in.
    """
    lens = [x for x in lens_fits if x is not None]
    return max(lens) if lens else fit_prob


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
                            "fit_ai": "DOUBLE", "fit_required": "DOUBLE", "fit_bullseye": "DOUBLE",
                            "level_fit": "VARCHAR",
                            "final_score": "INTEGER", "band": "VARCHAR", "reasons": "JSON",
                            "flags": "JSON", "top_terms": "JSON", "joined": "VARCHAR"}])
_BATCH_KEYS = ("posting_id", "verdict", "tier", "rule_score", "fit_prob", "fit_process", "fit_technical",
               "fit_ai", "fit_required", "fit_bullseye", "level_fit", "final_score", "band", "reasons", "flags",
               "top_terms")


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
                                            rule_score, fit_prob, fit_process, fit_technical, fit_ai, fit_required,
                                            fit_bullseye, level_fit, final_score, band, reasons, flags, top_terms)
            SELECT posting_id, $1, $2, $3, verdict, tier, rule_score, fit_prob, fit_process, fit_technical,
                   fit_ai, fit_required, fit_bullseye, level_fit, final_score, band, reasons, flags, top_terms
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


def required_scores(required_model: Optional[dict], rows: list) -> list:
    """[prob per row] from the Required-block ranking model (features.train(lens=features.REQUIRED_MODEL)).

    NOT a lens: this is a ranking signal only, stored as screens.fit_required and read by `required_value()` /
    `breadth_x_required` in SQL and by the judge queue ordering -- it must never be passed to `content_fit`,
    `combine`, or folded into `lens_scores`'s dict (which is what feeds gating and n_lenses_good-style counts).
    Kept as its own function, not a `lens_scores` entry, for that reason. No top terms, same rationale as
    `lens_scores`: explaining a score is the main model's job."""
    probs = [None] * len(rows)
    if required_model is None:
        return probs
    from . import features
    idx = [i for i, r in enumerate(rows) if (r.get("description_text") or "").strip()]
    if idx:
        texts = [features.doc_text(rows[i]["title"], rows[i]["description_text"], rows[i]["employer"]) for i in idx]
        for j, prob in zip(idx, features.predict(required_model, texts)):
            probs[j] = prob
    return probs


def bullseye_scores(bullseye_model: Optional[dict], rows: list) -> list:
    """[prob per row] from the `bullseye` model (features.train(lens=features.BULLSEYE_MODEL)).

    NOT a lens: same rationale as required_scores -- a ranking signal only, stored as screens.fit_bullseye and
    read by the rank feed's rank_lens_best term in SQL. Must never be passed to `content_fit`, `combine`, or
    folded into `lens_scores`'s dict. No top terms, same rationale as `lens_scores` / `required_scores`."""
    probs = [None] * len(rows)
    if bullseye_model is None:
        return probs
    from . import features
    idx = [i for i, r in enumerate(rows) if (r.get("description_text") or "").strip()]
    if idx:
        texts = [features.doc_text(rows[i]["title"], rows[i]["description_text"], rows[i]["employer"]) for i in idx]
        for j, prob in zip(idx, features.predict(bullseye_model, texts)):
            probs[j] = prob
    return probs


def screen(con, *, since=None, full: bool = False, limit=None, model=None, lens_models=None, required_model=None,
           bullseye_model=None, encoder=None, log=print) -> dict:
    """Screens every row that needs it in 500-row transactions. Returns counts by verdict and band.

    `lens_models` ({lens: model}) adds fit_process / fit_technical / fit_ai alongside fit_prob, and the BEST of
    them is the content score that gates the verdict and feeds final_score (`content_fit`); `fit_prob`, the
    averaged-grade model, is still stored but decides nothing once a lens score exists. `level_fit` (20.2) comes from the level rule inside
    `rules.screen_row` and is likewise stored and reported only -- it never changes verdict or rule_score.
    `required_model` (features.REQUIRED_MODEL) adds fit_required, stored and reported only -- NOT a lens, it
    never reaches `content_fit`/`combine` and never moves verdict, tier or final_score. `bullseye_model`
    (features.BULLSEYE_MODEL) adds fit_bullseye, same rules as required_model."""
    t0, started = time.monotonic(), _now()
    rv, mv = version.rules_version(), (model or {}).get("version", "none")
    calib = {"fit_weight": (model or {}).get("fit_weight")}
    total = len(candidate_ids(con, rv, mv, since=since, limit=limit, full=full))
    verdicts, bands, done = Counter(), Counter(), 0
    for n_batch, rows in enumerate(iter_candidate_batches(con, rv, mv, since=since, limit=limit, full=full)):
        recs = []
        probs, terms = model_scores(model, rows)
        lens_probs = lens_scores(lens_models, rows)
        required_probs = required_scores(required_model, rows)
        bullseye_probs = bullseye_scores(bullseye_model, rows)
        none_col = [None] * len(rows)
        for i, (row, fit_prob, top) in enumerate(zip(rows, probs, terms)):
            rec = rules.screen_row(row)
            shown_fit = content_fit(fit_prob, [lens_probs.get(k, none_col)[i] for k in ("process", "technical", "ai")])
            apply_content_gate(rec, shown_fit)
            final, band = combine(rec.rule_score, shown_fit, None, None, calib, tier=rec.tier,
                                  rejected=rec.verdict == "reject", flags=penalized_flags(rec.flags))
            recs.append({"posting_id": rec.posting_id, "verdict": rec.verdict, "tier": rec.tier,
                         "rule_score": rec.rule_score, "fit_prob": fit_prob,
                         "fit_process": lens_probs.get("process", none_col)[i],
                         "fit_technical": lens_probs.get("technical", none_col)[i],
                         "fit_ai": lens_probs.get("ai", none_col)[i],
                         # NOT a lens (features.REQUIRED_MODEL) -- stored and reported only, see required_scores.
                         "fit_required": required_probs[i],
                         # NOT a lens (features.BULLSEYE_MODEL) -- stored and reported only, see bullseye_scores.
                         "fit_bullseye": bullseye_probs[i],
                         # Agent A's level rule writes this note; until it lands rec.notes has no "level_fit"
                         # key and .get() returns None, same as an unscreened lens probability.
                         "level_fit": rec.notes.get("level_fit"),
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
             "lens_models": {k: v.get("version") for k, v in (lens_models or {}).items()},
             "required_model": (required_model or {}).get("version"),
             "bullseye_model": (bullseye_model or {}).get("version")}
    log(f"Screen: {done} rows in {stats['seconds']}s — " +
        ", ".join(f"{k} {v}" for k, v in sorted(verdicts.items())) + " | bands " +
        ", ".join(f"{k} {v}" for k, v in sorted(bands.items())) + f" | rules {rv} · model {mv}")
    return stats


def bullseye_backfill(con, bullseye_model, *, log=print) -> dict:
    """One-time UPDATE of `screens.fit_bullseye` on the LATEST screens row only, for the population no
    rescreen can reach yet: active, non-rejected postings whose best TF-IDF lens probability or `lens_best`
    (vw_lens_fit) is >= 0.5. `vw_screen_latest` picks the newest screens row per posting_id by screened_at;
    this UPDATEs exactly that (posting_id, rules_version, model_version) triple and nothing else -- it never
    inserts a new screens row, never touches verdict/final_score/any other column, and never touches a row
    outside this population. Meant to run once after `finder.py train --lens bullseye`, before the orchestrator's
    single rescreen makes it unnecessary going forward."""
    if bullseye_model is None:
        log("Bullseye backfill: skipped (no trained bullseye model; run `finder.py train --lens bullseye`)")
        return {"updated": 0, "candidates": 0}
    rows = con.execute("""
        SELECT v.posting_id, p.title, p.description_text, p.employer, s.rules_version, s.model_version
        FROM vw_lens_fit v
        JOIN postings p USING (posting_id)
        JOIN vw_screen_latest s USING (posting_id)
        WHERE v.verdict != 'reject'
          AND (greatest(coalesce(v.fit_process, 0), coalesce(v.fit_technical, 0), coalesce(v.fit_ai, 0)) >= 0.5
               OR v.lens_best >= 0.5)
    """).fetchall()
    from . import features
    idx = [i for i, r in enumerate(rows) if (r[2] or "").strip()]
    if not idx:
        log(f"Bullseye backfill: {len(rows)} candidates, none with JD text")
        return {"updated": 0, "candidates": len(rows)}
    texts = [features.doc_text(rows[i][1], rows[i][2], rows[i][3]) for i in idx]
    probs = features.predict(bullseye_model, texts)
    con.execute("BEGIN")
    try:
        for i, prob in zip(idx, probs):
            pid, _, _, _, rv, mv = rows[i]
            con.execute("UPDATE screens SET fit_bullseye = ? WHERE posting_id = ? AND rules_version = ? "
                       "AND model_version = ?", [prob, pid, rv, mv])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    log(f"Bullseye backfill: {len(idx)} of {len(rows)} candidates updated (rest had no JD text)")
    return {"updated": len(idx), "candidates": len(rows)}


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
        stats = coverage.cover(con, manifest, embed.load_encoder(model_name=manifest.embed_model), since=since, log=log)
        log(f"Coverage stage: {stats['covered']} postings ({time.monotonic() - t:.1f}s)")
        return stats
    except Exception as exc:  # logged, never fatal to the sweep
        log(f"Coverage: failed ({type(exc).__name__}: {exc}); continuing without it")
        return None


def required_embed_stage(con, *, log=print) -> Optional[dict]:
    """The "second layer" stack (backend/finder/required_embed.py) scoring pass, run AFTER coverage_stage
    because it depends on `requirement_units`. Never fatal: no trained model, no encoder library, or any other
    failure prints one log line and the sweep continues, same pattern as coverage_stage."""
    t = time.monotonic()
    try:
        from . import required_embed
        if required_embed.load_latest(con, log=log) is None:
            log("Required-embed: skipped (no trained model; run `finder.py required-embed train`)")
            return None
        stats = required_embed.score(con, log=log)
        log(f"Required-embed stage: {stats.get('scored', 0)} postings ({time.monotonic() - t:.1f}s)")
        return stats
    except Exception as exc:  # logged, never fatal to the sweep
        log(f"Required-embed: failed ({type(exc).__name__}: {exc}); continuing without it")
        return None


def judge2_stage(con, *, top_n: int = 0, log=print) -> Optional[dict]:
    """The LLM second judge (backend/finder/judge2.py, sprint plan §25), wired into the dead `llm_top` hook.
    Same "logged, never fatal to the sweep" pattern as coverage_stage / required_embed_stage above, PLUS an
    explicit live-call gate: without `JUDGE2_LIVE_OK=1` in the environment, this logs one line and skips --
    it never makes the first live call on its own, however the pipeline is scheduled. `top_n <= 0` (the
    `llm_top` default) also skips outright, same as the old "LLM stage: not built yet" line did."""
    import os
    if top_n <= 0:
        return None
    if not os.environ.get("JUDGE2_LIVE_OK"):
        log("Judge2: skipped (JUDGE2_LIVE_OK not set -- the second judge never runs unscheduled; "
           "run `finder.py judge2 run --dry-run` first, then set JUDGE2_LIVE_OK=1 to allow a live run).")
        return None
    t = time.monotonic()
    try:
        from . import judge2
        # JUDGE2_BACKGROUND = public | file: the SAME background the evaluated prompt_version used, or the bar
        # that was passed says nothing about these reviews (prompt_version hashes the background text).
        result = judge2.run(con, top_n=top_n, i_have_approval=True,
                            background=os.environ.get("JUDGE2_BACKGROUND", "public"), log=log)
        log(f"Judge2 stage: {result.get('reviewed', 0)} reviewed ({time.monotonic() - t:.1f}s)")
        return result
    except Exception as exc:  # logged, never fatal to the sweep
        log(f"Judge2: failed ({type(exc).__name__}: {exc}); continuing without it")
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
    model, lens_models, required_model, bullseye_model = None, {}, None, None
    if use_model:
        from . import features
        model = features.load_latest(con, log=log)
        lens_models = features.load_lens_models(con, log=log)
        required_model = features.load_required_model(con, log=log)   # NOT a lens; see features.REQUIRED_MODEL
        bullseye_model = features.load_bullseye_model(con, log=log)   # NOT a lens; see features.BULLSEYE_MODEL
    out["screen"] = screen(con, since=since, full=full, model=model, lens_models=lens_models,
                           required_model=required_model, bullseye_model=bullseye_model, log=log)
    if use_coverage:
        out["coverage"] = coverage_stage(con, since=None if full else since, log=log)
        out["required_embed"] = required_embed_stage(con, log=log)
    if llm_top:
        out["judge2"] = judge2_stage(con, top_n=llm_top, log=log)
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
