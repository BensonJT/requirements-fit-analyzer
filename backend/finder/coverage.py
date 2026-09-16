"""Requirement coverage: how much of what a JD asks for the user's evidence shows (sprint plan §15.3 as amended
by §16.1-§16.6).

Per requirement unit: the raw cosine to every evidence unit decides a band (strong >= COVER_STRONG, partial >=
COVER_PARTIAL, else gap); credit = band credit (1.0 / 0.5 / 0) x the evidence kind weight, best over evidence
units. A `not_in_record` term forces a gap; a `light_in_record` term caps the band at partial. Each unit's weight
is its section weight x specificity (§16.3), so a line every JD carries counts little.

coverage_required (Required + Preferred units) is the gated figure; coverage_role (Responsibilities / body) is
reported and stands in only when Required has fewer than MIN_WORK units. Step 3a: stored and shown, weight 0.
numpy is imported inside functions.
"""
import math
import re
from typing import Optional

from . import rules
from .evidence import KIND_WEIGHTS

DEFAULT_STRONG, DEFAULT_PARTIAL = 0.72, 0.62
BAND_CREDIT = {"strong": 1.0, "partial": 0.5, "gap": 0.0}
REQ_DUP = 0.92          # cosine at which two requirement units are the same claim (specificity df)
SPEC_FLOOR = 0.2
MIN_WORK = 3
MAX_UNITS = 40
KINDS = tuple(KIND_WEIGHTS)


def term_cap(text: str, not_in_record: list, light_in_record: list) -> tuple:
    """('not_in_record' | 'light_in_record' | None, the term). A requirement naming both is a gap."""
    hits = rules.find_terms(not_in_record, text)
    if hits:
        return "not_in_record", hits[0]
    hits = rules.find_terms(light_in_record, text)
    return ("light_in_record", hits[0]) if hits else (None, None)


def best_by_kind(req_vecs, evid_vecs, evid_kinds: list):
    """(max cosine, argmax evidence index) per requirement per kind: arrays shaped (n_req, n_kinds)."""
    import numpy as np
    n_req = req_vecs.shape[0]
    best = np.full((n_req, len(KINDS)), -1.0, dtype=np.float32)
    arg = np.full((n_req, len(KINDS)), -1, dtype=np.int64)
    if n_req == 0 or evid_vecs.shape[0] == 0:
        return best, arg
    sims = req_vecs @ evid_vecs.T
    kinds = np.asarray(evid_kinds)
    for k, kind in enumerate(KINDS):
        cols = np.nonzero(kinds == kind)[0]
        if cols.size:
            sub = sims[:, cols]
            j = sub.argmax(axis=1)
            best[:, k] = sub[np.arange(n_req), j]
            arg[:, k] = cols[j]
    return best, arg


def band_of(cos: float, strong: float, partial: float) -> str:
    return "strong" if cos >= strong else "partial" if cos >= partial else "gap"


def credit_row(best_row, arg_row, strong: float, partial: float, cap: Optional[str]) -> tuple:
    """(band, credit, evidence index, cosine) for one requirement: the kind giving the most credit (ties: higher
    cosine). §16.2: the band reads the raw cosine; the kind weight scales credit."""
    top = ("gap", 0.0, int(arg_row.max()) if len(arg_row) else -1, float(best_row.max()) if len(best_row) else -1.0)
    best_key = (0.0, top[3])
    for k, kind in enumerate(KINDS):
        cos = float(best_row[k])
        if arg_row[k] < 0:
            continue
        band = band_of(cos, strong, partial)
        if cap == "not_in_record":
            band = "gap"
        elif cap == "light_in_record" and band == "strong":
            band = "partial"
        credit = BAND_CREDIT[band] * KIND_WEIGHTS[kind]
        if (credit, cos) > best_key:
            best_key, top = (credit, cos), (band, credit, int(arg_row[k]), cos)
    return top


def specificity(df: int, n_docs: int) -> float:
    """clamp(log(N / df) / log(N), SPEC_FLOOR, 1)."""
    if n_docs <= 1:
        return 1.0
    return max(SPEC_FLOOR, min(1.0, math.log(n_docs / max(df, 1)) / math.log(n_docs)))


def doc_frequencies(unit_vecs, unit_owner: list, ref_vecs, ref_owner, chunk: int = 512) -> list:
    """For each unit: 1 + the number of distinct OTHER reference postings holding a unit with cosine >= REQ_DUP."""
    import numpy as np
    ref_owner = np.asarray(ref_owner)
    out = []
    for start in range(0, unit_vecs.shape[0], chunk):
        sims = unit_vecs[start:start + chunk] @ ref_vecs.T
        rows, cols = np.nonzero(sims >= REQ_DUP)
        owners = ref_owner[cols]
        for i in range(sims.shape[0]):
            own = unit_owner[start + i]
            hit = owners[rows == i]
            out.append(1 + len(set(hit.tolist()) - {own}))
    return out


