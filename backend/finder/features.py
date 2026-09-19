"""The fit model: TF-IDF + logistic regression trained on the user's own labels (`vw_label_set`).

Positives are JDs the user took far enough to save (applications, escalated blocks, build decisions) plus the
postings the judge graded `bullseye` / `adjacent`; negatives are the judge's `wrong` / `stretch` grades and
pseudo-negatives sampled from the corpus. Nothing the user read is a negative: a pass on a saved JD was a
doubt about nuance, not about its language.

The graded labels (sprint plan section 17) are why the negatives are worth anything. Trained on random
pseudo-negatives alone the model could not tell a bullseye from a senior generalist that shares its
vocabulary, because it had never seen one; `wrong` and `stretch` rows from the confusable band are exactly
that missing evidence. Grades are weighted, not thresholded -- bullseye 1.0, adjacent 0.6, stretch 0.5,
wrong 1.0 -- so the uncertain middle informs the fit without being asserted as a hard label.

The model sees function and content only:
employer names, digits, level words, logistics and career-site boilerplate are removed (MODEL_STOP_WORDS),
and terms in more than half the documents are dropped, so it cannot learn level or page layout.

scikit-learn, numpy and joblib are imported inside the functions, so importing this module
costs nothing and the sweep runs without them. `load_latest` returns None (rules only) when no
model has been trained or the libraries are missing.
"""
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Optional

import re

from backend import profile as P
from backend.ats import store
from backend.screen import company_keys, company_matches, norm_company, similar_title

from .labels import strip_boilerplate

MODEL_DIR = os.path.join(os.path.dirname(store.DEFAULT_DB_PATH), "models")
# Mirrors store.DEFAULT_DB_PATH's own derivation (three dirname calls off this file, same nesting depth as
# backend/ats/store.py) so a `models.path` row can be stored relative to it and stay portable across machines --
# an absolute path baked in from a deleted drive (e.g. /mnt/e/code/jobsearch/...) is exactly the bug this fixes.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOW_DATA_MIN = 150          # positives or pseudo-negatives with text below this = low-data warning
LOW_DATA_FIT_WEIGHT = 0.15  # pipeline.combine weight for `fit` under the warning
FEATURE_VERSION = "4"       # bump when doc_text / vectorizer settings change (part of model_version)
TRAIN_EXCLUDED_SOURCES = ("jobs_found_passed",)   # context only: a JD that reached the vault is never a negative
TOKEN_PATTERN = r"(?u)\b[^\W\d_]{2,}\b"          # letters only: no years, percentages or req numbers
# When one job appears under several sources, the first source in this order supplies its label. The user's own
# adjudication outranks everything; the vault outranks the judge (sprint plan section 17, STATUS decisions 3-4).
SOURCE_PRIORITY = ("user_adjudicated", "application", "decision", "jobs_found_escalated", "llm_judge",
                   "jobs_found_passed", "pseudo_neg")
GRADED_SOURCES = ("user_adjudicated", "llm_judge")
# Sources deduped against higher-priority rows by company + similar title, not only by posting_id: one job can
# reach the corpus as several requisitions. Graded rows are never matched against each other -- an employer's
# boilerplate makes unrelated roles look alike (section 17.2), and collapsing them would throw labels away.
FUZZY_DEDUPED_SOURCES = ("decision", "llm_judge")
FUZZY_DEDUPE_KEYS_FROM = ("application", "decision", "jobs_found_escalated", "jobs_found_passed",
                          "user_adjudicated")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def strip_employer(text: str, company: Optional[str]) -> str:
    """Removes the employer's name (and any parenthetical alias) so the model cannot learn employers."""
    names = {n for n in [company or "", *company_keys(company or "")] if len(n.strip()) >= 3}
    for name in sorted(names, key=len, reverse=True):
        words = r"\W+".join(re.escape(w) for w in re.split(r"\W+", name.strip()) if w)
        if words:
            text = re.sub(rf"(?<![A-Za-z0-9]){words}(?![A-Za-z0-9])", " ", text, flags=re.I)
    return text


