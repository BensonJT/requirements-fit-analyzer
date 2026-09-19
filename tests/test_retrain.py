"""Unit tests for `backend/finder/retrain.py` (sprint plan §24): the promotion gate, the candidate-vs-live
artifact handoff, `run()`'s orchestration, `--history`, and the sweep staleness reminder. Trainers are
stubbed throughout (no real TF-IDF fit, no encoder download) -- the gate math and the file/ledger mechanics
are exercised for real.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import store  # noqa: E402
from backend.finder import features, retrain  # noqa: E402


def _quiet(*_a, **_k):
    pass


def _insert_run(con, *, model, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted, reason="x", version="v1"):
    con.execute("INSERT INTO model_runs (run_id, model, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted, "
                "reason, model_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [f"{model}-{trained_at}", model, trained_at, n_pos, n_neg, auc, shuffle_auc, promoted, reason,
                 version])


# ---------------------------------------------------------------- _gate
def test_gate_promotes_first_run_with_no_ledger_history(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    promoted, reason = retrain._gate(con, "process", 100, 100, 0.55, 0.50)
    assert promoted and "first run" in reason
    con.close()


def test_gate_fails_on_auc_drop_below_tolerance(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _insert_run(con, model="process", trained_at=datetime(2026, 9, 1), n_pos=100, n_neg=100, auc=0.75,
               shuffle_auc=0.50, promoted=True)
    # 0.75 - 0.02 = 0.73; 0.729 must fail
    promoted, reason = retrain._gate(con, "process", 100, 100, 0.729, 0.50)
    assert not promoted and "employer-grouped AUC" in reason
    # exactly at the tolerance edge must pass
    promoted, reason = retrain._gate(con, "process", 100, 100, 0.73, 0.50)
    assert promoted
    con.close()


def test_gate_fails_on_shuffle_auc_outside_band(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    promoted, reason = retrain._gate(con, "process", 100, 100, 0.80, 0.75)
    assert not promoted and "shuffled-label AUC" in reason
    promoted, reason = retrain._gate(con, "process", 100, 100, 0.80, 0.10)
    assert not promoted and "shuffled-label AUC" in reason
    con.close()


def test_gate_fails_on_label_count_drop(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _insert_run(con, model="bullseye", trained_at=datetime(2026, 9, 1), n_pos=100, n_neg=100, auc=0.80,
               shuffle_auc=0.50, promoted=True)
    promoted, reason = retrain._gate(con, "bullseye", 90, 100, 0.80, 0.50)   # 190 < 200
    assert not promoted and "label count" in reason
    promoted, reason = retrain._gate(con, "bullseye", 100, 100, 0.80, 0.50)  # 200 == 200, ok
    assert promoted
    con.close()


def test_gate_fails_when_aucs_unavailable(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    promoted, reason = retrain._gate(con, "process", 100, 100, None, 0.50)
    assert not promoted and "employer-grouped AUC unavailable" in reason
    promoted, reason = retrain._gate(con, "process", 100, 100, 0.80, None)
    assert not promoted and "shuffled-label AUC unavailable" in reason
    con.close()


def test_gate_only_considers_promoted_previous_runs(tmp_path):
    """A non-promoted ledger row must not become the baseline -- only the newest PROMOTED run counts."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _insert_run(con, model="ai", trained_at=datetime(2026, 9, 1), n_pos=1000, n_neg=1000, auc=0.95,
               shuffle_auc=0.50, promoted=False)
    promoted, reason = retrain._gate(con, "ai", 10, 10, 0.55, 0.50)
    assert promoted and "first run" in reason
    con.close()


# ---------------------------------------------------------------- _train_one (candidate/live handoff)
def _fake_train(version, n_pos=100, n_neg=100):
    def _train(con, *, lens, model_dir, insert_row, log=print, **_kw):
        assert insert_row is False
        path = os.path.join(model_dir, f"{version}.joblib")
        with open(path, "wb") as f:
            f.write(b"candidate")
        return {"model_version": version, "path": path, "kind": f"tfidf_lr_{lens}",
                "trained_at": datetime(2026, 9, 19), "notes": {}, "n_pos": n_pos, "n_neg": n_neg,
                "cv_auc": 0.9, "cv_precision_at_20": 0.5, "rows": []}
    return _train


