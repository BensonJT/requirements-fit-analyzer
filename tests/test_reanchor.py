"""Unit tests for:
 - backend/ats/store.normalize_for_hash / description_hash (whitespace / NBSP / zero-width / case / NFKC)
 - the v10 -> v11 schema migration (_migrate_v11_hash_normalization) in backend/ats/store.py
 - backend/finder/reanchor.py (orphan recovery from judge batch files)

Temp DuckDB files only, no network, no personal data.
"""
import json
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import pipeline, reanchor, requirements as R  # noqa: E402

NOW = datetime(2026, 9, 18)


def _quiet(*_a, **_k):
    pass


def _seed_posting(con, req_id, text, *, now=NOW, employer="Acme"):
    store.record_board(con, employer, "greenhouse",
                       [N.base(req_id=req_id, title="Director, Process Excellence", url=f"https://x/{req_id}",
                               location_primary="Remote - USA", workplace_type="remote",
                               description_text=text)], now)
    return store.posting_id(employer, "greenhouse", req_id)


TEXT_A = ("Responsibilities\n"
          "- Lead process improvement initiatives across the finance organization\n"
          "- Build automated reporting dashboards for the operations team\n"
          "- Partner with senior stakeholders to design new workflow standards\n")

TEXT_B = ("Responsibilities\n"
          "- Operate surgical robotics equipment in a live clinical setting\n"
          "- Manage inventory of sterile instruments for the OR\n"
          "- Coordinate patient scheduling with attending physicians\n")


def _units(text):
    return [f"[{u.group}] {u.text}" for u in R.split_requirements(text) if u.klass == "work"]


# ---------------------------------------------------------------- normalize_for_hash / description_hash
def test_normalize_for_hash_collapses_incidental_differences():
    base = "Lead process improvement across the org."
    variants = [
        base + "  ",                                   # trailing whitespace
        base.replace(" ", " ", 1),                 # a non-breaking space
        base.replace(" ", "​ ", 1),                # a zero-width space next to a real space
        base.upper(),                                   # case
        "Lead  process   improvement across the org.",  # collapsed internal whitespace run
        unicodedata_nfkc_variant(base),                 # a compatibility-equivalent (fullwidth) character
    ]
    for v in variants:
        assert store.normalize_for_hash(v) == store.normalize_for_hash(base), repr(v)
        assert store.description_hash(v) == store.description_hash(base), repr(v)


def unicodedata_nfkc_variant(s: str) -> str:
    # Replace the first 'o' with a fullwidth 'ｏ' (U+FF4F), which NFKC folds back to ASCII 'o'.
    return s.replace("o", "ｏ", 1)


def test_description_hash_real_change_differs():
    assert store.description_hash(TEXT_A) != store.description_hash(TEXT_B)


def test_description_hash_empty_or_none():
    assert store.description_hash(None) is None
    assert store.description_hash("") is None


def test_description_hash_shape():
    h = store.description_hash(TEXT_A)
    assert isinstance(h, str) and len(h) == 16
    int(h, 16)  # hex


# ---------------------------------------------------------------- v11 migration
def _make_v10_db(path, now=NOW):
    """A v10-shaped DB: one posting whose CURRENT description_hash is the OLD raw hash of its text, a
    label/gold/coverage row anchored at that old hash, and one orphan label already stranded elsewhere."""
    con = store.connect(str(path))
    pid = _seed_posting(con, "P1", TEXT_A, now=now)
    old_hash = _raw_sha1(TEXT_A)
    con.execute("UPDATE postings SET description_hash = ? WHERE posting_id = ?", [old_hash, pid])
    con.execute("INSERT INTO llm_labels VALUES (?, ?, 'r1', 'claude-sonnet-batch', 'bullseye', 'bullseye', "
                "'adjacent', NULL, 'process', 'high', NULL, 'good fit', 'batch_001', ?)", [pid, old_hash, now])
    con.execute("INSERT INTO report_feedback VALUES (?, ?, NULL, 80, 'strong', 'bullseye', 'adjacent', "
                "'bullseye', 'in_range', 'build', NULL, NULL, NULL, 'high', 'jd_read', 'user', true, "
                "NULL, NULL, NULL, NULL, ?, ?)", [pid, old_hash, now, now])
    con.execute("INSERT INTO coverage VALUES (?, ?, 'ev1', 'm1', 'c1', 80.0, 70.0, 3, 2, 1, 3, 2, 1, "
                "'[]', '[]', '[]', ?)", [pid, old_hash, now])
    # a label already orphaned under the OLD schema -- untouched by the migration (its stored hash never
    # equaled the posting's own old hash, so it is not in scope; the reanchor command handles it separately)
    con.execute("INSERT INTO llm_labels VALUES (?, 'some-other-stale-hash', 'r1', 'claude-sonnet-batch', "
                "'wrong', 'wrong', 'wrong', NULL, NULL, NULL, NULL, NULL, 'batch_000', ?)", [pid, now])
    con.execute("UPDATE schema_info SET version = 10")
    con.close()
    return pid, old_hash


