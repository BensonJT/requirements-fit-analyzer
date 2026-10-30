"""Stage 2 of the Jev tier (backend/finder/jev_gate.py; user's go 2026-09-30): the end-of-pipeline demotion
pass over Top Jobs. Every test builds its own tmp DuckDB; nothing here makes a network call -- `run_over_top`
is exercised with a fake transport through jev.run's own injection points, the same way tests/test_jev.py does.
"""
import os
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import store  # noqa: E402
from backend.finder import jev_gate, report  # noqa: E402
from backend.finder.jev_types import LineRecord, ReviewRecord, RunSummary  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0)
REVIEWED_AT = "2026-09-28T12:00:00+00:00"
PV = "pv-passed"


def _posting(con, pid, *, grades=("bullseye", "adjacent", "wrong"), required_fit="meets", scorer="claude-sonnet-batch",
             lens_grade_source=None, score=80, jd="## Requirements\n5 years of process work.\n"):
    con.execute(
        "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary, status, "
        "description_hash, description_text, first_seen_at, last_seen_at) VALUES (?, 'Acme', 'greenhouse', ?, "
        "'Director, Ops', ?, 'Remote - USA', 'active', 'h', ?, ?, ?)", [pid, pid, f"https://x/{pid}", jd, NOW, NOW])
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
        "final_score, band, level_fit) VALUES (?, 'rv', 'mv', ?, 'review', 70, ?, 'strong', 'in_range')",
        [pid, NOW, score])
    con.execute(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
        "grade_technical, grade_ai, required_fit, lens_grade_source, judged_at) VALUES (?, 'h', 'rv1', ?, "
        "'bullseye', ?, ?, ?, ?, ?, ?)", [pid, scorer, *grades, required_fit, lens_grade_source, NOW])


def _line(n, verdict, text, section="required", grey_p=None):
    return LineRecord(line_no=n, section=section, line_text=text, kind="skill", kind_conf=0.9, verdict=verdict,
                      verdict_raw=verdict, verdict_probs={"met": 0.1, "adjacent": 0.1, "unmet": 0.7, "unclear": 0.1},
                      verdict_conf=0.7, evidence_fact_id=None, evidence_p=None, evidence_text=None,
                      evidence_downgraded=False, grey_p=grey_p)


def _review(pid, *, pv=PV, required_fit="meets", grades=("adjacent", "adjacent", "adjacent"), why="all required met",
            lines=(), reviewed_at=REVIEWED_AT):
    probs = {"wrong": 0.1, "stretch": 0.2, "adjacent": 0.5, "bullseye": 0.2}
    gp, gt, ga = grades
    return ReviewRecord(
        posting_id=pid, description_hash="h", prompt_version=pv, run_tag=None, endpoint="typesafe",
        model_requested="jev-1.13.0", model_answered="jev-1.13.0", version_drift=False, input_tokens=1000,
        lens_process_score=1.0, lens_process_grade=gp, lens_process_conf=0.7, lens_process_probs=probs,
        lens_technical_score=1.0, lens_technical_grade=gt, lens_technical_conf=0.7, lens_technical_probs=dict(probs),
        lens_ai_score=1.0, lens_ai_grade=ga, lens_ai_conf=0.7, lens_ai_probs=dict(probs),
        gates={}, injection_p=0.02, required_fit=required_fit, derive_why=why, lines_fit=required_fit,
        shape_fit="fits", shape_score=0.8, raw_response={}, reviewed_at=reviewed_at, lines=list(lines))


def _pass_required(con, pv=PV):
    store.insert_jev_eval(con, run_id="r1", prompt_version=pv, family="required", n=115, metrics={"catch": 0.9},
                          passed=True, reason="test")


def _gemma(con, pid, required_fit, unmet="Active Secret clearance."):
    con.execute("""INSERT INTO judge2_reviews (posting_id, description_hash, prompt_version, provider, model,
                   required_fit, unmet, unmet_discarded, downgraded, held_clearance, years_gap, confidence,
                   raw_response, prompt_chars, reviewed_at) VALUES (?, 'h', 'g1', 'gemini', 'm', ?, ?, 0, false,
                   false, NULL, 'high', '', 0, ?)""", [pid, required_fit, f'["{unmet}"]', NOW])