def test_train_one_promotes_and_moves_the_candidate_file_into_model_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "MODEL_DIR", str(tmp_path / "models"))
    con = store.connect(str(tmp_path / "t.duckdb"))
    monkeypatch.setattr(features, "train", _fake_train("cand1"))
    monkeypatch.setattr(features, "employer_grouped_auc",
                        lambda *a, shuffle_labels=False, **kw: (0.50 if shuffle_labels else 0.80))
    result = retrain._train_one(con, "process", dry_run=False, log=_quiet)
    assert result["promoted"] is True
    final_path = os.path.join(features.MODEL_DIR, "cand1.joblib")
    assert os.path.exists(final_path)
    row = con.execute("SELECT kind, n_pos, n_neg, path FROM models WHERE model_version = 'cand1'").fetchone()
    assert row == ("tfidf_lr_process", 100, 100, os.path.relpath(final_path, features.REPO_ROOT))
    ledger = con.execute("SELECT model, promoted FROM model_runs").fetchall()
    assert ledger == [("process", True)]
    con.close()


def test_train_one_keeps_previous_artifact_and_deletes_candidate_on_gate_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "MODEL_DIR", str(tmp_path / "models"))
    con = store.connect(str(tmp_path / "t.duckdb"))
    _insert_run(con, model="process", trained_at=datetime(2026, 9, 1), n_pos=500, n_neg=500, auc=0.90,
               shuffle_auc=0.50, promoted=True, version="live1")
    os.makedirs(features.MODEL_DIR, exist_ok=True)
    live_path = os.path.join(features.MODEL_DIR, "live1.joblib")
    with open(live_path, "wb") as f:
        f.write(b"live")

    monkeypatch.setattr(features, "train", _fake_train("cand2"))
    monkeypatch.setattr(features, "employer_grouped_auc",
                        lambda *a, shuffle_labels=False, **kw: (0.50 if shuffle_labels else 0.60))  # too low
    result = retrain._train_one(con, "process", dry_run=False, log=_quiet)
    assert result["promoted"] is False and "employer-grouped AUC" in result["reason"]
    assert not os.path.exists(os.path.join(features.MODEL_DIR, "cand2.joblib"))
    assert os.path.exists(live_path)   # the live artifact was never touched
    con.close()


def test_train_one_dry_run_never_promotes_even_when_gate_would_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(features, "MODEL_DIR", str(tmp_path / "models"))
    con = store.connect(str(tmp_path / "t.duckdb"))
    monkeypatch.setattr(features, "train", _fake_train("cand3"))
    monkeypatch.setattr(features, "employer_grouped_auc",
                        lambda *a, shuffle_labels=False, **kw: (0.50 if shuffle_labels else 0.90))
    result = retrain._train_one(con, "bullseye", dry_run=True, log=_quiet)
    assert result["promoted"] is False and result["reason"] == "dry-run"
    assert not os.path.exists(os.path.join(features.MODEL_DIR, "cand3.joblib"))  # discarded, never promoted
    assert con.execute("SELECT count(*) FROM models").fetchone()[0] == 0
    row = con.execute("SELECT model, promoted, reason FROM model_runs").fetchone()
    assert row == ("bullseye", False, "dry-run")
    con.close()


