"""Tests for the Jev measurement-system study (backend/finder/jev_eval.py, docs/JEV_PLAN.md §4).

Every fixture is synthetic and hand-computable; every database is a tmp_path DuckDB built by store.connect().
Nothing here opens the live DB or makes a network call.
"""
import json
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import store  # noqa: E402
from backend.finder import jev_eval as E  # noqa: E402
from backend.finder import jev_questions as Q  # noqa: E402
from backend.finder import judge2  # noqa: E402
from backend.finder.jev_types import LineRecord, ReviewRecord  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0)
REVIEWED_AT = "2026-09-28T12:00:00+00:00"
PV = "jevpv1"
J2PV = "j2pv1"


def _noop(*_a, **_kw):
    return None


@pytest.fixture
def con(tmp_path):
    c = store.connect(str(tmp_path / "t.duckdb"))
    yield c
    c.close()


# ---------------------------------------------------------------- fixture builders
def _posting(con, pid, dh=None, *, text="Required:\nA synthetic requirement line."):
    dh = dh if dh is not None else f"h-{pid}"
    con.execute(
        "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, status, description_text, "
        "description_hash, first_seen_at, last_seen_at) VALUES (?, 'Acme', 'greenhouse', ?, 'Analyst', ?, "
        "'active', ?, ?, ?, ?)", [pid, pid, f"https://x/{pid}", text, dh, NOW, NOW])
    return dh


def _screen(con, pid, *, fit_process=None, fit_technical=None, fit_ai=None):
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
        "final_score, band, fit_process, fit_technical, fit_ai) VALUES (?, 'rv', 'mv', ?, 'review', 50, 60, "
        "'partial', ?, ?, ?)", [pid, NOW, fit_process, fit_technical, fit_ai])


def _blind(con, pid, dh, human_fit, *, unmet=None, basis="blind"):
    con.execute(
        "INSERT INTO report_feedback (posting_id, description_hash, verdict, basis, assessor, required_fit, "
        "required_unmet, assessed_at, loaded_at) VALUES (?, ?, ?, ?, 'user', ?, ?, ?, ?)",
        [pid, dh, "pass" if human_fit == "fails" else "build", basis, human_fit, unmet, NOW, NOW])


def _judge1(con, pid, dh, *, required_fit=None, grades=None):
    grades = grades or {}
    con.execute(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
        "grade_technical, grade_ai, required_fit, judged_at) VALUES (?, ?, 'rv1', 'claude-sonnet-batch', "
        "'adjacent', ?, ?, ?, ?, ?)",
        [pid, dh, grades.get("process"), grades.get("technical"), grades.get("ai"), required_fit, NOW])


def _judge2(con, pid, dh, fit, pv=J2PV):
    con.execute(
        "INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model, required_fit, "
        "unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence, raw_response, prompt_chars, "
        "reviewed_at) VALUES (?, ?, ?, 'gemini', 'm', ?, '[]', 0, false, false, NULL, 'high', '', 0, ?)",
        [pid, dh, pv, fit, NOW])


def _human_lens(con, pid, dh, lens, grade, basis="blind"):
    con.execute(
        "INSERT INTO human_lens_grades (posting_id, description_hash, lens, grade, basis, graded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)", [pid, dh, lens, grade, basis, NOW])


def _probs_for(grade):
    """0.7 on the grade, 0.1 elsewhere: P(bullseye)+P(adjacent) is 0.8 for a strong grade, else 0.2."""
    return {g: (0.7 if g == grade else 0.1) for g in Q.GRADE_LEVELS}


def _line(no, *, section="required", text=None, verdict="met", kind="skill", probs=None):
    return LineRecord(
        line_no=no, section=section, line_text=text or f"synthetic line {no}", kind=kind, kind_conf=0.9,
        verdict=verdict, verdict_raw=verdict,
        verdict_probs=probs or {"met": 0.7, "adjacent": 0.1, "unmet": 0.1, "unclear": 0.1}, verdict_conf=0.7,
        evidence_fact_id="f01", evidence_p=0.8, evidence_text="a synthetic fact", evidence_downgraded=False)


def _gates(*, canary=0.02, noul=0.1, choice="occasional", choice_p=0.8):
    return {"gate_people_manager": noul, "canary_ai_directed": canary,
            "onsite_cadence": {"choice": choice, "confidence": 0.7,
                               "probabilities": {choice: choice_p, "not_stated": round(1 - choice_p, 6)}}}


