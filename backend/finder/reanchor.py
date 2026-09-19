"""Orphan recovery for labels stranded before the description_hash normalization fix (store.normalize_for_hash
/ the v11 migration): an `llm_labels` row whose stored hash no longer equals its posting's current hash is
invisible to training (vw_llm_labels_latest), and the migration only fixes rows where the OLD raw hash is
still the posting's current hash. For everything already orphaned, the raw text that produced the old hash is
gone -- but the judge's batch files under db/batches* preserve exactly which requirement units it read.

Recovery rule: find the posting in a batch manifest at its OLD stored hash and ask what share of the units
the judge read still appear VERBATIM (after normalize_for_hash) in the posting's CURRENT text. At or above
REANCHOR_SIMILARITY the JD did not materially change and the label is re-anchored to the current hash;
otherwise it stays expired. Containment, not a re-split comparison: the requirement splitter itself changed
after most batches were written, so re-splitting identical text yields different units (measured on the live
DB 2026-09-18: a Jaccard-over-resplit rule recovered 170 orphans, containment 549 of 720). A row copied to a
near-duplicate (`batch` column `dup:<representative_id>`) is checked with ITS OWN current text against the
representative's batch units -- it never had a batch entry of its own.
"""
import json
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from backend.ats.store import normalize_for_hash

REANCHOR_SIMILARITY = 0.90  # share of judged units still verbatim in the current JD; 0.90 tolerates one
                            # edited line in the usual 18, never two
_UNIT_TAG_RE = re.compile(r"^\[[^\]]*\]\s*")  # the "[required] " / "[role] " prefix write_batches adds


def parse_batch_units(md_text: str) -> dict:
    """posting_id -> the bullet-line units under its `### <posting_id>` heading, as judge.write_batches wrote
    them. Splitting on the heading marker rather than regexing each bullet keeps this in step with whatever
    prose judge.BATCH_HEADER carries."""
    lines = md_text.splitlines()
    out, pid, body = {}, None, []
    for line in lines:
        if line.startswith("### "):
            if pid is not None:
                out[pid] = [ln[2:].strip() for ln in body if ln.startswith("- ")]
            pid, body = line[4:].strip(), []
        elif pid is not None:
            body.append(line)
    if pid is not None:
        out[pid] = [ln[2:].strip() for ln in body if ln.startswith("- ")]
    return out


def _containment(units: list, normalized_text: str) -> float:
    """Share of the judged units found verbatim in the normalized current text. No units = no evidence = 0."""
    keys = [normalize_for_hash(_UNIT_TAG_RE.sub("", u)).rstrip(". ") for u in units]
    keys = [k for k in keys if k]
    if not keys:
        return 0.0
    return sum(1 for k in keys if k in normalized_text) / len(keys)


