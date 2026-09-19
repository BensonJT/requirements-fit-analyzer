"""Tests for the "second layer" stacked model (backend/finder/required_embed.py). Uses a small
deterministic fake encoder -- no model download, no network -- so the whole train/score round
trip runs in a temp DuckDB file in well under a second.

Design of the synthetic corpus: the fake encoder makes the Required-block / line embeddings
PERFECTLY separable on a marker token (MEETSKEY / GAPKEY) that never appears in the plain JD
text used for TF-IDF, so TF-IDF stays near chance while the embedding-based arms are strong --
this reliably clears the acceptance gate (stack AUC >= 0.71 and >= tfidf + 0.05) without relying
on randomness.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import required_embed as RE  # noqa: E402

pytest.importorskip("sklearn")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class FakeEncoder:
    """Deterministic, keyword-based embedding: dim 0 carries the label signal (MEETSKEY /
    GAPKEY vs not), the rest is a stable hash of the text so identical texts get identical
    vectors and distinct texts don't collide."""
    name = "fake-test-encoder"
    dim = 6

    def encode(self, texts, query=True, batch_size=48):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            signal = 1.0 if ("MEETSKEY" in t or "GAPKEY" not in t and "REQLINE" in t) else -1.0
            if "GAPKEY" in t:
                signal = 1.0
            elif "REQLINE" in t:
                signal = -1.0
            h = abs(hash(t)) % 1000 / 1000.0
            out[i] = [signal, h, h * 0.5, -h, signal * 0.3, h * h]
        return out


@pytest.fixture(autouse=True)
def fake_encoder(monkeypatch, tmp_path):
    monkeypatch.setattr(RE.E, "load_encoder", lambda *a, **kw: FakeEncoder())
    monkeypatch.setattr(RE, "CACHE_DIR", str(tmp_path / "embed_cache"))
    monkeypatch.setattr(RE.F, "MODEL_DIR", str(tmp_path / "models"))
    os.makedirs(RE.F.MODEL_DIR, exist_ok=True)


def _make_posting(con, req_id, employer, now, jd_text):
    job = N.base(req_id=req_id, title="Operations Manager", url=f"https://x/{req_id}",
                workplace_type="remote", description_text=jd_text)
    # truncated=True: each posting is its own record_board call (one employer, many reqs across this test
    # module), and a non-truncated pull closes anything from that employer NOT in the just-seen list.
    store.record_board(con, employer, "greenhouse", [job], now, truncated=True)
    pid, dh = con.execute("SELECT posting_id, description_hash FROM postings WHERE req_id = ?",
                          [req_id]).fetchone()
    return pid, dh


import random  # noqa: E402

# A word bank shuffled per-posting (seeded on the posting's counter, not on its label) so TF-IDF sees a mix of
# terms that occur in several documents (clears min_df=3) but never in most of them (stays under max_df=0.5),
# with no correlation to required_fit -- the TF-IDF arm should stay near chance, unlike the embedding arms
# (which read the GAPKEY/REQLINE marker the fake encoder keys on).
_FILLER = ["standups", "regional", "variance", "budget", "coordinate", "offices", "quarterly", "vendor",
          "logistics", "calendar", "onboarding", "escalation"]


def _neutral_jd(counter: int) -> str:
    words = list(_FILLER)
    random.Random(counter).shuffle(words)
    picked = words[:4]
    sentence = "We are hiring an operations leader to run daily " + " and ".join(picked) + " across the team. "
    return sentence * 8


