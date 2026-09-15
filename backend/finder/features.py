"""The fit model: TF-IDF + logistic regression trained on the user's own labels (`vw_label_set`).

scikit-learn, numpy and joblib are imported inside the functions, so importing this module
costs nothing and the sweep runs without them. `load_latest` returns None (rules only) when no
model has been trained or the libraries are missing.
"""
import hashlib
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from backend.ats import store
from backend.screen import company_keys, company_matches, norm_company, similar_title

from .labels import strip_boilerplate

MODEL_DIR = os.path.join(os.path.dirname(store.DEFAULT_DB_PATH), "models")
LOW_DATA_MIN = 150          # positives or non-pseudo negatives with text below this = low-data warning
LOW_DATA_FIT_WEIGHT = 0.15  # pipeline.combine weight for `fit` under the warning
# When one job appears under several sources, the first source in this order supplies its label.
SOURCE_PRIORITY = ("application", "decision", "jobs_found_escalated", "jobs_found_passed", "pseudo_neg")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def doc_text(title: Optional[str], text: Optional[str]) -> str:
    """Title twice (it carries the function) plus the JD without boilerplate."""
    title = title or ""
    return f"{title}\n{title}\n{strip_boilerplate(text or '')}"


def training_set(con) -> list:
    """vw_label_set rows as dicts, one per job: deduped on posting_id, then on company + similar title
    (decision rows only, where the vault copy of the same job usually exists), by SOURCE_PRIORITY."""
    cols = ("label_id", "source", "posting_id", "company", "title", "text", "label", "weight")
    rows = [dict(zip(cols, r)) for r in con.execute(f"SELECT {', '.join(cols)} FROM vw_label_set").fetchall()]
    rank = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
    rows.sort(key=lambda r: (rank.get(r["source"], len(rank)), r["label_id"]))
    kept, seen_pids, by_first_word = [], set(), defaultdict(list)
    for r in rows:
        if r["posting_id"] and r["posting_id"] in seen_pids:
            continue
        if r["source"] == "decision":
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
        if r["source"] != "pseudo_neg":
            for k in company_keys(r["company"] or ""):
                by_first_word[k.split()[0]].append(r)
    return kept


def model_version(label_ids: list, params: dict) -> str:
    payload = json.dumps([sorted(label_ids), sorted(params.items())], default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _pipeline_parts(C: float, min_df: int, ngram: tuple, max_features: int):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    vec = TfidfVectorizer(ngram_range=tuple(ngram), min_df=min_df, sublinear_tf=True, stop_words="english",
                          max_features=max_features)
    clf = LogisticRegression(class_weight="balanced", C=C, max_iter=2000)
    return vec, clf


def _auc(y, p) -> Optional[float]:
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if len(set(y)) == 2 else None


def cross_validate(texts: list, y: list, w: list, *, C: float, min_df: int, ngram: tuple, max_features: int,
                   cv: int, seed: int) -> dict:
    """Out-of-fold probabilities plus fold-mean AUC and precision@20 on held-out rows."""
    import numpy as np
    from sklearn.model_selection import StratifiedKFold
    y_arr, w_arr = np.asarray(y), np.asarray(w, dtype=float)
    oof = np.zeros(len(y), dtype=float)
    aucs, precs = [], []
    for train_idx, test_idx in StratifiedKFold(n_splits=cv, shuffle=True, random_state=seed).split(texts, y_arr):
        vec, clf = _pipeline_parts(C, min_df, ngram, max_features)
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
          seed: int = 7, model_dir: Optional[str] = None, log=print) -> dict:
    """Cross-validates, fits on all labels, saves db/models/<version>.joblib and inserts a `models` row."""
    import joblib
    import numpy as np
    rows = training_set(con)
    y = [int(r["label"]) for r in rows]
    n_pos, n_neg = sum(y), len(y) - sum(y)
    if n_pos < cv or n_neg < cv:
        raise ValueError(f"not enough labels to train ({n_pos} positive, {n_neg} negative); run `finder.py labels`")
    texts = [doc_text(r["title"], r["text"]) for r in rows]
    weights = [float(r["weight"] or 1.0) for r in rows]
    params = {"C": C, "min_df": min_df, "ngram": list(ngram), "max_features": max_features, "cv": cv, "seed": seed}
    version = model_version([r["label_id"] for r in rows], params)

    real_neg = sum(1 for r in rows if r["label"] == 0 and r["source"] != "pseudo_neg")
    warnings = []
    if n_pos < LOW_DATA_MIN:
        warnings.append(f"only {n_pos} positives with text (< {LOW_DATA_MIN})")
    if real_neg < LOW_DATA_MIN:
        warnings.append(f"only {real_neg} non-pseudo negatives with text (< {LOW_DATA_MIN})")
    fit_weight = LOW_DATA_FIT_WEIGHT if warnings else None
    for w in warnings:
        log(f"WARNING: {w}; fit weight in the blend drops to {LOW_DATA_FIT_WEIGHT}")

    cvr = cross_validate(texts, y, weights, C=C, min_df=min_df, ngram=ngram, max_features=max_features, cv=cv, seed=seed)
    oof = np.asarray(cvr["oof"])
    y_arr = np.asarray(y)
    real = np.asarray([r["source"] != "pseudo_neg" for r in rows])
    pred = (oof >= 0.5).astype(int)
    confusion = {"tp": int(((pred == 1) & (y_arr == 1)).sum()), "fp": int(((pred == 1) & (y_arr == 0)).sum()),
                 "fn": int(((pred == 0) & (y_arr == 1)).sum()), "tn": int(((pred == 0) & (y_arr == 0)).sum())}

    vec, clf = _pipeline_parts(C, min_df, ngram, max_features)
    clf.fit(vec.fit_transform(texts), y_arr, sample_weight=np.asarray(weights))
    notes = {"fit_weight": fit_weight, "warnings": warnings, "params": params, "n_real_neg": real_neg}
    model = {"vec": vec, "clf": clf, "version": version, "fit_weight": fit_weight, "params": params}
    folder = model_dir or MODEL_DIR
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{version}.joblib")
    joblib.dump(model, path)
    con.execute("INSERT OR REPLACE INTO models (model_version, kind, trained_at, n_pos, n_neg, cv_auc, "
                "cv_precision_at_20, path, notes) VALUES (?, 'tfidf_lr', ?, ?, ?, ?, ?, ?, ?)",
                [version, _now(), n_pos, n_neg, cvr["auc"], cvr["precision_at_20"], path, json.dumps(notes)])

    in_sample = clf.predict_proba(vec.transform(texts))[:, 1]
    result = {
        "model_version": version, "path": path, "n_pos": n_pos, "n_neg": n_neg, "n_real_neg": real_neg,
        "cv_auc": cvr["auc"], "cv_precision_at_20": cvr["precision_at_20"], "oof_auc": _auc(y, oof),
        "oof_auc_real_negatives": _auc(y_arr[real].tolist(), oof[real]) if real.any() else None,
        "confusion_at_0_5": confusion, "pos_mean_fit_oof": float(oof[y_arr == 1].mean()),
        "pos_mean_fit_in_sample": float(in_sample[y_arr == 1].mean()),
        "neg_mean_fit_oof": float(oof[y_arr == 0].mean()), "warnings": warnings, "fit_weight": fit_weight,
        "rows": rows, "oof": cvr["oof"],
    }
    log(f"Model {version}: {n_pos} pos / {n_neg} neg ({real_neg} non-pseudo) · {cv}-fold AUC "
        f"{_fmt(cvr['auc'])} · precision@20 {_fmt(cvr['precision_at_20'])} · AUC vs non-pseudo negatives "
        f"{_fmt(result['oof_auc_real_negatives'])} · positives mean fit {result['pos_mean_fit_oof']:.2f} held-out "
        f"/ {result['pos_mean_fit_in_sample']:.2f} in-sample · confusion@0.5 {confusion} → {path}")
    return result


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def hard_negatives(result: dict, k: int = 20) -> list:
    """Top-k held-out fit_prob among label-0 docs: (prob, source, label_id, company, title)."""
    pairs = [(p, r) for p, r in zip(result["oof"], result["rows"]) if r["label"] == 0]
    pairs.sort(key=lambda pr: -pr[0])
    return [(p, r["source"], r["label_id"], r["company"], r["title"]) for p, r in pairs[:k]]


