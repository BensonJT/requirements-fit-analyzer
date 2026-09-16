"""The labeling run: hand screen survivors to an LLM judge in batches, get graded function labels back.

Why: the finder has 362 positives and almost no labeled NEAR MISSES, so nothing downstream (the fit model, the
coverage calibration, any future re-ranker) can be tuned or measured on the only question that matters — is this
the same kind of work? This module exports batch files a Claude Code session works through, validates what comes
back, and writes `llm_labels`. No API call is made from here; the files never leave the machine.

Queue design: every wave mixes the confusable band (strong / very_strong), the tail (partial / weak) and a
deliberate sample of REJECTED postings, so that stopping early still leaves a label set that spans the range.
Rejected rows also measure something never measured before: how often the screen throws away good work.
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from typing import Optional

from backend.screen import similar_title

from . import rubric

BATCH_SIZE = 20
UNITS_PER_POSTING = 18
UNIT_CHARS = 220
WAVE = {"high": 17, "low": 5, "reject": 3}     # per 25 exported postings
DUP_COSINE = 0.995                             # a repost, not a sibling role: near-identical text AND a similar title
                                               # (0.97 collapsed genuinely different roles that share employer boilerplate)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class Posting:
    posting_id: str
    employer: str
    title: str
    tier: str
    score: int
    description_hash: str
    units: list


def _rows(con, sql: str, params=None) -> list:
    return con.execute(sql, params or []).fetchall()


def exported_ids(dirs) -> set:
    """Every posting already queued in another batch directory (judged rows and their duplicates), so a second
    export never pays to grade the same posting twice."""
    out = set()
    for d in dirs or []:
        path = Path(d) / "manifest.json"
        if path.exists():
            m = json.loads(path.read_text(encoding="utf-8"))
            out |= {p for b in m["batches"].values() for p in b["postings"]} | set(m.get("duplicates", {}))
    return out


def pools(con, n_reject_content: int = 100, n_reject_logistics: int = 100, n_reject_random: int = 50,
          seed: int = 7, exclude: Optional[set] = None, only: Optional[list] = None,
          platform: Optional[str] = None, relabel: Optional[int] = None) -> dict:
    """The three source pools: the confusable band, the tail, and a deliberate reject sample.
    `exclude` drops postings already queued elsewhere; `only` keeps just the named pools.
    `relabel` re-grades postings that ALREADY carry a label, for when the rubric changed rather than the JD:
    0 = every labelled posting, N = a sample of about N drawn evenly across the four grades so the
    grade-migration matrix is measurable at a fraction of the cost."""
    high = [r[0] for r in _rows(con, """
        SELECT s.posting_id FROM vw_screen_latest s JOIN postings p USING (posting_id)
        WHERE p.status = 'active' AND s.verdict != 'reject' AND s.band IN ('very_strong', 'strong')
        ORDER BY s.final_score DESC, p.posting_id""")]
    low = [r[0] for r in _rows(con, """
        SELECT s.posting_id FROM vw_screen_latest s JOIN postings p USING (posting_id)
        WHERE p.status = 'active' AND s.verdict != 'reject' AND s.band IN ('partial', 'weak')
        ORDER BY hash(s.posting_id || ?), s.posting_id""", [str(seed)])]
    content = [r[0] for r in _rows(con, """
        SELECT s.posting_id FROM vw_screen_latest s JOIN postings p USING (posting_id)
        WHERE p.status = 'active' AND s.verdict = 'reject' AND p.description_text IS NOT NULL
          AND s.reasons::VARCHAR LIKE '%content does not fit%'
        ORDER BY s.fit_prob DESC NULLS LAST, s.posting_id LIMIT ?""", [n_reject_content * 6])][:n_reject_content * 6]
    logistics = [r[0] for r in _rows(con, """
        SELECT s.posting_id FROM vw_screen_latest s JOIN postings p USING (posting_id)
        WHERE p.status = 'active' AND s.verdict = 'reject' AND p.description_text IS NOT NULL
          AND s.fit_prob >= 0.5 AND s.reasons::VARCHAR NOT LIKE '%content does not fit%'
        ORDER BY hash(s.posting_id || ?), s.posting_id LIMIT ?""", [str(seed), n_reject_logistics * 6])]
    random_rej = [r[0] for r in _rows(con, """
        SELECT s.posting_id FROM vw_screen_latest s JOIN postings p USING (posting_id)
        WHERE p.status = 'active' AND s.verdict = 'reject' AND p.description_text IS NOT NULL
        ORDER BY hash(s.posting_id || ?), s.posting_id LIMIT ?""", [str(seed + 1), n_reject_random * 6])]
    over = n_reject_content + n_reject_logistics + n_reject_random
    seen, rejects = set(), []
    caps = {"content": n_reject_content, "logistics": n_reject_logistics, "random": n_reject_random}
    taken = {"content": 0, "logistics": 0, "random": 0}
    for kind, ids in (("content", content), ("logistics", logistics), ("random", random_rej)):
        for pid in ids:                                   # keep the deliberate order, drop repeats and exclusions
            if pid in seen or (exclude and pid in exclude) or taken[kind] >= caps[kind]:
                continue
            seen.add(pid)
            taken[kind] += 1
            rejects.append(pid)
    result = {"high": high, "low": low, "reject": rejects}
    if relabel is not None:
        labelled = _rows(con, """
            SELECT l.posting_id, l.grade FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
            WHERE p.status = 'active' AND p.description_text IS NOT NULL
              AND l.posting_id NOT IN (SELECT posting_id FROM training_exclusions)
            ORDER BY hash(l.posting_id || ?), l.posting_id""", [str(seed)])
        by_grade = defaultdict(list)
        for pid, grade in labelled:
            by_grade[grade].append(pid)
        if relabel <= 0:
            picked = [pid for _, ids in sorted(by_grade.items()) for pid in ids]
        else:   # even draw per grade, then top up from the largest pools so the total lands near `relabel`
            per = max(1, relabel // max(1, len(by_grade)))
            picked = [pid for _, ids in sorted(by_grade.items()) for pid in ids[:per]]
            for _, ids in sorted(by_grade.items(), key=lambda kv: -len(kv[1])):
                picked += [pid for pid in ids[per:] if len(picked) < relabel]
        result = {"relabel": picked}
    elif platform:
        # Re-grade a whole platform: used after an ingest fix changes what the JDs actually say.
        result = {"platform": [r[0] for r in _rows(con, """
            SELECT p.posting_id FROM postings p LEFT JOIN vw_screen_latest s USING (posting_id)
            WHERE p.status = 'active' AND p.platform = ? AND p.description_text IS NOT NULL
            ORDER BY coalesce(s.final_score, 0) DESC, p.posting_id""", [platform])]}

    if exclude:
        result = {k: [p for p in v if p not in exclude] for k, v in result.items()}
    if only:
        result = {k: (v if k in only else []) for k, v in result.items()}
    return result


def interleave(pool: dict, limit: Optional[int] = None) -> list:
    """One flat queue mixing the pools per WAVE, so any stopping point spans the range."""
    queues = {k: list(v) for k, v in pool.items() if v}
    order = [k for k in WAVE if k in queues] + [k for k in queues if k not in WAVE]
    out = []
    while any(queues.values()) and (limit is None or len(out) < limit):
        took = False
        for key in order:
            for _ in range(WAVE.get(key, 25)):
                if queues.get(key):
                    out.append((queues[key].pop(0), key))
                    took = True
                    if limit is not None and len(out) >= limit:
                        return out
        if not took:
            break
    return out


def duplicate_map(con, ids: list) -> dict:
    """posting_id -> the id whose label it copies: the same employer with identical JD text, or near-identical
    text AND a similar title. Employer boilerplate alone makes sibling roles look alike, so text similarity by
    itself is not enough."""
    import numpy as np
    from .embed import stack
    if not ids:
        return {}
    ids_json = json.dumps(sorted(set(ids)))
    titles = dict(_rows(con, """SELECT posting_id, coalesce(title, '') FROM postings
        WHERE posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))""", [ids_json]))
    meta = dict(_rows(con, """SELECT posting_id, employer || '|' || coalesce(description_hash, '') FROM postings
        WHERE posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))""", [ids_json]))
    cur = con.execute("""SELECT posting_id, weight, spec, vector FROM requirement_units
        WHERE klass = 'work' AND posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))
        ORDER BY posting_id, ord""", [ids_json]).fetchnumpy()
    if not len(cur["posting_id"]):
        return {}
    pids = np.asarray(cur["posting_id"]).astype(str)
    w = np.asarray(cur["weight"], dtype=np.float64) * np.nan_to_num(np.asarray(cur["spec"], dtype=np.float64), nan=1.0)
    V = stack(cur["vector"])
    order = np.argsort(pids, kind="stable")
    pids, w, V = pids[order], w[order], V[order]
    bounds = np.flatnonzero(np.r_[True, pids[1:] != pids[:-1], True])
    doc_ids, docs = [], []
    for a, b in zip(bounds[:-1], bounds[1:]):
        vec = (V[a:b] * w[a:b, None]).sum(axis=0)
        n = np.linalg.norm(vec)
        if n:
            doc_ids.append(pids[a])
            docs.append(vec / n)
    if not docs:
        return {}
    M = np.vstack(docs)
    rank = {pid: i for i, pid in enumerate(ids)}          # queue order: the earlier row is the representative
    by_employer = {}
    for i, pid in enumerate(doc_ids):
        by_employer.setdefault(meta.get(pid, "?").split("|")[0], []).append(i)
    dup = {}
    for rows in by_employer.values():
        rows.sort(key=lambda i: rank.get(doc_ids[i], 10**9))
        for a_i, i in enumerate(rows):
            if doc_ids[i] in dup:
                continue
            sims = M[rows[a_i + 1:]] @ M[i] if rows[a_i + 1:] else []
            for j, sim in zip(rows[a_i + 1:], sims):
                # Text similarity alone is not evidence of a repost: an employer's boilerplate makes unrelated
                # roles look alike (0.97 collapsed Analytics Engineer with BI Manager; even 0.995 plus a loose
                # title match collapsed Enterprise Transformation with Software Engineer). Identical text only.
                if doc_ids[j] not in dup and meta.get(doc_ids[i]) == meta.get(doc_ids[j]) and sim >= DUP_COSINE:
                    dup[doc_ids[j]] = doc_ids[i]
    return dup


def fetch_postings(con, ids: list, tiers: dict) -> list:
    """Trimmed Posting records: the highest-weight requirement units only, no evidence text."""
    ids_json = json.dumps(sorted(set(ids)))
    base = dict((r[0], r[1:]) for r in _rows(con, """
        SELECT p.posting_id, p.employer, p.title, coalesce(p.description_hash, ''), s.final_score
        FROM postings p LEFT JOIN vw_screen_latest s USING (posting_id)
        WHERE p.posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))""", [ids_json]))
    units = {}
    for pid, text, grp in _rows(con, """
        SELECT posting_id, text, grp FROM requirement_units
        WHERE klass = 'work' AND posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))
        ORDER BY posting_id, weight * coalesce(spec, 1.0) DESC, ord""", [ids_json]):
        rows = units.setdefault(pid, [])
        if len(rows) < UNITS_PER_POSTING:
            rows.append(f"[{grp}] {text[:UNIT_CHARS]}")
    # Rejected postings never went through coverage, so they have no cached units; split their JD on demand.
    # (Without this they vanish from the export silently, taking the false-negative measurement with them.)
    missing = [pid for pid in ids if pid in base and not units.get(pid)]
    if missing:
        from . import requirements as R
        for pid, text in _rows(con, """SELECT posting_id, description_text FROM postings
                WHERE description_text IS NOT NULL
                  AND posting_id IN (SELECT unnest(json_transform(?, '["VARCHAR"]')))""", [json.dumps(missing)]):
            reqs = [u for u in R.split_requirements(text, max_units=UNITS_PER_POSTING * 2) if u.klass == "work"]
            reqs.sort(key=lambda u: -u.weight)
            units[pid] = [f"[{u.group}] {u.text[:UNIT_CHARS]}" for u in reqs[:UNITS_PER_POSTING]]
    out = []
    for pid in ids:
        if pid not in base:
            continue
        employer, title, dhash, score = base[pid]
        out.append(Posting(pid, employer or "", title or "", tiers.get(pid, "?"), int(score or 0), dhash,
                           units.get(pid, [])))
    dropped = [p.posting_id for p in out if not p.units]
    if dropped:
        print(f"  note: {len(dropped)} postings have no usable requirement text and were left out")
    return [p for p in out if p.units]


BATCH_HEADER = """# Labeling batch {n} — grade the WORK, not the candidate's odds