def _seed_corpus(con, n_employers=6, meets_per=1, fails_per=1):
    """n_employers companies, each with `meets_per` required_fit='meets' rows and `fails_per`
    required_fit='fails' rows, all lens-surfaced (grade_process='bullseye'), JD >= 800 chars,
    each with 3 requirement_units (2 required + 1 role), klass='work'."""
    now = _now()
    pids = []
    counter = 0
    for e in range(n_employers):
        employer = f"Employer{e}"
        for label, n in (("meets", meets_per), ("fails", fails_per)):
            for _ in range(n):
                counter += 1
                req_id = f"R{counter}"
                pid, dh = _make_posting(con, req_id, employer, now, _neutral_jd(counter))
                # requirement_units: one line always clean (REQLINE, no gap marker), one line
                # is the "Required" test line -- carries GAPKEY when this posting is 'fails'
                # (so required_unmet quotes it and it becomes an 'unmet' training line).
                gap_text = "GAPKEY certification required for this role advanced" if label == "fails" else \
                    "REQLINE standard qualification for this role advanced"
                rows = [
                    (pid, dh, "v1", 0, "h0", "REQLINE baseline requirement for the role", "Requirements",
                     "required", 1.0, "work", 1.0, "none", None, now),
                    (pid, dh, "v1", 1, "h1", gap_text, "Requirements", "required", 1.0, "work", 1.0, "none",
                     None, now),
                    (pid, dh, "v1", 2, "h2", "REQLINE role duty context only", "Responsibilities", "role",
                     1.0, "work", 1.0, "none", None, now),
                ]
                con.executemany(
                    "INSERT INTO requirement_units (posting_id, description_hash, splitter, ord, unit_hash, "
                    "text, section, grp, weight, klass, spec, model, vector, embedded_at) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                required_unmet = "GAPKEY certification required for this role advanced" if label == "fails" else ""
                con.execute(
                    "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                    "grade_process, required_fit, required_unmet, judged_at) VALUES "
                    "(?, ?, 'rv1', 'claude-sonnet-batch', 'bullseye', 'bullseye', ?, ?, ?)",
                    [pid, dh, label, required_unmet, now])
                con.execute(
                    "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                    "tier, rule_score, fit_required, final_score, band, reasons, flags, top_terms) VALUES "
                    "(?, 'v1', 'none', ?, 'candidate', 1, 70, 0.5, 70, 'strong', '[]', '[]', '[]')",
                    [pid, now])
                pids.append((pid, label))
    return pids


def _add_non_lens_surfaced_judged_posting(con, employer, now, label):
    """A judged posting (required_fit set, has a Required block) that is NOT lens-surfaced
    (grade_process='stretch') -- outside the block/rollup/stack TRAINING population, but it feeds
    the TF-IDF model (vw_label_set_required does not care about lens grade) and the line model
    (_line_labels runs over every judged posting, not just lens-surfaced ones). FIX 1's rule is that
    this posting must still get a genuinely held-out score, never an in-sample one. `fit_process` is
    set high enough that it also clears SCORE_POPULATION_SQL's gate despite not being lens-surfaced."""
    req_id = f"NONLENS_{employer}_{label}"
    pid, dh = _make_posting(con, req_id, employer, now, _neutral_jd(abs(hash((employer, label))) % 1000))
    gap_text = "GAPKEY certification required for this role advanced" if label == "fails" else \
        "REQLINE standard qualification for this role advanced"
    rows = [
        (pid, dh, "v1", 0, "h0", "REQLINE baseline requirement for the role", "Requirements",
         "required", 1.0, "work", 1.0, "none", None, now),
        (pid, dh, "v1", 1, "h1", gap_text, "Requirements", "required", 1.0, "work", 1.0, "none", None, now),
        (pid, dh, "v1", 2, "h2", "REQLINE role duty context only", "Responsibilities", "role", 1.0, "work",
         1.0, "none", None, now),
    ]
    con.executemany(
        "INSERT INTO requirement_units (posting_id, description_hash, splitter, ord, unit_hash, "
        "text, section, grp, weight, klass, spec, model, vector, embedded_at) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    required_unmet = "GAPKEY certification required for this role advanced" if label == "fails" else ""
    con.execute(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
        "grade_process, required_fit, required_unmet, judged_at) VALUES "
        "(?, ?, 'rv1', 'claude-sonnet-batch', 'stretch', 'stretch', ?, ?, ?)",
        [pid, dh, label, required_unmet, now])
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
        "tier, rule_score, fit_required, fit_process, final_score, band, reasons, flags, top_terms) VALUES "
        "(?, 'v1', 'none', ?, 'candidate', 1, 70, 0.5, 0.6, 70, 'strong', '[]', '[]', '[]')",
        [pid, now])
    return pid, dh