def _section(text, heading):
    body = text.split(heading, 1)[1].split("\n## ", 1)[0]
    return [ln for ln in body.splitlines() if ln.startswith("| ") and not ln.startswith("|---")][1:]


def _fixture(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "keep-meets", score=90)
    _posting(con, "demote-fails", score=85)
    _posting(con, "demote-all-wrong", score=80)
    _posting(con, "exempt-human", score=75, scorer="user-adjudicated", lens_grade_source="human")
    _posting(con, "keep-unevaluated-pv", score=70)
    _posting(con, "keep-no-review", score=65)
    _posting(con, "review-demote", score=60, required_fit="arguable")
    store.insert_jev_review(con, _review("keep-meets"))
    store.insert_jev_review(con, _review("demote-fails", required_fit="fails", why="hard gate unmet: Secret",
                                         lines=[_line(0, "unmet", "Active Secret clearance required."),
                                                _line(1, "met", "5 years of process work.")]))
    store.insert_jev_review(con, _review("demote-all-wrong", grades=("wrong", "wrong", "wrong"),
                                         why="all hard gates met, no soft gap"))
    store.insert_jev_review(con, _review("exempt-human", required_fit="fails", grades=("wrong", "wrong", "wrong")))
    store.insert_jev_review(con, _review("keep-unevaluated-pv", pv="pv-new", required_fit="fails"))
    store.insert_jev_review(con, _review("review-demote", required_fit="fails", why="shape: wrong"))
    _pass_required(con)
    _gemma(con, "demote-fails", "fails")
    _gemma(con, "demote-all-wrong", "meets")
    return con


# ---------------------------------------------------------------- the pure rule
@pytest.mark.parametrize("req, grades, passed, adj, expect", [
    ("fails", ("adjacent", "adjacent", "adjacent"), True, False, ["required fails"]),
    ("meets", ("wrong", "wrong", "wrong"), True, False, ["wrong on every lens"]),
    ("fails", ("wrong", "wrong", "wrong"), True, False, ["required fails", "wrong on every lens"]),
    ("meets", ("wrong", "wrong", "adjacent"), True, False, []),          # one lens not wrong: keep
    ("partial", ("adjacent", "adjacent", "adjacent"), True, False, []),  # partial never demotes
    ("fails", ("wrong", "wrong", "wrong"), False, False, []),            # no passed required bar: keep
    ("fails", ("wrong", "wrong", "wrong"), True, True, []),              # adjudicated: human > Jev
    (None, (), True, False, []),                                         # no call, no grades
])
def test_decide(req, grades, passed, adj, expect):
    assert jev_gate.decide(req, grades, required_passed=passed, adjudicated=adj) == expect


# ---------------------------------------------------------------- demotions against a database
def test_demotions_apply_the_rule_with_the_bar_gate_and_the_human_exemption(tmp_path):
    con = _fixture(tmp_path)
    shown = jev_gate.top_subset_ids(con)
    assert shown == {"keep-meets": "apply", "demote-fails": "apply", "demote-all-wrong": "apply",
                     "exempt-human": "apply", "keep-unevaluated-pv": "apply", "keep-no-review": "apply",
                     "review-demote": "review"}
    d = jev_gate.demotions(con, shown)
    assert set(d) == {"demote-fails", "demote-all-wrong", "review-demote"}
    assert d["demote-fails"].reasons == ["required fails"]
    assert d["demote-fails"].unmet == ["Active Secret clearance required."]
    assert d["demote-fails"].gemma_required == "fails"
    assert d["demote-fails"].gemma_unmet == "Active Secret clearance."
    assert d["demote-all-wrong"].reasons == ["wrong on every lens"]
    assert d["demote-all-wrong"].gemma_required == "meets"      # reported beside, never a veto
    assert d["review-demote"].was == "review"
    assert jev_gate.gate_status(con) == f"active under prompt_version {PV}"
    con.close()