{rubric}
{lane}
{personal}
## How to answer
Return ONE JSON object per posting below, as a JSON array, written to `{result}`. No prose, no markdown fence.
Grade every posting in this file. Use the posting_id exactly as given.

## Postings ({count})
"""


def write_batches(con, out_dir: str, queue: list, *, batch_size: int = BATCH_SIZE, log=print) -> dict:
    """Writes batch markdown files plus a manifest; returns counts and the duplicate map."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ids = [pid for pid, _ in queue]
    tiers = dict(queue)
    dup = duplicate_map(con, ids)
    judged = [pid for pid in ids if pid not in dup]
    postings = fetch_postings(con, judged, tiers)
    version = rubric.rubric_version()
    personal = getattr(rubric, "RUBRIC_PERSONAL", "")
    files, manifest = [], {}
    for i in range(0, len(postings), batch_size):
        chunk = postings[i:i + batch_size]
        n = i // batch_size + 1
        name = f"batch_{n:03d}"
        result = f"{name}.result.json"
        body = [BATCH_HEADER.format(n=n, rubric=rubric.RUBRIC_PUBLIC, lane=rubric.RUBRIC_LANE, personal=personal,
                                    result=result, count=len(chunk))]
        for p in chunk:
            body.append(f"\n### {p.posting_id}\n**{p.employer} — {p.title}**\n")
            body.extend(f"- {u}" for u in p.units)
        (out / f"{name}.md").write_text("\n".join(body) + "\n", encoding="utf-8")
        manifest[name] = {"result": result, "postings": {p.posting_id: p.description_hash for p in chunk},
                          "tiers": {p.posting_id: p.tier for p in chunk}}
        files.append(str(out / f"{name}.md"))
    (out / "manifest.json").write_text(json.dumps(
        {"rubric_version": version, "created": _now().isoformat(), "batches": manifest,
         "duplicates": {k: dup[k] for k in dup}}, indent=1), encoding="utf-8")
    log(f"Judge export: {len(postings)} postings in {len(files)} batches → {out} "
        f"(rubric {version}; {len(dup)} near-duplicates will copy their representative's label)")
    return {"batches": len(files), "postings": len(postings), "duplicates": len(dup), "rubric_version": version,
            "dir": str(out)}


