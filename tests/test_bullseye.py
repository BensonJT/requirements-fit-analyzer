"""Unit tests for the `bullseye` model (schema v15) -- NOT a lens, see features.BULLSEYE_MODEL.

Mirrors tests/test_finder.py's Required-block ranking model tests (_seed_required_corpus /
test_train_required_model_*), since `bullseye` follows the exact same plumbing pattern.
"""
import os
import runpy
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend import profile as P  # noqa: E402
from backend.ats import normalize as N  # noqa: E402
from backend.ats import store  # noqa: E402
from backend.finder import features, pipeline  # noqa: E402

EXAMPLE = {k: v for k, v in runpy.run_path(os.path.join(ROOT, "backend", "profile_local.example.py")).items()
           if k.isupper()}


@pytest.fixture(autouse=True)
def neutral_profile(monkeypatch):
    for key, value in EXAMPLE.items():
        monkeypatch.setattr(P, key, value)


def _quiet(*_a, **_k):
    pass


FIT_JD = ("Lead process excellence and continuous improvement across operations. Map value streams, remove handoffs, "
          "own the operating model and lean six sigma deployment with cross-functional stakeholders. ") * 12
OFF_JD = ("Write production Java microservices, own Kubernetes deployments, on-call rotation, code reviews, "
          "distributed systems design and CI pipelines for the platform engineering team. ") * 12


def _seed_bullseye_corpus(con, n_each=20):
    """Judged postings covering all four grades on `grade_process`, so vw_label_set_bullseye has both a
    label-1 (best grade bullseye) and label-0 (best grade adjacent) population, and stretch/wrong rows to
    prove they are excluded outright rather than folded in as negatives."""
    now = datetime(2026, 9, 19, 12, 0)
    grades = {"bullseye": ("BE", FIT_JD), "adjacent": ("AJ", FIT_JD), "stretch": ("ST", OFF_JD),
              "wrong": ("WR", OFF_JD)}
    rows = []
    for grade, (prefix, jd) in grades.items():
        postings = [N.base(req_id=f"{prefix}{i}", title=f"Process Lead {prefix}{i}", url=f"https://x/{prefix}{i}",
                           location="Remote - USA", workplace_type="remote") for i in range(n_each)]
        store.record_board(con, f"Employer{prefix}", "greenhouse", postings, now)
        for i in range(n_each):
            pid = con.execute("SELECT posting_id FROM postings WHERE req_id = ?", [f"{prefix}{i}"]).fetchone()[0]
            text = f"{jd} Case {prefix}{i}."
            dh = store.description_hash(text)
            con.execute("UPDATE postings SET description_text = ?, description_hash = ? WHERE posting_id = ?",
                       [text, dh, pid])
            rows.append([pid, dh, "claude-sonnet-batch", grade, grade, now])
    con.executemany("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                    "grade_process, judged_at) VALUES (?, ?, 'rv1', ?, ?, ?, ?)", rows)