def test_fix1_judged_non_lens_posting_gets_true_held_out_score(tmp_path):
    """A judged posting outside the stack/block/rollup TRAINING population (not lens-surfaced) must
    still land in the model's held-out table and get a genuinely different number than the full
    (in-sample) models would produce for it -- the leak FIX 1 closes."""
    con = store.connect(str(tmp_path / "db.duckdb"))
    _seed_corpus(con, n_employers=6, meets_per=1, fails_per=1)
    now = _now()
    non_lens_pid, non_lens_dh = _add_non_lens_surfaced_judged_posting(con, "Employer0", now, "fails")

    result = RE.train(con)
    assert result["accepted"], f"acceptance gate unexpectedly failed: {result}"
    bundle = RE.load_latest(con)

    assert non_lens_pid in bundle["oof_pids"], "judged + has a Required block => held-out eligible"
    held_out_value = bundle["oof_scores"][non_lens_pid]
    assert 0.0 <= held_out_value <= 1.0

    stats = RE.score(con, all_rows=True)
    assert stats["oof"] > 0
    row = con.execute("SELECT embed_required, is_oof FROM required_embed WHERE posting_id = ?",
                      [non_lens_pid]).fetchone()
    assert row is not None, "posting must clear SCORE_POPULATION_SQL's gate (fit_process >= 0.5)"
    embed_required, is_oof = row
    assert is_oof is True
    assert embed_required == pytest.approx(held_out_value)

    # What the FULL (in-sample) models would have said for this SAME posting -- they were fit on
    # data that, via the line model, includes this posting's own quoted-unmet line, so applying them
    # here would be exactly the in-sample score FIX 1 forbids. Reproduce that computation directly
    # (bypassing score(), which never takes this path for a judged posting) to show the two numbers
    # genuinely differ, proving the held-out value is doing real work, not coincidentally matching.
    title, desc, employer = con.execute(
        "SELECT title, description_text, employer FROM postings WHERE posting_id = ?", [non_lens_pid]).fetchone()
    seen = RE._seen_units(con, [non_lens_pid])[non_lens_pid]
    block_text = RE._required_block_text(seen)
    encoder = RE.E.load_encoder()
    full_block_p = float(bundle["block_clf"].predict_proba(encoder.encode([block_text]))[:, 1][0])
    full_tfidf_p = float(bundle["tfidf_clf"].predict_proba(
        bundle["tfidf_vec"].transform([RE.F.doc_text(title, desc, employer)]))[:, 1][0])
    req_lines = [u for u in seen if u["grp"] == "required"]
    line_x_s = bundle["line_scaler"].transform(encoder.encode([u["text"] for u in req_lines]))
    line_p = bundle["line_clf"].predict_proba(line_x_s)[:, 1]
    rf = RE._rollup_stats(list(line_p), [u["weight"] for u in req_lines])
    full_rollup_p = float(bundle["rollup_clf"].predict_proba(bundle["rollup_scaler"].transform(
        np.array([[rf[n] for n in RE.ROLLUP_FEAT_NAMES]], dtype=np.float32)))[:, 1][0])
    full_stack_p = float(bundle["stack_clf"].predict_proba(
        np.array([[full_tfidf_p, full_block_p, full_rollup_p]]))[:, 1][0])
    assert full_stack_p != pytest.approx(embed_required, abs=1e-6), (
        "held-out value must differ from the full-model path it must never take")