def _raw_sha1(text):
    import hashlib
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def test_migration_reveals_records_through_latest_views(tmp_path):
    path = tmp_path / "v10.duckdb"
    pid, old_hash = _make_v10_db(path)

    con = store.connect(str(path))  # triggers the v11 migration on open
    new_hash = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [pid]).fetchone()[0]
    assert new_hash == store.description_hash(TEXT_A)
    assert new_hash != old_hash

    assert con.execute("SELECT count(*) FROM vw_llm_labels_latest WHERE posting_id = ?", [pid]).fetchone()[0] == 1
    assert con.execute("SELECT grade FROM vw_llm_labels_latest WHERE posting_id = ?", [pid]).fetchone()[0] == "bullseye"
    assert con.execute("SELECT count(*) FROM vw_report_feedback_latest WHERE posting_id = ?", [pid]).fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM vw_coverage_latest WHERE posting_id = ?", [pid]).fetchone()[0] == 1

    # the pre-existing orphan (stored at a totally different stale hash) is untouched: still absent
    stale_left = con.execute("SELECT count(*) FROM llm_labels WHERE description_hash = 'some-other-stale-hash'"
                             ).fetchone()[0]
    assert stale_left == 1
    assert con.execute("SELECT count(*) FROM vw_llm_labels_latest").fetchone()[0] == 1  # not 2
    con.close()


def test_migration_idempotent(tmp_path):
    path = tmp_path / "v10b.duckdb"
    pid, old_hash = _make_v10_db(path)
    con = store.connect(str(path))
    snap1 = con.execute("SELECT posting_id, description_hash FROM llm_labels ORDER BY 1, 2").fetchall()
    n = store._migrate_v11_hash_normalization(con)  # calling again directly must be a no-op
    snap2 = con.execute("SELECT posting_id, description_hash FROM llm_labels ORDER BY 1, 2").fetchall()
    assert n == 0
    assert snap1 == snap2
    con.close()


def test_migration_does_not_trigger_full_rescreen(tmp_path):
    """The hash rewrite must not make `screen` think every posting's JD changed -- pipeline.RESCREEN_SQL
    compares screened_at to description_fetched_at, never the hash."""
    path = tmp_path / "v10c.duckdb"
    pid, old_hash = _make_v10_db(path)
    con = store.connect(str(path))
    fetched_at = con.execute("SELECT description_fetched_at FROM postings WHERE posting_id = ?", [pid]).fetchone()[0]
    now2 = datetime(2026, 9, 18, 12, 0, 0)
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier, "
                "rule_score, final_score, band, reasons, flags, top_terms) VALUES (?, 'rv1', 'none', ?, "
                "'candidate', 1, 70, 70, 'strong', '[]', '[]', '[]')", [pid, now2])
    assert fetched_at <= now2
    ids = pipeline.candidate_ids(con, "rv1", "none")
    assert pid not in ids  # already screened under rv1/none after its last text change -> not a candidate
    con.close()