def cap_units(units: list, max_units: int = MAX_UNITS) -> list:
    """Work units beyond the cap drop out, lowest section weight x specificity first (§16.3)."""
    work = [i for i, u in enumerate(units) if u["klass"] == "work"]
    if len(work) <= max_units:
        return units
    keep = set(sorted(work, key=lambda i: (-units[i]["weight"] * (units[i].get("spec") or 1.0), i))[:max_units])
    return [u for i, u in enumerate(units) if u["klass"] != "work" or i in keep]


def score_doc(units: list, credits: list, evidence: list) -> dict:
    """Coverage figures for one JD. `units` are dicts (text, grp, weight, spec, klass, cap, cap_term); `credits`
    aligns with the work units in order: (band, credit, evidence index, cosine)."""
    groups = {"required": [], "role": []}
    work = [u for u in units if u["klass"] == "work"]
    for u, c in zip(work, credits):
        groups[u["grp"]].append((u, c))
    out = {}
    for name, items in groups.items():
        total = sum(u["weight"] * (u.get("spec") or 1.0) for u, _ in items)
        got = sum(u["weight"] * (u.get("spec") or 1.0) * c[1] for u, c in items)
        out[f"coverage_{name}"] = round(100.0 * got / total, 1) if len(items) >= MIN_WORK and total > 0 else None
        out[f"n_{name}"] = len(items)
        out[f"n_{name}_strong"] = sum(1 for _, c in items if c[0] == "strong")
        out[f"n_{name}_partial"] = sum(1 for _, c in items if c[0] == "partial")
    ranked = sorted(zip(work, credits), key=lambda uc: -(uc[0]["weight"] * (uc[0].get("spec") or 1.0)))
    gap_pool = [uc for uc in ranked if uc[1][0] == "gap"]
    gap_pool.sort(key=lambda uc: uc[0]["grp"] != "required")
    out["gaps"] = [u["text"] + (f" [{u['cap']}: {u['cap_term']}]" if u.get("cap") else "") for u, _ in gap_pool[:3]]
    matched = [uc for uc in ranked if uc[1][0] != "gap"]
    matched.sort(key=lambda uc: -(uc[1][1] * uc[0]["weight"] * (uc[0].get("spec") or 1.0)))
    out["matches"] = [[u["text"], evidence[c[2]]["ref"], round(c[3], 3), evidence[c[2]]["source"],
                       evidence[c[2]]["kind"], c[0]] + ([u["cap"]] if u.get("cap") else [])
                      for u, c in matched[:5]]
    out["notes"] = {"domain": [u["text"] for u in units if u["klass"] == "domain"],
                    "level_lines": sum(1 for u in units if u["klass"] == "level"),
                    "logistics_lines": sum(1 for u in units if u["klass"] == "logistics"),
                    "capped": [[u["text"][:120], u["cap"], u["cap_term"]] for u in work if u.get("cap")],
                    "sources": _source_histogram(credits, evidence)}
    return out


def _source_histogram(credits: list, evidence: list) -> dict:
    hist = {}
    for band, _, idx, _ in credits:
        if band != "gap" and idx >= 0:
            hist[evidence[idx]["source"]] = hist.get(evidence[idx]["source"], 0) + 1
    return hist


def primary(cov: dict) -> Optional[float]:
    """The gated figure: coverage_required, else coverage_role."""
    return cov.get("coverage_required") if cov.get("coverage_required") is not None else cov.get("coverage_role")


def auc(pos: list, neg: list) -> Optional[float]:
    """Mann-Whitney AUC with ties counted half; None when a side is empty."""
    pos, neg = [p for p in pos if p is not None], [n for n in neg if n is not None]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


# ================================================================ store: requirement units, coverage rows
import json  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from . import requirements as R  # noqa: E402
from .embed import stack, vectors_json  # noqa: E402

SURVIVOR_SQL = """
SELECT p.posting_id FROM postings p JOIN vw_screen_latest s USING (posting_id)
WHERE p.status = 'active' AND s.verdict != 'reject' AND p.description_text IS NOT NULL"""


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def current_calibration(con) -> dict:
    """Newest `models` row of kind 'coverage' (thresholds in notes JSON), else the defaults."""
    row = con.execute("SELECT model_version, notes FROM models WHERE kind = 'coverage' "
                      "ORDER BY trained_at DESC LIMIT 1").fetchone()
    calib = {"version": "default", "strong": DEFAULT_STRONG, "partial": DEFAULT_PARTIAL, "reject": None,
             "review": None, "blend": None}
    if row:
        calib.update(json.loads(row[1] or "{}"))
        calib["version"] = row[0]
    return calib


def survivor_ids(con, *, evidence_version: Optional[str] = None, model: Optional[str] = None,
                 calibration: Optional[str] = None, since=None, limit=None) -> list:
    """Active, not-rejected postings with a JD, best first. With the three versions given, only those without a
    coverage row for their current JD hash under them."""
    sql, params = SURVIVOR_SQL, []
    if evidence_version:
        sql = sql.replace("WHERE", """LEFT JOIN coverage c ON c.posting_id = p.posting_id
              AND c.description_hash = coalesce(p.description_hash, '') AND c.evidence_version = ? AND c.model = ?
              AND c.calibration = ? WHERE""", 1) + " AND c.posting_id IS NULL"
        params += [evidence_version, model, calibration]
    if since is not None:
        sql += " AND (p.first_seen_at >= ? OR p.description_fetched_at >= ?)"
        params += [since, since]
    sql += " ORDER BY s.final_score DESC, p.posting_id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [r[0] for r in con.execute(sql, params).fetchall()]