def _load_manifests(batch_dirs) -> list:
    """[(dir_path, manifest_dict), ...] for every readable manifest.json."""
    out = []
    for d in batch_dirs or []:
        path = Path(d) / "manifest.json"
        if path.exists():
            try:
                out.append((Path(d), json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, OSError):
                continue
    return out


def _find_direct(manifests: list, posting_id: str, want_hash: str) -> Optional[tuple]:
    """(dir, batch_name) of the manifest entry that exported `posting_id` at exactly `want_hash`."""
    for d, manifest in manifests:
        for bname, meta in manifest.get("batches", {}).items():
            if meta.get("postings", {}).get(posting_id) == want_hash:
                return d, bname
    return None


def _find_dup_source(manifests: list, dup_id: str) -> Optional[tuple]:
    """(dir, batch_name, representative_id) for a duplicate row: the manifest that copied `dup_id`'s label
    from a representative, and that representative's own batch entry (co-located in the same manifest)."""
    for d, manifest in manifests:
        rep = manifest.get("duplicates", {}).get(dup_id)
        if not rep:
            continue
        for bname, meta in manifest.get("batches", {}).items():
            if rep in meta.get("postings", {}):
                return d, bname, rep
    return None


def _bucket(sim: float) -> str:
    if sim >= REANCHOR_SIMILARITY:
        return f">= {REANCHOR_SIMILARITY:.2f}"
    if sim >= 0.80:
        return f"0.80-{REANCHOR_SIMILARITY:.2f}"
    if sim >= 0.50:
        return "0.50-0.80"
    return "< 0.50"


def _llm_label_would_win(con, pid: str, new_hash: str, rubric_version: str, scorer: str, judged_at) -> bool:
    """Whether re-anchoring this row would win a PK collision at (pid, new_hash, rubric_version, scorer):
    no existing row there, or its `judged_at` is no newer (ties favor the row being re-anchored). Read-only,
    so both --dry-run and a real run agree on the count."""
    target = con.execute("""SELECT judged_at FROM llm_labels
                            WHERE posting_id = ? AND description_hash = ? AND rubric_version = ? AND scorer = ?""",
                         [pid, new_hash, rubric_version, scorer]).fetchone()
    return not target or target[0] <= judged_at


def _reanchor_llm_label(con, pid: str, old_hash: str, new_hash: str, rubric_version: str, scorer: str) -> None:
    """Moves one llm_labels row to the new hash. Call only after `_llm_label_would_win` confirms it wins;
    deletes whatever losing row sits at the new hash first so the UPDATE never hits the primary key."""
    con.execute("""DELETE FROM llm_labels
                   WHERE posting_id = ? AND description_hash = ? AND rubric_version = ? AND scorer = ?""",
                [pid, new_hash, rubric_version, scorer])
    con.execute("""UPDATE llm_labels SET description_hash = ?
                   WHERE posting_id = ? AND description_hash = ? AND rubric_version = ? AND scorer = ?""",
                [new_hash, pid, old_hash, rubric_version, scorer])


def _reanchor_feedback(con, pid: str, old_hash: str, new_hash: str) -> int:
    """Re-anchors every report_feedback row for `pid` stored at `old_hash` (the label's ORIGINAL hash) to
    `new_hash`. PK = (posting_id, description_hash, assessor); collision keeps the newer assessed_at."""
    rows = con.execute("SELECT assessor, assessed_at FROM report_feedback WHERE posting_id = ? AND description_hash = ?",
                       [pid, old_hash]).fetchall()
    n = 0
    for assessor, assessed_at in rows:
        target = con.execute("SELECT assessed_at FROM report_feedback WHERE posting_id = ? AND description_hash = ? "
                             "AND assessor = ?", [pid, new_hash, assessor]).fetchone()
        if target and target[0] > assessed_at:
            continue
        if target:
            con.execute("DELETE FROM report_feedback WHERE posting_id = ? AND description_hash = ? AND assessor = ?",
                        [pid, new_hash, assessor])
        con.execute("UPDATE report_feedback SET description_hash = ? "
                    "WHERE posting_id = ? AND description_hash = ? AND assessor = ?",
                    [new_hash, pid, old_hash, assessor])
        n += 1
    return n


def reanchor(con, batch_dirs, *, dry_run: bool = False, log=print) -> dict:
    """Re-anchors orphaned llm_labels (and their report_feedback companions) whose JD did not materially
    change, evidenced by the judge's own batch files. Never touches a row for which no matching batch entry
    is found. `dry_run` computes and logs everything but writes nothing."""
    manifests = _load_manifests(batch_dirs)
    orphans = con.execute("""
        SELECT l.posting_id, l.description_hash, l.rubric_version, l.scorer, l.judged_at, l.batch, p.description_hash
        FROM llm_labels l JOIN postings p USING (posting_id)
        WHERE p.description_hash IS DISTINCT FROM l.description_hash AND p.description_text IS NOT NULL
    """).fetchall()
    md_cache, text_cache = {}, {}
    buckets = Counter()
    reanchored_keys = []  # (pid, old_hash, new_hash) for the feedback pass
    no_match = 0

    for pid, old_hash, rubric_version, scorer, judged_at, batch, cur_hash in orphans:
        if batch and batch.startswith("dup:"):
            loc = _find_dup_source(manifests, pid)
            if loc is None:
                no_match += 1
                continue
            d, bname, rep = loc
            ref_units = md_cache.setdefault((d, bname), parse_batch_units((d / f"{bname}.md")
                                                                          .read_text(encoding="utf-8"))).get(rep, [])
        else:
            loc = _find_direct(manifests, pid, old_hash)
            if loc is None:
                no_match += 1
                continue
            d, bname = loc
            ref_units = md_cache.setdefault((d, bname), parse_batch_units((d / f"{bname}.md")
                                                                          .read_text(encoding="utf-8"))).get(pid, [])

        if pid not in text_cache:
            row = con.execute("SELECT description_text FROM postings WHERE posting_id = ?", [pid]).fetchone()
            text_cache[pid] = normalize_for_hash(row[0]) if row and row[0] else ""
        sim = _containment(ref_units, text_cache[pid])
        buckets[_bucket(sim)] += 1
        if sim < REANCHOR_SIMILARITY:
            continue
        if not _llm_label_would_win(con, pid, cur_hash, rubric_version, scorer, judged_at):
            continue  # a newer label already sits at the posting's current hash; leave this one expired
        if not dry_run:
            _reanchor_llm_label(con, pid, old_hash, cur_hash, rubric_version, scorer)
        reanchored_keys.append((pid, old_hash, cur_hash))

    feedback_n = 0
    if not dry_run:
        for pid, old_hash, new_hash in reanchored_keys:
            feedback_n += _reanchor_feedback(con, pid, old_hash, new_hash)

    n_reanchored = len(reanchored_keys)
    n_expired = len(orphans) - n_reanchored
    log(f"Reanchor{' (dry run)' if dry_run else ''}: {len(orphans)} labels examined, "
        f"{n_reanchored} re-anchored, {n_expired} left expired "
        f"({no_match} with no matching batch entry) — similarity buckets {dict(buckets)}")
    log(f"  feedback rows re-anchored: {feedback_n}")
    return {"examined": len(orphans), "reanchored": n_reanchored, "expired": n_expired,
            "no_match": no_match, "buckets": dict(buckets), "feedback_reanchored": feedback_n}
