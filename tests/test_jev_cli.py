"""WP4 of the Jev tier (docs/JEV_PLAN.md §5): `finder.py jev ...`, `pipeline.jev_stage`, the J3 report cell
and the launch.sh wiring.

Nothing here makes a network call or opens the live DB: CLI subprocesses get a tmp `--db` AND a tmp
`JOBSEARCH_DB`, the key env vars are stripped, live paths use a fake transport, and launch.sh is exercised
only through its menu listing (answered "q") and functions extracted from its text.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from backend.ats import store  # noqa: E402
from backend.finder import jev, jev_cli, jev_eval, judge2, pipeline, report  # noqa: E402
from backend.finder import jev_questions as Q  # noqa: E402
from backend.finder.jev_types import LineRecord, ReviewRecord  # noqa: E402

FINDER = os.path.join(ROOT, "finder.py")
NOW = datetime(2026, 9, 28, 12, 0)
REVIEWED_AT = "2026-09-28T12:00:00+00:00"
KEY_VARS = ("TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY", "JEV_LIVE_OK", "JEV_ENDPOINT", "JEV_STAGE_ENABLED",
            "JUDGE2_BACKGROUND_FILE")

JD = """About the role.

Required:
5+ years of experience in process improvement and operations management.
Experience building reports in a SQL database.