def _unit_dicts(reqs: list) -> list:
    return [{"text": u.text, "section": u.section, "grp": u.group, "weight": u.weight, "klass": u.klass,
             "unit_hash": u.unit_hash, "spec": None} for u in reqs]


def units_for_text(text: str, encoder) -> tuple:
    """(unit dicts, work-unit vectors) for a JD that is not a stored posting (vault-only label text)."""
    units = _unit_dicts(R.split_requirements(text))
    work = [u["text"] for u in units if u["klass"] == "work"]
    return units, encoder.encode(work, query=True)


def ensure_requirement_units(con, posting_ids: list, encoder, model: str, log=print) -> dict:
    """posting_id -> (unit dicts in order, work-unit vector matrix). Cached per (JD hash, splitter, model); only
    postings without a cache row are split and embedded, with identical unit texts encoded once."""
    import numpy as np
    splitter = R.splitter_fingerprint()
    if not posting_ids:
        return {}
    ids_json = json.dumps(sorted(set(posting_ids)))
    rows = con.execute("SELECT posting_id, description_hash, description_text FROM postings WHERE posting_id IN "
                       "(SELECT unnest(json_transform(?, '[\"VARCHAR\"]')))", [ids_json]).fetchall()
    cached = {r[0] for r in con.execute("""
        SELECT DISTINCT r.posting_id FROM requirement_units r JOIN postings p
          ON p.posting_id = r.posting_id AND coalesce(p.description_hash, '') = r.description_hash
        WHERE r.splitter = ? AND r.model = ? AND r.posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))""",
                                              [splitter, model, ids_json]).fetchall()}
    todo = [(pid, h, t) for pid, h, t in rows if pid not in cached and t]
    if todo:
        split = {pid: (h, _unit_dicts(R.split_requirements(t)) or [_placeholder()]) for pid, h, t in todo}
        texts = sorted({u["text"] for _, units in split.values() for u in units if u["klass"] == "work"})
        vecs = encoder.encode(texts, query=True) if texts else np.zeros((0, 384), dtype=np.float32)
        index = {t: i for i, t in enumerate(texts)}
        now, payload = _now(), []
        for pid, (h, units) in split.items():
            for ord_, u in enumerate(units):
                vec = vecs[index[u["text"]]] if u["klass"] == "work" else None
                payload.append({"posting_id": pid, "description_hash": h or "", "ord": ord_, **u,
                                "vec": None if vec is None else json.loads(vectors_json(vec[None, :]))[0]})
        _insert_requirement_units(con, payload, splitter, model, now)
        log(f"  requirement units: {len(todo)} postings split, {len(texts)} distinct units embedded")
    return load_requirement_units(con, posting_ids, model)


def _placeholder() -> dict:
    """A marker row so a JD with no units is cached as such and never re-split."""
    return {"text": "", "section": "body", "grp": "role", "weight": 0.0, "klass": "empty", "unit_hash": "", "spec": None}


def _insert_requirement_units(con, payload: list, splitter: str, model: str, now) -> None:
    shape = json.dumps([{"posting_id": "VARCHAR", "description_hash": "VARCHAR", "ord": "INTEGER", "text": "VARCHAR",
                         "section": "VARCHAR", "grp": "VARCHAR", "weight": "DOUBLE", "klass": "VARCHAR",
                         "unit_hash": "VARCHAR", "vec": "FLOAT[]"}])
    con.execute("BEGIN")
    try:
        for start in range(0, len(payload), 5000):
            con.execute("""
                INSERT OR REPLACE INTO requirement_units (posting_id, description_hash, splitter, ord, unit_hash, text,
                    section, grp, weight, klass, spec, model, vector, embedded_at)
                SELECT posting_id, description_hash, $3, ord, unit_hash, text, section, grp, weight, klass, NULL, $4,
                       vec::FLOAT[384], $5
                FROM (SELECT unnest(json_transform($1, $2), recursive := true))""",
                        [json.dumps(payload[start:start + 5000]), shape, splitter, model, now])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def load_requirement_units(con, posting_ids: list, model: str) -> dict:
    """posting_id -> (unit dicts, work-unit matrix) from the cache for each posting's current JD hash."""
    import numpy as np
    splitter = R.splitter_fingerprint()
    ids_json = json.dumps(sorted(set(posting_ids)))
    base = """FROM requirement_units r JOIN postings p ON p.posting_id = r.posting_id
              AND coalesce(p.description_hash, '') = r.description_hash
              WHERE r.splitter = ? AND r.model = ? AND r.klass != 'empty'
                AND r.posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))"""
    meta = con.execute(f"SELECT r.posting_id, r.text, r.section, r.grp, r.weight, r.klass, r.unit_hash, r.spec {base} "
                       "ORDER BY r.posting_id, r.ord", [splitter, model, ids_json]).fetchall()
    vec = con.execute(f"SELECT r.posting_id, r.vector {base} AND r.klass = 'work' ORDER BY r.posting_id, r.ord",
                      [splitter, model, ids_json]).fetchnumpy()
    out = {pid: ([], None) for pid in posting_ids}
    for pid, text, section, grp, weight, klass, unit_hash, spec in meta:
        out[pid][0].append({"text": text, "section": section, "grp": grp, "weight": weight, "klass": klass,
                            "unit_hash": unit_hash, "spec": spec})
    owners = np.asarray(vec["posting_id"]).astype(str) if len(vec["posting_id"]) else np.asarray([], dtype=str)
    matrix = stack(vec["vector"])
    for pid in out:
        rows = np.nonzero(owners == pid)[0]
        out[pid] = (out[pid][0], matrix[rows] if rows.size else np.zeros((0, matrix.shape[1] if matrix.ndim == 2 else 384),
                                                                           dtype=np.float32))
    return out