def _review(pid, dh, *, pv=PV, tag=None, drift=False, required_fit="meets", grades=None, probs=None,
            gates=None, injection_p=0.02, lines=None):
    grades = grades or {}
    probs = probs or {}
    fields = {}
    for lens in E.LENSES:
        g = grades.get(lens, "adjacent")
        fields[f"lens_{lens}_score"] = float(Q.GRADE_LEVELS.index(g))
        fields[f"lens_{lens}_grade"] = g
        fields[f"lens_{lens}_conf"] = 0.7
        fields[f"lens_{lens}_probs"] = probs.get(lens, _probs_for(g))
    return ReviewRecord(
        posting_id=pid, description_hash=dh, prompt_version=E.tagged_pv(pv, tag), run_tag=tag,
        endpoint="typesafe", model_requested=Q.PINNED_MODEL,
        model_answered="jev-9.9.9" if drift else Q.PINNED_MODEL, version_drift=drift, input_tokens=100,
        **fields, gates=gates if gates is not None else _gates(canary=injection_p), injection_p=injection_p,
        required_fit=required_fit, derive_why="test", lines_fit=required_fit, shape_fit="fits", shape_score=0.8,
        raw_response={}, reviewed_at=REVIEWED_AT, lines=lines if lines is not None else [])


def _store(con, record):
    store.insert_jev_review(con, record)
    return record


def _eval_rows(con):
    return con.execute("SELECT family, passed, n, metrics FROM jev_evals ORDER BY family").fetchall()


# ---------------------------------------------------------------- numeric helpers
def test_auc_hand_example_and_ties_and_missing_class():
    assert E.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == pytest.approx(0.75)
    assert E.auc([0.5, 0.5], [0, 1]) == pytest.approx(0.5)            # a tie counts half
    assert E.auc([0.2, 0.8, 0.8, 0.9], [0, 1, 0, 1]) == pytest.approx(0.875)
    assert E.auc([0.1, 0.9], [1, 1]) is None
    assert E.auc([], []) is None


def test_quantile_and_ece_bins():
    assert E._quantile([1, 2, 3, 4], 0.5) == pytest.approx(2.5)
    assert E._quantile([0, 10], 0.95) == pytest.approx(9.5)
    assert E._quantile([], 0.5) is None
    assert E._bin_of(0.0) == 0 and E._bin_of(0.2) == 1 and E._bin_of(1.0) == 4
    # all forecasts 0.5 but no events: one bin, |0.5 - 0| = 0.5
    assert E.ece([0.5] * 4, [0] * 4) == pytest.approx(0.5)


def test_bootstrap_ci_is_deterministic_for_a_seed():
    f = [0.1, 0.3, 0.6, 0.9, 0.7, 0.2] * 5
    e = [0, 0, 1, 1, 0, 0] * 5
    a = E.calibration_metrics(f, e, n_boot=200, seed=7)
    b = E.calibration_metrics(f, e, n_boot=200, seed=7)
    c = E.calibration_metrics(f, e, n_boot=200, seed=8)
    assert a["ece_ci90"] == b["ece_ci90"]
    assert a["ece_ci90"] != c["ece_ci90"]
    assert a["ece_ci90"][0] <= a["ece_ci90"][1]


# ---------------------------------------------------------------- 1. required fit
# Six catch rows (judge1 meets, human fails), seven agree rows (human meets), one all-fails-only row
# (judge1 fails, human fails) and one unjudged gold row. Jev and judge2 receive IDENTICAL calls.
CATCH_CALLS = ["fails", "partial", "meets", "fails", "partial", "fails"]      # 5/6 caught, 3/6 strict
AGREE_CALLS = ["meets"] * 6 + ["partial"]                                       # 6/7 agree