def doc_text(title: Optional[str], text: Optional[str], company: Optional[str] = None) -> str:
    """Title twice (it carries the function) plus the JD without boilerplate or the employer's name."""
    title = title or ""
    return strip_employer(f"{title}\n{title}\n{strip_boilerplate(text or '')}", company)


LENS_VIEWS = {None: "vw_label_set", "process": "vw_label_set_process", "technical": "vw_label_set_technical",
             "ai": "vw_label_set_ai", "required": "vw_label_set_required"}


def training_set(con, lens=None) -> list:
    """vw_label_set rows as dicts, one per job: postings in `training_exclusions` dropped outright, then
    deduped on posting_id and on company + similar title (FUZZY_DEDUPED_SOURCES), by SOURCE_PRIORITY.

    `lens` selects the per-lens view (sprint plan 18.8); None keeps the averaged-grade set. `lens="required"`
    is NOT a lens -- see REQUIRED_MODEL -- but reads through this same generic view-lookup mechanism, since it
    is just another named training set."""
    if lens not in LENS_VIEWS:
        raise ValueError(f"unknown lens {lens!r}; expected one of {sorted(k for k in LENS_VIEWS if k)}")
    cols = ("label_id", "source", "posting_id", "company", "title", "text", "label", "weight", "grade")
    rows = [dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM {LENS_VIEWS[lens]}").fetchall()]
    excluded = {pid for (pid,) in con.execute("SELECT posting_id FROM training_exclusions").fetchall()}
    rows = [r for r in rows if r["source"] not in TRAIN_EXCLUDED_SOURCES
            and not (r["source"] == "decision" and r["label"] == 0)
            and r["posting_id"] not in excluded]
    rank = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    rows.sort(key=lambda r: (rank.get(r["source"], len(rank)), r["label_id"]))
    kept, seen_pids, by_first_word = [], set(), defaultdict(list)
    for r in rows:
        if r["posting_id"] and r["posting_id"] in seen_pids:
            continue
        if r["source"] in FUZZY_DEDUPED_SOURCES:
            keys = company_keys(r["company"] or "")
            bucket = {k.split()[0] for k in keys if k}
            if any(company_matches(norm_company(o["company"] or ""), keys) and similar_title(r["title"] or "", o["title"] or "")
                   for w in bucket for o in by_first_word[w]):
                if r["posting_id"]:
                    seen_pids.add(r["posting_id"])   # the vault copy stands for it; no lower source may relabel it
                continue
        kept.append(r)
        if r["posting_id"]:
            seen_pids.add(r["posting_id"])
        if r["source"] in FUZZY_DEDUPE_KEYS_FROM:
            for k in company_keys(r["company"] or ""):
                by_first_word[k.split()[0]].append(r)
    return kept


def model_version(labels: list, params: dict) -> str:
    """Hash of the training LABELS (id, label, weight) plus the params.

    Hashing ids alone was a bug: re-grading the corpus changed every grade but not a single label_id, so the
    version stayed identical, the model file was silently overwritten, and `rescreen` -- which re-screens a row
    only when `model_version` differs -- would have skipped the entire corpus. The scores would have stayed on
    the old model with no error anywhere. Caught 2026-09-16 when the two-lens re-grade produced a materially
    better model under the same version string."""
    payload = json.dumps([sorted(labels), sorted(params.items())], default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _pipeline_parts(C: float, min_df: int, ngram: tuple, max_features: int, max_df: float = 0.5):
    from sklearn.feature_extraction.text import ENGLISH_STOP_WORDS, TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    stop = sorted(set(ENGLISH_STOP_WORDS) | {w.lower() for w in P.MODEL_STOP_WORDS})
    vec = TfidfVectorizer(ngram_range=tuple(ngram), min_df=min_df, max_df=max_df, sublinear_tf=True, stop_words=stop,
                          token_pattern=TOKEN_PATTERN, max_features=max_features)
    clf = LogisticRegression(class_weight="balanced", C=C, max_iter=2000)
    return vec, clf


def _auc(y, p) -> Optional[float]:
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if len(set(y)) == 2 else None


def cross_validate(texts: list, y: list, w: list, *, C: float, min_df: int, ngram: tuple, max_features: int,
                   cv: int, seed: int, max_df: float = 0.5, groups: Optional[list] = None) -> dict:
    """Out-of-fold probabilities plus fold-mean AUC and precision@20 on held-out rows. `groups` keeps rows of
    the same job (its vault copy and its career-site copy) in the same fold."""
    import numpy as np
    from sklearn.model_selection import StratifiedGroupKFold, StratifiedKFold
    y_arr, w_arr = np.asarray(y), np.asarray(w, dtype=float)
    oof = np.zeros(len(y), dtype=float)
    aucs, precs = [], []
    if groups is not None:
        folds = StratifiedGroupKFold(n_splits=cv, shuffle=True, random_state=seed).split(texts, y_arr, groups)
    else:
        folds = StratifiedKFold(n_splits=cv, shuffle=True, random_state=seed).split(texts, y_arr)
    for train_idx, test_idx in folds:
        vec, clf = _pipeline_parts(C, min_df, ngram, max_features, max_df)
        X = vec.fit_transform([texts[i] for i in train_idx])
        clf.fit(X, y_arr[train_idx], sample_weight=w_arr[train_idx])
        p = clf.predict_proba(vec.transform([texts[i] for i in test_idx]))[:, 1]
        oof[test_idx] = p
        auc = _auc(y_arr[test_idx], p)
        if auc is not None:
            aucs.append(auc)
        top = np.argsort(-p)[:20]
        precs.append(float(y_arr[test_idx][top].mean()))
    return {"oof": oof.tolist(), "auc": float(np.mean(aucs)) if aucs else None,
            "precision_at_20": float(np.mean(precs)) if precs else None}


def train(con, *, C: float = 4.0, min_df: int = 3, ngram: tuple = (1, 2), max_features: int = 50_000, cv: int = 5,
          seed: int = 7, max_df: float = 0.5, model_dir: Optional[str] = None, lens=None, log=print) -> dict:
    """Cross-validates, fits on all labels, saves db/models/<version>.joblib and inserts a `models` row.

    `lens` ('process' | 'technical' | 'ai') trains that lens's model and stores it under its own `kind`, so the
    three coexist and `latest_model` can ask for one by name. `lens="required"` (REQUIRED_MODEL) runs through
    this exact same path and storage shape (kind `tfidf_lr_required`) but is NOT a lens -- it trains on
    vw_label_set_required (judge required_fit calls only, no vault/decision rows), and its score is a ranking
    signal that must never reach `pipeline.content_fit` or any lens list/count. Career-site-copy augmentation
    below is a no-op for it: vw_label_set_required's text already IS the posting's own current description_text,
    so the "copy differs from the row" check that triggers augmentation is never true unless the JD was
    re-fetched with different text since judging -- and even then it is ordinary signal, not vault leakage."""
    import joblib
    import numpy as np
    rows = training_set(con, lens=lens)
    y = [int(r["label"]) for r in rows]
    n_pos, n_neg = sum(y), len(y) - sum(y)
    n_jobs = len(rows)
    if n_pos < cv or n_neg < cv:
        raise ValueError(f"not enough labels to train ({n_pos} positive, {n_neg} negative); run `finder.py labels`")
    texts = [doc_text(r["title"], r["text"], r["company"]) for r in rows]
    weights = [float(r["weight"] or 1.0) for r in rows]
    # Each vault positive with a matched posting is also trained on as that posting's career-site text, so the
    # positives come from career sites too and "looks like a career-site page" stops meaning "not a fit".
    groups, pairs = _text_group_ids(rows), {}
    for i, copy in _career_site_copies(con, rows).items():
        pairs[i] = len(texts)
        texts.append(copy)
        y.append(y[i])
        weights.append(weights[i])
        groups.append(groups[i])   # the copy stays in its vault row's group, whatever id that group already has
    params = {"C": C, "min_df": min_df, "max_df": max_df, "ngram": list(ngram), "max_features": max_features, "cv": cv,
              "lens": lens or "overall",
              "seed": seed, "features": FEATURE_VERSION, "copies": len(pairs), "stop_words": hashlib.sha1(
                  " ".join(sorted(P.MODEL_STOP_WORDS)).encode()).hexdigest()[:8]}
    version = model_version([[r["label_id"], int(r["label"]), float(r["weight"] or 1.0)] for r in rows], params)

    warnings = []
    if n_pos < LOW_DATA_MIN:
        warnings.append(f"only {n_pos} positives with text (< {LOW_DATA_MIN})")
    if n_neg < LOW_DATA_MIN:
        warnings.append(f"only {n_neg} pseudo-negatives with text (< {LOW_DATA_MIN})")
    fit_weight = LOW_DATA_FIT_WEIGHT if warnings else None
    for w in warnings:
        log(f"WARNING: {w}; fit weight in the blend drops to {LOW_DATA_FIT_WEIGHT}")

    cvr = cross_validate(texts, y, weights, C=C, min_df=min_df, ngram=ngram, max_features=max_features, cv=cv,
                         seed=seed, max_df=max_df, groups=groups)
    oof = np.asarray(cvr["oof"])
    y_arr = np.asarray(y)
    # Career-site copies (appended above, index >= n_jobs) are extra training signal for the SAME job as an
    # earlier row; a metric meant to read as "one row per job" -- confusion, oof AUC, held-out fit means --
    # must be computed on the first n_jobs rows only, or it silently double-counts those jobs.
    oof_jobs, y_jobs = oof[:n_jobs], y_arr[:n_jobs]
    pred = (oof_jobs >= 0.5).astype(int)
    confusion = {"tp": int(((pred == 1) & (y_jobs == 1)).sum()), "fp": int(((pred == 1) & (y_jobs == 0)).sum()),
                 "fn": int(((pred == 0) & (y_jobs == 1)).sum()), "tn": int(((pred == 0) & (y_jobs == 0)).sum())}

    vec, clf = _pipeline_parts(C, min_df, ngram, max_features, max_df)
    clf.fit(vec.fit_transform(texts), y_arr, sample_weight=np.asarray(weights))
    notes = {"fit_weight": fit_weight, "warnings": warnings, "params": params}
    model = {"vec": vec, "clf": clf, "version": version, "fit_weight": fit_weight, "params": params}
    folder = model_dir or MODEL_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{version}.joblib")
    joblib.dump(model, path)
    # Store the path relative to the repo root, not absolute: an absolute path baked in from a machine/drive that
    # later disappears (the deleted /mnt/e checkout) is unrecoverable, while a relative one just needs REPO_ROOT
    # (load_latest resolves it back to the same absolute path via os.path.join, so this is a no-op for callers).
    stored_path = os.path.relpath(path, REPO_ROOT)
    kind = f"tfidf_lr_{lens}" if lens else "tfidf_lr"
    con.execute("INSERT OR REPLACE INTO models (model_version, kind, trained_at, n_pos, n_neg, cv_auc, "
                "cv_precision_at_20, path, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [version, kind, _now(), n_pos, n_neg, cvr["auc"], cvr["precision_at_20"], stored_path,
                 json.dumps(notes)])

    in_sample = clf.predict_proba(vec.transform(texts[:n_jobs]))[:, 1]
    source_gap = _source_gap(con, rows, oof, pairs)
    result = {
        "model_version": version, "path": path, "n_pos": n_pos, "n_neg": n_neg,
        "cv_auc": cvr["auc"], "cv_precision_at_20": cvr["precision_at_20"], "oof_auc": _auc(y_jobs, oof_jobs),
        "source_gap": source_gap, "coefficients": _extreme_terms(vec, clf),
        "confusion_at_0_5": confusion, "pos_mean_fit_oof": float(oof_jobs[y_jobs == 1].mean()),
        "pos_mean_fit_in_sample": float(in_sample[y_jobs == 1].mean()),
        "neg_mean_fit_oof": float(oof_jobs[y_jobs == 0].mean()), "warnings": warnings, "fit_weight": fit_weight,
        "rows": rows, "oof": cvr["oof"], "n_by_source": dict(Counter(r["source"] for r in rows)),
        "grades": grade_report(rows, oof_jobs),
        "grades_graded_only": grade_report(rows, oof_jobs, positives_from="graded"),
    }
    log(f"Model {version}: {n_pos} pos / {n_neg} pseudo-neg · {cv}-fold AUC {_fmt(cvr['auc'])} · precision@20 "
        f"{_fmt(cvr['precision_at_20'])} · positives mean fit {result['pos_mean_fit_oof']:.2f} held-out / "
        f"{result['pos_mean_fit_in_sample']:.2f} in-sample · confusion@0.5 {confusion} → {path}")
    log("Held-out fit of positives by where their text came from (a large gap = the model learned the source): "
        + " · ".join(f"{k} {v['mean']:.2f} (n={v['n']})" for k, v in source_gap.items()))
    log("Rows by source: " + " · ".join(f"{k} {v}" for k, v in sorted(result["n_by_source"].items())))
    g = result["grades"]
    if g["by_grade"]:
        log("Held-out fit by judge grade: "
            + " · ".join(f"{k} {v['mean']:.2f} (n={v['n']})" for k, v in g["by_grade"].items()))
        # Whichever negative grades this run actually has (wrong/stretch for a lens, fails/arguable for
        # `required` -- NEGATIVE_GRADES_TO_REPORT covers both vocabularies and grade_report only populates the
        # ones present), so the line reads correctly for either without a lens-specific branch here.
        present = [gr for gr in NEGATIVE_GRADES_TO_REPORT if f"auc_vs_{gr}" in g]
        if present:
            log("AUC positives vs graded " + " · ".join(
                f"`{gr}` {_fmt(g.get(f'auc_vs_{gr}'))} (graded positives only: "
                f"{_fmt(result['grades_graded_only'].get(f'auc_vs_{gr}'))})" for gr in present))
    return result


def _text_group_ids(rows: list) -> list:
    """Stable group id per row, from a sha1 of its normalized `text`: identical-text rows -- a repost sharing a
    description_hash, or a judge `dup:` copy of a vault row -- get the same id and so can never split across
    CV folds. `range(n_jobs)` groups (the old behaviour) let two rows of the same JD land in different folds,
    which is leakage: the model gets test-set credit for having memorized text it also saw in training."""
    seen: dict = {}
    ids = []
    for r in rows:
        key = hashlib.sha1((r["text"] or "").strip().lower().encode("utf-8")).hexdigest()
        ids.append(seen.setdefault(key, len(seen)))
    return ids


def _career_site_copies(con, rows: list) -> dict:
    """Row index -> the matched posting's own JD, for vault positives whose text is not already that JD."""
    pids = sorted({r["posting_id"] for r in rows if r["posting_id"] and r["label"] == 1})
    if not pids:
        return {}
    ats = dict(con.execute("SELECT posting_id, description_text FROM postings WHERE posting_id IN "
                           "(SELECT unnest(?::VARCHAR[])) AND length(description_text) >= 800", [pids]).fetchall())
    return {i: doc_text(r["title"], ats[r["posting_id"]], r["company"]) for i, r in enumerate(rows)
            if r["label"] == 1 and r["source"] != "decision" and r["posting_id"] in ats and ats[r["posting_id"]] != r["text"]}


def _source_gap(con, rows: list, oof, pairs: Optional[dict] = None) -> dict:
    """Mean held-out fit of positives whose text is the posting's own career-site text vs vault-only text,
    of near-miss positives (weight below 1 from pass / not-pursuing folders), and the paired comparison:
    the same jobs scored on their vault text and on their career-site text."""
    pids = sorted({r["posting_id"] for r in rows if r["posting_id"]})
    ats = dict(con.execute("SELECT posting_id, description_text FROM postings WHERE posting_id IN "
                           "(SELECT unnest(?::VARCHAR[]))", [pids]).fetchall()) if pids else {}
    groups = defaultdict(list)
    for r, p in zip(rows, oof):
        if r["label"] != 1:
            continue
        groups["career-site text" if r["posting_id"] and ats.get(r["posting_id"]) == r["text"] else "vault text"].append(p)
        if r["source"] == "application" and (r["weight"] or 1) < 1:
            groups["near-miss folders"].append(p)
    for i, j in (pairs or {}).items():
        groups["paired: vault copy"].append(float(oof[i]))
        groups["paired: career-site copy"].append(float(oof[j]))
    return {k: {"n": len(v), "mean": float(sum(v) / len(v))} for k, v in groups.items() if v}


# Grade values that mark a row a POSITIVE under either vocabulary this function sees: the four-way lens grade
# (bullseye/adjacent/stretch/wrong) and the Required-block grade (meets/arguable/fails, stored in the `grade`
# column by vw_label_set_required). The two vocabularies never share a value, so one set is safe for both.
POSITIVE_GRADES = ("bullseye", "adjacent", "meets")
# Grade values worth an AUC-against-positives line when they have rows: the lens "hard negative" grades plus
# the Required-block's two non-meets answers. A grade absent from the data (e.g. `wrong`/`stretch` on a
# required-model run, or `fails`/`arguable` on a lens run) simply has no rows, so its line is skipped, not
# crashed on -- the `if positives and neg` guard below already handles that.
NEGATIVE_GRADES_TO_REPORT = ("wrong", "stretch", "fails", "arguable")


def grade_report(rows: list, oof, positives_from: str = "any") -> dict:
    """Held-out fit by judge grade (bullseye/adjacent/stretch/wrong for a lens, meets/arguable/fails for the
    Required-block model -- `grade` carries whichever vocabulary the training view used), and the AUCs that
    matter after the labeling run: positives against each populated negative grade (`wrong` and `stretch` for a
    lens, `fails` and `arguable` for `required`). Before the run those negatives did not exist, so the only
    measurable AUC was against random postings, which any vocabulary model wins.

    `positives_from` = 'any' scores every label-1 row; 'graded' uses only the rows graded a positive value
    (POSITIVE_GRADES), which is the harder and more honest comparison (both sides then come from the same
    corpus and the same judge) -- for `required`, every positive is already a graded row, so this mode changes
    nothing there."""
    by_grade, positives = defaultdict(list), []
    for r, p in zip(rows, oof):
        p = float(p)
        if r.get("grade"):
            by_grade[r["grade"]].append(p)
        if r["label"] == 1 and (positives_from == "any" or r.get("grade") in POSITIVE_GRADES):
            positives.append(p)
    out = {"by_grade": {g: {"n": len(v), "mean": sum(v) / len(v)} for g, v in sorted(by_grade.items())},
           "positives": {"n": len(positives), "mean": sum(positives) / len(positives) if positives else None}}
    for grade in NEGATIVE_GRADES_TO_REPORT:
        neg = by_grade.get(grade, [])
        if positives and neg:
            y = [1] * len(positives) + [0] * len(neg)
            out[f"auc_vs_{grade}"] = _auc(y, positives + neg)
    return out


def _extreme_terms(vec, clf, k: int = 25) -> dict:
    import numpy as np
    names, coef = vec.get_feature_names_out(), clf.coef_[0]
    order = np.argsort(coef)
    return {"negative": [(str(names[i]), round(float(coef[i]), 2)) for i in order[:k]],
            "positive": [(str(names[i]), round(float(coef[i]), 2)) for i in order[::-1][:k]]}


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def hard_negatives(result: dict, k: int = 20) -> list:
    """Top-k held-out fit_prob among label-0 docs: (prob, source, label_id, company, title)."""
    pairs = [(p, r) for p, r in zip(result["oof"], result["rows"]) if r["label"] == 0]
    pairs.sort(key=lambda pr: -pr[0])
    return [(p, r["source"], r["label_id"], r["company"], r["title"]) for p, r in pairs[:k]]


def latest_row(con, lens=None) -> Optional[tuple]:
    """(model_version, path, notes) of the newest model for this lens, SQL only."""
    kind = f"tfidf_lr_{lens}" if lens else "tfidf_lr"
    return con.execute("SELECT model_version, path, notes FROM models WHERE kind = ? "
                       "ORDER BY trained_at DESC LIMIT 1", [kind]).fetchone()


def _resolve_model_path(path: str, model_dir: Optional[str] = None) -> Optional[str]:
    """The stored `models.path` resolved to a real file, or None.

    A relative path is joined against REPO_ROOT (the portable form `train` now writes). Failing that -- an
    absolute path from another machine/drive (e.g. the deleted /mnt/e checkout), or any path whose file has
    since moved -- falls back to the file's basename inside MODEL_DIR and, if given, `model_dir`, since the
    joblib itself is what matters and it is always named `<version>.joblib`."""
    if not path:
        return None
    candidates = [path if os.path.isabs(path) else os.path.join(REPO_ROOT, path)]
    base = os.path.basename(path)
    candidates.append(os.path.join(MODEL_DIR, base))
    if model_dir:
        candidates.append(os.path.join(model_dir, base))
    return next((c for c in candidates if os.path.exists(c)), None)


def load_latest(con, lens=None, log=print, model_dir: Optional[str] = None) -> Optional[dict]:
    """The newest trained model for `lens` (None = the averaged-grade model), or None when none exists,
    its file is gone, or scikit-learn is missing.

    `model_dir` is an extra fallback location to check by basename (see `_resolve_model_path`) -- useful when a
    non-default `train(..., model_dir=...)` was used and the DB row's path no longer resolves as-is."""
    row = latest_row(con, lens=lens)
    if not row:
        return None
    version, path, _ = row
    resolved = _resolve_model_path(path, model_dir)
    if not resolved:
        log(f"Model {version}: file {path} missing; rules only.")
        return None
    try:
        import joblib
        model = joblib.load(resolved)
    except ImportError:
        log(f"Model {version} exists but scikit-learn/joblib is not installed; rules only.")
        return None
    return model


LENSES = ("process", "technical", "ai")
# The probability thresholds that turn these scores into buckets live in SQL, as the `lens_strong_p()` /
# `lens_standout_p()` macros next to the view that reads them (backend/ats/store.py).

# The Required-block ranking model's name. Deliberately kept OUT of LENSES and out of load_lens_models's
# result, in its own constant rather than a fourth entry in that tuple, so every site that iterates LENSES (or
# treats load_lens_models's dict as "the lenses") can never pick it up by accident: it answers a different
# question (does the candidate clear THIS posting's Required block) than the three lenses (what kind of work
# is this), and it is a ranking signal only -- it must never enter content_fit, lens_best, lens_breadth,
# n_lenses_good, lens_bucket or any "lenses" report/count.
REQUIRED_MODEL = "required"


def load_lens_models(con, log=print, model_dir: Optional[str] = None) -> dict:
    """{lens: model} for every lens that has a trained model on disk. Missing lenses are simply absent, so
    the screen runs unchanged on a database that never trained them.

    Never includes REQUIRED_MODEL -- callers that want the Required-block ranking model ask for it by name
    with `load_required_model`, kept as a separate call precisely so it cannot leak into this dict."""
    out = {}
    for lens in LENSES:
        model = load_latest(con, lens=lens, log=log, model_dir=model_dir)
        if model is not None:
            out[lens] = model
    return out


def load_required_model(con, log=print, model_dir: Optional[str] = None) -> Optional[dict]:
    """The Required-block ranking model (features.train(lens=REQUIRED_MODEL)), or None when it has not been
    trained. NOT a lens (see REQUIRED_MODEL) -- a separate function, not a LENSES entry, so it can only ever
    be loaded by a caller that explicitly asks for it."""
    return load_latest(con, lens=REQUIRED_MODEL, log=log, model_dir=model_dir)


def predict(model: dict, texts: list) -> list:
    """fit_prob per text (texts already passed through doc_text)."""
    return model["clf"].predict_proba(model["vec"].transform(texts))[:, 1].tolist()


def predict_with_terms(model: dict, texts: list, k: int = 6) -> tuple:
    """(fit_probs, top_terms) for a batch: one transform, then each row's tf-idf x coef top positives."""
    import numpy as np
    X = model["vec"].transform(texts)
    probs = model["clf"].predict_proba(X)[:, 1].tolist()
    coef = model["clf"].coef_[0]
    names = model.get("_names")
    if names is None:
        names = model["_names"] = model["vec"].get_feature_names_out()
    X = X.tocsr()
    terms = []
    for i in range(X.shape[0]):
        start, end = X.indptr[i], X.indptr[i + 1]
        idx, vals = X.indices[start:end], X.data[start:end]
        contrib = vals * coef[idx]
        order = np.argsort(-contrib)[:k]
        terms.append([[str(names[idx[j]]), round(float(contrib[j]), 3)] for j in order if contrib[j] > 0])
    return probs, terms


def top_terms(model: dict, text: str, k: int = 6) -> list:
    """[[term, contribution], ...]: the k largest positive tf-idf x coefficient products in one text."""
    return predict_with_terms(model, [text], k)[1][0]


def signal_report(con, result: dict) -> list:
    """(signal, auc_all, auc_non_pseudo, n_all, n_non_pseudo) for rule_score, fit_prob (held-out), embed_sim
    and the blend, over training rows whose posting has a screen.

    `blend` is read straight from `vw_screen_latest.final_score` -- the score the pipeline actually stored --
    rather than recomputed via `pipeline.combine()` here. Recomputing it needs the exact `flags`/`llm_score`
    the original screen used (`combine(..., rejected=...)` alone drops the penalty flags and re-derives a
    number that was never the one shown anywhere), so reading it back is both simpler and correct."""
    screens = {r[0]: r[1:] for r in con.execute(
        "SELECT posting_id, rule_score, embed_sim, final_score FROM vw_screen_latest").fetchall()}
    table = []
    for r, fit in zip(result["rows"], result["oof"]):
        if r["posting_id"] in screens:
            rule_score, embed_sim, blend = screens[r["posting_id"]]
            table.append((r["source"] != "pseudo_neg", r["label"], rule_score, fit, embed_sim, blend))
    out = []
    for name, col in (("rule_score", 2), ("fit_prob", 3), ("embed_sim", 4), ("blend", 5)):
        rows_all = [(t[1], t[col]) for t in table if t[col] is not None]
        rows_real = [(t[1], t[col]) for t in table if t[col] is not None and t[0]]
        auc_all = _auc([y for y, _ in rows_all], [x for _, x in rows_all]) if rows_all else None
        auc_real = _auc([y for y, _ in rows_real], [x for _, x in rows_real]) if rows_real else None
        out.append((name, _fmt(auc_all), _fmt(auc_real), len(rows_all), len(rows_real)))
    return out