def load_reference(con, model: str) -> tuple:
    """(work-unit matrix, owner posting ids, distinct posting count) over every survivor's cached units: the
    corpus specificity is measured against."""
    import numpy as np
    splitter = R.splitter_fingerprint()
    cur = con.execute(f"""
        SELECT r.posting_id, r.vector FROM requirement_units r
        JOIN postings p ON p.posting_id = r.posting_id AND coalesce(p.description_hash, '') = r.description_hash
        WHERE r.splitter = ? AND r.model = ? AND r.klass = 'work' AND r.posting_id IN ({SURVIVOR_SQL})""",
                      [splitter, model]).fetchnumpy()
    owners = np.asarray(cur["posting_id"]).astype(str) if len(cur["posting_id"]) else np.asarray([], dtype=str)
    owner_set = set(owners.tolist())
    return stack(cur["vector"]), owners, len(owner_set), owner_set


def score_units(units: list, work_vecs, owner: str, reference: tuple, evidence: tuple, calib: dict,
                manifest) -> dict:
    """Specificity against the reference, the §16.3 cap, term caps, credit per work unit, and the doc's figures."""
    import numpy as np
    ref_vecs, ref_owner, n_ref, ref_set = reference
    work_idx = [i for i, u in enumerate(units) if u["klass"] == "work"]
    if work_idx and ref_vecs.shape[0]:
        dfs = doc_frequencies(work_vecs, [owner] * len(work_idx), ref_vecs, ref_owner)
        n_docs = max(n_ref + (0 if owner in ref_set else 1), 2)
        for i, df in zip(work_idx, dfs):
            units[i]["spec"] = round(specificity(df, n_docs), 3)
    for i in work_idx:
        units[i]["cap"], units[i]["cap_term"] = term_cap(units[i]["text"], manifest.not_in_record,
                                                         manifest.light_in_record)
    capped = cap_units(units)
    keep = [i for i in work_idx if units[i] in capped]
    vecs = work_vecs[[work_idx.index(i) for i in keep]] if keep else np.zeros((0, work_vecs.shape[1]), dtype=np.float32)
    ev_units, ev_vecs = evidence
    best, arg = best_by_kind(vecs, ev_vecs, [u["kind"] for u in ev_units])
    credits = [credit_row(best[j], arg[j], calib["strong"], calib["partial"], units[i].get("cap"))
               for j, i in enumerate(keep)]
    result = score_doc([u for u in capped], credits, ev_units)
    result["_best"], result["_caps"] = best, [units[i].get("cap") for i in keep]
    result["_units"] = [units[i] for i in keep]
    return result