def _required_fixture(con, *, jev=True, j2=True, pv=PV):
    specs = ([("C", i, "fails", "meets", c) for i, c in enumerate(CATCH_CALLS)]
             + [("A", i, "meets", "meets", c) for i, c in enumerate(AGREE_CALLS)]
             + [("F", 0, "fails", "fails", "partial"), ("U", 0, "meets", "meets", None)])
    for prefix, i, human, first, call in specs:
        pid = f"{prefix}{i:02d}"
        dh = _posting(con, pid)
        _blind(con, pid, dh, human, unmet="A synthetic requirement line." if human == "fails" else None)
        _judge1(con, pid, dh, required_fit=first)
        if call is None:
            continue
        if j2:
            _judge2(con, pid, dh, call)
        if jev:
            _store(con, _review(pid, dh, pv=pv, required_fit=call))


def test_required_matches_judge2_evaluate_on_equivalent_data(con):
    _required_fixture(con)
    j2 = judge2.evaluate(con, prompt_version_override=J2PV, log=_noop)
    jv = E.evaluate_required(con, PV, run_id="run1")
    for key in ("n_catch", "catch_rate", "catch_rate_strict", "n_all_fails", "catch_rate_all_fails",
                "n_agree", "agree_rate", "n_unjudged", "insufficient", "passed"):
        assert jv[key] == j2[key], key
    assert jv["catch_rate"] == pytest.approx(5 / 6)
    assert jv["catch_rate_strict"] == pytest.approx(3 / 6)
    assert jv["catch_rate_all_fails"] == pytest.approx(6 / 7)
    assert jv["agree_rate"] == pytest.approx(6 / 7)
    assert jv["n_unjudged"] == 1 and jv["passed"] is True
    assert [m["posting_id"] for m in jv["catch_misses"]] == ["C02"]
    family, passed, n, metrics = _eval_rows(con)[0]
    assert (family, passed, n) == ("required", True, 15)
    assert json.loads(metrics)["n_catch"] == 6


def test_required_ignores_drifted_stale_tagged_and_other_version_rows(con):
    _required_fixture(con, jev=False)
    # every gold posting gets ONLY rows that must not count: drifted, stale hash, a rerun, another pv
    for (pid,) in con.execute("SELECT posting_id FROM postings").fetchall():
        dh = f"h-{pid}"
        _store(con, _review(pid, dh, drift=True, required_fit="fails"))
        _store(con, _review(pid, "stale-hash", required_fit="fails"))
        _store(con, _review(pid, dh, tag="r2", required_fit="fails"))
        _store(con, _review(pid, dh, pv="otherpv", required_fit="fails"))
    r = E.evaluate_required(con, PV, run_id="run1", write=False)
    assert r["n_eval_rows"] == 15 and r["n_unjudged"] == 15
    assert r["insufficient"] is True and r["passed"] is False
    assert r["reason"].startswith("insufficient data")
    assert _eval_rows(con) == []                      # write=False stores nothing


def test_required_none_call_is_unjudged(con):
    _required_fixture(con)
    con.execute("UPDATE jev_reviews SET required_fit = NULL WHERE posting_id = 'A00'")
    r = E.evaluate_required(con, PV, run_id="run1", write=False)
    assert r["n_unjudged"] == 2 and r["n_agree"] == 6


def test_required_on_empty_db_is_insufficient_and_stored(con):
    r = E.evaluate_required(con, PV, run_id="run1")
    assert r["passed"] is False and r["n_eval_rows"] == 0 and r["catch_rate"] is None
    assert _eval_rows(con)[0][:2] == ("required", False)


# ---------------------------------------------------------------- 2. compare
def test_compare_counts_and_rows(con):
    calls = {"P1": ("meets", "meets", "meets"), "P2": ("fails", "partial", "fails"),
             "P3": ("fails", "fails", None), "P4": ("meets", None, "fails")}
    for pid, (gold, jv, j2) in calls.items():
        dh = _posting(con, pid)
        _blind(con, pid, dh, gold)
        if jv:
            _store(con, _review(pid, dh, required_fit=jv))
        if j2:
            _judge2(con, pid, dh, j2)
    r = E.compare(con, PV, J2PV, run_id="run1")
    assert r["jev_vs_gold"] == {"n": 3, "agree": 2, "rate": pytest.approx(2 / 3)}
    assert r["judge2_vs_gold"] == {"n": 3, "agree": 2, "rate": pytest.approx(2 / 3)}
    assert r["jev_vs_judge2"] == {"n": 2, "agree": 1, "rate": pytest.approx(0.5)}
    assert [row["posting_id"] for row in r["rows"]] == ["P1", "P2", "P3", "P4"]
    assert r["rows"][3] == {"posting_id": "P4", "gold": "meets", "jev": None, "judge2": "fails"}
    assert r["passed"] is True and r["reason"] == "reported only (no bar)"
    assert _eval_rows(con)[0][:2] == ("compare", True)


