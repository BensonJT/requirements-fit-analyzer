"""The "second layer": a stacked model producing `embed_required` = P(judge would call the
Required block `meets`), ported FAITHFULLY from the gitignored lab (`db/embed_lab/`:
`experiments.py`, `phase2b_line_model.py`, `line_labels.py`). NOT a lens -- see
`features.REQUIRED_MODEL` -- this is a second, independent estimate of the same quantity as
`screens.fit_required` (the TF-IDF model), stacked with two embedding-based signals:

1. TF-IDF required prob (`screens.fit_required` at scoring time, for a posting that never
   contributed a label anywhere; an out-of-fold value at training time, computed for every judged
   posting under the employer folds).
2. Block classifier: bge-base embedding of the posting's own Required block -> LogisticRegression,
   TRAINED on LENS-SURFACED judged rows only (the lab found training on all rows hurt), but able to
   SCORE (out-of-fold) any judged posting assigned to that fold, lens-surfaced or not.
3. Line roll-up: per-Required-line P(unmet) from a LogReg on the line's OWN bge-base embedding
   (no evidence-coverage features -- the lab's ablation showed they add nothing), rolled up per
   posting with six features (max/mean/weighted-mean/noisy-or/count>0.5/n_lines) -> LogReg (again
   trained on lens-surfaced rows, scored out-of-fold for any judged posting in that fold).

All three OOF estimates go into a final LogReg stack, fit WITHOUT class_weight="balanced" at a
fixed C so its output is a calibrated probability near the ~33% base rate -- the gate's OOF numbers
and every stored held-out value use that SAME unbalanced, fixed-C configuration, never the
balanced/grid-searched one used for the block and roll-up components.

RULE (the leak this module must never reintroduce): no judged posting that contributed a label to
ANY component -- TF-IDF's training_set, the line model's quoted-unmet lines, or the block/roll-up
training population -- may ever receive an in-sample ("fresh") score. `train()` therefore computes
and stores a held-out value for EVERY judged posting with a Required block that can get one (see
`_broad_oof`), not only the narrower lens-surfaced population the acceptance gate is measured on;
`score()` looks those up verbatim (`is_oof=True`) instead of re-embedding or re-running the full
models. A posting judged AFTER the model was trained has no held-out value and legitimately gets
the full-model ("fresh", `is_oof=False`) score, since it contributed no label to this model at all.

Scores land in their own table (`required_embed`), never on `screens` (a rescreen rewrites
`screens`). Training/scoring embeddings are cached on disk keyed by sha1(text) so a retrain does
not re-embed (`_EmbedCache`), and are always materialized once -- never re-indexed out of the lazy
`.npz` mapping in a loop (that cost 17 GB in the lab).
"""
import difflib
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from . import embed as E
from . import features as F

MODEL_KIND = "required_embed"
ENCODER_NAME = "BAAI/bge-base-en-v1.5"
SEED = 7
N_FOLDS = 5
UNITS_PER_POSTING = 18
UNIT_CHARS = 220
QUOTE_MATCH_THRESHOLD = 0.62
C_GRID = (0.25, 1.0, 4.0)
TFIDF_PARAMS = dict(C=4.0, min_df=3, ngram=(1, 2), max_features=50_000, max_df=0.5)
LENS_GRADES_SURFACED = {"bullseye", "adjacent"}
ROLLUP_FEAT_NAMES = ("max_p", "mean_p", "wmean_p", "noisy_or", "count_gt_0.5", "n_lines")