def test_score_skip_counters(tmp_path):
    """score()'s skip counters must account for postings that clear SCORE_POPULATION_SQL but can't
    be scored: no Required-group line survives the top-N 'seen' cut (skipped_no_rollup), or no
    stored TF-IDF `fit_required` (skipped_no_tfidf) -- the audit's unexplained 913 -> 874 gap."""
    con = store.connect(str(tmp_path / "db.duckdb"))
    _seed_corpus(con, n_employers=6, meets_per=1, fails_per=1)
    result = RE.train(con)
    assert result["accepted"]
    now = _now()

    # A Required-group unit exists (satisfies SCORE_POPULATION_SQL's EXISTS check) but is outweighed
    # out of the top-UNITS_PER_POSTING "seen" cut by higher-weighted role-group filler -- no
    # Required-group line survives for the roll-up.
    pid_b, dh_b = _make_posting(con, "SKIP_NOROLLUP", "EmployerB", now, _neutral_jd(9002))
    rows_b = [(pid_b, dh_b, "v1", i, f"h{i}", f"REQLINE role duty context filler {i}", "Responsibilities",
              "role", 10.0, "work", 1.0, "none", None, now) for i in range(RE.UNITS_PER_POSTING)]
    rows_b.append((pid_b, dh_b, "v1", RE.UNITS_PER_POSTING, "hreq",
                   "REQLINE baseline requirement pushed out of the cut", "Requirements", "required",
                   0.01, "work", 1.0, "none", None, now))
    con.executemany(
        "INSERT INTO requirement_units (posting_id, description_hash, splitter, ord, unit_hash, text, "
        "section, grp, weight, klass, spec, model, vector, embedded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows_b)
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier, "
        "rule_score, fit_required, fit_process, final_score, band, reasons, flags, top_terms) VALUES "
        "(?, 'v1', 'none', ?, 'candidate', 1, 70, 0.5, 0.6, 70, 'strong', '[]', '[]', '[]')", [pid_b, now])

    # A properly-formed Required block within the cut, but no stored fit_required (fit_required NULL).
    pid_c, dh_c = _make_posting(con, "SKIP_NOTFIDF", "EmployerC", now, _neutral_jd(9003))
    rows_c = [(pid_c, dh_c, "v1", 0, "h0", "REQLINE baseline requirement for the role", "Requirements",
              "required", 1.0, "work", 1.0, "none", None, now)]
    con.executemany(
        "INSERT INTO requirement_units (posting_id, description_hash, splitter, ord, unit_hash, text, "
        "section, grp, weight, klass, spec, model, vector, embedded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows_c)
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier, "
        "rule_score, fit_required, fit_process, final_score, band, reasons, flags, top_terms) VALUES "
        "(?, 'v1', 'none', ?, 'candidate', 1, 70, NULL, 0.6, 70, 'strong', '[]', '[]', '[]')", [pid_c, now])

    stats = RE.score(con, all_rows=True)
    assert stats["skipped_no_rollup"] >= 1
    assert stats["skipped_no_tfidf"] >= 1
    accounted = stats["scored"] + stats["skipped_no_units"] + stats["skipped_no_rollup"] + stats["skipped_no_tfidf"]
    assert accounted == stats["population"], "every population row must land in exactly one scored/skip bucket"

    row_b = con.execute("SELECT 1 FROM required_embed WHERE posting_id = ?", [pid_b]).fetchone()
    row_c = con.execute("SELECT 1 FROM required_embed WHERE posting_id = ?", [pid_c]).fetchone()
    assert row_b is None and row_c is None


def test_line_label_matching_marks_quoted_line_unmet():
    seen = [{"ord": 0, "text": "REQLINE baseline requirement for the role", "grp": "required", "weight": 1.0},
            {"ord": 1, "text": "GAPKEY certification required for this role advanced", "grp": "required",
             "weight": 1.0}]
    pop = {"p1": {"required_fit": "fails", "required_unmet": "GAPKEY certification required"}}
    labels = RE._line_labels(pop, {"p1": seen})
    by_ord = {l["ord"]: l for l in labels}
    assert by_ord[1]["status"] == "unmet" and by_ord[1]["y01"] == 1
    assert by_ord[0]["status"] == "noisy_met"  # required_fit != meets, and this line was not quoted


def test_line_label_clean_met_when_posting_meets():
    seen = [{"ord": 0, "text": "REQLINE baseline requirement", "grp": "required", "weight": 1.0}]
    pop = {"p1": {"required_fit": "meets", "required_unmet": ""}}
    labels = RE._line_labels(pop, {"p1": seen})
    assert labels[0]["status"] == "clean_met" and labels[0]["y01"] == 0


def test_rollup_stats_shape_and_values():
    stats = RE._rollup_stats([0.9, 0.1, 0.2], [1.0, 1.0, 2.0])
    assert set(stats) == set(RE.ROLLUP_FEAT_NAMES)
    assert stats["max_p"] == pytest.approx(0.9)
    assert stats["n_lines"] == 3.0
    assert 0.0 <= stats["noisy_or"] <= 1.0