def test_gate_is_inactive_without_a_passed_required_bar(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "p1")
    store.insert_jev_review(con, _review("p1", required_fit="fails"))
    assert jev_gate.demotions(con, {"p1": "apply"}) == {}
    assert jev_gate.gate_status(con).startswith("INACTIVE")
    con.close()


# ---------------------------------------------------------------- the page
def test_write_top_jobs_moves_demoted_rows_to_their_own_section(tmp_path):
    con = _fixture(tmp_path)
    text = report.write_top_jobs(con, None, out_path=str(tmp_path / "out")).read_text(encoding="utf-8")
    apply_rows = _section(text, "## Apply")
    review_rows = _section(text, "## Review")
    demoted = _section(text, "## Jev demoted")
    assert [r.split("|")[2].strip() for r in demoted] == ["Acme"] * 3
    assert "demote-fails" in demoted[0] and "demote-all-wrong" in demoted[1] and "review-demote" in demoted[2]
    assert all("demote" not in r for r in apply_rows + review_rows)
    assert any("exempt-human" in r for r in apply_rows)            # human > Jev
    assert any("keep-unevaluated-pv" in r for r in apply_rows)     # its prompt_version has no passed bar
    assert any("keep-no-review" in r for r in apply_rows)
    assert not review_rows                                         # its only row was demoted
    # the demoted table: Was, judge grades, Jev grades, Gemma beside, the why with the unmet line
    cells = [c.strip() for c in demoted[0].strip("|").split("|")]
    assert cells[0] == "apply"
    assert cells[3] == "bull / adj / wrong · meets"
    assert cells[4] == "adj / adj / adj · fails"
    assert cells[5].startswith("fails: Active Secret clearance.")
    assert cells[6].startswith("required fails — hard gate unmet: Secret · unmet: Active Secret clearance required.")
    cells = [c.strip() for c in demoted[1].strip("|").split("|")]
    assert cells[4] == "wrong / wrong / wrong · meets" and cells[5] == "meets" and cells[6].startswith("wrong on every lens")
    assert f"**Jev demotion pass:** active under prompt_version {PV} — **WARNING: 1 shown row(s) reviewed under pv-new" in text
    assert "; 3 row(s) demoted" in text
    assert "finder.py mark <id> build" in text
    # the rank itself never moved: Apply is still in rank order and the demoted rows keep their rank_score
    assert con.execute("SELECT count(*) FROM vw_lens_fit WHERE rank_score IS NOT NULL").fetchone()[0] == 7
    con.close()


def test_write_top_jobs_without_the_gate_shows_every_row(tmp_path):
    con = _fixture(tmp_path)
    text = report.write_top_jobs(con, None, out_path=str(tmp_path / "out"), jev_gate_on=False).read_text(encoding="utf-8")
    assert "## Jev demoted" not in text
    assert any("demote-fails" in r for r in _section(text, "## Apply"))
    assert "**Jev demotion pass:** off" in text
    con.close()


# ---------------------------------------------------------------- run_over_top
def test_run_over_top_sends_the_subset_minus_adjudicated_and_never_fails_the_report(tmp_path, monkeypatch):
    con = _fixture(tmp_path)
    from backend.finder import jev
    sent = []

    def fake_run(con_, rows, **kw):
        sent.extend(p.posting_id for p in rows)
        raise RuntimeError("cap reached")           # a failure must be logged, not raised

    monkeypatch.setattr(jev, "run", fake_run)
    monkeypatch.setattr(jev_gate, "ensure_required_bar", lambda *a, **k: None)
    logs = []
    summary = jev_gate.run_over_top(con, facts=[], live_ok=True, log=logs.append)
    assert summary.stopped_by_error == "run failed (RuntimeError)"     # 2026-10-28: reported, still never raised
    # 2026-10-28: the four rows reviewed under the older passed PV are kept, not re-sent (the current
    # prompt_version has no passed bar, so not even their demotions are re-reviewed)
    assert set(sent) == {"keep-unevaluated-pv", "keep-no-review"}
    assert "exempt-human" not in sent
    assert any("7 row(s) in the review set (6 apply / 1 review / 0 other eligible), 1 adjudicated exempt, "
               "4 kept from an earlier passed prompt_version, 2 with a JD to send or skip as cached, 0 held" in l
               for l in logs)
    assert any("run failed (RuntimeError: cap reached)" in l for l in logs)
    con.close()