CACHE_DIR = os.path.join(F.MODEL_DIR, "required_embed_cache")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- text helpers (mirrors line_labels.py)
def _norm(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _token_jaccard(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _match_score(quote_n: str, unit_n: str) -> float:
    if not quote_n or not unit_n:
        return 0.0
    if quote_n in unit_n or unit_n in quote_n:
        return 1.0
    return max(_token_jaccard(quote_n, unit_n), difflib.SequenceMatcher(None, quote_n, unit_n).ratio())


def _is_lens_surfaced(row: dict) -> bool:
    grades = {(row.get("grade_process") or "").lower(), (row.get("grade_technical") or "").lower(),
              (row.get("grade_ai") or "").lower()}
    return bool(grades & LENS_GRADES_SURFACED)


# ---------------------------------------------------------------- disk-backed embedding cache
class _EmbedCache:
    """sha1(text) -> vector, one .npz per encoder under CACHE_DIR. Arrays are materialized ONCE
    on load (never indexed lazily out of the NpzFile in a loop)."""

    def __init__(self, encoder_name: str):
        os.makedirs(CACHE_DIR, exist_ok=True)
        self.path = os.path.join(CACHE_DIR, f"{encoder_name.replace('/', '__')}.npz")
        self._map: dict = {}
        if os.path.exists(self.path):
            import numpy as np
            d = np.load(self.path, allow_pickle=True)
            keys, emb = list(d["keys"]), d["emb"]  # materialize once
            self._map = {k: emb[i] for i, k in enumerate(keys)}

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def get_or_encode(self, texts: list, encoder, query: bool = True, batch_size: int = 48, log=print):
        import numpy as np
        keys = [self._key(t) for t in texts]
        missing = [(k, t) for k, t in zip(keys, texts) if k not in self._map]
        if missing:
            m_keys, m_texts = zip(*dict(zip((k for k, _ in missing), (t for _, t in missing))).items())
            vecs = encoder.encode(list(m_texts), query=query, batch_size=batch_size)
            for k, v in zip(m_keys, vecs):
                self._map[k] = v.astype("float32")
            self._save()
            log(f"  embedded {len(m_texts)} new texts ({encoder.name}); cache now {len(self._map)}")
        import numpy as np
        return np.stack([self._map[k] for k in keys], axis=0)

    def _save(self):
        import numpy as np
        keys = list(self._map.keys())
        emb = np.stack([self._map[k] for k in keys], axis=0) if keys else np.zeros((0, 768), dtype="float32")
        np.savez(self.path, keys=np.array(keys, dtype=object), emb=emb)


# ---------------------------------------------------------------- data assembly
def _fetch_population(con) -> dict:
    """Every lens-surfaced-or-not judged posting with a required_fit call, matching the lab's extract.py
    filter (JD >= 800 chars). Reads `vw_llm_labels_latest`, so a human required_fit call (`finder.py mark`,
    sprint plan §22.3 Gap 2) already outranks the judge's own here -- no separate carve-out for
    scorer='user-adjudicated': that used to be excluded outright, before a human required_fit call quoted its
    own unmet lines, which the line model below (_line_labels) can now learn from like any other quote.

    The inclusion rule is `l.required_fit IS NOT NULL` alone -- explicit, not incidental: it is the ONLY gate
    on a scorer='user-adjudicated' row here, and it is what makes admitting them safe. It reaches every
    PRE-EXISTING golden-CSV adjudicated row (loaded by feedback.load_csv, not only rows `finder.py mark`
    writes) that carries a required_fit, exactly as it reaches a judge row -- there is no way to tell those
    two producers apart from this query, nor any need to: both are "a human's own Required-block call, at
    this description_hash", the only thing this module borrows from llm_labels. It does NOT admit a row on
    the strength of scorer='user-adjudicated' alone: `record_mark()`'s 'placeholder' rows (required_fit set,
    but the posting was never actually judged, and no lane info exists) still pass this filter -- correctly,
    since a human required_fit call on a never-judged posting is real information for THIS model's target
    (required_fit), whatever it does or doesn't say about the lane (`_is_lens_surfaced` reads grade_process/
    technical/ai, all NULL on a placeholder row, so it never enters the lens-surfaced block/roll-up training
    population -- see train() below).

    Every posting_id this returns goes through the SAME employer-fold / _broad_oof machinery as an ordinary
    judge row (`train()` never branches on scorer or `lens_grade_source`), so the "every judged posting gets
    a held-out value, never an in-sample one" rule (this module's docstring) covers a human-adjudicated
    required_fit row exactly like a judge one -- confirmed by reading train()/`_broad_oof`, which key
    entirely on posting_id/fold_of, not on scorer identity. Returns posting_id -> row."""
    cols = ("posting_id", "employer", "title", "description_text", "grade_process", "grade_technical",
            "grade_ai", "required_fit", "required_unmet")
    p_cols = {"employer", "title", "description_text"}
    rows = con.execute(f"""
        SELECT {', '.join(('p.' if c in p_cols else 'l.') + c for c in cols)}
        FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
        WHERE l.required_fit IS NOT NULL AND length(p.description_text) >= 800
    """).fetchall()
    return {r[0]: dict(zip(cols, r)) for r in rows}


def _seen_units(con, pids: list) -> dict:
    """Top 18 requirement_units (klass='work', any grp) per posting, DB order = weight*spec DESC, ord --
    same source of "seen" used by judge.fetch_postings, mirrored by the lab's line_labels.py."""
    if not pids:
        return {}
    rows = con.execute("""
        SELECT posting_id, ord, text, grp, weight FROM requirement_units
        WHERE klass = 'work' AND posting_id IN (SELECT unnest(?::VARCHAR[]))
        ORDER BY posting_id, weight * coalesce(spec, 1.0) DESC, ord
    """, [pids]).fetchall()
    out = defaultdict(list)
    for pid, ordn, text, grp, weight in rows:
        lst = out[pid]
        if len(lst) < UNITS_PER_POSTING:
            lst.append({"ord": ordn, "text": text[:UNIT_CHARS], "grp": grp, "weight": weight})
    return out


def _required_block_text(seen: list) -> str:
    return "\n".join(u["text"] for u in seen if u["grp"] == "required")


def _line_labels(pop: dict, seen_by_pid: dict) -> list:
    """Replicates db/embed_lab/line_labels.py: required_unmet quotes matched to a seen [required]
    unit at threshold 0.62; matched -> unmet=1; unmatched, from a 'meets' posting -> clean_met;
    unmatched, otherwise -> noisy_met (kept but never used by the strict variant)."""
    out = []
    for pid, row in pop.items():
        seen = seen_by_pid.get(pid) or []
        if not seen:
            continue
        required_fit = (row.get("required_fit") or "").strip().lower()
        quotes = [_norm(q) for q in (row.get("required_unmet") or "").split(";") if _norm(q)]
        matched_ords = set()
        for q in quotes:
            best_score, best_unit = -1.0, None
            for u in seen:
                sc = _match_score(q, _norm(u["text"]))
                if sc > best_score:
                    best_score, best_unit = sc, u
            if best_score >= QUOTE_MATCH_THRESHOLD:
                matched_ords.add(best_unit["ord"])
        for u in seen:
            if u["grp"] != "required":
                continue
            if u["ord"] in matched_ords:
                status, y01 = "unmet", 1
            elif required_fit == "meets":
                status, y01 = "clean_met", 0
            else:
                status, y01 = "noisy_met", 0
            out.append({"posting_id": pid, "ord": u["ord"], "text": u["text"], "status": status,
                        "y01": y01, "weight": u["weight"]})
    return out


def _employer_folds(pids: list, employer_of: dict, seed=SEED, n_splits=N_FOLDS) -> dict:
    import numpy as np
    from sklearn.model_selection import GroupKFold
    pids_sorted = sorted(pids)
    groups = [employer_of.get(pid, "?") for pid in pids_sorted]
    fold_of = {}
    gkf = GroupKFold(n_splits=min(n_splits, len(set(groups))) if len(set(groups)) >= 2 else 1)
    if len(set(groups)) < 2:
        for pid in pids_sorted:
            fold_of[pid] = 0
        return fold_of
    for fold_idx, (_, test_idx) in enumerate(gkf.split(pids_sorted, groups=groups)):
        for i in test_idx:
            fold_of[pids_sorted[i]] = fold_idx
    return fold_of


def _group_kfold_indices(pids: list, fold_of: dict):
    import numpy as np
    fold_arr = np.array([fold_of.get(pid, -1) for pid in pids])
    out = {}
    for f in sorted(set(fold_arr.tolist()) - {-1}):
        test_idx = np.where(fold_arr == f)[0]
        train_idx = np.where((fold_arr != f) & (fold_arr != -1))[0]
        out[f] = (train_idx, test_idx)
    return out


def _select_C_inner(Xtr, ytr, groups_train, C_grid=C_GRID, seed=SEED, n_inner=4, sample_weight=None):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    n_groups = len(set(groups_train))
    if n_groups < 2 or len(set(ytr.tolist())) < 2:
        return C_grid[len(C_grid) // 2]
    gkf = GroupKFold(n_splits=min(n_inner, n_groups))
    best_c, best_auc = C_grid[0], -1.0
    for C in C_grid:
        aucs = []
        for tr_idx, val_idx in gkf.split(Xtr, ytr, groups_train):
            yv_tr, yv_val = ytr[tr_idx], ytr[val_idx]
            if len(set(yv_tr.tolist())) < 2 or len(set(yv_val.tolist())) < 2:
                continue
            clf = LogisticRegression(class_weight="balanced", C=C, max_iter=2000, random_state=seed)
            sw = sample_weight[tr_idx] if sample_weight is not None else None
            clf.fit(Xtr[tr_idx], yv_tr, sample_weight=sw)
            p = clf.predict_proba(Xtr[val_idx])[:, 1]
            aucs.append(roc_auc_score(yv_val, p))
        m = float(np.mean(aucs)) if aucs else -1.0
        if m > best_auc:
            best_auc, best_c = m, C
    return best_c


def _broad_oof(vec_by_pid: dict, y_train_by_pid: dict, score_pids: list, fold_of: dict, employer_of: dict,
              scale: bool = False, C_grid=C_GRID, seed=SEED, balanced: bool = True,
              fixed_C: Optional[float] = None) -> dict:
    """Employer-grouped, fold-k-holds-out-fold-k logistic regression, with TRAINING restricted to
    `y_train_by_pid`'s keys (e.g. lens-surfaced rows only) but SCORING applied to every pid in
    `score_pids` that shares fold k with the training rows -- so a judged posting that never
    trained this component (e.g. not lens-surfaced) can still get a genuinely held-out score from
    the fold-k model that never saw its label anywhere. This is what makes FIX 1 possible: every
    judged posting that contributed a label to ANY component gets a held-out value, not just the
    rows in the stacker's own training population.

    `fixed_C`, when given, skips the inner grouped-CV C search (used for the final stack, which is
    NOT class-weight-balanced -- see FIX 2 -- so the same fixed C is used both here, for the OOF/gate
    numbers, and in the final refit, keeping them on the identical configuration).
    """
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    train_pids = sorted(y_train_by_pid.keys())
    all_relevant = set(train_pids) | set(score_pids)
    fold_ids = sorted({fold_of.get(pid, -1) for pid in all_relevant} - {-1})
    oof = {}
    for k in fold_ids:
        tr_pids = [pid for pid in train_pids if fold_of.get(pid, -1) not in (-1, k)]
        if len({y_train_by_pid[pid] for pid in tr_pids}) < 2:
            continue
        Xtr = np.stack([vec_by_pid[pid] for pid in tr_pids])
        ytr = np.array([y_train_by_pid[pid] for pid in tr_pids], dtype=float)
        sc = None
        if scale:
            sc = StandardScaler().fit(Xtr)
            Xtr = sc.transform(Xtr)
        if fixed_C is not None:
            C = fixed_C
        else:
            groups_tr = [employer_of.get(pid, "?") for pid in tr_pids]
            C = _select_C_inner(Xtr, ytr, groups_tr, C_grid, seed=seed)
        clf = LogisticRegression(class_weight="balanced" if balanced else None, C=C, max_iter=2000, random_state=seed)
        clf.fit(Xtr, ytr)
        apply_pids = [pid for pid in score_pids if fold_of.get(pid) == k and pid in vec_by_pid]
        if not apply_pids:
            continue
        Xte = np.stack([vec_by_pid[pid] for pid in apply_pids])
        if sc is not None:
            Xte = sc.transform(Xte)
        p = clf.predict_proba(Xte)[:, 1]
        for pid, pi in zip(apply_pids, p):
            oof[pid] = float(pi)
    return oof


def _metrics(oof_map: dict, y_by_pid: dict, precisions_at=None) -> dict:
    import numpy as np
    from sklearn.metrics import roc_auc_score
    pids = [pid for pid in oof_map if pid in y_by_pid]
    y = np.array([y_by_pid[pid] for pid in pids])
    p = np.array([oof_map[pid] for pid in pids])
    out = {"n": len(pids), "n_pos": int((y == 1).sum()), "n_neg": int((y == 0).sum())}
    if out["n_pos"] == 0 or out["n_neg"] == 0:
        out["auc"] = None
        return out
    out["auc"] = float(roc_auc_score(y, p))
    if precisions_at:
        order = np.argsort(-p)
        for k in precisions_at:
            top = order[:k]
            out[f"precision@{k}"] = float(y[top].mean()) if len(top) else None
    return out


def _rollup_stats(ps: list, ws: list) -> dict:
    """The six lab roll-up features (phase2_common.rollup_features) from a posting's per-line
    P(unmet) scores `ps` and their weights `ws`. Shared by train() (OOF roll-up) and score()
    (scoring-time roll-up), so the two paths can never drift apart."""
    import numpy as np
    ps_a, ws_a = np.array(ps, dtype=float), np.array(ws, dtype=float)
    wsum = ws_a.sum() or 1.0
    return {"max_p": float(ps_a.max()), "mean_p": float(ps_a.mean()),
            "wmean_p": float((ps_a * ws_a).sum() / wsum), "noisy_or": 1.0 - float(np.prod(1.0 - ps_a)),
            "count_gt_0.5": float((ps_a > 0.5).sum()), "n_lines": float(len(ps_a))}


def _reliability_table(oof_map: dict, y_by_pid: dict, n_bins=5) -> list:
    import numpy as np
    pids = [pid for pid in oof_map if pid in y_by_pid]
    p = np.array([oof_map[pid] for pid in pids])
    y = np.array([y_by_pid[pid] for pid in pids], dtype=float)
    order = np.argsort(p)
    p, y = p[order], y[order]
    bins = np.array_split(np.arange(len(p)), n_bins) if len(p) else []
    rows = []
    for b in bins:
        if len(b) == 0:
            continue
        rows.append({"n": len(b), "mean_pred": float(p[b].mean()), "actual_rate": float(y[b].mean())})
    return rows


# ---------------------------------------------------------------- training
def train(con, log=print) -> dict:
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    pop = _fetch_population(con)
    log(f"required_embed.train: {len(pop)} judged rows (JD>=800)")
    lens_pids = [pid for pid, r in pop.items() if _is_lens_surfaced(r)]
    yB = {pid: (1 if (pop[pid]["required_fit"] or "").lower() == "meets" else 0) for pid in lens_pids}
    yA = {pid: (1 if pop[pid]["required_fit"] == "meets" else 0) for pid in lens_pids
          if (pop[pid]["required_fit"] or "").lower() in ("meets", "fails")}

    all_pids = list(pop.keys())
    seen_by_pid = _seen_units(con, all_pids)
    block_text_by_pid = {pid: _required_block_text(seen_by_pid.get(pid, [])) for pid in all_pids}
    pop_with_block = [pid for pid in lens_pids if block_text_by_pid.get(pid)]
    # FIX 1: EVERY judged posting with a Required block -- not only the lens-surfaced training
    # population -- gets a genuinely held-out score if it can (see _broad_oof). A posting that
    # contributed a label anywhere (TF-IDF's training_set includes every judged row; the line
    # model's quoted-unmet lines come from every judged row) must never receive an in-sample
    # full-model score merely because it fell outside the lens-surfaced stacker population.
    all_block_pids = [pid for pid in all_pids if block_text_by_pid.get(pid)]
    log(f"  lens-surfaced: {len(lens_pids)}; target B population (has Required block): {len(pop_with_block)}; "
        f"all judged rows with a Required block (held-out eligible): {len(all_block_pids)}")

    # ONE employer-grouped fold assignment over every judged posting (not just the target-B
    # population), matching the lab's extract.py, which froze folds over its whole extracted set.
    employer_of = {pid: (pop[pid]["employer"] or "").strip().lower() for pid in all_pids}
    fold_of = _employer_folds(all_pids, employer_of)

    # ---- 1. TF-IDF OOF, from features.training_set(lens="required"), employer folds computed above
    req_rows = F.training_set(con, lens="required")
    req_texts_by_pid, req_y_by_pid = {}, {}
    for r in req_rows:
        pid = r["posting_id"]
        if not pid:
            continue
        req_texts_by_pid[pid] = F.doc_text(r["title"], r["text"], r["company"])
        req_y_by_pid[pid] = int(r["label"])
        employer_of.setdefault(pid, (r["company"] or "").strip().lower())
    # extend fold_of to cover every TF-IDF-training pid not already assigned, deterministically
    extra = [pid for pid in req_texts_by_pid if pid not in fold_of]
    if extra:
        fold_of.update(_employer_folds(extra, employer_of))
    vec_cls, clf_cls = F._pipeline_parts(**TFIDF_PARAMS)
    tfidf_oof = {}
    idx_pids = sorted(req_texts_by_pid.keys())
    idx_by_fold = _group_kfold_indices(idx_pids, fold_of)
    for f, (train_idx, test_idx) in idx_by_fold.items():
        y_tr = [req_y_by_pid[idx_pids[i]] for i in train_idx]
        if len(set(y_tr)) < 2 or not test_idx.size:
            continue
        vec, clf = F._pipeline_parts(**TFIDF_PARAMS)
        try:
            Xtr = vec.fit_transform([req_texts_by_pid[idx_pids[i]] for i in train_idx])
            clf.fit(Xtr, y_tr)
        except ValueError:
            continue  # too few rows/too sparse a vocabulary for this fold (only possible on tiny corpora)
        test_pids = [idx_pids[i] for i in test_idx]
        p = clf.predict_proba(vec.transform([req_texts_by_pid[pid] for pid in test_pids]))[:, 1]
        for pid, pi in zip(test_pids, p):
            tfidf_oof[pid] = float(pi)
    m = _metrics({pid: tfidf_oof[pid] for pid in pop_with_block if pid in tfidf_oof}, yB)
    log(f"  TF-IDF OOF on target B: n={m['n']} AUC={m.get('auc')}")

    # ---- 2. block classifier: bge-base Required-block embedding -> LogReg, TRAINED on lens-surfaced
    # only (the lab found training on all rows hurt), but SCORED (held out) for every judged posting
    # with a block, via _broad_oof -- see FIX 1.
    cache = _EmbedCache(ENCODER_NAME)
    encoder = E.load_encoder(model_name=ENCODER_NAME)
    X_block_all = cache.get_or_encode([block_text_by_pid[pid] for pid in all_block_pids], encoder, query=True, log=log)
    vec_by_pid_block = {pid: X_block_all[i] for i, pid in enumerate(all_block_pids)}
    block_oof_all = _broad_oof(vec_by_pid_block, {pid: yB[pid] for pid in pop_with_block}, all_block_pids,
                               fold_of, employer_of, scale=False)
    m = _metrics({pid: block_oof_all[pid] for pid in pop_with_block if pid in block_oof_all}, yB)
    log(f"  Block-embedding OOF on target B: n={m['n']} AUC={m.get('auc')}")

    # ---- 3. line model: strict labels, line embedding only, roll-up
    line_labels = _line_labels(pop, seen_by_pid)
    line_texts = sorted({l["text"] for l in line_labels})
    X_all_lines = cache.get_or_encode(line_texts, encoder, query=True, log=log) if line_texts else None
    line_vec = {t: X_all_lines[i] for i, t in enumerate(line_texts)} if line_texts else {}

    strict_lines = [l for l in line_labels if l["status"] in ("unmet", "clean_met")]
    line_fold_arr_pids = [l["posting_id"] for l in strict_lines]
    n_folds_present = len(set(fold_of.get(pid, -1) for pid in line_fold_arr_pids) - {-1})

    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import roc_auc_score

    line_oof: dict = {}
    line_aucs = []
    fold_ids_all = sorted(set(fold_of.get(l["posting_id"], -1) for l in line_labels) - {-1})
    for k in fold_ids_all:
        train_lines = [l for l in strict_lines if fold_of.get(l["posting_id"]) not in (k, None) and fold_of.get(l["posting_id"], -1) != -1]
        if len({l["y01"] for l in train_lines}) < 2:
            continue
        Xtr = np.stack([line_vec[l["text"]] for l in train_lines])
        ytr = np.array([l["y01"] for l in train_lines], dtype=float)
        groups_tr = [employer_of.get(l["posting_id"], "?") for l in train_lines]
        sc = StandardScaler().fit(Xtr)
        Xtr_s = sc.transform(Xtr)
        C = _select_C_inner(Xtr_s, ytr, groups_tr, C_GRID, seed=SEED)
        clf = LogisticRegression(class_weight="balanced", C=C, max_iter=1000, random_state=SEED)
        clf.fit(Xtr_s, ytr)
        test_lines = [l for l in line_labels if fold_of.get(l["posting_id"]) == k]
        if not test_lines:
            continue
        Xte = sc.transform(np.stack([line_vec[l["text"]] for l in test_lines]))
        p = clf.predict_proba(Xte)[:, 1]
        for l, pi in zip(test_lines, p):
            line_oof[(l["posting_id"], l["ord"])] = float(pi)
        y_eval = [l["y01"] for l in test_lines if l["status"] in ("unmet", "clean_met")]
        p_eval = [pi for l, pi in zip(test_lines, p) if l["status"] in ("unmet", "clean_met")]
        if len(set(y_eval)) == 2:
            line_aucs.append(roc_auc_score(y_eval, p_eval))
    log(f"  Line-level OOF (unmet~clean_met): n_folds={n_folds_present} "
        f"AUC={float(np.mean(line_aucs)) if line_aucs else None}")

    # roll-up features from OOF line scores, per posting (all lines with an oof score) -- broad over
    # every judged posting with required lines, not only pop_with_block (see FIX 1).
    pid_lines = defaultdict(list)
    line_text_by_key = {}
    for l in line_labels:
        pid_lines[l["posting_id"]].append((l["ord"], l["weight"]))
        line_text_by_key[(l["posting_id"], l["ord"])] = l["text"]

    def rollup_feats(oof_map):
        feats = {}
        for pid, lines in pid_lines.items():
            ps, ws = [], []
            for ordn, w in lines:
                key = (pid, ordn)
                if key in oof_map:
                    ps.append(oof_map[key])
                    ws.append(w)
            if not ps:
                continue
            feats[pid] = _rollup_stats(ps, ws)
        return feats

    roll_feats = rollup_feats(line_oof)
    rollup_pids = [pid for pid in pop_with_block if pid in roll_feats]  # TRAIN on lens-surfaced only
    X_roll = np.array([[roll_feats[pid][n] for n in ROLLUP_FEAT_NAMES] for pid in rollup_pids], dtype=np.float32)
    score_pids_roll = [pid for pid in all_block_pids if pid in roll_feats]  # SCORE every judged row with lines
    vec_by_pid_roll = {pid: np.array([roll_feats[pid][n] for n in ROLLUP_FEAT_NAMES], dtype=np.float32)
                       for pid in roll_feats}
    rollup_oof_all = _broad_oof(vec_by_pid_roll, {pid: yB[pid] for pid in rollup_pids}, score_pids_roll,
                                fold_of, employer_of, scale=True)
    m = _metrics({pid: rollup_oof_all[pid] for pid in pop_with_block if pid in rollup_oof_all}, yB)
    log(f"  Roll-up OOF on target B: n={m['n']} AUC={m.get('auc')}")

    # ---- 4. stack: [tfidf_oof, block_oof, rollup_oof] -> LogReg. FIX 2: the final stacker is fit
    # WITHOUT class_weight="balanced" (so its output is a calibrated probability near the ~33% base
    # rate) at a fixed C -- the gate's OOF numbers, the reliability table and every stored OOF score
    # must come from that SAME unbalanced, fixed-C configuration, or the gate would be measuring a
    # different model than the one actually shipped. Chose the "fix C=1.0 in both" option from the
    # audit (rather than picking C once by inner grouped CV on all stack rows) -- three raw
    # probabilities as input features leave little for C to do, so a fixed C keeps the OOF gate and
    # the shipped model trivially identical in configuration rather than merely close.
    STACK_C = 1.0
    stack_pids = [pid for pid in pop_with_block if pid in tfidf_oof and pid in block_oof_all and pid in rollup_oof_all]
    y_train_stack = {pid: yB[pid] for pid in stack_pids}
    stack_vec_by_pid_all = {pid: np.array([tfidf_oof[pid], block_oof_all[pid], rollup_oof_all[pid]])
                            for pid in all_block_pids if pid in tfidf_oof and pid in block_oof_all
                            and pid in rollup_oof_all}
    stack_oof_all = _broad_oof(stack_vec_by_pid_all, y_train_stack, list(stack_vec_by_pid_all.keys()),
                               fold_of, employer_of, scale=False, balanced=False, fixed_C=STACK_C)
    X_stack = np.array([[tfidf_oof[pid], block_oof_all[pid], rollup_oof_all[pid]] for pid in stack_pids])
    y_stack = [yB[pid] for pid in stack_pids]
    m_stack = _metrics({pid: stack_oof_all[pid] for pid in stack_pids if pid in stack_oof_all}, yB,
                       precisions_at=(25, 50, 100))
    m_tfidf_only = _metrics({pid: tfidf_oof[pid] for pid in stack_pids}, yB)

    # target A (meets vs fails) using the same stack OOF, restricted to yA population
    stack_oof_A = {pid: stack_oof_all[pid] for pid in stack_oof_all if pid in yA}
    m_A = _metrics(stack_oof_A, yA, precisions_at=(25, 50, 100))

    # FIX 3: a REAL end-to-end sanity check -- shuffle the target-B label ONCE (seeded), then rerun
    # the block -> roll-up -> stack chain against the shuffled target (all three are just logistic
    # regressions over already-cached embeddings/features, so this is cheap). TF-IDF's OOF and the
    # line model's OOF are reused UNSHUFFLED (retraining those per-shuffle is expensive and the leak
    # this sanity check guards against lives entirely in the block/roll-up/stack chain), which is
    # noted in the log line below rather than left implicit.
    rng = np.random.RandomState(SEED)
    shuf_order = list(pop_with_block)
    shuf_vals = [yB[pid] for pid in shuf_order]
    rng.shuffle(shuf_vals)
    yB_shuf = dict(zip(shuf_order, shuf_vals))
    block_oof_shuf = _broad_oof(vec_by_pid_block, yB_shuf, pop_with_block, fold_of, employer_of, scale=False)
    rollup_pids_shuf = [pid for pid in pop_with_block if pid in roll_feats]
    rollup_oof_shuf = _broad_oof(vec_by_pid_roll, {pid: yB_shuf[pid] for pid in rollup_pids_shuf},
                                 rollup_pids_shuf, fold_of, employer_of, scale=True)
    stack_vec_shuf = {pid: np.array([tfidf_oof[pid], block_oof_shuf[pid], rollup_oof_shuf[pid]])
                      for pid in pop_with_block if pid in tfidf_oof and pid in block_oof_shuf
                      and pid in rollup_oof_shuf}
    stack_oof_shuf = _broad_oof(stack_vec_shuf, {pid: yB_shuf[pid] for pid in stack_vec_shuf},
                                list(stack_vec_shuf.keys()), fold_of, employer_of, scale=False,
                                balanced=False, fixed_C=STACK_C)
    m_shuf = _metrics(stack_oof_shuf, yB_shuf)
    log("  [sanity: block/roll-up/stack retrained on a once-shuffled target-B label; "
        "TF-IDF OOF and the line model's OOF are reused unshuffled (they are not shuffled and not "
        "retrained -- the leak this check guards against lives in the block/roll-up/stack chain)]")
    log(f"  Shuffled end-to-end sanity AUC: {m_shuf.get('auc')} (expect ~0.45-0.56)")
    # FIX 3: an end-to-end check on a randomized target must land near chance. Above 0.60 means the
    # block/roll-up/stack chain can still predict a label it was never given real information about
    # -- i.e. a leak survived FIX 1/2 -- so training STOPS rather than shipping a model on top of it.
    # A shuffled AUC is itself a noisy statistic at small n (a handful of postings can swing it well
    # past 0.60 by chance alone -- true on the tiny synthetic corpora the test suite trains on, which
    # otherwise never approaches the real corpus's ~680-row scale). Gate only once there's enough of a
    # sample for 0.60 to mean something; below that, the number is still logged, just not enforced.
    SHUFFLE_GATE_MIN_N = 30
    shuffle_leak = (m_shuf.get("n", 0) >= SHUFFLE_GATE_MIN_N and m_shuf.get("auc") is not None
                    and m_shuf["auc"] > 0.60)
    if shuffle_leak:
        log(f"  SHUFFLE SANITY FAILED: AUC {m_shuf['auc']} > 0.60 -- stopping, not fitting/saving a model.")

    reliability = _reliability_table({pid: stack_oof_all[pid] for pid in stack_pids if pid in stack_oof_all},
                                     yB, n_bins=5)

    gate_auc_ok = m_stack["auc"] is not None and m_stack["auc"] >= 0.71
    gate_lift_ok = (m_stack["auc"] is not None and m_tfidf_only["auc"] is not None
                    and (m_stack["auc"] - m_tfidf_only["auc"]) >= 0.05)
    accepted = gate_auc_ok and gate_lift_ok and not shuffle_leak

    log("\n=== ACCEPTANCE GATE ===")
    log(f"Target B: n={m_stack['n']} n_pos={m_stack['n_pos']} n_neg={m_stack['n_neg']}")
    log(f"  TF-IDF alone AUC:  {m_tfidf_only.get('auc')}")
    log(f"  Stack AUC:         {m_stack.get('auc')} (gate: >= 0.71 AND >= tfidf+0.05)")
    log(f"  precision@25/50/100: {m_stack.get('precision@25')}/{m_stack.get('precision@50')}/{m_stack.get('precision@100')}")
    log(f"  Shuffled end-to-end sanity AUC: {m_shuf.get('auc')} (expect ~0.45-0.56; see FIX 3 note above)")
    log(f"  Target A (meets vs fails): n={m_A['n']} AUC={m_A.get('auc')} "
        f"precision@25/50/100={m_A.get('precision@25')}/{m_A.get('precision@50')}/{m_A.get('precision@100')}")
    log(f"  Reliability (5 bins, mean_pred vs actual_rate): {reliability}")
    log(f"  GATE {'PASSED' if accepted else 'FAILED'}" + ("  (shuffle-sanity leak)" if shuffle_leak else ""))

    result = {
        "accepted": accepted, "n": m_stack["n"], "n_pos": m_stack["n_pos"], "n_neg": m_stack["n_neg"],
        "auc_stack_B": m_stack.get("auc"), "auc_tfidf_only_B": m_tfidf_only.get("auc"),
        "precision_at_25": m_stack.get("precision@25"), "precision_at_50": m_stack.get("precision@50"),
        "precision_at_100": m_stack.get("precision@100"),
        "auc_A": m_A.get("auc"), "n_A": m_A.get("n"),
        "shuffled_auc": m_shuf.get("auc"), "shuffle_leak": shuffle_leak, "reliability": reliability,
        "oof_stack": stack_oof_all,
    }

    if not accepted:
        log("Acceptance gate FAILED: not fitting/saving a final model, not wiring the rank.")
        return result

    # ---- refit final models on ALL data
    final_vec, final_clf = F._pipeline_parts(**TFIDF_PARAMS)
    try:
        final_clf.fit(final_vec.fit_transform([req_texts_by_pid[pid] for pid in idx_pids]),
                     [req_y_by_pid[pid] for pid in idx_pids])
    except ValueError:
        # too sparse a vocabulary to prune at these params (only possible on a tiny corpus); fall back
        # to an unpruned vectorizer so the bundle still has a usable (if weak) TF-IDF component.
        final_vec, final_clf = F._pipeline_parts(TFIDF_PARAMS["C"], min_df=1, ngram=TFIDF_PARAMS["ngram"],
                                                 max_features=TFIDF_PARAMS["max_features"], max_df=1.0)
        final_clf.fit(final_vec.fit_transform([req_texts_by_pid[pid] for pid in idx_pids]),
                     [req_y_by_pid[pid] for pid in idx_pids])

    block_pids = pop_with_block
    X_block = np.stack([vec_by_pid_block[pid] for pid in block_pids])
    block_clf = LogisticRegression(class_weight="balanced",
                                   C=_select_C_inner(X_block, np.array([yB[pid] for pid in block_pids], dtype=float),
                                                     [employer_of.get(pid, "?") for pid in block_pids]),
                                   max_iter=2000, random_state=SEED)
    block_clf.fit(X_block, [yB[pid] for pid in block_pids])

    line_sc = StandardScaler().fit(np.stack([line_vec[l["text"]] for l in strict_lines]))
    Xtr_all = line_sc.transform(np.stack([line_vec[l["text"]] for l in strict_lines]))
    y_all_lines = np.array([l["y01"] for l in strict_lines], dtype=float)
    line_clf = LogisticRegression(class_weight="balanced",
                                  C=_select_C_inner(Xtr_all, y_all_lines,
                                                    [employer_of.get(l["posting_id"], "?") for l in strict_lines]),
                                  max_iter=1000, random_state=SEED)
    line_clf.fit(Xtr_all, y_all_lines)

    # final roll-up features use the FINAL (in-sample) line model, matching "final models refit on all data"
    p_all_lines = line_clf.predict_proba(Xtr_all)[:, 1]
    line_p_final = {(l["posting_id"], l["ord"]): float(p) for l, p in zip(strict_lines, p_all_lines)}
    # every line (not only strict) also gets a final score, for scoring-time roll-up completeness
    other_lines = [l for l in line_labels if l["status"] not in ("unmet", "clean_met")]
    if other_lines:
        Xo = line_sc.transform(np.stack([line_vec[l["text"]] for l in other_lines]))
        po = line_clf.predict_proba(Xo)[:, 1]
        for l, p in zip(other_lines, po):
            line_p_final[(l["posting_id"], l["ord"])] = float(p)
    roll_feats_final = rollup_feats(line_p_final)
    rollup_sc = StandardScaler().fit(X_roll)
    rollup_clf = LogisticRegression(class_weight="balanced",
                                    C=_select_C_inner(rollup_sc.transform(X_roll),
                                                      np.array([yB[pid] for pid in rollup_pids], dtype=float),
                                                      [employer_of.get(pid, "?") for pid in rollup_pids]),
                                    max_iter=2000, random_state=SEED)
    rollup_clf.fit(rollup_sc.transform(X_roll), [yB[pid] for pid in rollup_pids])

    stack_clf = LogisticRegression(C=STACK_C, max_iter=2000, random_state=SEED)  # no class_weight: calibrated prob
    stack_clf.fit(X_stack, y_stack)

    # FIX 1: build the held-out-value table used by score() for every judged posting that has a
    # computable stack OOF value (all_block_pids ∩ tfidf_oof ∩ block_oof_all ∩ rollup_oof_all) --
    # not just stack_pids (the narrower, lens-surfaced-only population the gate is measured on).
    oof_components = {}
    for pid, stack_p in stack_oof_all.items():
        worst_key, worst_p, n_lines = None, None, 0
        for ordn, _w in pid_lines.get(pid, []):
            key = (pid, ordn)
            p = line_oof.get(key)
            if p is None:
                continue
            n_lines += 1
            if worst_p is None or p > worst_p:
                worst_p, worst_key = p, line_text_by_key.get(key)
        oof_components[pid] = {"tfidf_p": tfidf_oof[pid], "block_p": block_oof_all[pid],
                               "rollup_p": rollup_oof_all[pid], "n_lines": n_lines,
                               "worst_line": worst_key, "worst_line_p": worst_p}
    log(f"  held-out (OOF) values available for {len(oof_components)} judged postings with a Required "
        f"block (of {len(all_block_pids)} total) -- these NEVER get an in-sample score at scoring time")

    import joblib
    version = hashlib.sha1(json.dumps(sorted(stack_pids)).encode()).hexdigest()[:12]
    bundle = {"version": version, "encoder_name": ENCODER_NAME, "tfidf_vec": final_vec, "tfidf_clf": final_clf,
              "block_clf": block_clf, "line_scaler": line_sc, "line_clf": line_clf,
              "rollup_scaler": rollup_sc, "rollup_clf": rollup_clf, "stack_clf": stack_clf,
              "oof_pids": set(stack_oof_all.keys()), "oof_scores": dict(stack_oof_all),
              "oof_components": oof_components, "seed": SEED, "n_folds": N_FOLDS}
    os.makedirs(F.MODEL_DIR, exist_ok=True)
    path = os.path.join(F.MODEL_DIR, f"required_embed_{version}.joblib")
    joblib.dump(bundle, path)
    stored_path = os.path.relpath(path, F.REPO_ROOT)
    con.execute("INSERT OR REPLACE INTO models (model_version, kind, trained_at, n_pos, n_neg, cv_auc, path, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [version, MODEL_KIND, _now(), m_stack["n_pos"], m_stack["n_neg"], m_stack.get("auc"), stored_path,
                 json.dumps({k: v for k, v in result.items() if k != "oof_stack"})])
    result["model_version"] = version
    result["path"] = path
    log(f"Saved required_embed model {version} -> {path}")
    return result


# ---------------------------------------------------------------- load / score
def load_latest(con, log=print) -> Optional[dict]:
    row = con.execute("SELECT model_version, path FROM models WHERE kind = ? ORDER BY trained_at DESC LIMIT 1",
                      [MODEL_KIND]).fetchone()
    if not row:
        return None
    version, path = row
    resolved = path if os.path.isabs(path) else os.path.join(F.REPO_ROOT, path)
    if not os.path.exists(resolved):
        resolved = os.path.join(F.MODEL_DIR, os.path.basename(path))
    if not os.path.exists(resolved):
        log(f"required_embed model {version}: file missing; skipping.")
        return None
    import joblib
    return joblib.load(resolved)


SCORE_POPULATION_SQL = """
    SELECT f.posting_id, p.description_hash, s.fit_required
    FROM vw_lens_fit f
    JOIN postings p USING (posting_id)
    JOIN vw_screen_latest s USING (posting_id)
    WHERE p.status = 'active' AND f.verdict != 'reject'
      AND (f.lens_best >= 0.5
           OR greatest(coalesce(s.fit_process, 0), coalesce(s.fit_technical, 0), coalesce(s.fit_ai, 0)) >= 0.5)
      AND EXISTS (SELECT 1 FROM requirement_units ru WHERE ru.posting_id = f.posting_id
                  AND ru.klass = 'work' AND ru.grp = 'required')
"""


def score(con, *, all_rows: bool = False, log=print) -> dict:
    """Scores the population defined by SCORE_POPULATION_SQL.

    FIX 1: a posting that is in the model's held-out table (`bundle["oof_pids"]` -- EVERY judged
    posting with a Required block that could get a genuinely out-of-fold value at train time, not
    only the lens-surfaced stacker-training population) gets that stored held-out value verbatim,
    `is_oof=True`, with NO re-embedding and NO full-model inference -- it must never receive an
    in-sample score just because it happened to be outside the narrower training population.
    Every other posting (never judged, or judged after this model was trained) gets a fresh
    full-model score, `is_oof=False`.

    Skips postings already scored under this model_version + current description_hash unless
    `all_rows`. Never touches `screens`."""
    import numpy as np
    bundle = load_latest(con, log=log)
    if bundle is None:
        log("required_embed.score: no trained model; run `finder.py required-embed train` first.")
        return {"scored": 0}
    version = bundle["version"]
    rows = con.execute(SCORE_POPULATION_SQL).fetchall()
    pop = {pid: {"description_hash": dh, "fit_required": fr} for pid, dh, fr in rows}
    n_population = len(pop)
    log(f"required_embed.score: population {n_population} rows")
    if not all_rows:
        scored_now = {(pid, dh) for pid, dh in con.execute(
            "SELECT posting_id, description_hash FROM required_embed WHERE model_version = ?", [version]).fetchall()}
        pop = {pid: v for pid, v in pop.items() if (pid, v["description_hash"]) not in scored_now}
    if not pop:
        log("required_embed.score: nothing to score.")
        return {"scored": 0, "population": n_population}

    pids = list(pop.keys())
    seen_by_pid = _seen_units(con, pids)
    no_units = [pid for pid in pids if not seen_by_pid.get(pid)]
    pids = [pid for pid in pids if seen_by_pid.get(pid)]

    oof_pids = bundle["oof_pids"]
    oof_here = [pid for pid in pids if pid in oof_pids]
    fresh_pids = [pid for pid in pids if pid not in oof_pids]

    now = _now()
    inserted = []

    # ---- OOF path: exact stored held-out values, no re-embedding, no full-model inference.
    for pid in oof_here:
        comp = bundle["oof_components"][pid]
        inserted.append((pid, pop[pid]["description_hash"], version, bundle["oof_scores"][pid],
                         comp["block_p"], comp["rollup_p"], comp["tfidf_p"], comp["n_lines"],
                         comp["worst_line"], comp["worst_line_p"], True, now))

    # ---- fresh path: full (in-sample-trained but never-judged-here) models, only for postings
    # that never contributed a label to any component.
    skipped_no_rollup, skipped_no_tfidf = 0, 0
    if fresh_pids:
        cache = _EmbedCache(bundle["encoder_name"])
        encoder = E.load_encoder(model_name=bundle["encoder_name"])

        block_texts = [_required_block_text(seen_by_pid[pid]) for pid in fresh_pids]
        X_block = cache.get_or_encode(block_texts, encoder, query=True, log=log)
        block_p = dict(zip(fresh_pids, bundle["block_clf"].predict_proba(X_block)[:, 1]))

        line_records = []  # (pid, ord, text, weight)
        for pid in fresh_pids:
            for u in seen_by_pid[pid]:
                if u["grp"] == "required":
                    line_records.append((pid, u["ord"], u["text"], u["weight"]))
        line_texts = sorted({r[2] for r in line_records})
        X_lines = cache.get_or_encode(line_texts, encoder, query=True, log=log) if line_texts else None
        line_vec = {t: X_lines[i] for i, t in enumerate(line_texts)} if line_texts else {}
        line_p_by_key = {}
        if line_records:
            X_all = bundle["line_scaler"].transform(np.stack([line_vec[r[2]] for r in line_records]))
            p_all = bundle["line_clf"].predict_proba(X_all)[:, 1]
            for (pid, ordn, text, weight), p in zip(line_records, p_all):
                line_p_by_key[(pid, ordn)] = (float(p), text)

        pid_lines = defaultdict(list)
        for pid, ordn, text, weight in line_records:
            pid_lines[pid].append((ordn, weight))

        def rollup_row(pid):
            ps, ws = [], []
            for ordn, w in pid_lines.get(pid, []):
                key = (pid, ordn)
                if key in line_p_by_key:
                    ps.append(line_p_by_key[key][0])
                    ws.append(w)
            if not ps:
                return None
            return _rollup_stats(ps, ws)

        for pid in fresh_pids:
            rf = rollup_row(pid)
            if rf is None:
                skipped_no_rollup += 1
                continue
            tfidf_p = pop[pid]["fit_required"]
            if tfidf_p is None:
                skipped_no_tfidf += 1
                continue
            bp = float(block_p[pid])
            rollup_p = float(bundle["rollup_clf"].predict_proba(
                bundle["rollup_scaler"].transform(np.array([[rf[n] for n in ROLLUP_FEAT_NAMES]], dtype=np.float32))
            )[:, 1][0])
            embed_p = float(bundle["stack_clf"].predict_proba(np.array([[tfidf_p, bp, rollup_p]]))[:, 1][0])
            worst_key, worst_p = None, None
            for ordn, _w in pid_lines.get(pid, []):
                p, text = line_p_by_key.get((pid, ordn), (None, None))
                if p is not None and (worst_p is None or p > worst_p):
                    worst_p, worst_key = p, text
            inserted.append((pid, pop[pid]["description_hash"], version, embed_p, bp, rollup_p, tfidf_p,
                             int(rf["n_lines"]), worst_key, worst_p, False, now))

    if inserted:
        con.executemany("INSERT OR REPLACE INTO required_embed VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", inserted)
    n_oof = sum(1 for r in inserted if r[10])
    n_fresh = len(inserted) - n_oof
    n_scored = len(inserted)
    n_not_scored = len(pids) - n_scored  # skipped_no_rollup + skipped_no_tfidf, among fresh_pids only
    log(f"required_embed.score: scored {n_scored} ({n_oof} OOF, {n_fresh} fresh); "
        f"skipped {len(no_units)} for no requirement units, {skipped_no_rollup} for no Required-group "
        f"lines surviving the top-{UNITS_PER_POSTING} cut, {skipped_no_tfidf} for no stored fit_required")
    return {"population": n_population, "scored": n_scored, "oof": n_oof, "fresh": n_fresh,
            "skipped_no_units": len(no_units), "skipped_no_rollup": skipped_no_rollup,
            "skipped_no_tfidf": skipped_no_tfidf}