def test_v14_migration_adds_required_embed_table_and_view(tmp_path):
    con = store.connect(str(tmp_path / "db.duckdb"))
    assert store.SCHEMA_VERSION == 14
    assert con.execute("SELECT count(*) FROM required_embed").fetchone()[0] == 0
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'vw_lens_fit'").fetchall()}
    assert {"embed_required", "embed_worst_line", "embed_worst_line_p"} <= cols
    con.close()


def test_train_score_round_trip(tmp_path):
    con = store.connect(str(tmp_path / "db.duckdb"))
    _seed_corpus(con, n_employers=6, meets_per=1, fails_per=1)

    result = RE.train(con)
    assert result["n"] > 0
    assert result["accepted"], f"acceptance gate unexpectedly failed: {result}"
    assert result["auc_stack_B"] >= 0.71
    assert result["auc_stack_B"] - (result["auc_tfidf_only_B"] or 0) >= 0.05
    assert "model_version" in result

    bundle = RE.load_latest(con)
    assert bundle is not None and bundle["version"] == result["model_version"]

    stats = RE.score(con)
    assert stats["scored"] > 0
    assert stats["oof"] > 0  # every training posting scores OOF, never in-sample

    rows = con.execute("SELECT posting_id, embed_required, is_oof, worst_line, worst_line_p, n_lines "
                       "FROM required_embed").fetchall()
    assert len(rows) == stats["scored"]
    for pid, embed_required, is_oof, worst_line, worst_line_p, n_lines in rows:
        assert 0.0 <= embed_required <= 1.0
        assert is_oof is True  # every seeded posting was in the training population
        assert n_lines >= 1

    # re-scoring without --all is a no-op (already scored under this model_version + hash)
    stats2 = RE.score(con)
    assert stats2["scored"] == 0


def test_vw_lens_fit_prefers_embed_for_unjudged_and_judge_for_judged(tmp_path):
    con = store.connect(str(tmp_path / "db.duckdb"))
    seeded = _seed_corpus(con, n_employers=6, meets_per=1, fails_per=1)
    result = RE.train(con)
    assert result["accepted"]
    RE.score(con)

    # An UNJUDGED posting (no llm_labels row) with an embed_required score: rank_why should read
    # from the embed model, and required_value should use embed_required (via coalesce), not
    # the bare TF-IDF fit_required.
    now = _now()
    pid, dh = _make_posting(con, "UNJUDGED1", "EmployerX", now, _neutral_jd(999))
    rows = [(pid, dh, "v1", 0, "h0", "REQLINE baseline requirement for the role", "Requirements", "required",
             1.0, "work", 1.0, "none", None, now),
            (pid, dh, "v1", 1, "h1", "GAPKEY certification required for this role advanced", "Requirements",
             "required", 1.0, "work", 1.0, "none", None, now)]
    con.executemany(
        "INSERT INTO requirement_units (posting_id, description_hash, splitter, ord, unit_hash, text, "
        "section, grp, weight, klass, spec, model, vector, embedded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier, "
        "rule_score, fit_required, fit_process, final_score, band, reasons, flags, top_terms) VALUES "
        "(?, 'v1', 'none', ?, 'candidate', 1, 70, 0.5, 0.6, 70, 'strong', '[]', '[]', '[]')", [pid, now])
    RE.score(con, all_rows=True)

    row = con.execute("SELECT required_fit, fit_required, embed_required, rank_why FROM vw_lens_fit "
                      "WHERE posting_id = ?", [pid]).fetchone()
    required_fit, fit_required, embed_required, rank_why = row
    assert required_fit is None
    assert embed_required is not None
    assert "embed model, not judged" in rank_why
    got = con.execute("SELECT required_value(?, coalesce(?, ?))", [required_fit, embed_required, fit_required]) \
        .fetchone()[0]
    assert got == pytest.approx(embed_required)

    # A JUDGED posting still shows the judge's own word, regardless of embed_required.
    judged_pid = next(p for p, label in seeded if label == "meets")
    jrow = con.execute("SELECT required_fit, rank_why FROM vw_lens_fit WHERE posting_id = ?",
                       [judged_pid]).fetchone()
    assert jrow[0] == "meets"
    assert "Required: meets" in jrow[1]