Responsibilities:
Lead cross-functional process redesign initiatives.
"""

FACT_SHEET = """# Experience
- Redesigned an intake workflow shared by three departments.
- Built SQL reporting pipelines for a regional operations team.
"""


# ---------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def facts_file(tmp_path):
    path = tmp_path / "facts.md"
    path.write_text(FACT_SHEET, encoding="utf-8")
    return str(path)


def _posting(con, pid, *, dh="h", text=JD, title="Process Analyst", status="active"):
    con.execute(
        "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, status, description_text, "
        "description_hash, first_seen_at, last_seen_at) VALUES (?, 'Acme', 'greenhouse', ?, ?, ?, ?, ?, ?, ?, ?)",
        [pid, pid, title, f"https://x/{pid}", status, text, dh, NOW, NOW])


def _gold(con, pid, human_fit="meets"):
    _posting(con, pid)
    con.execute(
        "INSERT INTO report_feedback (posting_id, description_hash, verdict, basis, assessor, required_fit, "
        "assessed_at, loaded_at) VALUES (?, 'h', 'build', 'blind', 'user', ?, ?, ?)", [pid, human_fit, NOW, NOW])


def _gold_db(tmp_path, n=2, name="t.duckdb"):
    path = str(tmp_path / name)
    con = store.connect(path)
    for i in range(n):
        _gold(con, f"G{i:02d}")
    return con, path


def _line(no, *, verdict="met", section="required"):
    return LineRecord(
        line_no=no, section=section, line_text=f"Synthetic requirement line number {no}.", kind="skill",
        kind_conf=0.9, verdict=verdict, verdict_raw=verdict,
        verdict_probs={"met": 0.7, "adjacent": 0.1, "unmet": 0.1, "unclear": 0.1}, verdict_conf=0.7,
        evidence_fact_id="f01" if verdict in ("met", "adjacent") else None, evidence_p=0.8,
        evidence_text="a synthetic fact" if verdict in ("met", "adjacent") else None, evidence_downgraded=False)


def _review(pid, *, dh="h", pv="pv1", tag=None, required_fit="meets", grade="adjacent", injection_p=0.02,
            lines=None, drift=False):
    probs = {g: (0.7 if g == grade else 0.1) for g in Q.GRADE_LEVELS}
    fields = {}
    for lens in jev.LENS_NAMES:
        fields.update({f"lens_{lens}_score": float(Q.GRADE_LEVELS.index(grade)), f"lens_{lens}_grade": grade,
                       f"lens_{lens}_conf": 0.7, f"lens_{lens}_probs": dict(probs)})
    return ReviewRecord(
        posting_id=pid, description_hash=dh, prompt_version=jev_eval.tagged_pv(pv, tag), run_tag=tag,
        endpoint="typesafe", model_requested=Q.PINNED_MODEL,
        model_answered="jev-9.9.9" if drift else Q.PINNED_MODEL, version_drift=drift, input_tokens=100, **fields,
        gates={"canary_ai_directed": injection_p}, injection_p=injection_p, required_fit=required_fit,
        derive_why="stored", lines_fit=required_fit, shape_fit=None, shape_score=None, raw_response={},
        reviewed_at=REVIEWED_AT, lines=lines if lines is not None else [])


class _Response:
    def __init__(self, body):
        self.status_code = 200
        self.headers = {}
        self.text = ""
        self._body = body

    def json(self):
        return self._body


def _answer(qid, question):
    if question["type"] == "noul":
        return {"type": "noul", "noul": 0.1}
    if question["type"] == "score":
        n = len(question["criteria"])
        return {"type": "score", "score": float(n - 1), "confidence": 0.8,
                "probabilities": {str(i): (1.0 if i == n - 1 else 0.0) for i in range(n)}}
    options = list(question["criteria"])
    choice = "met" if qid.startswith("verdict_") else options[0]
    return {"type": "choice", "choice": choice, "confidence": 0.8,
            "probabilities": {o: (0.9 if o == choice else 0.1 / (len(options) - 1)) for o in options}}


class FakeJev:
    """Answers every question in a request; counts calls. Never touches the network."""

    def __init__(self):
        self.calls = 0

    def __call__(self, url, headers=None, json=None):
        self.calls += 1
        answers = {qid: _answer(qid, q) for qid, q in json["questions"].items()}
        return _Response({"model": Q.PINNED_MODEL, "answers": answers, "usage": {"input_tokens": 500}})


def _ns(**kw):
    base = dict(eval_set=False, gated=False, injection=False, sentinel=False, limit=None, dry_run=False, show=1,
                force=False, run_tag=None, i_have_approval=False, background_file=None, judge2_pv=None,
                tags="r2,r3", sentinel_tag=None, no_write=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _cli(args, tmp_path, db, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    # finder.py load_dotenv()s the repo .env, which may hold a REAL key; dotenv never overrides a variable that
    # is already set, so an explicit empty value keeps every subprocess here key-less (no live call, ever).
    env.update({k: "" for k in KEY_VARS})
    env["JOBSEARCH_DB"] = str(tmp_path / "default_fallback.duckdb")   # never the live DB
    env.update(env_extra or {})
    return subprocess.run([sys.executable, FINDER, "jev"] + args + ["--db", db], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120, env=env)


# ---------------------------------------------------------------- CLI (subprocess)
def test_cli_dry_run_needs_no_key_and_prints_the_payload(tmp_path, facts_file):
    con, db = _gold_db(tmp_path)
    con.close()
    res = _cli(["run", "--eval-set", "--dry-run", "--show", "1", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "jev dry-run role request: G00" in res.stdout
    assert "jev dry-run role request: G01" not in res.stdout          # --show 1
    assert "to_send=2" in res.stdout and "RunSummary(considered=2" in res.stdout
    assert "Redesigned an intake workflow" in res.stdout              # the lines request carries the facts
    assert not os.path.exists(tmp_path / "default_fallback.duckdb")    # --db was honoured


def test_cli_live_run_refuses_without_approval_or_key(tmp_path, facts_file):
    con, db = _gold_db(tmp_path)
    con.close()
    res = _cli(["run", "--eval-set", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 1 and "refused" in res.stdout and "--i-have-approval" in res.stdout
    res = _cli(["run", "--eval-set", "--background-file", facts_file, "--i-have-approval"], tmp_path, db)
    assert res.returncode == 1 and "TYPESAFE_API_KEY is not set" in res.stdout
    res = _cli(["run", "--eval-set", "--background-file", facts_file], tmp_path, db, {"JEV_LIVE_OK": "1"})
    assert res.returncode == 1 and "TYPESAFE_API_KEY is not set" in res.stdout   # env approval, still no key


def test_cli_population_flags_are_validated(tmp_path, facts_file):
    con, db = _gold_db(tmp_path)
    con.close()
    res = _cli(["run", "--dry-run", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 1 and "exactly one of" in res.stdout
    res = _cli(["run", "--eval-set", "--gated", "--dry-run"], tmp_path, db)
    assert res.returncode == 2 and "not allowed with" in res.stderr               # argparse exclusivity
    res = _cli(["run", "--eval-set", "--limit", "3", "--dry-run", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 1 and "--limit applies to --gated only" in res.stdout
    res = _cli(["run", "--sentinel", "--dry-run", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 1 and "needs --run-tag" in res.stdout
    res = _cli(["run", "--injection", "--run-tag", "r2", "--dry-run", "--background-file", facts_file],
               tmp_path, db)
    assert res.returncode == 1 and "'inj'" in res.stdout


def test_cli_missing_fact_sheet_is_a_clear_error(tmp_path):
    con, db = _gold_db(tmp_path)
    con.close()
    res = _cli(["run", "--eval-set", "--dry-run", "--background-file", str(tmp_path / "nope.md")], tmp_path, db)
    assert res.returncode == 1 and "cannot read the fact sheet" in res.stdout


def test_cli_sentinel_init_pins_once_and_run_reads_it(tmp_path, facts_file):
    con, db = _gold_db(tmp_path, n=3)
    con.close()
    pinned = tmp_path / "jev_sentinel_ids.txt"
    res = _cli(["run", "--sentinel", "--run-tag", "s1", "--dry-run", "--background-file", facts_file],
               tmp_path, db)
    assert res.returncode == 1 and "sentinel --init" in res.stdout                # no pinned set yet

    res = _cli(["sentinel", "--init", "--n", "2"], tmp_path, db)
    assert res.returncode == 0, res.stdout + res.stderr
    assert pinned.read_text().split() == ["G00", "G01"]
    res = _cli(["sentinel", "--init"], tmp_path, db)
    assert res.returncode == 1 and "--force" in res.stdout and pinned.read_text().split() == ["G00", "G01"]
    res = _cli(["sentinel", "--init", "--n", "3", "--force"], tmp_path, db)
    assert res.returncode == 0 and pinned.read_text().split() == ["G00", "G01", "G02"]

    pinned.write_text("G02\nGONE\nG00\n")
    res = _cli(["run", "--sentinel", "--run-tag", "s1", "--dry-run", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "1 pinned sentinel id(s) no longer present, skipped: GONE" in res.stdout
    assert "considered=2" in res.stdout and "prompt_version=" in res.stdout and ":s1" in res.stdout


def test_cli_status_eval_and_rederive_run_on_an_empty_db(tmp_path, facts_file):
    con, db = _gold_db(tmp_path, n=0)
    con.close()
    res = _cli(["status", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 0 and "no Jev reviews stored" in res.stdout and "tokens today 0" in res.stdout
    res = _cli(["eval", "--background-file", facts_file], tmp_path, db)
    assert res.returncode == 0 and "nothing evaluated" in res.stdout
    res = _cli(["rederive"], tmp_path, db)
    assert res.returncode == 0 and "0 review(s) recomputed" in res.stdout


def test_sentinel_file_is_gitignored():
    rel = os.path.join("db", jev_cli.SENTINEL_FILE_NAME)
    assert jev_cli.sentinel_path(None) == jev_cli.sentinel_path(store.DEFAULT_DB_PATH)
    assert str(jev_cli.sentinel_path(None)).endswith(rel)
    if shutil.which("git") and os.path.exists(os.path.join(ROOT, ".git")):
        assert subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", rel]).returncode == 0


# ---------------------------------------------------------------- CLI helpers (in process)
def test_run_cmd_live_with_approval_stores_reviews(tmp_path, facts_file, monkeypatch):
    con, db = _gold_db(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-real")
    fake = FakeJev()
    monkeypatch.setattr(jev, "default_transport", lambda: fake)
    summary = jev_cli.run_cmd(con, _ns(eval_set=True, i_have_approval=True, background_file=facts_file), db,
                              log=lambda *_a: None)
    assert (summary.reviewed, summary.errors, fake.calls) == (2, 0, 4)
    assert con.execute("SELECT count(*) FROM jev_reviews WHERE run_tag = ''").fetchone()[0] == 2

    # JEV_LIVE_OK=1 is the other half of the gate; a tagged rerun is stored beside the canonical rows
    monkeypatch.setenv("JEV_LIVE_OK", "1")
    summary = jev_cli.run_cmd(con, _ns(eval_set=True, run_tag="r2", background_file=facts_file), db,
                              log=lambda *_a: None)
    assert summary.reviewed == 2
    assert con.execute("SELECT count(*) FROM jev_reviews WHERE run_tag = 'r2'").fetchone()[0] == 2

    out = []
    result = jev_cli.status(con, _ns(background_file=facts_file), log=out.append)
    assert result["tokens_today"] >= 0 and len(result["rows"]) == 2
    assert not any("Acme" in line or "Process Analyst" in line for line in out)   # ids only, never titles

    # eval: a fresh run id per call, rows written unless --no-write
    results = jev_cli.eval_cmd(con, _ns(background_file=facts_file, no_write=True), db, log=lambda *_a: None)
    assert "required" in results and "repeatability" in results
    assert con.execute("SELECT count(*) FROM jev_evals").fetchone()[0] == 0
    jev_cli.eval_cmd(con, _ns(background_file=facts_file), db, log=lambda *_a: None)
    run_ids = {r[0] for r in con.execute("SELECT run_id FROM jev_evals").fetchall()}
    assert len(run_ids) == 1 and next(iter(run_ids)).startswith("jev-")
    con.close()


def test_run_cmd_live_refusal_raises_before_any_transport(tmp_path, facts_file, monkeypatch):
    con, db = _gold_db(tmp_path)
    monkeypatch.setattr(jev, "default_transport", lambda: pytest.fail("no transport may be built"))
    with pytest.raises(jev_cli.JevCliError, match="refused"):
        jev_cli.run_cmd(con, _ns(eval_set=True, background_file=facts_file), db)
    monkeypatch.setenv("JEV_LIVE_OK", "yes")                       # only exactly "1" approves
    with pytest.raises(jev_cli.JevCliError, match="refused"):
        jev_cli.run_cmd(con, _ns(eval_set=True, background_file=facts_file), db)
    con.close()


def test_background_path_precedence(monkeypatch):
    assert jev_cli.background_path("/x/explicit.md") == "/x/explicit.md"
    monkeypatch.setenv("JUDGE2_BACKGROUND_FILE", "/x/env.md")
    assert jev_cli.background_path() == "/x/env.md"
    monkeypatch.delenv("JUDGE2_BACKGROUND_FILE")
    assert jev_cli.background_path() == str(jev_cli.DEFAULT_BACKGROUND)


def test_rederive_recomputes_from_stored_lines_only(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "P1")
    lines = [_line(0), _line(1, verdict="unmet"), _line(2, section="responsibility")]
    store.insert_jev_review(con, _review("P1", required_fit="garbage", lines=lines))
    store.insert_jev_review(con, _review("P1", tag="r2", required_fit="garbage", lines=lines))
    store.insert_jev_review(con, _review("P1#inj0", dh="h#inj0", tag="inj", required_fit="garbage", lines=lines))
    out = jev_cli.rederive(con, log=lambda *_a: None)
    assert out == {"updated": 3, "changed": 3}

    fit, why = judge2.derive_required_fit(lines, title="Process Analyst")
    sfit, sscore, _ = judge2.shape_fit(lines)
    for pid, dh, tag in (("P1", "h", None), ("P1", "h", "r2"), ("P1#inj0", "h#inj0", "inj")):
        rec = store.load_jev_review(con, pid, dh, jev_eval.tagged_pv("pv1", tag), tag)
        assert (rec.required_fit, rec.derive_why, rec.shape_fit, rec.shape_score) == (fit, why, sfit, sscore)
        assert rec.lens_process_grade == "adjacent" and len(rec.lines) == 3          # answers untouched
    assert jev_cli.rederive(con, log=lambda *_a: None) == {"updated": 3, "changed": 0}   # idempotent
    con.close()


# ---------------------------------------------------------------- pipeline.jev_stage
def _gated_db(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    _posting(con, "P1")
    con.execute("INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
                "final_score, band, fit_process, fit_technical, fit_ai) "
                "VALUES ('P1', 'rv', 'mv', ?, 'review', 60, 70, 'strong', 0.9, 0.2, 0.1)", [NOW])
    return con


def test_jev_stage_is_off_by_default(tmp_path, monkeypatch, facts_file):
    con = _gated_db(tmp_path)
    fake = FakeJev()
    logs = []
    assert pipeline.jev_stage(con, transport=fake, log=logs.append) is None
    assert fake.calls == 0 and logs == []
    monkeypatch.setenv("JEV_STAGE_ENABLED", "1")                      # enabled, but no live approval
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-real")
    assert pipeline.jev_stage(con, transport=fake, log=logs.append) is None
    assert fake.calls == 0 and "JEV_LIVE_OK" in logs[0]
    con.close()


def test_jev_stage_runs_on_the_gated_population_when_enabled(tmp_path, monkeypatch, facts_file):
    con = _gated_db(tmp_path)
    for var, value in (("JEV_STAGE_ENABLED", "1"), ("JEV_LIVE_OK", "1"), ("TYPESAFE_API_KEY", "test-key-not-real"),
                       ("JUDGE2_BACKGROUND_FILE", facts_file)):
        monkeypatch.setenv(var, value)
    fake = FakeJev()
    logs = []
    summary = pipeline.jev_stage(con, transport=fake, sleep_fn=lambda _s: None, log=logs.append)
    assert summary.reviewed == 1 and fake.calls == 2
    assert any(line.startswith("Jev stage: 1 reviewed") for line in logs)
    assert con.execute("SELECT posting_id FROM jev_reviews").fetchall() == [("P1",)]
    con.close()


def test_jev_stage_swallows_and_logs_a_failure(tmp_path, monkeypatch, facts_file):
    con = _gated_db(tmp_path)
    for var, value in (("JEV_STAGE_ENABLED", "1"), ("JEV_LIVE_OK", "1"), ("JUDGE2_BACKGROUND_FILE", facts_file)):
        monkeypatch.setenv(var, value)

    def boom(*_a, **_kw):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(jev, "gated_postings", boom)
    logs = []
    assert pipeline.jev_stage(con, transport=FakeJev(), log=logs.append) is None
    assert logs == ["Jev: failed (RuntimeError: synthetic failure); continuing without it"]
    con.close()


def test_daily_calls_jev_stage_before_judge2(monkeypatch):
    order = []
    monkeypatch.setattr(pipeline, "screen", lambda con, **kw: {"screened": 0})
    monkeypatch.setattr(pipeline, "coverage_stage", lambda con, since=None, log=None: {})
    monkeypatch.setattr(pipeline, "required_embed_stage", lambda con, log=None: {})
    monkeypatch.setattr(pipeline, "jev_stage", lambda con, log=None: order.append("jev"))
    monkeypatch.setattr(pipeline, "judge2_stage", lambda con, top_n=0, log=None: order.append("judge2"))
    monkeypatch.setattr(pipeline.report_mod, "snapshots", lambda con, out_dir=None: [])
    pipeline.daily(None, since=None, vault_dir=None, llm_top=5, use_model=False, report=False, log=lambda *_a: None)
    assert order == ["jev", "judge2"]


# ---------------------------------------------------------------- report: the J3 cell
@pytest.mark.parametrize("args, cell", [
    (("meets", "bullseye", "adjacent", "wrong", 0.02, False), "m B·A·W"),
    (("partial", "stretch", "stretch", "adjacent", None, None), "p S·S·A"),
    (("fails", "wrong", "wrong", "wrong", 0.02, True), "f W·W·W*"),
    ((None, "bullseye", "adjacent", "wrong", 0.02, False), "B·A·W"),          # no required call: blank
    (("meets", "bullseye", "adjacent", "wrong", Q.CANARY_FLAG_AT, False), "m B·A·W inj!"),
    (("meets", "bullseye", "adjacent", "wrong", 0.9, True), "m B·A·W inj!*"),
    (("meets", "bullseye", None, "wrong", 0.02, False), "m B·-·W"),
    ((None, None, None, None, None, False), "—"),                             # no Jev row at all
    ((None, None, None, None, None, None), "—"),
])
def test_j3_cell(args, cell):
    assert report._j3_cell(*args) == cell


def _judged(con, pid, *, grades=("bullseye", "bullseye", "bullseye"), required_fit="meets", score=80):
    seen = datetime(2026, 9, 16)
    con.execute(
        "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary, status, "
        "description_hash, first_seen_at, last_seen_at) VALUES (?, 'Acme', 'greenhouse', ?, 'Director, Ops', ?, "
        "'Remote - USA', 'active', 'h', ?, ?)", [pid, pid, f"https://x/{pid}", seen, seen])
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
        "final_score, band, level_fit) VALUES (?, 'rv', 'mv', ?, 'review', 70, ?, 'strong', 'in_range')",
        [pid, seen, score])
    con.execute(
        "INSERT INTO llm_labels (posting_id, description_hash, rubric_version, scorer, grade, grade_process, "
        "grade_technical, grade_ai, required_fit, judged_at) VALUES (?, 'h', 'rv1', 'claude-sonnet-batch', "
        "'bullseye', ?, ?, ?, ?, ?)", [pid, *grades, required_fit, seen])


def _unjudged(con, pid, *, fp, fr, score=95):
    seen = datetime(2026, 9, 26)
    con.execute(
        "INSERT INTO postings (posting_id, employer, platform, req_id, title, url, location_primary, status, "
        "description_hash, first_seen_at, last_seen_at) VALUES (?, 'Acme', 'greenhouse', ?, 'Process Lead', ?, "
        "'Remote - USA', 'active', 'h', ?, ?)", [pid, pid, f"https://x/{pid}", seen, seen])
    con.execute(
        "INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, rule_score, "
        "final_score, band, level_fit, fit_process, fit_required) "
        "VALUES (?, 'rv', 'mv', ?, 'review', 70, ?, 'very_strong', 'in_range', ?, ?)", [pid, seen, score, fp, fr])


def _report_fixture(con):
    _judged(con, "a" * 20)
    _judged(con, "b" * 20, grades=("bullseye", "wrong", "wrong"), score=75)
    _judged(con, "c" * 20, grades=("adjacent", "wrong", "wrong"), required_fit="arguable", score=90)
    _judged(con, "d" * 20, grades=("adjacent", "adjacent", "wrong"), score=60)
    _unjudged(con, "e" * 20, fp=0.90, fr=0.80)
    _unjudged(con, "f" * 20, fp=0.85, fr=0.50, score=70)


def _add_extreme_jev(con):
    """Jev disagrees with everything, the canary fires, and its bar has passed -- the rank must not care."""
    pids = [r[0] for r in con.execute("SELECT posting_id FROM postings ORDER BY posting_id").fetchall()]
    for i, pid in enumerate(pids):
        store.insert_jev_review(con, _review(pid, required_fit=("fails", "meets", "partial")[i % 3],
                                             grade=("wrong", "bullseye")[i % 2], injection_p=0.95))
    for family in ("required", "lens", "repeatability"):
        store.insert_jev_eval(con, run_id="r1", prompt_version="pv1", family=family, n=50, metrics={},
                              passed=True, reason="test")


def _table_rows(text, heading):
    """(first cell, J3-less cells) of every data row in the table under `heading`."""
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("| ") and not ln.startswith("|---")]
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    j3 = header.index("J3")
    return [[c.strip() for i, c in enumerate(r.strip("|").split("|")) if i != j3] for r in rows[1:]], \
        [r.strip("|").split("|")[j3].strip() for r in rows[1:]]


def test_report_row_order_is_identical_with_and_without_jev_rows(tmp_path):
    """The v23 guarantee, narrowed by stage 2 (2026-09-30, tests/test_jev_gate.py): the RANK never reads Jev,
    and the page written with `jev_gate_on=False` is row-for-row the same with or without Jev rows. The
    default page now carries the demotion pass; that behaviour is tested in test_jev_gate.py."""
    con = store.connect(str(tmp_path / "t.duckdb"))
    _report_fixture(con)
    top_before = report.write_top_jobs(con, None, out_path=str(tmp_path / "before"),
                                       jev_gate_on=False).read_text(encoding="utf-8")
    lens_before = report.write_lens_lists(con, None, out_path=str(tmp_path / "before")).read_text(encoding="utf-8")
    rank_before = con.execute("SELECT posting_id, rank_score, rank_why FROM vw_lens_fit ORDER BY 1").fetchall()

    _add_extreme_jev(con)
    assert con.execute("SELECT count(*) FROM vw_lens_fit WHERE jev_bar_passed").fetchone()[0] == 6
    top_after = report.write_top_jobs(con, None, out_path=str(tmp_path / "after"),
                                      jev_gate_on=False).read_text(encoding="utf-8")
    lens_after = report.write_lens_lists(con, None, out_path=str(tmp_path / "after")).read_text(encoding="utf-8")
    assert con.execute("SELECT posting_id, rank_score, rank_why FROM vw_lens_fit ORDER BY 1").fetchall() == rank_before

    for heading in ("## Apply", "## Review"):
        before, j3_before = _table_rows(top_before, heading)
        after, j3_after = _table_rows(top_after, heading)
        assert before and after == before                     # every non-J3 cell, row for row
        assert set(j3_before) == {"—"} and all(c.endswith("inj!*") for c in j3_after)
    both_before, _ = _table_rows(lens_before, "## Strong on BOTH")
    both_after, j3_lens = _table_rows(lens_after, "## Strong on BOTH")
    assert both_before and both_after == both_before and all(c != "—" for c in j3_lens)
    assert "**J3**" in top_after and "**J3**" in lens_after
    con.close()


# ---------------------------------------------------------------- launch.sh
def test_launch_menu_lists_the_jev_preset(tmp_path):
    """Only the menu listing runs: the choice 'q' exits before pre-flight, so nothing reaches the network. The
    script runs from a tmp copy, so its `mkdir logs` never touches the repo."""
    shutil.copy(os.path.join(ROOT, "launch.sh"), tmp_path / "launch.sh")
    res = subprocess.run(["bash", str(tmp_path / "launch.sh")], input="q\n", capture_output=True, text=True,
                         timeout=60, cwd=tmp_path)
    assert res.returncode == 0 and "Nothing launched." in res.stdout
    assert "Jev gold score (MSA)" in res.stdout
    text = open(os.path.join(ROOT, "launch.sh"), encoding="utf-8").read()
    presets = re.search(r"^PRESETS=\((.*?)^\)", text, re.S | re.M).group(1)
    assert '"jevrun|jevrep|jevinj|jeveval|maint"' in presets
    labels = re.search(r"^LABELS=\((.*?)^\)", text, re.S | re.M).group(1)
    estimates = re.search(r"^ESTIMATES=\((.*)\)$", text, re.M).group(1)
    n_presets = len(re.findall(r'^\s+"', presets, re.M))
    assert n_presets == len(re.findall(r'^\s+"', labels, re.M)) == len(re.findall(r'"[^"]*"', estimates))
    # the report-last rule: every preset that writes Top_Jobs ends with top, or top then maint
    for preset in re.findall(r'^\s+"([^"]*)"', presets, re.M):
        steps = preset.split("|")
        if "top" in steps:
            assert steps[-1] == "top" or steps[-2:] == ["top", "maint"], preset


def _launch_functions(*names):
    text = open(os.path.join(ROOT, "launch.sh"), encoding="utf-8").read()
    return "\n".join(re.search(rf"^{name}\(\) {{.*?^}}", text, re.S | re.M).group(0) for name in names)


def test_launch_jev_steps_and_env(tmp_path):
    script = ('PY=.venv/bin/python; JUDGE_BG=/tmp/facts.md\n' + _launch_functions("step_cmd", "jev_env")
              + '\nfor s in jevrun jevrep:r2 jevrep:r3 jevinj jeveval; do step_cmd "$s"; done\n'
              + 'jev_env; echo "LIVE=$JEV_LIVE_OK BG=$JUDGE2_BACKGROUND_FILE"\n')
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                         env={"PATH": os.environ["PATH"]})
    lines = res.stdout.splitlines()
    assert lines[0] == (".venv/bin/python -u finder.py jev run --eval-set --background-file /tmp/facts.md "
                        "--i-have-approval")
    assert "--run-tag r2" in lines[1] and "--run-tag r3" in lines[2] and "--i-have-approval" in lines[2]
    assert "jev run --injection" in lines[3] and "--i-have-approval" in lines[3]
    assert lines[4] == ".venv/bin/python -u finder.py jev eval --background-file /tmp/facts.md"
    assert lines[5] == "LIVE=1 BG=/tmp/facts.md"


def test_launch_jev_preflight_fails_fast_without_a_key(tmp_path):
    """No key: FAIL with a clear line and return before curl is ever invoked (a stub curl proves it)."""
    bg = tmp_path / "facts.md"
    bg.write_text("x")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "curl").write_text("#!/bin/bash\necho CURL_CALLED >&2\nexit 99\n")
    (stub / "curl").chmod(0o755)
    script = f'JUDGE_BG={bg}\n' + _launch_functions("jev_preflight") + '\njev_preflight; echo "rc=$?"\n'
    env = {"PATH": f"{stub}:{os.environ['PATH']}"}
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, env=env)
    assert "FAIL  TYPESAFE_API_KEY is not set" in res.stdout and "rc=1" in res.stdout
    assert "CURL_CALLED" not in res.stderr

    # with a key, the probe goes to curl with the key on stdin only (never in argv), and non-200 fails
    seen = tmp_path / "curl_seen.txt"                  # the probe discards curl's stderr, so log to a file
    (stub / "curl").write_text(f'#!/bin/bash\necho "ARGS $*" >> {seen}\ncat >> {seen}\nprintf 401\n')
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                         env=dict(env, TYPESAFE_API_KEY="sk-test-not-real"))
    assert "rc=1" in res.stdout and "HTTP 401" in res.stdout
    logged = seen.read_text().splitlines()
    args_line = next(ln for ln in logged if ln.startswith("ARGS"))
    assert "sk-test-not-real" not in args_line and "https://api.typesafe.ai/v1/models" in args_line
    assert "Authorization: Bearer sk-test-not-real" in logged                  # via stdin only
    assert "sk-test-not-real" not in res.stdout + res.stderr

    (stub / "curl").write_text("#!/bin/bash\ncat >/dev/null\nprintf 200\n")
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                         env=dict(env, TYPESAFE_API_KEY="sk-test-not-real"))
    assert "rc=0" in res.stdout and "GET /v1/models 200" in res.stdout


def test_lens_set_population_is_blind_human_lens_graded_postings(tmp_path):
    from backend.ats import store
    from backend.finder import jev
    con = store.connect(str(tmp_path / "t.duckdb"))
    for pid in ("p1", "p2"):
        _posting(con, pid)
    con.execute("INSERT INTO human_lens_grades VALUES ('p1', 'h', 'ai', 'bullseye', 'blind', NULL, NULL, 'f.csv', now())")
    con.execute("INSERT INTO human_lens_grades VALUES ('p2', 'h', 'process', 'stretch', 'seen', NULL, NULL, 'f.csv', now())")
    assert [p.posting_id for p in jev.lens_set_postings(con)] == ["p1"]
    con.close()