def test_best_judge2_pv_prefers_passed_then_rates(con):
    assert E.best_judge2_pv(con) is None
    for pv, passed, c, a in (("a", False, 0.9, 0.9), ("b", True, 0.7, 0.85), ("c", True, 0.8, 0.9),
                             ("c:r2", True, 1.0, 1.0)):
        con.execute("INSERT INTO judge2_evals (run_id, prompt_version, evaluated_at, catch_rate, agree_rate, "
                    "passed, reason) VALUES (?, ?, ?, ?, ?, ?, 't')", [pv, pv, NOW, c, a, passed])
    assert E.best_judge2_pv(con) == "c"


# ---------------------------------------------------------------- 3. lens
HUMAN = ["bullseye", "adjacent", "stretch", "wrong", "bullseye", "wrong", "adjacent", "stretch"]
JEV = ["bullseye", "adjacent", "stretch", "wrong", "adjacent", "wrong", "adjacent", "bullseye"]
TFIDF_WORSE = [0.9, 0.3, 0.6, 0.1, 0.7, 0.2, 0.4, 0.5]      # AUC 0.75 (Jev's is 0.875)
TFIDF_BETTER = [0.9, 0.8, 0.1, 0.1, 0.9, 0.1, 0.8, 0.1]     # AUC 1.0


def _lens_fixture(con, tfidf, *, prefix="L", pv=PV):
    """Eight BLIND process grades (the hand numbers above), plus two SEEN rows where Jev says `wrong`, the
    human `bullseye` and TF-IDF 0.95: were they counted, exact, within-one and both AUCs would all move."""
    for i, (h, j, t) in enumerate(zip(HUMAN, JEV, tfidf)):
        pid = f"{prefix}{i:02d}"
        dh = _posting(con, pid)
        _screen(con, pid, fit_process=t)
        _human_lens(con, pid, dh, "process", h, basis="blind")
        _store(con, _review(pid, dh, pv=pv, grades={"process": j}))
    for i in range(2):
        pid = f"{prefix}S{i}"
        dh = _posting(con, pid)
        _screen(con, pid, fit_process=0.95)
        _human_lens(con, pid, dh, "process", "bullseye", basis="seen")
        _store(con, _review(pid, dh, pv=pv, grades={"process": "wrong"}))


def test_lens_agreement_confusion_and_auc_fairness(con):
    _lens_fixture(con, TFIDF_WORSE)
    _judge1(con, "L00", "h-L00", grades={"process": "bullseye", "technical": "wrong"})
    r = E.evaluate_lens(con, PV, run_id="run1")
    o, p = r["overall"], r["lenses"]["process"]
    assert o["n"] == 8 and o["exact"] == pytest.approx(6 / 8) and o["within_one"] == pytest.approx(7 / 8)
    assert o["confusion"]["bullseye"] == {"wrong": 0, "stretch": 0, "adjacent": 1, "bullseye": 1}
    assert o["confusion"]["stretch"]["bullseye"] == 1
    # all bases (reported only): the two seen rows are both misses -> 6 of 10
    assert o["all_bases"] == {"n": 10, "exact": pytest.approx(6 / 10)}
    assert p["all_bases"] == {"n": 10, "exact": pytest.approx(6 / 10)}
    assert r["basis"] == "blind" and r["tfidf_note"] == E.TFIDF_NOTE
    assert p["auc_jev"] == pytest.approx(0.875) and p["auc_tfidf"] == pytest.approx(0.75)
    assert p["n_auc"] == 8 and p["n_auc_pos"] == 4
    assert r["lenses"]["technical"]["n"] == 0 and r["lenses"]["technical"]["auc_jev"] is None
    # secondary: first-judge process bullseye == Jev bullseye; technical wrong != Jev's default adjacent
    assert r["secondary_llm_judge"]["process"] == {"n": 1, "exact": 1.0}
    assert r["secondary_llm_judge"]["overall"] == {"n": 2, "exact": 0.5}
    assert r["passed"] is True and r["reason"].startswith("blind exact=0.75")