def test_judge2_run_filters_posting_ids_before_the_top_n_cut(tmp_path):
    """2026-10-01: with posting_ids, top_n must count the WANTED rows, not the top N of the whole population
    (the cut came first and dropped wanted ids ranked below N)."""
    from backend.finder import judge2
    con = _fixture(tmp_path)
    wanted = ["review-demote"]                      # lowest-ranked fixture row
    res = judge2.run(con, top_n=1, posting_ids=wanted, dry_run=True, show=0, background="public", log=lambda *_: None)
    assert res["count"] == 1
    con.close()


# ---------------------------------------------------------------- 2026-10-15: a re-keyed prompt_version
def test_gate_status_warns_about_shown_rows_under_an_unmeasured_prompt_version(tmp_path):
    con = _fixture(tmp_path)
    line = jev_gate.gate_status(con, list(jev_gate.top_subset_ids(con)))
    assert line.startswith(f"active under prompt_version {PV}")
    assert "1 shown row(s) reviewed under pv-new" in line and "are NOT demoted" in line
    assert "WARNING" not in jev_gate.gate_status(con)          # no shown ids: the old one-liner
    con.close()


def test_ensure_required_bar_measures_only_an_unmeasured_current_pv_and_only_live(tmp_path, monkeypatch):
    from backend.finder import jev, jev_eval
    con = _fixture(tmp_path)
    calls = []
    monkeypatch.setattr(jev, "prompt_version", lambda facts, endpoint: "pv-new")
    monkeypatch.setattr(jev, "eval_set_postings", lambda con_: ["gold"])
    monkeypatch.setattr(jev, "run", lambda con_, rows, **kw: calls.append(("run", rows)) or RunSummary())

    def fake_eval(con_, pv, *, run_id, write):
        calls.append(("eval", pv))
        store.insert_jev_eval(con_, run_id=run_id, prompt_version=pv, family="required", n=115, metrics={},
                              passed=True, reason="test")
        return {"passed": True, "reason": "test"}

    monkeypatch.setattr(jev_eval, "evaluate_required", fake_eval)
    logs = []
    assert jev_gate.ensure_required_bar(con, facts=[], live_ok=False, log=logs.append) is None
    assert calls == [] and any("not live" in l for l in logs)
    assert jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=logs.append)["passed"]
    assert calls == [("run", ["gold"]), ("eval", "pv-new")]
    assert jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=logs.append) is None   # measured now
    assert len(calls) == 2
    # and the row under pv-new now demotes
    assert "keep-unevaluated-pv" in jev_gate.demotions(con, jev_gate.top_subset_ids(con))
    con.close()


# ---------------------------------------------------------------- 2026-10-20: grey lines go to "check", not demoted
@pytest.mark.parametrize("req, unmet, clear, expect", [
    ("fails", ["or-list line"], 0, True),          # every unmet line grey
    ("fails", ["a", "b"], 1, False),               # one clear-cut unmet line: demote
    ("fails", [], 0, False),                       # fails with no unmet line (shape): demote as before
    ("meets", ["a"], 0, False),                    # not a fails at all
])
def test_grey_only(req, unmet, clear, expect):
    assert jev_gate.grey_only(req, unmet, clear) is expect