def _validate(obj: dict, allowed: dict) -> Optional[str]:
    pid = str(obj.get("posting_id", "")).strip()
    if pid not in allowed:
        return f"unknown posting_id {pid!r}"
    if obj.get("grade") not in rubric.GRADES:
        return f"{pid}: grade {obj.get('grade')!r} not in {rubric.GRADES}"
    return None


def status(out_dir: str, log=print) -> dict:
    """Which batches still need judging — the resume point after a crash, a token limit or a plan switch."""
    out = Path(out_dir)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    done, pending = [], []
    for name, meta in sorted(manifest["batches"].items()):
        (done if (out / meta["result"]).exists() else pending).append(name)
    log(f"Judge status in {out}: {len(done)} batches done, {len(pending)} pending "
        f"({sum(len(manifest['batches'][b]['postings']) for b in pending)} postings left)")
    if pending:
        log(f"  next: {', '.join(pending[:6])}{' …' if len(pending) > 6 else ''}")
    return {"done": done, "pending": pending, "dir": str(out), "rubric_version": manifest["rubric_version"]}


def to_csv(con, path: str, log=print) -> str:
    """Every judged posting as a row to eyeball: grade, rationale, blocker, the score it had, and its URL."""
    con.execute("""COPY (
        SELECT l.grade, l.lane, l.confidence, s.final_score, s.band, p.employer, p.title, l.blocker, l.rationale,
               p.url, l.posting_id
        FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
        LEFT JOIN vw_screen_latest s USING (posting_id)
        ORDER BY CASE l.grade WHEN 'bullseye' THEN 0 WHEN 'adjacent' THEN 1 WHEN 'stretch' THEN 2 ELSE 3 END,
                 s.final_score DESC) TO ? (HEADER, DELIMITER ',')""", [path])
    log(f"Judged labels → {path}")
    return path