def test_migration_pk_collision_keeps_newest(tmp_path):
    """A pre-existing llm_labels row already sitting at the NEW hash (e.g. a fresh direct labeling run) and
    an older historical row still at the OLD hash for the same (posting, rubric, scorer): remapping the old
    row into the same primary key must not raise a constraint error, and the newer judged_at survives."""
    path = tmp_path / "v10d.duckdb"
    con = store.connect(str(path))
    pid = _seed_posting(con, "P2", TEXT_A)
    new_hash = store.description_hash(TEXT_A)
    con.execute("UPDATE postings SET description_hash = ? WHERE posting_id = ?", [new_hash, pid])
    old_hash = "collide-hash-old"
    con.execute("INSERT INTO llm_labels VALUES (?, ?, 'r1', 'scorerA', 'wrong', 'wrong', 'wrong', NULL, NULL, "
                "NULL, NULL, NULL, 'b1', ?)", [pid, old_hash, datetime(2026, 9, 1)])
    con.execute("INSERT INTO llm_labels VALUES (?, ?, 'r1', 'scorerA', 'bullseye', 'bullseye', 'adjacent', "
                "NULL, NULL, NULL, NULL, NULL, 'b2', ?)", [pid, new_hash, datetime(2026, 9, 10)])
    con.execute("BEGIN")
    con.execute("CREATE OR REPLACE TEMP TABLE hash_map (posting_id VARCHAR, old_hash VARCHAR, new_hash VARCHAR)")
    con.execute("INSERT INTO hash_map VALUES (?, ?, ?)", [pid, old_hash, new_hash])
    store._remap_hash_pk_table(con, "llm_labels", ("rubric_version", "scorer"), "judged_at")
    con.execute("DROP TABLE hash_map")
    con.execute("COMMIT")
    rows = con.execute("SELECT description_hash, grade, judged_at FROM llm_labels WHERE posting_id = ?",
                       [pid]).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == new_hash and rows[0][1] == "bullseye"  # the newer (2026-09-10) pre-existing row won
    con.close()


# ---------------------------------------------------------------- reanchor.py
def _write_batch(dirpath, name, entries):
    """entries: {posting_id: (employer_title_line, [unit_lines])}"""
    body = ["# Labeling batch 1 — grade the WORK, not the candidate's odds\n"]
    for pid, (heading, units) in entries.items():
        body.append(f"\n### {pid}\n**{heading}**\n")
        body.extend(f"- {u}" for u in units)
    (dirpath / f"{name}.md").write_text("\n".join(body) + "\n", encoding="utf-8")


def test_parse_batch_units_roundtrip(tmp_path):
    d = tmp_path
    _write_batch(d, "batch_001", {
        "p1": ("Acme — Role", ["[role] Lead the thing", "[role] Build the other thing"]),
        "p2": ("Acme — Role2", ["[required] Own the analytics stack"]),
    })
    text = (d / "batch_001.md").read_text(encoding="utf-8")
    parsed = reanchor.parse_batch_units(text)
    assert parsed["p1"] == ["[role] Lead the thing", "[role] Build the other thing"]
    assert parsed["p2"] == ["[required] Own the analytics stack"]


def _judge_db_and_batch(tmp_path, *, same_text_for="p_same", changed_text_for="p_changed"):
    con = store.connect(str(tmp_path / "j.duckdb"))
    p_same = _seed_posting(con, "P_SAME", TEXT_A)
    p_changed = _seed_posting(con, "P_CHANGED", TEXT_B)
    p_rep = _seed_posting(con, "P_REP", TEXT_A, employer="OtherCo")
    p_dup = _seed_posting(con, "P_DUP", TEXT_A, employer="ThirdCo")

    old_same, old_changed, old_rep = "old-hash-same", "old-hash-changed", "old-hash-rep"
    now = NOW
    con.execute("INSERT INTO llm_labels VALUES (?, ?, 'r1', 'claude-sonnet-batch', 'bullseye', 'bullseye', "
                "'adjacent', NULL, NULL, NULL, NULL, NULL, 'batch_001', ?)", [p_same, old_same, now])
    con.execute("INSERT INTO llm_labels VALUES (?, ?, 'r1', 'claude-sonnet-batch', 'adjacent', 'adjacent', "
                "'adjacent', NULL, NULL, NULL, NULL, NULL, 'batch_001', ?)", [p_changed, old_changed, now])
    con.execute("INSERT INTO llm_labels VALUES (?, ?, 'r1', 'claude-sonnet-batch', 'bullseye', 'bullseye', "
                "'adjacent', NULL, NULL, NULL, NULL, NULL, ?, ?)",
               [p_dup, "old-hash-dup", f"dup:{p_rep}", now])
    # a gold row that must follow p_same's label when it re-anchors
    con.execute("INSERT INTO report_feedback VALUES (?, ?, NULL, 80, 'strong', 'bullseye', 'adjacent', "
                "'bullseye', 'in_range', 'build', NULL, NULL, NULL, 'high', 'jd_read', 'user', true, "
                "NULL, NULL, NULL, NULL, ?, ?)", [p_same, old_same, now, now])

    batch_dir = tmp_path / "batches"
    batch_dir.mkdir()
    _write_batch(batch_dir, "batch_001", {
        p_same: ("Acme — Role", _units(TEXT_A)),
        p_changed: ("Acme — Role", _units(TEXT_A)),   # judge read TEXT_A; current text is now TEXT_B
        p_rep: ("OtherCo — Role", _units(TEXT_A)),
    })
    manifest = {"rubric_version": "r1", "created": now.isoformat(),
                "batches": {"batch_001": {"result": "batch_001.result.json",
                                          "postings": {p_same: old_same, p_changed: old_changed,
                                                       p_rep: old_rep},
                                          "tiers": {}}},
                "duplicates": {p_dup: p_rep}}
    (batch_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return con, str(batch_dir), {"p_same": p_same, "p_changed": p_changed, "p_rep": p_rep, "p_dup": p_dup}


def test_reanchor_same_text_reanchors_and_carries_feedback(tmp_path):
    con, batch_dir, ids = _judge_db_and_batch(tmp_path)
    stats = reanchor.reanchor(con, [batch_dir], log=_quiet)
    p_same = ids["p_same"]
    cur_hash = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [p_same]).fetchone()[0]
    assert con.execute("SELECT description_hash FROM llm_labels WHERE posting_id = ?", [p_same]).fetchone()[0] == cur_hash
    assert con.execute("SELECT count(*) FROM vw_llm_labels_latest WHERE posting_id = ?", [p_same]).fetchone()[0] == 1
    assert con.execute("SELECT description_hash FROM report_feedback WHERE posting_id = ?", [p_same]).fetchone()[0] == cur_hash
    assert con.execute("SELECT count(*) FROM vw_report_feedback_latest WHERE posting_id = ?", [p_same]).fetchone()[0] == 1
    assert stats["reanchored"] >= 1
    assert stats["feedback_reanchored"] == 1
    con.close()