def _grey_fixture(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    for pid in ("grey-only", "one-clear", "legacy-null", "grey-but-all-wrong"):
        _posting(con, pid)
    store.insert_jev_review(con, _review("grey-only", required_fit="fails", lines=[
        _line(0, "unmet", "5 years in strategic operations, consulting or HR strategy.", grey_p=0.8),
        _line(1, "unmet", "Familiarity with workforce analytics.", grey_p=0.6),
        _line(2, "met", "Bachelor's degree.", grey_p=0.05)]))
    store.insert_jev_review(con, _review("one-clear", required_fit="fails", lines=[
        _line(0, "unmet", "Active TS/SCI clearance.", grey_p=0.02),
        _line(1, "unmet", "Familiarity with workforce analytics.", grey_p=0.7)]))
    store.insert_jev_review(con, _review("legacy-null", required_fit="fails", lines=[
        _line(0, "unmet", "CPA licence required.")]))
    store.insert_jev_review(con, _review("grey-but-all-wrong", required_fit="fails", grades=("wrong", "wrong", "wrong"),
                                         lines=[_line(0, "unmet", "An or-list line.", grey_p=0.9)]))
    _pass_required(con)
    return con


def test_grey_only_fails_go_to_check_and_clear_gaps_still_demote(tmp_path):
    con = _grey_fixture(tmp_path)
    shown = {pid: "apply" for pid in ("grey-only", "one-clear", "legacy-null", "grey-but-all-wrong")}
    out = jev_gate.demotions(con, shown)
    assert out["grey-only"].check is True and out["grey-only"].reasons == ["required fails on judgment-call lines only"]
    assert out["one-clear"].check is False and out["one-clear"].reasons == ["required fails"]
    assert out["legacy-null"].check is False                     # reviews from before the question: unchanged
    assert out["grey-but-all-wrong"].check is False              # every lens wrong still demotes
    assert out["grey-but-all-wrong"].reasons == ["wrong on every lens"]
    con.close()


def test_grey_check_respects_bar_and_human_exemption(tmp_path):
    con = _grey_fixture(tmp_path)
    con.execute("DELETE FROM jev_evals")                         # no passed bar: neither demoted nor checked
    assert jev_gate.demotions(con, {"grey-only": "apply"}) == {}
    con.close()


def test_report_shows_check_section(tmp_path):
    con = _grey_fixture(tmp_path)
    text = report.write_top_jobs(con, None, out_path=str(tmp_path / "out")).read_text(encoding="utf-8")
    rows = _section(text, "## Check the Required block")
    assert len(rows) == 1 and "5 years in strategic operations" in rows[0]
    demoted = _section(text, "## Jev demoted")
    assert len(demoted) == 3 and not any("x/grey-only" in r for r in demoted)
    assert "1 sent to Check the Required block" in text
    con.close()



# ---------------------------------------------------------------- 2026-10-25: every eligible row, new-review limit
def test_limit_new_keeps_cached_rows_and_holds_uncached_ones_over_the_limit(monkeypatch):
    from types import SimpleNamespace
    from backend.finder import jev
    cached = {"c1", "c2"}

    class FakePersist:
        def jev_already_reviewed(self, con, pid, h, pv, run_tag):
            return pid in cached

    monkeypatch.setattr(jev, "StorePersist", FakePersist)
    rows = [SimpleNamespace(posting_id=i, description_hash="h") for i in ("n1", "c1", "n2", "n3", "c2", "n4")]
    keep, held = jev_gate.limit_new(None, rows, facts=[], limit=2)
    assert [p.posting_id for p in keep] == ["n1", "c1", "n2", "c2"]      # cached rows never count
    assert held == ["n3", "n4"]


def test_review_subset_puts_top_jobs_rows_first(tmp_path, monkeypatch):
    con = _fixture(tmp_path)
    monkeypatch.setattr(jev_gate, "eligible_ids", lambda con_, **kw: ["extra-strong", "keep-meets"])
    out = jev_gate.review_subset_ids(con)
    top = jev_gate.top_subset_ids(con)
    assert list(out)[:len(top)] == list(top)                          # Top Jobs order kept, first
    assert out["extra-strong"] == "eligible" and out["keep-meets"] == top["keep-meets"]
    con.close()


# ---------------------------------------------------------------- 2026-10-28: stuck bar, reuse, warnings
def _bar_fixture(tmp_path, monkeypatch, gold_summary):
    from backend.finder import jev, jev_eval
    con = _fixture(tmp_path)
    calls = []
    monkeypatch.setattr(jev, "prompt_version", lambda facts, endpoint: "pv-new")
    monkeypatch.setattr(jev, "eval_set_postings", lambda con_: ["gold"])
    monkeypatch.setattr(jev, "run", lambda con_, rows, **kw: calls.append(("run", rows)) or gold_summary)

    def fake_eval(con_, pv, *, run_id, write):
        calls.append(("eval", pv))
        return {"passed": True, "reason": "test"}

    monkeypatch.setattr(jev_eval, "evaluate_required", fake_eval)
    return con, calls


def test_ensure_required_bar_measures_again_after_insufficient_data(tmp_path, monkeypatch):
    """10/27: the cap stopped the gold set at 0 of 121, an "insufficient data" row was written, and that row
    blocked every later measurement. It now counts as unmeasured."""
    con, calls = _bar_fixture(tmp_path, monkeypatch, RunSummary())
    store.insert_jev_eval(con, run_id="r0", prompt_version="pv-new", family="required", n=121, metrics={},
                          passed=False, reason="insufficient data: unjudged=121 of 121")
    logs = []
    assert jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=logs.append)["passed"]
    assert calls == [("run", ["gold"]), ("eval", "pv-new")]
    assert any("had too little data last time" in l for l in logs)
    con.close()


