"""The weekly retrain command (sprint plan §24): trains every step-4 model in order, gates each one on
employer-grouped held-out AUC vs the previous PROMOTED run, a shuffled-label leak check, and a label-count
floor, and only rescreens / recomputes coverage / rescores required-embed when at least one model was
promoted. Ledger: `model_runs` (schema v17). `finder.py retrain --history` reads it back.

Candidate-vs-live artifacts: `features.train()` already writes its joblib under `model_dir` (default
MODEL_DIR) and, when `insert_row=True` (the default, still used by `finder.py train`), inserts its `models`
row immediately. This module is the first caller that ever passes `insert_row=False`: it writes the
candidate joblib into a tempdir created UNDER MODEL_DIR (so promotion is an atomic `os.replace`, never a
copy across filesystems) and withholds the `models` row until the gate passes -- see `features.write_model_row`
and `features.promote_artifact`. A failing candidate's file is simply deleted; the live artifact and its row
are never touched, so a bad retrain cannot demote a good one.

`required_embed.train()` is different: it already has its OWN acceptance gate (AUC >= 0.71, lift over
TF-IDF-alone >= 0.05, shuffled end-to-end sanity AUC <= 0.60 -- see the "ACCEPTANCE GATE" block in
required_embed.py) and only writes its joblib / `models` row when that gate passes. It has no candidate-path
mode, so this module does not re-wrap it in a second, possibly-conflicting gate: it reuses `result["accepted"]`
as the promoted answer and `result["auc_stack_B"]` / `result["shuffled_auc"]` for the ledger row. Because it
can only ever write when accepted, `--dry-run` does not call it at all (there is no way to "evaluate without
writing" it) -- see `_train_required_embed`'s docstring.
"""
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import features

# The five step-4 TF-IDF models (sprint plan §22.1 step 4 / §24): the three lenses plus the two
# ranking-only models `required` and `bullseye` (features.REQUIRED_MODEL / features.BULLSEYE_MODEL).
LENS_MODELS = ("process", "technical", "ai", "required", "bullseye")

SHUFFLE_LOW, SHUFFLE_HIGH = 0.40, 0.60   # leak-check band for the shuffled-label AUC (§24)
AUC_DROP_TOLERANCE = 0.02                # a candidate may trail the previous promoted AUC by at most this much
STALE_DAYS = 7                           # sweep_ats.py's reminder trigger #1
STALE_LABEL_COUNT = 25                   # sweep_ats.py's reminder trigger #2

# Same employer-grouped-AUC pipeline params `finder.py train --lens bullseye`'s `_bullseye_gate` already
# uses -- kept identical rather than plumbing a second set of CLI flags through for a command that runs all
# five models in one pass.
_EMPLOYER_GATE_KWARGS = dict(C=4.0, min_df=3, ngram=(1, 2), max_features=50_000, max_df=0.5)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _quiet(*_a, **_k):
    pass


def _previous_promoted(con, model: str) -> Optional[dict]:
    row = con.execute("SELECT auc, n_pos, n_neg FROM model_runs WHERE model = ? AND promoted "
                      "ORDER BY trained_at DESC LIMIT 1", [model]).fetchone()
    return {"auc": row[0], "n_pos": row[1], "n_neg": row[2]} if row else None


def _insert_ledger(con, *, model: str, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted: bool, reason: str,
                   model_version) -> None:
    con.execute("INSERT INTO model_runs (run_id, model, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted, "
                "reason, model_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [uuid.uuid4().hex, model, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted, reason,
                 model_version])


def _gate(con, model: str, n_pos: int, n_neg: int, emp_auc, shuf_auc) -> tuple:
    """(promoted, reason) against the sprint plan §24 gate: employer-grouped AUC no more than
    AUC_DROP_TOLERANCE below the previous PROMOTED run's AUC (no previous promoted run = promote outright),
    shuffled-label AUC inside [SHUFFLE_LOW, SHUFFLE_HIGH] (a leak check -- a real score there means the
    candidate is reading something it should not), and the label count (n_pos + n_neg) not falling versus
    that same previous run."""
    if emp_auc is None:
        return False, "employer-grouped AUC unavailable (too few labeled rows carry a posting_id)"
    if shuf_auc is None:
        return False, "shuffled-label AUC unavailable (too few labeled rows carry a posting_id)"
    if not (SHUFFLE_LOW <= shuf_auc <= SHUFFLE_HIGH):
        return False, f"shuffled-label AUC {shuf_auc:.3f} outside [{SHUFFLE_LOW}, {SHUFFLE_HIGH}] (leak check)"
    prev = _previous_promoted(con, model)
    if prev is None:
        return True, f"promoted (first run in the ledger; AUC {emp_auc:.3f})"
    if emp_auc < prev["auc"] - AUC_DROP_TOLERANCE:
        return False, (f"employer-grouped AUC {emp_auc:.3f} < previous promoted {prev['auc']:.3f} - "
                       f"{AUC_DROP_TOLERANCE}")
    prev_n = prev["n_pos"] + prev["n_neg"]
    if (n_pos + n_neg) < prev_n:
        return False, f"label count {n_pos + n_neg} fell versus previous promoted {prev_n}"
    return True, f"promoted (AUC {emp_auc:.3f} vs previous promoted {prev['auc']:.3f})"