def latest_row(con) -> Optional[tuple]:
    """(model_version, path, notes) of the newest tfidf_lr model, SQL only."""
    return con.execute("SELECT model_version, path, notes FROM models WHERE kind = 'tfidf_lr' "
                       "ORDER BY trained_at DESC LIMIT 1").fetchone()


def load_latest(con, log=print) -> Optional[dict]:
    """The newest trained model, or None when none exists, its file is gone, or scikit-learn is missing."""
    row = latest_row(con)
    if not row:
        return None
    version, path, _ = row
    if not path or not os.path.exists(path):
        log(f"Model {version}: file {path} missing; rules only.")
        return None
    try:
        import joblib
        model = joblib.load(path)
    except ImportError:
        log(f"Model {version} exists but scikit-learn/joblib is not installed; rules only.")
        return None
    return model


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
    and the blend, over training rows whose posting has a screen."""
    from .pipeline import combine
    screens = {r[0]: r[1:] for r in con.execute(
        "SELECT posting_id, rule_score, tier, verdict, embed_sim FROM vw_screen_latest").fetchall()}
    calib = {"fit_weight": result.get("fit_weight")}
    table = []
    for r, fit in zip(result["rows"], result["oof"]):
        if r["posting_id"] in screens:
            rule_score, tier, verdict, embed_sim = screens[r["posting_id"]]
            blend = combine(rule_score, fit, embed_sim, None, calib, tier=tier, rejected=verdict == "reject")[0]
            table.append((r["source"] != "pseudo_neg", r["label"], rule_score, fit, embed_sim, blend))
    out = []
    for name, col in (("rule_score", 2), ("fit_prob", 3), ("embed_sim", 4), ("blend", 5)):
        rows_all = [(t[1], t[col]) for t in table if t[col] is not None]
        rows_real = [(t[1], t[col]) for t in table if t[col] is not None and t[0]]
        auc_all = _auc([y for y, _ in rows_all], [x for _, x in rows_all]) if rows_all else None
        auc_real = _auc([y for y, _ in rows_real], [x for _, x in rows_real]) if rows_real else None
        out.append((name, _fmt(auc_all), _fmt(auc_real), len(rows_all), len(rows_real)))
    return out