def test_ensure_required_bar_keeps_a_real_failure(tmp_path, monkeypatch):
    con, calls = _bar_fixture(tmp_path, monkeypatch, RunSummary())
    store.insert_jev_eval(con, run_id="r0", prompt_version="pv-new", family="required", n=121, metrics={},
                          passed=False, reason="catch_rate=0.5 (bar 0.7)")
    logs = []
    assert jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=logs.append) is None
    assert calls == [] and any("measured and NOT passed" in l for l in logs)
    con.close()


@pytest.mark.parametrize("summary", [RunSummary(stopped_by_cap="daily_token_cap"),
                                     RunSummary(stopped_by_error="HTTP 402 billing_error")])
def test_ensure_required_bar_does_not_score_a_gold_set_that_stopped_early(tmp_path, monkeypatch, summary):
    con, calls = _bar_fixture(tmp_path, monkeypatch, summary)
    logs = []
    assert jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=logs.append) is None
    assert calls == [("run", ["gold"])]                        # no eval row, so the next run measures again
    assert any("gold set stopped early" in l for l in logs)
    con.close()


def test_reusable_ids_keeps_older_passed_reviews_and_resends_their_demotions_once_the_bar_passes(tmp_path):
    con = _fixture(tmp_path)
    wanted = {pid: t for pid, t in jev_gate.review_subset_ids(con).items() if pid != "exempt-human"}
    # the current prompt_version has no passed bar: every older passed review is kept, demotions included
    assert jev_gate.reusable_ids(con, wanted, "pv-current") == {"keep-meets", "demote-fails", "demote-all-wrong",
                                                                "review-demote"}
    # once it passes, the rows the older reviews demote are reviewed again; the rest stay kept
    store.insert_jev_eval(con, run_id="r2", prompt_version="pv-current", family="required", n=115, metrics={},
                          passed=True, reason="test")
    assert jev_gate.reusable_ids(con, wanted, "pv-current") == {"keep-meets"}
    # a review under the current prompt_version is never "reused": jev.run's own cache skips it
    assert jev_gate.reusable_ids(con, wanted, PV) == set()
    assert jev_gate.reusable_ids(con, {}, "pv-current") == set()
    con.close()


def test_run_warning():
    assert jev_gate.run_warning(None) is None
    assert jev_gate.run_warning(RunSummary(considered=5, reviewed=5)) is None
    w = jev_gate.run_warning(RunSummary(considered=300, errors=1, stopped_by_error="HTTP 402 billing_error"))
    assert "HTTP 402 billing_error" in w and "stored reviews" in w
    assert jev_gate.run_warning(RunSummary(considered=10, skipped_cached=4, errors=2)) == "2 of 6 review(s) failed"
    assert "daily_token_cap" in jev_gate.run_warning(RunSummary(reviewed=3, stopped_by_cap="daily_token_cap"))


def test_write_top_jobs_header_carries_the_jev_warning(tmp_path):
    con = _fixture(tmp_path)
    text = report.write_top_jobs(con, None, out_path=str(tmp_path / "out"),
                                 jev_warning="stopped at the first account error (HTTP 402)").read_text(encoding="utf-8")
    assert "**JEV PROBLEM THIS RUN: stopped at the first account error (HTTP 402)**" in text
    assert "JEV PROBLEM" not in report.write_top_jobs(con, None, out_path=str(tmp_path / "o2")).read_text(encoding="utf-8")
    con.close()