def test_lens_fails_when_tfidf_beats_jev(con):
    _lens_fixture(con, TFIDF_BETTER)
    r = E.evaluate_lens(con, PV, run_id="run1", write=False)
    assert r["lenses"]["process"]["auc_tfidf"] == pytest.approx(1.0)
    assert r["passed"] is False and "behind TF-IDF on process" in r["reason"]


def test_lens_insufficient_below_min_n_and_ignores_drift(con):
    for i in range(3):
        pid = f"L{i}"
        dh = _posting(con, pid)
        _screen(con, pid, fit_process=0.5)
        _human_lens(con, pid, dh, "process", "bullseye")
        _store(con, _review(pid, dh, grades={"process": "bullseye"}))
    for i in range(3, 8):                                   # drifted: never counted
        pid = f"L{i}"
        dh = _posting(con, pid)
        _human_lens(con, pid, dh, "process", "wrong")
        _store(con, _review(pid, dh, drift=True))
    for i in range(8, 14):                                  # seen: reported under all_bases, never the bar
        pid = f"L{i}"
        dh = _posting(con, pid)
        _screen(con, pid, fit_process=0.1)
        _human_lens(con, pid, dh, "process", "wrong", basis="seen")
        _store(con, _review(pid, dh, grades={"process": "wrong"}))
    r = E.evaluate_lens(con, PV, run_id="run1")
    assert r["overall"]["n"] == 3 and r["overall"]["all_bases"]["n"] == 9
    assert r["passed"] is False and r["reason"].startswith("insufficient data")
    assert _eval_rows(con)[0][:2] == ("lens", False)


# ---------------------------------------------------------------- 4. repeatability
# Per pair: 3 lens grades + required_fit + 3 gates (2 Nouls, 1 Choice) + 2 lines x (verdict, kind) = 11
# categorical items; 12 lens probs + 2 Nouls + 2 Choice probs + 2 x 4 verdict probs = 24 probabilities.
def _two_lines():
    return [_line(0), _line(1, section="responsibility")]


def _repeat_fixture(con, *, n=5, tag="r2", flips=0, delta=0.0, pv=PV):
    for i in range(n):
        pid = f"P{i:02d}"
        dh = _posting(con, pid)
        _store(con, _review(pid, dh, pv=pv, lines=_two_lines()))
        grades = {"process": "bullseye"} if i < flips else {}
        probs = {}
        if i == 0 and delta:
            probs = {"process": {"wrong": 0.1, "stretch": 0.1, "adjacent": 0.7 - delta, "bullseye": 0.1 + delta}}
        _store(con, _review(pid, dh, pv=pv, tag=tag, grades=grades, probs=probs, lines=_two_lines()))


def test_repeatability_known_flip_count_passes_at_one_flip(con):
    _repeat_fixture(con, flips=1, delta=0.1)
    r = E.evaluate_repeatability(con, PV, ("r2", "r3"), run_id="run1")
    assert r["n_pairs"] == 5 and r["pairs_per_tag"] == {"r2": 5, "r3": 0}
    assert r["n_categorical"] == 55 and r["n_flips"] == 1
    assert r["flip_rate"] == pytest.approx(1 / 55)
    assert r["n_probabilities"] == 120
    assert r["median_abs_delta"] == 0.0 and r["max_abs_delta"] == pytest.approx(0.1)
    assert r["worst"][0] == {"posting_id": "P00", "tag": "r2", "field": "lens_process_grade",
                             "a": "adjacent", "b": "bullseye", "kind": "flip"}
    assert r["worst"][1]["kind"] == "delta" and r["worst"][1]["delta"] == pytest.approx(0.1)
    assert len(r["worst"]) == E.WORST_K
    assert r["passed"] is True


def test_repeatability_fails_at_two_flips(con):
    _repeat_fixture(con, flips=2)
    r = E.evaluate_repeatability(con, PV, run_id="run1", write=False)
    assert r["flip_rate"] == pytest.approx(2 / 55) and r["passed"] is False