def write_coverage(con, rows: list, evidence_version: str, model: str, calibration: str) -> None:
    """INSERT OR REPLACE coverage rows (dicts from score_units plus posting_id and description_hash)."""
    cols = ("posting_id", "description_hash", "coverage_required", "coverage_role", "n_required", "n_required_strong",
            "n_required_partial", "n_role", "n_role_strong", "n_role_partial", "gaps", "matches", "notes")
    shape = {"posting_id": "VARCHAR", "description_hash": "VARCHAR", "coverage_required": "DOUBLE",
             "coverage_role": "DOUBLE", "n_required": "INTEGER", "n_required_strong": "INTEGER",
             "n_required_partial": "INTEGER", "n_role": "INTEGER", "n_role_strong": "INTEGER",
             "n_role_partial": "INTEGER", "gaps": "JSON", "matches": "JSON", "notes": "JSON"}
    payload = json.dumps([{c: r.get(c) for c in cols} for r in rows])
    con.execute("BEGIN")
    try:
        con.execute(f"""
            INSERT OR REPLACE INTO coverage ({', '.join(cols)}, evidence_version, model, calibration, scored_at)
            SELECT {', '.join(cols)}, $3, $4, $5, $6 FROM (SELECT unnest(json_transform($1, $2), recursive := true))""",
                    [payload, json.dumps([shape]), evidence_version, model, calibration, _now()])
        specs = json.dumps([{"posting_id": r["posting_id"], "unit_hash": u["unit_hash"], "spec": u["spec"]}
                            for r in rows for u in r.get("_units", []) if u.get("spec") is not None])
        con.execute("""
            UPDATE requirement_units SET spec = s.spec
            FROM (SELECT unnest(json_transform(?, '[{"posting_id":"VARCHAR","unit_hash":"VARCHAR","spec":"DOUBLE"}]'),
                          recursive := true)) s
            WHERE requirement_units.posting_id = s.posting_id AND requirement_units.unit_hash = s.unit_hash""", [specs])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def cover(con, manifest, encoder, *, posting_ids: Optional[list] = None, all_rows: bool = False, since=None,
          limit=None, batch: int = 200, log=print) -> dict:
    """Coverage for survivors missing it (or every survivor with `all_rows`, or `posting_ids`). Evidence must be
    built (`finder.py evidence --rebuild`). Returns counts and seconds."""
    import time
    from . import evidence as E
    t0 = time.monotonic()
    model = manifest.embed_model
    if E.stored_version(con, model) is not None:
        E.ensure_current(con, manifest, encoder, log=log)
    ev_units, ev_vecs = E.load_matrix(con, model)
    ev_version = E.stored_version(con, model)
    if not ev_units:
        raise RuntimeError("no evidence units stored; run `finder.py evidence --rebuild` first")
    calib = current_calibration(con)
    if posting_ids is None:
        posting_ids = survivor_ids(con, since=since, limit=limit) if all_rows else survivor_ids(
            con, evidence_version=ev_version, model=model, calibration=calib["version"], since=since, limit=limit)
    log(f"Coverage: {len(posting_ids)} postings · evidence {ev_version} ({len(ev_units)} units) · calibration "
        f"{calib['version']} (strong {calib['strong']}, partial {calib['partial']})")
    for start in range(0, len(posting_ids), batch):
        ensure_requirement_units(con, posting_ids[start:start + batch], encoder, model, log=log)
    reference = load_reference(con, model)
    hashes = dict(con.execute("SELECT posting_id, description_hash FROM postings WHERE posting_id IN "
                              "(SELECT unnest(json_transform(?, '[\"VARCHAR\"]')))",
                              [json.dumps(posting_ids)]).fetchall())
    done = 0
    for start in range(0, len(posting_ids), batch):
        chunk = posting_ids[start:start + batch]
        loaded = load_requirement_units(con, chunk, model)
        rows = []
        for pid in chunk:
            units, vecs = loaded.get(pid, ([], None))
            res = score_units(units, vecs, pid, reference, (ev_units, ev_vecs), calib, manifest)
            rows.append({"posting_id": pid, "description_hash": hashes.get(pid) or "", **res})
        write_coverage(con, rows, ev_version, model, calib["version"])
        done += len(chunk)
        log(f"  covered {done}/{len(posting_ids)} ({time.monotonic() - t0:.0f}s)")
    stats = {"covered": done, "seconds": round(time.monotonic() - t0, 1), "evidence_version": ev_version,
             "calibration": calib["version"], "reference_docs": reference[2]}
    log(f"Coverage: {done} postings in {stats['seconds']}s (reference {reference[2]} postings)")
    return stats


# ================================================================ calibration (§16.1, §16.6)
import hashlib  # noqa: E402

from backend import profile as P  # noqa: E402

BLEND_GRID = (0.5, 0.6, 0.75, 0.9)


def _auc_np(pos, neg) -> Optional[float]:
    import numpy as np
    pos, neg = np.asarray([p for p in pos if p is not None], float), np.sort([n for n in neg if n is not None])
    if not len(pos) or not len(neg):
        return None
    below = np.searchsorted(neg, pos, side="left")
    equal = np.searchsorted(neg, pos, side="right") - below
    return float((below + 0.5 * equal).sum() / (len(pos) * len(neg)))


JUDGED_TOP = 300