def load_results(con, out_dir: str, scorer: str = "claude-sonnet-batch", log=print) -> dict:
    """Validates every `*.result.json` against the manifest and writes `llm_labels` (duplicates copy their
    representative's grade). Returns counts; refuses ids that were not exported."""
    out = Path(out_dir)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    version = manifest["rubric_version"]
    rows, errors, seen = [], [], {}
    for name, meta in manifest["batches"].items():
        path = out / meta["result"]
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
            text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
            payload = json.loads(text)
        except ValueError as exc:
            errors.append(f"{path.name}: not valid JSON ({exc})")
            continue
        for obj in payload if isinstance(payload, list) else [payload]:
            problem = _validate(obj, meta["postings"])
            if problem:
                errors.append(f"{path.name}: {problem}")
                continue
            pid = obj["posting_id"].strip()
            seen[pid] = obj["grade"]
            rows.append([pid, meta["postings"][pid], version, scorer, obj["grade"], obj.get("lane"),
                         obj.get("confidence"), (obj.get("blocker") or "")[:400],
                         (obj.get("rationale") or "")[:600], name, _now()])
    hashes = dict(_rows(con, "SELECT posting_id, coalesce(description_hash, '') FROM postings"))
    copied = 0
    for dup_id, rep in manifest.get("duplicates", {}).items():
        if rep in seen and dup_id in hashes:
            src = next(r for r in rows if r[0] == rep)
            rows.append([dup_id, hashes[dup_id], version, scorer, src[4], src[5], src[6], src[7], src[8],
                         f"dup:{rep}", _now()])
            copied += 1
    if rows:
        con.executemany("INSERT OR REPLACE INTO llm_labels VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    counts = dict(_rows(con, "SELECT grade, count(*) FROM llm_labels WHERE rubric_version = ? GROUP BY 1", [version]))
    log(f"Judge import: {len(rows)} labels written ({copied} copied to near-duplicates), {len(errors)} rejected; "
        f"grades so far {counts}")
    for e in errors[:10]:
        log(f"  rejected: {e}")
    return {"written": len(rows), "copied": copied, "errors": errors, "grades": counts}


THIN_TEXT = re.compile(r"too thin|no (actual |specific )?(requirements|role|duties|responsibilities)"
                       r"|marketing copy|company boilerplate|general[- ]application|placeholder", re.I)


def exclude(con, posting_id: str, reason: str, source: str = "cli", log=print) -> None:
    """Never train on this posting (the user retired the label, or its text cannot be judged)."""
    con.execute("INSERT OR REPLACE INTO training_exclusions VALUES (?, ?, ?, ?)",
                [posting_id, reason[:300], source, _now()])
    row = con.execute("SELECT employer, title FROM postings WHERE posting_id = ?", [posting_id]).fetchone()
    log(f"excluded from training: {posting_id} {row[0] + ' | ' + (row[1] or '') if row else ''} — {reason}")


def exclude_thin(con, log=print) -> int:
    """Auto-exclude postings the judge could not read: it said the text was boilerplate, a placeholder, or too
    thin. Those rows teach a model nothing except what a careers page looks like."""
    rows = con.execute("""SELECT posting_id, coalesce(blocker, '') || ' ' || coalesce(rationale, '')
                          FROM vw_llm_labels_latest WHERE confidence = 'low' OR blocker IS NOT NULL""").fetchall()
    n = 0
    for pid, text in rows:
        if THIN_TEXT.search(text or ""):
            exclude(con, pid, "unjudgeable: the stored JD is boilerplate or a placeholder", "auto-thin", log=lambda _: None)
            n += 1
    log(f"auto-excluded {n} postings whose stored text the judge could not judge")
    return n


def agreement(con, log=print) -> dict:
    """Checks the judge against the user's own behaviour: postings they applied to or marked build should not be
    graded `wrong`; postings they passed on for function reasons should not be graded `bullseye`."""
    applied = _rows(con, """
        SELECT l.grade, count(*) FROM vw_llm_labels_latest l
        WHERE l.posting_id IN (SELECT matched_posting_id FROM tracker WHERE matched_posting_id IS NOT NULL
                               UNION SELECT posting_id FROM vw_decisions WHERE decision = 'build'
                               UNION SELECT posting_id FROM label_docs WHERE label = 1 AND posting_id IS NOT NULL)
        GROUP BY 1 ORDER BY 2 DESC""")
    passed = _rows(con, """
        SELECT l.grade, count(*) FROM vw_llm_labels_latest l JOIN vw_decisions d USING (posting_id)
        WHERE d.decision = 'pass' AND d.reason_code = 'function' GROUP BY 1 ORDER BY 2 DESC""")
    total = dict(_rows(con, "SELECT grade, count(*) FROM vw_llm_labels_latest GROUP BY 1"))
    log(f"Grades over all judged postings: {total}")
    log(f"  on postings the user pursued: {dict(applied)}   (a `wrong` here is a disagreement worth reading)")
    log(f"  on postings the user passed for function: {dict(passed)}")
    return {"total": total, "pursued": dict(applied), "passed_function": dict(passed)}