def test_repeatability_counts_noul_threshold_choice_and_line_flips():
    a = _review("X", "h", lines=[_line(0), _line(1)])
    b = _review("X", "h", tag="r2", gates=_gates(noul=0.6, choice="not_stated"),
                lines=[_line(0, verdict="unmet", kind="tool")])
    cats, probs = E.diff_reviews(a, b)
    flipped = sorted(f for f, x, y in cats if x != y)
    assert flipped == ["gates.gate_people_manager>=0.5", "gates.onsite_cadence.choice",
                       "lines[0].kind", "lines[0].verdict", "lines[1].present"]
    by = {f: (x, y) for f, x, y in probs}
    assert by["gates.gate_people_manager"] == (0.1, 0.6)
    assert by["gates.onsite_cadence.p.occasional"] == (0.8, 0.0)      # missing option counts as 0.0


def test_repeatability_ignores_drifted_reruns_and_insufficient_below_min_n(con):
    _repeat_fixture(con, n=4)
    pid, dh = "P09", _posting(con, "P09")
    _store(con, _review(pid, dh))
    _store(con, _review(pid, dh, tag="r2", drift=True, grades={"ai": "wrong"}))
    r = E.evaluate_repeatability(con, PV, run_id="run1")
    assert r["n_pairs"] == 4 and r["n_flips"] == 0
    assert r["passed"] is False and r["reason"].startswith("insufficient data")


# ---------------------------------------------------------------- 5. calibration
LOW = {"wrong": 0.5, "stretch": 0.4, "adjacent": 0.05, "bullseye": 0.05}     # forecast 0.1
HIGH = {"wrong": 0.05, "stretch": 0.05, "adjacent": 0.45, "bullseye": 0.45}  # forecast 0.9


def test_perfectly_calibrated_lens_forecasts_give_ece_zero(con):
    # 20 postings x 2 lenses: forecast 0.1 with 2 of 20 events, forecast 0.9 with 18 of 20 events.
    for i in range(20):
        pid = f"K{i:02d}"
        dh = _posting(con, pid)
        high = i >= 10
        grade = ("bullseye" if i != 19 else "wrong") if high else ("adjacent" if i == 0 else "stretch")
        for lens in ("process", "technical"):
            _human_lens(con, pid, dh, lens, grade)
        _store(con, _review(pid, dh, probs={"process": HIGH if high else LOW, "technical": HIGH if high else LOW}))
    r = E.evaluate_calibration(con, PV, run_id="run1", n_boot=200)
    lens = r["lens"]
    assert lens["n"] == 40 and lens["n_events"] == 20
    assert lens["ece"] == pytest.approx(0.0, abs=1e-9)
    assert lens["brier"] == pytest.approx(0.09)
    table = {row["lo"]: row for row in lens["reliability"]}
    assert table[0.0]["n"] == 20 and table[0.0]["observed_rate"] == pytest.approx(0.1)
    assert table[0.8]["mean_forecast"] == pytest.approx(0.9) and table[0.4]["n"] == 0
    assert 0.0 <= lens["ece_ci90"][0] <= lens["ece_ci90"][1] < 0.2
    assert r["passed"] is True
    assert _eval_rows(con)[0][:3] == ("calibration", True, 40)


def test_lines_calibration_matches_gold_unmet_by_containment(con):
    lines = [_line(0, text="Active security clearance required on day one.",
                   probs={"met": 0.1, "adjacent": 0.0, "unmet": 0.8, "unclear": 0.1}),
             _line(1, text="Bachelor's degree in a related field.",
                   probs={"met": 0.7, "adjacent": 0.0, "unmet": 0.2, "unclear": 0.1}),
             _line(2, section="responsibility", text="Active security clearance required on day one.")]
    dh = _posting(con, "G1")
    _blind(con, "G1", dh, "fails", unmet="active   SECURITY clearance required ; Something not listed")
    _store(con, _review("G1", dh, lines=lines))
    dh = _posting(con, "G2")                                  # meets: every required line is a non-event
    _blind(con, "G2", dh, "meets")
    _store(con, _review("G2", dh, lines=lines[:2]))
    dh = _posting(con, "G3")                                  # fails with no unmet named: skipped
    _blind(con, "G3", dh, "fails")
    _store(con, _review("G3", dh, lines=lines))
    r = E.evaluate_calibration(con, PV, run_id="run1", write=False, n_boot=50)
    ln = r["lines"]
    assert ln["n"] == 4 and ln["n_events"] == 1 and ln["postings_skipped_no_unmet_named"] == 1
    # forecasts [0.8 (event), 0.2, 0.8, 0.2]: Brier = (0.04 + 0.04 + 0.64 + 0.04) / 4
    assert ln["brier"] == pytest.approx(0.19)
    assert r["lens"]["n"] == 0 and r["passed"] is False and r["reason"].startswith("insufficient data")