def refresh_hard_negatives(con, hard_top: int = 200, judged_top: int = JUDGED_TOP, log=print) -> dict:
    """Rewrites `hard_negatives` from three sources, hardest first.

    - `judged_wrong`: the `judged_top` highest-fit postings the LLM judge graded `wrong`. These are the
      calibration set's backbone now. The audit list held 7 examples, which is too few to tune anything
      against, and `fit_top` below only GUESSES that a high-fit posting with no function term in its title is
      off-function. A judged `wrong` row is the same claim, confirmed, and the labeling run produced 1,320 of
      them. The highest-fit slice is taken on purpose, but measured on the live corpus (2026-09-16) even that
      slice is mostly EASY: fit 0.17-0.72, median 0.23. It is a large, verified negative set, not a confusable
      one -- the confusable band is `stretch` (out-of-fold mean 0.51), so a high AUC against these rows proves
      little and a low one is decisive.
    - `audit`: AUDIT_NEGATIVES, posting ids the user read and called wrong-function himself.
    - `fit_top`: the highest-fit unlabeled active postings with no function term in the title -- the fit
      model's own confident mistakes, still useful for the rows nobody has graded.
    """
    now = _now()
    audit = [a for a in (P.AUDIT_NEGATIVES or []) if re.fullmatch(r"[0-9a-f]{20}", str(a))]
    unknown = [a for a in (P.AUDIT_NEGATIVES or []) if a not in audit]
    top = con.execute("""
        SELECT s.posting_id, s.fit_prob FROM vw_screen_latest s JOIN postings p USING (posting_id)
        WHERE p.status = 'active' AND p.description_text IS NOT NULL AND s.fit_prob IS NOT NULL AND s.tier IS NULL
          AND s.posting_id NOT IN (SELECT posting_id FROM label_docs WHERE posting_id IS NOT NULL AND label = 1)
          AND s.posting_id NOT IN (SELECT posting_id FROM decisions)
        ORDER BY s.fit_prob DESC, s.posting_id LIMIT ?""", [int(hard_top) + len(audit)]).fetchall()
    top = [(pid, fit) for pid, fit in top if pid not in audit][:int(hard_top)]
    judged = con.execute("""
        SELECT l.posting_id, s.fit_prob, l.grade_process, l.grade_technical
        FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
        LEFT JOIN vw_screen_latest s USING (posting_id)
        WHERE l.grade = 'wrong' AND p.status = 'active' AND length(p.description_text) >= 800
          AND l.posting_id NOT IN (SELECT posting_id FROM label_docs WHERE posting_id IS NOT NULL AND label = 1)
          AND l.posting_id NOT IN (SELECT posting_id FROM decisions)
        ORDER BY s.fit_prob DESC NULLS LAST, l.posting_id LIMIT ?""", [int(judged_top)]).fetchall()
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM hard_negatives")
        # DuckDB's executemany rejects an empty parameter list, and any of the three sources can be empty.
        audit_rows = [[a, now] for a in audit]
        judged_rows = [[pid, f"fit {fit:.2f}" if fit is not None else "fit n/a", now]
                       for pid, fit, _gp, _gt in judged if pid not in audit]
        seen = set(audit) | {j[0] for j in judged}
        top_rows = [[pid, f"fit {fit:.2f}", now] for pid, fit in top if pid not in seen]
        if audit_rows:
            con.executemany("INSERT OR REPLACE INTO hard_negatives VALUES (?, 'audit', NULL, ?)", audit_rows)
        if judged_rows:
            con.executemany("INSERT OR REPLACE INTO hard_negatives VALUES (?, 'judged_wrong', ?, ?)", judged_rows)
        if top_rows:
            con.executemany("INSERT OR REPLACE INTO hard_negatives VALUES (?, 'fit_top', ?, ?)", top_rows)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    if unknown:
        log(f"  AUDIT_NEGATIVES entries that are not posting ids were skipped: {unknown}")
    counts = dict(con.execute("SELECT source, count(*) FROM vw_hard_negatives GROUP BY 1").fetchall())
    log(f"  hard negatives: {counts}")
    if judged:
        fits = [f for _p, f, _gp, _gt in judged if f is not None]
        if fits:
            med = sorted(fits)[len(fits) // 2]
            log(f"  judged_wrong fit range: {min(fits):.2f}-{max(fits):.2f} (median {med:.2f})"
                + (" -- mostly EASY negatives; a high AUC against them proves little" if med < 0.5 else ""))
    return counts


def _grid_scores(docs: list, strong: float, partial: float, avail) -> list:
    """Primary coverage per doc under (strong, partial), from each doc's cached best-cosine-by-kind matrix."""
    import numpy as np
    kw = np.asarray([KIND_WEIGHTS[k] for k in KINDS], dtype=np.float32)
    out = []
    for d in docs:
        best, w, grp, notin, light = d["best"], d["w"], d["grp"], d["notin"], d["light"]
        if best.shape[0] == 0:
            out.append({"required": None, "role": None, "primary": None})
            continue
        strong_m = best >= strong
        partial_m = best >= partial
        band = np.where(strong_m & ~light[:, None], 1.0, np.where(partial_m, 0.5, 0.0))
        credit = (band * kw[None, :] * avail[None, :]).max(axis=1)
        credit[notin] = 0.0
        res = {}
        for name in ("required", "role"):
            m = grp == name
            res[name] = float(100.0 * (w[m] * credit[m]).sum() / w[m].sum()) if m.sum() >= MIN_WORK and w[m].sum() > 0 \
                else None
        res["primary"] = res["required"] if res["required"] is not None else res["role"]
        out.append(res)
    return out


def _prepare(res: dict) -> dict:
    import numpy as np
    units = res["_units"]
    return {"best": res["_best"], "w": np.asarray([u["weight"] * (u.get("spec") or 1.0) for u in units], np.float32),
            "grp": np.asarray([u["grp"] for u in units]), "notin": np.asarray([c == "not_in_record" for c in res["_caps"]],
                                                                             bool),
            "light": np.asarray([c == "light_in_record" for c in res["_caps"]], bool)}


def _oof_fit(con, log=print) -> dict:
    """label_id -> held-out fit_prob, cross-validated with the newest model's parameters (in-sample fit would flatter
    the fit model against coverage)."""
    from . import features
    model = features.load_latest(con, log=log)
    if model is None:
        return {}
    prm = model["params"]
    rows = features.training_set(con)
    texts = [features.doc_text(r["title"], r["text"], r["company"]) for r in rows]
    cvr = features.cross_validate(texts, [int(r["label"]) for r in rows], [float(r["weight"] or 1) for r in rows],
                                  C=prm["C"], min_df=prm["min_df"], ngram=tuple(prm["ngram"]),
                                  max_features=prm["max_features"], cv=prm["cv"], seed=prm["seed"], max_df=prm["max_df"])
    return {r["label_id"]: p for r, p in zip(rows, cvr["oof"])}


def calibrate(con, manifest, encoder, *, n_pseudo: int = 300, hard_top: int = 200, log=print) -> dict:
    """Chooses COVER_STRONG / COVER_PARTIAL on a grid to maximize positives-vs-hard-negative AUC of the gated coverage,
    sets COVERAGE_REJECT / REVIEW at the positives' 5th / 15th percentile, compares blend weights, prints the paired
    vault-vs-career-site gap, and stores a `models` row of kind 'coverage'."""
    import time
    import numpy as np
    from . import evidence as E
    from . import features
    t0 = time.monotonic()
    model = manifest.embed_model
    if E.stored_version(con, model) is not None:
        E.ensure_current(con, manifest, encoder, log=log)
    ev_units, ev_vecs = E.load_matrix(con, model)
    ev_version = E.stored_version(con, model)
    if not ev_units:
        raise RuntimeError("no evidence units stored; run `finder.py evidence --rebuild` first")
    refresh_hard_negatives(con, hard_top=hard_top, log=log)
    default = {"strong": DEFAULT_STRONG, "partial": DEFAULT_PARTIAL}

    positives = [r for r in features.training_set(con) if int(r["label"]) == 1]
    site = dict(con.execute("SELECT posting_id, description_text FROM postings WHERE length(description_text) >= 800 "
                            "AND posting_id IN (SELECT unnest(?::VARCHAR[]))",
                            [[r["posting_id"] for r in positives if r["posting_id"]]]).fetchall())
    hard = con.execute("""SELECT h.posting_id, any_value(h.source), any_value(p.employer), any_value(p.title)
                          FROM vw_hard_negatives h JOIN postings p USING (posting_id)
                          WHERE p.description_text IS NOT NULL GROUP BY 1""").fetchall()
    pos_pids = {r["posting_id"] for r in positives if r["posting_id"]}
    hard = [h for h in hard if h[0] not in pos_pids]
    pseudo = [r[0] for r in con.execute("""SELECT posting_id FROM label_docs WHERE source = 'pseudo_neg'
                                           AND posting_id IS NOT NULL ORDER BY hash(label_id), label_id LIMIT ?""",
                                        [int(n_pseudo)]).fetchall()]
    posting_ids = sorted({*site, *(h[0] for h in hard), *pseudo, *survivor_ids(con)})
    log(f"Calibration set: {len(positives)} positives ({len(site)} with career-site text) · {len(hard)} hard negatives "
        f"· {len(pseudo)} pseudo-negatives · embedding requirement units for {len(posting_ids)} postings")
    for start in range(0, len(posting_ids), 200):
        ensure_requirement_units(con, posting_ids[start:start + 200], encoder, model, log=log)
    reference = load_reference(con, model)

    def score_postings(pids: list) -> dict:
        out = {}
        for start in range(0, len(pids), 200):
            loaded = load_requirement_units(con, pids[start:start + 200], model)
            for pid, (units, vecs) in loaded.items():
                out[pid] = score_units(units, vecs, pid, reference, (ev_units, ev_vecs), default, manifest)
        return out

    def score_texts(items: list) -> list:
        split = [_unit_dicts(R.split_requirements(t)) for _, t in items]
        texts = sorted({u["text"] for units in split for u in units if u["klass"] == "work"})
        vecs = encoder.encode(texts, query=True)
        index = {t: i for i, t in enumerate(texts)}
        out = []
        for (key, _), units in zip(items, split):
            work = [index[u["text"]] for u in units if u["klass"] == "work"]
            mat = vecs[work] if work else np.zeros((0, vecs.shape[1] if vecs.ndim == 2 else 384), np.float32)
            out.append(score_units(units, mat, f"text:{key}", reference, (ev_units, ev_vecs), default, manifest))
        return out

    by_pid = score_postings(sorted({*site, *(h[0] for h in hard), *pseudo}))
    vault_items = [(r["label_id"], r["text"]) for r in positives if r["text"]]
    by_text = dict(zip([k for k, _ in vault_items], score_texts(vault_items)))
    pos_docs, pos_keys, pairs = [], [], []
    for r in positives:
        if r["posting_id"] in site:
            pos_docs.append(_prepare(by_pid[r["posting_id"]]))
            pos_keys.append(r)
            if r["label_id"] in by_text and r["text"] != site[r["posting_id"]] and r["source"] != "decision":
                pairs.append((len(pos_docs) - 1, _prepare(by_text[r["label_id"]])))
        elif r["label_id"] in by_text:
            pos_docs.append(_prepare(by_text[r["label_id"]]))
            pos_keys.append(r)
    hard_docs = [_prepare(by_pid[h[0]]) for h in hard]
    pseudo_docs = [_prepare(by_pid[p]) for p in pseudo]
    avail = np.asarray([any(u["kind"] == k for u in ev_units) for k in KINDS], dtype=np.float32)

    grid = []
    for strong in np.arange(0.66, 0.901, 0.02):
        for partial in np.arange(0.54, strong - 0.019, 0.02):
            pos_s = _grid_scores(pos_docs, strong, partial, avail)
            hard_s = _grid_scores(hard_docs, strong, partial, avail)
            grid.append((_auc_np([s["primary"] for s in pos_s], [s["primary"] for s in hard_s]) or 0.0,
                         round(float(strong), 2), round(float(partial), 2)))
    grid.sort(reverse=True)
    best_auc, strong, partial = grid[0]
    pos_s = _grid_scores(pos_docs, strong, partial, avail)
    hard_s = _grid_scores(hard_docs, strong, partial, avail)
    pseudo_s = _grid_scores(pseudo_docs, strong, partial, avail)
    pos_primary = [s["primary"] for s in pos_s if s["primary"] is not None]
    reject, review = (float(np.percentile(pos_primary, 5)), float(np.percentile(pos_primary, 15))) if pos_primary \
        else (None, None)
    hard_primary = [s["primary"] for s in hard_s if s["primary"] is not None]

    oof = _oof_fit(con, log=log)
    screen_fit = dict(con.execute("SELECT posting_id, fit_prob FROM vw_screen_latest WHERE fit_prob IS NOT NULL").fetchall())
    pos_fit = [oof.get(r["label_id"], screen_fit.get(r["posting_id"])) for r in pos_keys]
    hard_fit = [screen_fit.get(h[0]) for h in hard]

    def blend(cov, fit, w):
        if fit is None:
            return cov
        return 100 * fit if cov is None else w * cov + (1 - w) * 100 * fit

    aucs = {"coverage_required": _auc_np([s["required"] for s in pos_s], [s["required"] for s in hard_s]),
            "coverage_role": _auc_np([s["role"] for s in pos_s], [s["role"] for s in hard_s]),
            "coverage_gated": best_auc, "fit_heldout": _auc_np(pos_fit, hard_fit)}
    for w in BLEND_GRID:
        aucs[f"blend_{w}"] = _auc_np([blend(s["primary"], f, w) for s, f in zip(pos_s, pos_fit)],
                                     [blend(s["primary"], f, w) for s, f in zip(hard_s, hard_fit)])
    best_blend = max(BLEND_GRID, key=lambda w: aucs[f"blend_{w}"] or 0)
    pseudo_auc = _auc_np(pos_primary, [s["primary"] for s in pseudo_s])
    paired = [(pos_s[i]["primary"], _grid_scores([d], strong, partial, avail)[0]["primary"]) for i, d in pairs]
    paired = [(a, b) for a, b in paired if a is not None and b is not None]
    gap = float(np.mean([b - a for a, b in paired])) if paired else None

    notes = {"strong": strong, "partial": partial, "reject": reject, "review": review, "blend": best_blend,
             "aucs": aucs, "pseudo_auc": pseudo_auc, "hard_negative_median": float(np.median(hard_primary))
             if hard_primary else None, "positive_median": float(np.median(pos_primary)) if pos_primary else None,
             "paired_gap_vault_minus_site": gap, "paired_n": len(paired), "evidence_version": ev_version,
             "splitter": R.splitter_fingerprint(), "n_pos": len(pos_docs), "n_hard": len(hard_docs)}
    version = hashlib.sha1(json.dumps(notes, sort_keys=True, default=str).encode()).hexdigest()[:12]
    con.execute("INSERT OR REPLACE INTO models (model_version, kind, trained_at, n_pos, n_neg, cv_auc, notes) "
                "VALUES (?, 'coverage', ?, ?, ?, ?, ?)", [version, _now(), len(pos_docs), len(hard_docs), best_auc,
                                                         json.dumps(notes, default=str)])
    fmt = lambda x: "n/a" if x is None else f"{x:.3f}"  # noqa: E731
    log(f"Calibration {version} ({time.monotonic() - t0:.0f}s): COVER_STRONG {strong} · COVER_PARTIAL {partial} · "
        f"COVERAGE_REJECT {fmt(reject)} · COVERAGE_REVIEW {fmt(review)}")
    log("  AUC positives vs hard negatives: " + " · ".join(f"{k} {fmt(v)}" for k, v in aucs.items()))
    log(f"  best blend weight {best_blend} · sanity AUC positives vs pseudo-negatives {fmt(pseudo_auc)}")
    log(f"  medians: positives {fmt(notes['positive_median'])} · hard negatives {fmt(notes['hard_negative_median'])}")
    log(f"  paired gap (vault copy minus career-site copy, n={len(paired)}): {fmt(gap)} points")
    log("  top grid: " + " · ".join(f"({s}, {p}) {a:.3f}" for a, s, p in grid[:5]))
    order = sorted(range(len(hard)), key=lambda i: -(hard_s[i]["primary"] or -1))
    log("  hard negatives by coverage (promote a real fit with `finder.py mark <id> build`):")
    for i in order[:25]:
        pid, source, employer, title = hard[i]
        log(f"    {fmt(hard_s[i]['primary']):>6}  fit {fmt(hard_fit[i])}  {source:<18} {(employer or '')[:24]:<24} "
            f"{(title or '')[:60]:<60} {pid}")
    return {"version": version, **notes}