def test_reanchor_changed_text_left_expired(tmp_path):
    con, batch_dir, ids = _judge_db_and_batch(tmp_path)
    reanchor.reanchor(con, [batch_dir], log=_quiet)
    p_changed = ids["p_changed"]
    cur_hash = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [p_changed]).fetchone()[0]
    stored = con.execute("SELECT description_hash FROM llm_labels WHERE posting_id = ?", [p_changed]).fetchone()[0]
    assert stored == "old-hash-changed"        # untouched
    assert stored != cur_hash
    assert con.execute("SELECT count(*) FROM vw_llm_labels_latest WHERE posting_id = ?", [p_changed]).fetchone()[0] == 0
    con.close()


def test_reanchor_dup_follows_representative_when_own_text_matches(tmp_path):
    con, batch_dir, ids = _judge_db_and_batch(tmp_path)
    reanchor.reanchor(con, [batch_dir], log=_quiet)
    p_dup = ids["p_dup"]
    cur_hash = con.execute("SELECT description_hash FROM postings WHERE posting_id = ?", [p_dup]).fetchone()[0]
    stored = con.execute("SELECT description_hash FROM llm_labels WHERE posting_id = ?", [p_dup]).fetchone()[0]
    assert stored == cur_hash
    con.close()


def test_reanchor_dry_run_writes_nothing(tmp_path):
    con, batch_dir, ids = _judge_db_and_batch(tmp_path)
    before = con.execute("SELECT posting_id, description_hash FROM llm_labels ORDER BY 1").fetchall()
    stats = reanchor.reanchor(con, [batch_dir], dry_run=True, log=_quiet)
    after = con.execute("SELECT posting_id, description_hash FROM llm_labels ORDER BY 1").fetchall()
    assert before == after
    assert stats["reanchored"] >= 1     # it still computed what WOULD happen
    before_fb = con.execute("SELECT count(*) FROM report_feedback WHERE description_hash != 'old-hash-same'"
                            ).fetchone()[0]
    assert before_fb == 0
    con.close()


def test_reanchor_no_batch_match_leaves_row_alone(tmp_path):
    con = store.connect(str(tmp_path / "nomatch.duckdb"))
    pid = _seed_posting(con, "PX", TEXT_A)
    con.execute("INSERT INTO llm_labels VALUES (?, 'orphan-hash', 'r1', 'claude-sonnet-batch', 'bullseye', "
                "'bullseye', 'adjacent', NULL, NULL, NULL, NULL, NULL, 'batch_999', ?)", [pid, NOW])
    stats = reanchor.reanchor(con, [], log=_quiet)
    assert stats["examined"] == 1
    assert stats["reanchored"] == 0
    assert stats["no_match"] == 1
    assert con.execute("SELECT description_hash FROM llm_labels WHERE posting_id = ?", [pid]).fetchone()[0] == "orphan-hash"
    con.close()