# ---------------------------------------------------------------- 6. injection
def _gold(con, n, prefix="G"):
    out = []
    for i in range(n):
        pid = f"{prefix}{i:02d}"
        dh = _posting(con, pid, text=f"Required:\nSynthetic line {i}.")
        _blind(con, pid, dh, "meets")
        out.append((pid, dh))
    return out


def test_injection_postings_are_deterministic_and_keyed_apart(con):
    _gold(con, 6)
    posts = E.injection_postings(con)
    assert [p.posting_id for p in posts] == [f"G{i:02d}#inj{i}" for i in range(5)]
    assert posts[2].description_hash == "h-G02#inj2"
    assert posts[2].description_text.endswith(E.INJECTION_TEMPLATES[2])
    assert posts[0].description_text.startswith("Required:\nSynthetic line 0.")
    assert len(E.INJECTION_TEMPLATES) == 5
    assert E.injection_postings(con, k=2) == posts[:2]


def _injection_fixture(con, canaries):
    gold = _gold(con, 5)
    for (pid, dh), inj, p in zip(gold, E.injection_postings(con), canaries):
        _store(con, _review(pid, dh, injection_p=0.01))
        grades = {"process": "bullseye"} if pid == "G00" else {}
        _store(con, _review(inj.posting_id, inj.description_hash, tag=E.INJECTION_TAG, injection_p=p,
                            grades=grades, required_fit="partial" if pid == "G01" else "meets"))


def test_injection_catches_four_of_five_and_reports_shift(con):
    _injection_fixture(con, [0.9, 0.95, 0.6, Q.CANARY_FLAG_AT, 0.1])
    r = E.evaluate_injection(con, PV, run_id="run1")
    assert r["n_found"] == 5 and r["caught"] == 4 and r["passed"] is True
    rows = {row["posting_id"]: row for row in r["rows"]}
    assert rows["G00#inj0"]["shift"]["lens_grades_changed"] == ["process"]
    assert rows["G00#inj0"]["shift"]["max_abs_lens_prob_delta"] == pytest.approx(0.6)
    assert rows["G01#inj1"]["shift"]["required_fit_changed"] is True
    assert rows["G04#inj4"]["caught"] is False and rows["G04#inj4"]["original_injection_p"] == 0.01


def test_injection_fails_at_three_and_is_insufficient_when_missing(con):
    _injection_fixture(con, [0.9, 0.9, 0.9, 0.1, 0.1])
    assert E.evaluate_injection(con, PV, run_id="run1", write=False)["passed"] is False
    con.execute("DELETE FROM jev_reviews WHERE posting_id = 'G04#inj4'")
    r = E.evaluate_injection(con, PV, run_id="run2", write=False)
    assert r["n_found"] == 4 and r["reason"].startswith("insufficient data")


# ---------------------------------------------------------------- 7. sentinel
def test_sentinel_postings_and_no_alert_when_identical(con):
    gold = _gold(con, 12)
    assert [p.posting_id for p in E.sentinel_postings(con)] == [f"G{i:02d}" for i in range(10)]
    for pid, dh in gold:
        _store(con, _review(pid, dh, lines=_two_lines()))
        _store(con, _review(pid, dh, tag="s1", lines=_two_lines()))
    r = E.evaluate_sentinel(con, PV, "s1", run_id="run1")
    assert r["n_pairs"] == 10 and r["n_alerts"] == 0 and r["passed"] is True


def test_sentinel_alerts_on_a_big_probability_move_and_on_a_grade_flip(con):
    gold = _gold(con, 10)
    for pid, dh in gold:
        _store(con, _review(pid, dh))
        _store(con, _review(pid, dh, tag="s1", gates=_gates(noul=0.1 + (0.06 if pid == "G03" else 0.0))))
    r = E.evaluate_sentinel(con, PV, "s1", run_id="run1", write=False)
    assert r["n_alerts"] == 1 and r["alerts"][0]["field"] == "gates.gate_people_manager"
    assert r["passed"] is False
    con.execute("UPDATE jev_reviews SET required_fit = 'fails' WHERE posting_id = 'G05' AND run_tag = 's1'")
    r = E.evaluate_sentinel(con, PV, "s1", run_id="run2", write=False)
    assert {a["field"] for a in r["alerts"]} == {"gates.gate_people_manager", "required_fit"}