def _train_one(con, model: str, *, dry_run: bool, log=print) -> dict:
    """Trains one of the five step-4 TF-IDF models as a CANDIDATE and either promotes it (atomic rename into
    MODEL_DIR + the deferred `models` row) or discards the candidate file. Always writes one `model_runs`
    ledger row, promoted or not."""
    os.makedirs(features.MODEL_DIR, exist_ok=True)
    tmp_dir = tempfile.mkdtemp(dir=features.MODEL_DIR, prefix=f"candidate_{model}_")
    try:
        result = features.train(con, lens=model, model_dir=tmp_dir, insert_row=False, log=log)
    except ValueError as e:   # not enough labels -- features.train's own message names the counts
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log(f"retrain {model}: SKIPPED -- {e}")
        _insert_ledger(con, model=model, trained_at=_now(), n_pos=None, n_neg=None, auc=None, shuffle_auc=None,
                      promoted=False, reason=str(e), model_version=None)
        return {"model": model, "promoted": False, "reason": str(e)}

    rows = result["rows"]
    texts = [features.doc_text(r["title"], r["text"], r["company"]) for r in rows]
    y = [int(r["label"]) for r in rows]
    weights = [float(r["weight"] or 1.0) for r in rows]
    emp_auc = features.employer_grouped_auc(con, rows, texts, y, weights, log=_quiet, **_EMPLOYER_GATE_KWARGS)
    shuf_auc = features.employer_grouped_auc(con, rows, texts, y, weights, shuffle_labels=True, log=_quiet,
                                             **_EMPLOYER_GATE_KWARGS)
    n_pos, n_neg = result["n_pos"], result["n_neg"]

    if dry_run:
        promoted, reason = False, "dry-run"
    else:
        promoted, reason = _gate(con, model, n_pos, n_neg, emp_auc, shuf_auc)

    if promoted:
        notes = dict(result["notes"])
        notes["auc_employer_grouped"] = emp_auc
        notes["auc_employer_grouped_shuffled"] = shuf_auc
        stored_path = features.promote_artifact(result["path"], model_dir=features.MODEL_DIR)
        features.write_model_row(con, version=result["model_version"], kind=result["kind"],
                                 trained_at=result["trained_at"], n_pos=n_pos, n_neg=n_neg,
                                 cv_auc=result["cv_auc"], cv_precision_at_20=result["cv_precision_at_20"],
                                 stored_path=stored_path, notes=notes)
    elif os.path.exists(result["path"]):
        os.remove(result["path"])
    shutil.rmtree(tmp_dir, ignore_errors=True)

    log(f"retrain {model}: emp_auc={emp_auc} shuffle_auc={shuf_auc} n_pos={n_pos} n_neg={n_neg} -> "
        f"{'PROMOTED' if promoted else 'kept previous artifact'} ({reason})")
    _insert_ledger(con, model=model, trained_at=result["trained_at"], n_pos=n_pos, n_neg=n_neg, auc=emp_auc,
                   shuffle_auc=shuf_auc, promoted=promoted, reason=reason, model_version=result["model_version"])
    return {"model": model, "promoted": promoted, "reason": reason, "auc": emp_auc, "shuffle_auc": shuf_auc}


def _train_required_embed(con, *, dry_run: bool, log=print) -> dict:
    """required_embed.train() gates itself and only persists a model when its OWN gate passes (see this
    module's docstring), so there is nothing for `--dry-run` to safely evaluate without risking the exact
    write dry-run must not make -- it is skipped outright and the ledger row says so."""
    model = "required_embed"
    if dry_run:
        log(f"retrain {model}: SKIPPED in --dry-run -- required_embed.train() has no candidate/no-write mode")
        _insert_ledger(con, model=model, trained_at=_now(), n_pos=None, n_neg=None, auc=None, shuffle_auc=None,
                      promoted=False, reason="dry-run", model_version=None)
        return {"model": model, "promoted": False, "reason": "dry-run"}
    from . import required_embed
    result = required_embed.train(con, log=log)
    promoted = bool(result.get("accepted"))
    reason = (f"promoted (required_embed's own gate passed: stack AUC {result.get('auc_stack_B')})" if promoted
             else f"required_embed's own gate FAILED: stack AUC {result.get('auc_stack_B')}, "
                  f"shuffled sanity {result.get('shuffled_auc')}, shuffle_leak={result.get('shuffle_leak')}")
    log(f"retrain {model}: {reason}")
    _insert_ledger(con, model=model, trained_at=_now(), n_pos=result.get("n_pos"), n_neg=result.get("n_neg"),
                   auc=result.get("auc_stack_B"), shuffle_auc=result.get("shuffled_auc"), promoted=promoted,
                   reason=reason, model_version=result.get("model_version"))
    return {"model": model, "promoted": promoted, "reason": reason}