def test_vw_label_set_bullseye_excludes_stretch_and_wrong(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_bullseye_corpus(con, n_each=5)
    rows = features.training_set(con, lens=features.BULLSEYE_MODEL)
    assert rows
    assert {r["grade"] for r in rows} == {"bullseye", "adjacent"}   # stretch/wrong never appear
    by_grade = {}
    for r in rows:
        by_grade.setdefault(r["grade"], []).append(r["label"])
    assert set(by_grade["bullseye"]) == {1}
    assert set(by_grade["adjacent"]) == {0}
    assert all(r["weight"] == 1.0 for r in rows)
    assert len(rows) == 10   # 5 bullseye + 5 adjacent, none of the 10 stretch/wrong
    con.close()


def test_vw_label_set_bullseye_best_grade_wins_across_lenses(tmp_path):
    """A row bullseye on one lens and adjacent on another is a bullseye positive (best-of-three)."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    now = datetime(2026, 9, 19)
    posting = N.base(req_id="MIX1", title="Mixed Lead", url="https://x/MIX1",
                     location="Remote - USA", workplace_type="remote")
    store.record_board(con, "Acme", "greenhouse", [posting], now)
    pid = con.execute("SELECT posting_id FROM postings WHERE req_id = 'MIX1'").fetchone()[0]
    text = FIT_JD + " Case mix."
    dh = store.description_hash(text)
    con.execute("UPDATE postings SET description_text = ?, description_hash = ? WHERE posting_id = ?",
               [text, dh, pid])
    con.execute("INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, "
                "grade_process, grade_technical, grade_ai, judged_at) VALUES "
                "(?, ?, 'rv1', 'test', 'adjacent', 'adjacent', 'bullseye', 'stretch', ?)", [pid, dh, now])
    rows = features.training_set(con, lens=features.BULLSEYE_MODEL)
    assert len(rows) == 1 and rows[0]["label"] == 1 and rows[0]["grade"] == "bullseye"
    con.close()


def test_bullseye_never_in_lenses_or_load_lens_models(tmp_path):
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_bullseye_corpus(con, n_each=20)
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"),
                            lens=features.BULLSEYE_MODEL, log=_quiet)
    kind = con.execute("SELECT kind FROM models WHERE model_version = ?", [result["model_version"]]).fetchone()[0]
    assert kind == "tfidf_lr_bullseye"
    assert "bullseye" not in features.LENSES
    assert features.load_lens_models(con, log=_quiet) == {}
    model = features.load_bullseye_model(con, log=_quiet)
    assert model is not None and model["version"] == result["model_version"]
    con.close()


def test_schema_v15_migration_adds_fit_bullseye_column(tmp_path):
    fresh = store.connect(str(tmp_path / "fresh.duckdb"))
    assert "fit_bullseye" in store._columns(fresh, "screens")
    assert store.SCHEMA_VERSION >= 15
    fresh.close()

    old = store.connect(str(tmp_path / "old.duckdb"))
    old.execute("ALTER TABLE screens DROP COLUMN fit_bullseye")
    old.execute("UPDATE schema_info SET version = 14")
    old.close()
    upgraded = store.connect(str(tmp_path / "old.duckdb"))
    assert "fit_bullseye" in store._columns(upgraded, "screens")
    upgraded.close()


def test_fit_bullseye_written_by_screen_never_moves_verdict_or_final_score(tmp_path):
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_bullseye_corpus(con, n_each=20)
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"),
                            lens=features.BULLSEYE_MODEL, log=_quiet)
    bullseye_model = features.load_bullseye_model(con, log=_quiet)
    assert bullseye_model["version"] == result["model_version"]

    now = datetime(2026, 9, 20, 12, 0)
    posting = N.base(req_id="NEWROW", title="Process Excellence Lead 99", url="https://x/NEWROW",
                     location="Remote - USA", workplace_type="remote")
    store.record_board(con, "Acme", "greenhouse", [posting], now)
    pid = con.execute("SELECT posting_id FROM postings WHERE req_id = 'NEWROW'").fetchone()[0]
    con.execute("UPDATE postings SET description_text = ?, description_fetched_at = ? WHERE posting_id = ?",
               [FIT_JD + " Case new.", now, pid])

    pipeline.screen(con, full=True, bullseye_model=None, log=_quiet)
    before = con.execute("SELECT verdict, final_score, fit_bullseye FROM vw_screen_latest "
                         "WHERE posting_id = ?", [pid]).fetchone()
    assert before[2] is None

    pipeline.screen(con, full=True, bullseye_model=bullseye_model, log=_quiet)
    after = con.execute("SELECT verdict, final_score, fit_bullseye FROM vw_screen_latest "
                        "WHERE posting_id = ?", [pid]).fetchone()
    assert after[2] is not None
    assert (before[0], before[1]) == (after[0], after[1])
    con.close()


def test_content_fit_and_combine_ignore_fit_bullseye():
    import inspect
    from backend.finder import pipeline as PL
    assert "fit_bullseye" not in inspect.signature(PL.content_fit).parameters
    assert "bullseye" not in inspect.signature(PL.combine).parameters


# ---------------------------------------------------------------- rank feed (vw_lens_fit)

def _screened_posting(con, pid, *, employer="Acme", grade_process=None, grade_technical=None, grade_ai=None,
                      fit_process=None, fit_technical=None, fit_ai=None, fit_bullseye=None, verdict="review",
                      required_fit=None):
    now = datetime(2026, 9, 19)
    con.execute(
        "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary, "
        "status, description_hash, first_seen_at, last_seen_at) VALUES "
        "(?, ?, 'greenhouse', ?, 'T', ?, 'Remote - USA', 'active', 'h', ?, ?)",
        [pid, employer, pid, f"https://x/{pid}", now, now])
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
        "final_score, band, fit_process, fit_technical, fit_ai, fit_bullseye) VALUES "
        "(?, 'rv', 'mv', ?, ?, 70, 80, 'strong', ?, ?, ?, ?)",
        [pid, now, verdict, fit_process, fit_technical, fit_ai, fit_bullseye])
    if grade_process or grade_technical or grade_ai:
        con.execute(
            "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
            "grade_technical, grade_ai, required_fit, judged_at) VALUES (?, 'h', 'rv1', 'test', 'bullseye', ?, ?, ?, ?, ?)",
            [pid, grade_process, grade_technical, grade_ai, required_fit, now])


def test_rank_feed_uses_fit_bullseye_only_for_fully_unjudged_strong_rows(tmp_path):
    """rank_lens_best (and so rank_score) is boosted by fit_bullseye ONLY when all three lens grades are NULL,
    lens_best already clears lens_strong_p(), and fit_bullseye is not NULL. A judged row (any single grade
    present) and a row below lens_strong_p() must be untouched."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    # unjudged, strong (fit_process 0.9 >= 0.70), high fit_bullseye -> boosted
    _screened_posting(con, "u1" * 10, fit_process=0.9, fit_bullseye=0.95)
    # unjudged, strong, low fit_bullseye -> lower boost than u1, but still boosted (not equal to plain lens_best)
    _screened_posting(con, "u2" * 10, fit_process=0.9, fit_bullseye=0.10)
    # unjudged, strong, NO fit_bullseye -> untouched (rank_lens_best == lens_best)
    _screened_posting(con, "u3" * 10, fit_process=0.9, fit_bullseye=None)
    # unjudged, NOT strong (below lens_strong_p) even with high fit_bullseye -> untouched
    _screened_posting(con, "u4" * 10, fit_process=0.3, fit_bullseye=0.95)
    # JUDGED (one grade present) with high fit_bullseye -> untouched, judged grade wins as always
    _screened_posting(con, "u5" * 10, fit_process=0.9, grade_process="adjacent", fit_bullseye=0.95)

    rows = {r[0]: r[1] for r in con.execute(
        "SELECT posting_id, rank_score, rank_why FROM vw_lens_fit WHERE posting_id IN (?, ?, ?, ?, ?)",
        ["u1" * 10, "u2" * 10, "u3" * 10, "u4" * 10, "u5" * 10]).fetchall()}
    assert rows["u1" * 10] > rows["u3" * 10]                      # high fit_bullseye boosts above the untouched row
    assert rows["u3" * 10] > rows["u2" * 10]                      # low fit_bullseye actually drags it down (0.10 < 0.9)
    assert rows["u4" * 10] == 0.0                                 # below lens_strong_p -> zero gate, untouched by bullseye
    # u5 (judged) must equal what an identical row scores with NO fit_bullseye at all -- judged grade untouched
    con.execute("UPDATE screens SET fit_bullseye = NULL WHERE posting_id = ?", ["u5" * 10])
    u5_no_bull = con.execute("SELECT rank_score FROM vw_lens_fit WHERE posting_id = ?", ["u5" * 10]).fetchone()[0]
    assert u5_no_bull == rows["u5" * 10]
    con.close()


def test_rank_why_mentions_bullseye_only_for_the_same_population(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _screened_posting(con, "b1" * 10, fit_process=0.9, fit_bullseye=0.81)     # unjudged, strong, >= 0.5 -> mentioned
    _screened_posting(con, "b2" * 10, fit_process=0.9, fit_bullseye=0.30)     # unjudged, strong, < 0.5 -> not mentioned
    _screened_posting(con, "b3" * 10, fit_process=0.9, grade_process="adjacent", fit_bullseye=0.90)  # judged -> not mentioned
    rows = dict(con.execute("SELECT posting_id, rank_why FROM vw_lens_fit WHERE posting_id IN (?, ?, ?)",
                            ["b1" * 10, "b2" * 10, "b3" * 10]).fetchall())
    assert "bullseye ~0.81 (model)" in rows["b1" * 10]
    assert "bullseye" not in rows["b2" * 10]
    assert "bullseye" not in rows["b3" * 10]
    con.close()


def test_fit_bullseye_exposed_as_vw_lens_fit_column(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'vw_lens_fit'").fetchall()}
    assert "fit_bullseye" in cols
    con.close()


# ---------------------------------------------------------------- backfill

def test_bullseye_backfill_touches_only_latest_row_of_in_population_postings(tmp_path):
    """Population: active, non-rejected, best TF-IDF lens prob or lens_best >= 0.5. Only the LATEST screens
    row (per vw_screen_latest) is updated -- an older row for the same posting must stay untouched."""
    pytest.importorskip("sklearn")
    con = store.connect(str(tmp_path / "t.duckdb"))
    _seed_bullseye_corpus(con, n_each=20)
    result = features.train(con, cv=3, min_df=1, max_df=1.0, model_dir=str(tmp_path / "models"),
                            lens=features.BULLSEYE_MODEL, log=_quiet)
    bullseye_model = features.load_bullseye_model(con, log=_quiet)
    assert bullseye_model["version"] == result["model_version"]

    now = datetime(2026, 9, 20, 12, 0)
    posting = N.base(req_id="BF1", title="Process Excellence Lead", url="https://x/BF1",
                     location="Remote - USA", workplace_type="remote")
    store.record_board(con, "Acme", "greenhouse", [posting], now)
    pid = con.execute("SELECT posting_id FROM postings WHERE req_id = 'BF1'").fetchone()[0]
    con.execute("UPDATE postings SET description_text = ?, description_fetched_at = ? WHERE posting_id = ?",
               [FIT_JD + " Case bf.", now, pid])
    # an OLD screens row (older screened_at), strong on process, so it is NOT vw_screen_latest
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                "rule_score, final_score, band, fit_process) VALUES (?, 'rv0', 'mv0', ?, 'review', 70, 80, "
                "'strong', 0.9)", [pid, datetime(2026, 9, 18)])
    # the LATEST row, also strong -> in population
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                "rule_score, final_score, band, fit_process) VALUES (?, 'rv1', 'mv1', ?, 'review', 70, 80, "
                "'strong', 0.9)", [pid, now])
    # a rejected posting, otherwise in population -- must be excluded (own employer so this board pull does not
    # close BF1: record_board closes anything missing from a clean pull of the SAME employer/platform)
    rej = N.base(req_id="BF2", title="Process Excellence Lead 2", url="https://x/BF2",
                location="Remote - USA", workplace_type="remote")
    store.record_board(con, "AcmeRej", "greenhouse", [rej], now)
    rej_pid = con.execute("SELECT posting_id FROM postings WHERE req_id = 'BF2'").fetchone()[0]
    con.execute("UPDATE postings SET description_text = ?, description_fetched_at = ? WHERE posting_id = ?",
               [FIT_JD + " Case bf2.", now, rej_pid])
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, "
                "rule_score, final_score, band, fit_process) VALUES (?, 'rv1', 'mv1', ?, 'reject', 70, 80, "
                "'strong', 0.9)", [rej_pid, now])

    stats = pipeline.bullseye_backfill(con, bullseye_model, log=_quiet)
    assert stats["updated"] >= 1

    old_row = con.execute("SELECT fit_bullseye FROM screens WHERE posting_id = ? AND rules_version = 'rv0'",
                          [pid]).fetchone()[0]
    new_row = con.execute("SELECT fit_bullseye FROM screens WHERE posting_id = ? AND rules_version = 'rv1'",
                          [pid]).fetchone()[0]
    rej_row = con.execute("SELECT fit_bullseye FROM screens WHERE posting_id = ?", [rej_pid]).fetchone()[0]
    assert old_row is None                 # only the latest row was touched
    assert new_row is not None
    assert rej_row is None                 # rejected postings are excluded from the population
    con.close()