def test_sentinel_uses_the_pinned_set_when_given(con):
    """A pinned set that is NOT the first-n gold postings is what gets compared (gold can grow after pinning)."""
    gold = _gold(con, 12)
    pinned = [p for p in E.sentinel_postings(con, 12) if p.posting_id in ("G10", "G11")]
    for pid, dh in gold:
        _store(con, _review(pid, dh))
    for p in pinned:
        _store(con, _review(p.posting_id, p.description_hash, tag="s1"))
    r = E.evaluate_sentinel(con, PV, "s1", run_id="run1", write=False, postings=pinned)
    assert r["n_postings"] == 2 and r["n_pairs"] == 2 and r["passed"] is True
    # without the pinned set it falls back to the first 10, none of which were rerun
    assert E.evaluate_sentinel(con, PV, "s1", run_id="run2", write=False)["passed"] is False


def test_sentinel_missing_rerun_is_insufficient(con):
    gold = _gold(con, 3)
    for pid, dh in gold:
        _store(con, _review(pid, dh))
    r = E.evaluate_sentinel(con, PV, "s1", run_id="run1")
    assert r["passed"] is False and r["reason"].startswith("insufficient data")


# ---------------------------------------------------------------- 8. evaluate_all, the report, the bar view
def _bar(con, pv=PV):
    row = con.execute("SELECT jev_bar_passed FROM vw_jev_bar WHERE prompt_version = ?", [pv]).fetchone()
    return None if row is None else row[0]


def test_bar_view_flips_true_once_required_lens_and_repeatability_pass(con):
    _required_fixture(con)
    _lens_fixture(con, TFIDF_WORSE)
    _repeat_fixture(con, flips=1)
    assert E.evaluate_required(con, PV, run_id="run1")["passed"] is True
    assert E.evaluate_lens(con, PV, run_id="run1")["passed"] is True
    assert _bar(con) is False
    assert E.evaluate_repeatability(con, PV, run_id="run1")["passed"] is True
    assert _bar(con) is True


def test_evaluate_all_runs_families_with_data_and_formats_every_bar(con):
    _required_fixture(con)
    _lens_fixture(con, TFIDF_WORSE)
    _repeat_fixture(con, flips=1)
    results = E.evaluate_all(con, PV, run_id="all1", judge2_pv=J2PV)
    assert set(results) == {"required", "compare", "lens", "calibration", "repeatability"}
    assert {r[0] for r in _eval_rows(con)} == set(results)
    assert _bar(con) is True
    text = E.format_report(results)
    for needle in (f"bar >= {E.CATCH_BAR}", f"bar >= {E.AGREE_BAR}", f"bar >= {E.LENS_EXACT_BAR}",
                   f"bar <= {E.REPEAT_FLIP_BAR}", f"bar <= {E.REPEAT_MEDIAN_DELTA_BAR}",
                   f"lens ECE <= {E.CALIBRATION_ECE_BAR}", "[compare] reported",
                   "all bases, reported only", E.TFIDF_NOTE,
                   "Bar (required + lens + repeatability): PASSED"):
        assert needle in text, needle
    assert "Acme" not in text and "Analyst" not in text      # posting ids only


def test_evaluate_all_on_empty_db_and_injection_sentinel_paths(con):
    assert E.evaluate_all(con, PV, run_id="x") == {}
    assert "nothing evaluated" in E.format_report({})
    _injection_fixture(con, [0.9] * 5)
    for p in E.sentinel_postings(con):
        _store(con, _review(p.posting_id, p.description_hash, pv=PV, tag="s1", injection_p=0.01))
    results = E.evaluate_all(con, PV, run_id="x2", sentinel_tag="s1", write=False)
    assert results["injection"]["passed"] is True and results["sentinel"]["passed"] is True
    assert "compare" not in results and "repeatability" not in results
    text = E.format_report(results)
    assert "[injection] PASSED" in text and "[sentinel] PASSED" in text
    assert "Bar (required + lens + repeatability): NOT PASSED" in text
    assert _eval_rows(con) == []
