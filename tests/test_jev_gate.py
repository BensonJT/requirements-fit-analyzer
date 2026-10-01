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
from backend.finder.jev_types import LineRecord, ReviewRecord  # noqa: E402

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


def _line(n, verdict, text, section="required"):
    return LineRecord(line_no=n, section=section, line_text=text, kind="skill", kind_conf=0.9, verdict=verdict,
                      verdict_raw=verdict, verdict_probs={"met": 0.1, "adjacent": 0.1, "unmet": 0.7, "unclear": 0.1},
                      verdict_conf=0.7, evidence_fact_id=None, evidence_p=None, evidence_text=None,
                      evidence_downgraded=False)


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
    assert f"**Jev demotion pass:** active under prompt_version {PV}; 3 row(s) demoted" in text
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
    logs = []
    assert jev_gate.run_over_top(con, facts=[], live_ok=True, log=logs.append) is None
    assert set(sent) == {"keep-meets", "demote-fails", "demote-all-wrong", "keep-unevaluated-pv", "keep-no-review",
                         "review-demote"}
    assert "exempt-human" not in sent
    assert any("7 Top Jobs row(s) (6 apply / 1 review), 1 adjudicated exempt, 6 with a JD" in l for l in logs)
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