def test_a_hold_is_not_decided_but_a_later_build_or_pass_is(tmp_path):
    """2026-09-22 (user): a hold means "not ready to build yet, may build later" -- it keeps surfacing (marked
    held); only the LATEST decision counts, so a build or pass after the hold hides the posting."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    for pid in ("h1" * 10, "h2" * 10, "h3" * 10):
        _screened_posting(con, pid)
    con.execute("INSERT INTO decisions VALUES (?, 'hold', NULL, 'cli', NULL, ?)", ["h1" * 10, datetime(2026, 9, 22, 9)])
    con.execute("INSERT INTO decisions VALUES (?, 'hold', NULL, 'cli', NULL, ?)", ["h2" * 10, datetime(2026, 9, 22, 9)])
    con.execute("INSERT INTO decisions VALUES (?, 'pass', 'level: too junior', 'cli', NULL, ?)",
                ["h2" * 10, datetime(2026, 9, 22, 10)])
    got = {r[0]: (r[1], r[2]) for r in con.execute("SELECT posting_id, decided, held FROM vw_lens_fit").fetchall()}
    assert got["h1" * 10] == (False, True)     # held: still shown
    assert got["h2" * 10] == (True, False)     # held, then passed: hidden
    assert got["h3" * 10] == (False, False)    # untouched
    con.close()