# ---------------------------------------------------------------- 2026-10-30: a fact-only change inherits the bar
def _sibling(con, pv, base, *, passed, reason="catch_rate=0.83 (bar 0.7)", run_id="r-old"):
    store.record_jev_prompt_base(con, pv, base)
    store.insert_jev_eval(con, run_id=run_id, prompt_version=pv, family="required", n=131,
                          metrics={"catch_rate": 0.83}, passed=passed, reason=reason)


def test_fact_only_change_inherits_the_bar_without_a_gold_run(tmp_path, monkeypatch):
    from backend.finder import jev
    con, calls = _bar_fixture(tmp_path, monkeypatch, RunSummary())
    monkeypatch.setattr(jev, "prompt_base", lambda endpoint: "base-1")
    _sibling(con, "pv-old", "base-1", passed=True)
    logs = []
    res = jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=logs.append)
    assert res["passed"] and res["inherited_from"] == "pv-old"
    assert calls == []                                          # no gold set, no eval
    assert con.execute("SELECT required_passed FROM vw_jev_bar WHERE prompt_version = 'pv-new'").fetchone()[0]
    assert any("no gold run" in l for l in logs)
    # measured now: the next run does nothing
    assert jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=logs.append) is None
    # and the older version's demotions are re-reviewed at once (the bar under pv-new has passed)
    wanted = {pid: t for pid, t in jev_gate.review_subset_ids(con).items() if pid != "exempt-human"}
    assert "demote-fails" not in jev_gate.reusable_ids(con, wanted, "pv-new")
    con.close()


def test_inherited_bar_names_the_version_the_gold_set_was_measured_at(tmp_path, monkeypatch):
    from backend.finder import jev
    con, calls = _bar_fixture(tmp_path, monkeypatch, RunSummary())
    monkeypatch.setattr(jev, "prompt_base", lambda endpoint: "base-1")
    _sibling(con, "pv-measured", "base-1", passed=True, run_id="r1")
    assert jev_gate.inherit_required_bar(con, "pv-measured", log=lambda *_: None) is None   # never from itself
    store.record_jev_prompt_base(con, "pv-mid", "base-1")
    jev_gate.inherit_required_bar(con, "pv-mid", log=lambda *_: None)
    jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=lambda *_: None)
    reason = con.execute("SELECT reason FROM vw_jev_eval_latest WHERE prompt_version = 'pv-new'").fetchone()[0]
    assert "measured at pv-measured" in reason and calls == []
    con.close()


@pytest.mark.parametrize("sibling_base, passed, reason", [
    ("base-2", True, "catch_rate=0.83 (bar 0.7)"),              # the questions/model changed: different base
    ("base-1", False, "catch_rate=0.5 (bar 0.7)"),               # the sibling's bar failed
    ("base-1", False, "insufficient data: unjudged=131 of 131"),  # never measured
])
def test_bar_is_measured_when_it_cannot_be_inherited(tmp_path, monkeypatch, sibling_base, passed, reason):
    from backend.finder import jev
    con, calls = _bar_fixture(tmp_path, monkeypatch, RunSummary())
    monkeypatch.setattr(jev, "prompt_base", lambda endpoint: "base-1")
    _sibling(con, "pv-old", sibling_base, passed=passed, reason=reason)
    jev_gate.ensure_required_bar(con, facts=[], live_ok=True, log=lambda *_: None)
    assert calls == [("run", ["gold"]), ("eval", "pv-new")]
    con.close()


def test_prompt_base_ignores_the_facts_only(monkeypatch):
    from backend.finder import jev
    from backend.finder.jev import parse_facts
    a = parse_facts("## Summary\nfact one\n")
    b = parse_facts("## Summary\nfact one\nfact two\n")
    assert jev.prompt_version(a, "typesafe") != jev.prompt_version(b, "typesafe")
    assert jev.prompt_base("typesafe") != jev.prompt_base("vercel")