def run(con, *, dry_run: bool = False, log=print) -> list:
    """labels -> train each of the five step-4 models -> required-embed train -> (only if at least one
    model was promoted) rescreen-all, coverage, required-embed score. Returns one result dict per model."""
    vault_dir = os.getenv("JOBSEARCH_VAULT_DIR")
    if vault_dir:
        from . import labels as labels_mod
        labels_mod.sync_labels(con, vault_dir, log=log)
    else:
        log("retrain: JOBSEARCH_VAULT_DIR not set -- skipping `labels` (label_docs left as-is)")

    results = [_train_one(con, model, dry_run=dry_run, log=log) for model in LENS_MODELS]
    results.append(_train_required_embed(con, dry_run=dry_run, log=log))

    if dry_run:
        log("retrain: --dry-run -- promoting nothing, rescreening nothing")
        return results
    if any(r["promoted"] for r in results):
        _rescreen_and_rescore(con, log=log)
    else:
        log("retrain: no model promoted -- skipping rescreen-all / coverage / required-embed score")
    return results


def _rescreen_and_rescore(con, log=print) -> None:
    from . import pipeline
    pipeline.screen(con, full=True, model=features.load_latest(con), lens_models=features.load_lens_models(con),
                    required_model=features.load_required_model(con),
                    bullseye_model=features.load_bullseye_model(con))
    _run_coverage(con, log=log)
    from . import required_embed
    log(required_embed.score(con, all_rows=False, log=log))


def _run_coverage(con, log=print) -> None:
    from . import coverage, embed, evidence
    path = evidence.manifest_path(None)
    if not path.exists():
        log("retrain: no evidence manifest -- skipping coverage")
        return
    manifest = evidence.load_manifest(str(path))
    encoder = embed.load_encoder(model_name=manifest.embed_model)
    coverage.cover(con, manifest, encoder)


def history(con) -> list:
    """model_runs, newest first -- `finder.py retrain --history`."""
    return con.execute("SELECT run_id, model, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted, reason, "
                       "model_version FROM model_runs ORDER BY trained_at DESC").fetchall()


def print_history(con, log=print) -> None:
    rows = history(con)
    if not rows:
        log("retrain --history: no runs recorded yet")
        return
    log(f"{'trained_at':<20} {'model':<14} {'n_pos':>5} {'n_neg':>5} {'auc':>6} {'shuf':>6} {'promoted':<9} reason")
    for _run_id, model, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted, reason, _model_version in rows:
        log(f"{str(trained_at)[:19]:<20} {model:<14} {n_pos if n_pos is not None else '-':>5} "
            f"{n_neg if n_neg is not None else '-':>5} "
            f"{'-' if auc is None else round(auc, 3):>6} {'-' if shuffle_auc is None else round(shuffle_auc, 3):>6} "
            f"{str(promoted):<9} {reason}")


def staleness_reminder(con, log=print, stale_days: int = STALE_DAYS, label_threshold: int = STALE_LABEL_COUNT) -> None:
    """One line, called from `sweep_ats.py`'s pipeline stage, when the newest PROMOTED run is more than
    `stale_days` old or `label_threshold`+ new human labels (report_feedback rows with assessor='user', or
    llm_labels rows with scorer='user-adjudicated') have arrived since it trained -- silent otherwise. Never
    raises: every query degrades to 'never trained' rather than erroring, and the caller wraps this call too."""
    row = con.execute("SELECT trained_at FROM model_runs WHERE promoted "
                      "ORDER BY trained_at DESC LIMIT 1").fetchone()
    if not row:
        log("RETRAIN REMINDER: no promoted retrain on record yet -- run `finder.py retrain`.")
        return
    trained_at = row[0]
    age_days = (_now() - trained_at).days
    n_new_labels = con.execute(
        "SELECT (SELECT count(*) FROM report_feedback WHERE assessor = 'user' AND assessed_at > ?) "
        "+ (SELECT count(*) FROM llm_labels WHERE scorer = 'user-adjudicated' AND judged_at > ?)",
        [trained_at, trained_at]).fetchone()[0]
    reasons = []
    if age_days > stale_days:
        reasons.append(f"newest promoted retrain is {age_days}d old (> {stale_days}d)")
    if n_new_labels >= label_threshold:
        reasons.append(f"{n_new_labels} new human labels since the last promoted retrain (>= {label_threshold})")
    if reasons:
        log("RETRAIN REMINDER: " + "; ".join(reasons) + " -- run `finder.py retrain`.")