# ---------------------------------------------------------------- run() orchestration
def test_run_dry_run_trains_every_model_but_never_rescreens(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("JOBSEARCH_VAULT_DIR", raising=False)
    calls = []

    def fake_train_one(con_, model, *, dry_run, log=print):
        calls.append(("tfidf", model, dry_run))
        return {"model": model, "promoted": False, "reason": "dry-run"}

    def fake_required_embed(con_, *, dry_run, log=print):
        calls.append(("required_embed", dry_run))
        return {"model": "required_embed", "promoted": False, "reason": "dry-run"}

    monkeypatch.setattr(retrain, "_train_one", fake_train_one)
    monkeypatch.setattr(retrain, "_train_required_embed", fake_required_embed)
    monkeypatch.setattr(retrain, "_rescreen_and_rescore", lambda con_, log=print: calls.append("RESCREEN"))

    results = retrain.run(con, dry_run=True, log=_quiet)
    assert [c[1] for c in calls if c[0] == "tfidf"] == list(retrain.LENS_MODELS)
    assert all(c[2] is True for c in calls if c[0] == "tfidf")
    assert ("required_embed", True) in calls
    assert "RESCREEN" not in calls
    assert len(results) == len(retrain.LENS_MODELS) + 1
    con.close()


def test_run_rescreens_only_when_a_model_was_promoted(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("JOBSEARCH_VAULT_DIR", raising=False)
    rescreen_calls = []
    monkeypatch.setattr(retrain, "_rescreen_and_rescore", lambda con_, log=print: rescreen_calls.append(1))

    # none promoted -> no rescreen
    monkeypatch.setattr(retrain, "_train_one",
                        lambda con_, model, *, dry_run, log=print: {"model": model, "promoted": False, "reason": "x"})
    monkeypatch.setattr(retrain, "_train_required_embed",
                        lambda con_, *, dry_run, log=print: {"model": "required_embed", "promoted": False,
                                                              "reason": "x"})
    retrain.run(con, dry_run=False, log=_quiet)
    assert rescreen_calls == []

    # one promoted -> rescreen runs exactly once
    seen = {"n": 0}

    def one_promoted(con_, model, *, dry_run, log=print):
        seen["n"] += 1
        return {"model": model, "promoted": seen["n"] == 1, "reason": "x"}

    monkeypatch.setattr(retrain, "_train_one", one_promoted)
    retrain.run(con, dry_run=False, log=_quiet)
    assert rescreen_calls == [1]
    con.close()


def test_run_skips_labels_sync_without_vault_dir(tmp_path, monkeypatch):
    con = store.connect(str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("JOBSEARCH_VAULT_DIR", raising=False)
    monkeypatch.setattr(retrain, "_train_one",
                        lambda con_, model, *, dry_run, log=print: {"model": model, "promoted": False, "reason": "x"})
    monkeypatch.setattr(retrain, "_train_required_embed",
                        lambda con_, *, dry_run, log=print: {"model": "required_embed", "promoted": False,
                                                              "reason": "x"})
    logged = []
    retrain.run(con, dry_run=True, log=logged.append)
    assert any("skipping `labels`" in line for line in logged)
    con.close()


# ---------------------------------------------------------------- history
def test_history_prints_newest_first(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _insert_run(con, model="process", trained_at=datetime(2026, 9, 1), n_pos=1, n_neg=1, auc=0.5,
               shuffle_auc=0.5, promoted=True, version="old")
    _insert_run(con, model="process", trained_at=datetime(2026, 9, 19), n_pos=2, n_neg=2, auc=0.6,
               shuffle_auc=0.5, promoted=True, version="new")
    rows = retrain.history(con)
    assert [r[2] for r in rows] == [datetime(2026, 9, 19), datetime(2026, 9, 1)]
    lines = []
    retrain.print_history(con, log=lines.append)
    assert lines[1].strip().startswith("2026-09-19")   # newest first, after the header line
    con.close()


def test_history_empty_says_so(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    lines = []
    retrain.print_history(con, log=lines.append)
    assert lines == ["retrain --history: no runs recorded yet"]
    con.close()


# ---------------------------------------------------------------- staleness_reminder
def test_staleness_reminder_silent_when_fresh_and_no_new_labels(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _insert_run(con, model="process", trained_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1), n_pos=1, n_neg=1,
               auc=0.5, shuffle_auc=0.5, promoted=True)
    lines = []
    retrain.staleness_reminder(con, log=lines.append)
    assert lines == []
    con.close()


def test_staleness_reminder_never_trained(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    lines = []
    retrain.staleness_reminder(con, log=lines.append)
    assert len(lines) == 1 and "no promoted retrain on record" in lines[0]
    con.close()


def test_staleness_reminder_triggers_on_age(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _insert_run(con, model="process", trained_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10), n_pos=1, n_neg=1,
               auc=0.5, shuffle_auc=0.5, promoted=True)
    lines = []
    retrain.staleness_reminder(con, log=lines.append)
    assert len(lines) == 1 and "10d old" in lines[0]
    con.close()


def test_staleness_reminder_triggers_on_new_human_labels(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    trained_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    _insert_run(con, model="process", trained_at=trained_at, n_pos=1, n_neg=1, auc=0.5, shuffle_auc=0.5,
               promoted=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(25):
        con.execute("INSERT INTO report_feedback (posting_id, description_hash, verdict, assessor, basis, "
                    "confirmed_by_user, assessed_at, loaded_at) VALUES (?, 'h', 'build', 'user', 'jd_read', "
                    "true, ?, ?)", [f"p{i}", now, now])
    lines = []
    retrain.staleness_reminder(con, log=lines.append)
    assert len(lines) == 1 and "25 new human labels" in lines[0]
    con.close()


def test_staleness_reminder_below_label_threshold_is_silent(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    trained_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    _insert_run(con, model="process", trained_at=trained_at, n_pos=1, n_neg=1, auc=0.5, shuffle_auc=0.5,
               promoted=True)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(24):
        con.execute("INSERT INTO report_feedback (posting_id, description_hash, verdict, assessor, basis, "
                    "confirmed_by_user, assessed_at, loaded_at) VALUES (?, 'h', 'build', 'user', 'jd_read', "
                    "true, ?, ?)", [f"p{i}", now, now])
    lines = []
    retrain.staleness_reminder(con, log=lines.append)
    assert lines == []
    con.close()
