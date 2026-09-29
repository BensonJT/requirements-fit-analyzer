"""Tests for the Jev typed-decision tier client (backend/finder/jev.py, docs/JEV_PLAN.md).

NO test here makes a network call or opens the live database: `transport` is always a fake callable, `sleep_fn`
a recording stub, persistence a fake object, and databases are tmp_path files. Response shapes copy the real
jev-1.13.0 smoke fixture (tests/fixtures/jev/smoke_response.json). All fact-sheet text is synthetic.
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
from backend.finder import feedback, jev, judge2  # noqa: E402
from backend.finder import jev_questions as Q  # noqa: E402
from backend.finder.jev_types import Caps, LineRecord, Posting  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "jev", "smoke_response.json")

JD = """About the role.

Required:
5+ years of experience in process improvement and operations management.
Active TS/SCI clearance required.
Bachelor's degree or equivalent experience.

Preferred:
Lean Six Sigma Black Belt certification.

Responsibilities:
Lead cross-functional process redesign initiatives across the organization.
Build dashboards that track operational throughput for leadership.
"""

FACT_SHEET = """# Experience
- Redesigned an intake workflow shared by three departments.
- Built SQL reporting pipelines for a regional operations team.

---
# Education
* Bachelor's degree in industrial engineering.
**Note:** wrote the measurement plan for a pilot.
"""


def _posting(pid="P1", jd=JD, title="Process Manager"):
    return Posting(posting_id=pid, description_hash=f"h-{pid}", title=title, employer="Acme Corp",
                   description_text=jd)


def _facts():
    return jev.parse_facts(FACT_SHEET)


# ---------------------------------------------------------------- fake Jev responses (smoke-fixture shape)
def _choice_ans(choice, options, p=0.9, conf=0.8):
    rest = [o for o in options if o != choice]
    probs = {o: round((1 - p) / len(rest), 4) if rest else 0.0 for o in rest}
    probs[choice] = p
    return {"type": "choice", "choice": choice, "confidence": conf, "probabilities": probs}


def _score_ans(score, conf=0.9):
    legend = {str(i): f"level {i}" for i in range(len(Q.GRADE_LEVELS))}
    probs = {str(i): 0.0 for i in range(len(Q.GRADE_LEVELS))}
    probs[str(min(max(int(round(score)), 0), 3))] = 1.0
    return {"type": "score", "score": score, "confidence": conf, "legend": legend, "probabilities": probs}


def role_response(*, scores=None, canary=0.02, model=Q.PINNED_MODEL, tokens=500):
    scores = scores or {"lens_process": 2.8, "lens_technical": 1.2, "lens_ai": 0.1}
    answers = {qid: _score_ans(scores[qid]) for qid in Q.LENS_QUESTIONS}
    for qid, q in Q.GATE_QUESTIONS.items():
        if q["type"] == "noul":
            answers[qid] = {"type": "noul", "noul": 0.1}
        else:
            answers[qid] = _choice_ans(list(q["criteria"])[0], list(q["criteria"]))
    answers["canary_ai_directed"] = {"type": "noul", "noul": canary}
    return {"model": model, "answers": answers, "usage": {"input_tokens": tokens, "output_tokens": 40}}


def lines_response(body, verdicts=None, *, model=Q.PINNED_MODEL, tokens=900):
    """Answers every line in a lines-request `body`. `verdicts` maps line index -> (kind, verdict, evidence)."""
    verdicts = verdicts or {}
    fact_ids = [f["id"] for f in body["state"]["facts"]]
    answers = {}
    for i, _line in enumerate(body["state"]["lines"]):
        kind, verdict, evidence = verdicts.get(i, ("skill", "met", fact_ids[0] if fact_ids else "none"))
        kid, vid, eid = Q.line_question_ids(i)
        answers[kid] = _choice_ans(kind, list(Q.KIND_CRITERIA))
        answers[vid] = _choice_ans(verdict, list(Q.VERDICT_CRITERIA))
        answers[eid] = _choice_ans(evidence, fact_ids + ["none"], p=0.7)
    return {"model": model, "answers": answers, "usage": {"input_tokens": tokens, "output_tokens": 60}}


class _FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def fake_transport(*, role=None, lines=None, calls=None):
    """Routes by request kind: a body with `posting` in its state is the role request, else lines."""
    def transport(url, headers=None, json=None):
        if calls is not None:
            calls.append((url, headers, json))
        assert headers["Authorization"].startswith("Bearer ")
        if "posting" in json["state"]:
            return _FakeResponse(200, role(json) if role else role_response())
        return _FakeResponse(200, lines(json) if lines else lines_response(json))
    return transport


class _ExplodingTransport:
    def __call__(self, *a, **kw):
        raise AssertionError("transport must never be called here")

    def post(self, *a, **kw):
        raise AssertionError("transport must never be called here")


class FakePersist:
    def __init__(self, reviewed=(), tokens_today=0):
        self.reviewed = set(reviewed)
        self.inserted = []
        self.tokens_today = tokens_today
        self.cache_queries = []

    def jev_already_reviewed(self, con, posting_id, description_hash, prompt_version, run_tag):
        self.cache_queries.append((posting_id, description_hash, prompt_version, run_tag))
        return posting_id in self.reviewed

    def insert_jev_review(self, con, record):
        self.inserted.append(record)

    def jev_tokens_since(self, con, since_iso):
        assert since_iso.endswith("T00:00:00+00:00")
        return self.tokens_today


def _run(rows, **kw):
    kw.setdefault("transport", fake_transport())
    kw.setdefault("sleep_fn", lambda s: None)
    kw.setdefault("live_ok", True)
    kw.setdefault("facts", _facts())
    kw.setdefault("endpoint", "typesafe")
    kw.setdefault("persist", FakePersist())
    kw.setdefault("log", lambda *a: None)
    return jev.run(None, rows, **kw)


@pytest.fixture
def key_env(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-test-key")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "fake-gateway-key")


# ---------------------------------------------------------------- 1. facts + settings
def test_parse_facts_ids_headings_and_markers():
    facts = _facts()
    assert [f.id for f in facts] == ["f01", "f02", "f03", "f04"]
    assert [f.heading for f in facts] == ["Experience", "Experience", "Education", "Education"]
    assert facts[0].text == "Redesigned an intake workflow shared by three departments."
    assert facts[2].text == "Bachelor's degree in industrial engineering."
    assert facts[3].text == "**Note:** wrote the measurement plan for a pilot."   # bold is not a bullet
    assert all(f.text != "---" for f in facts)


def test_parse_facts_skips_html_comments():
    text = "<!-- edit note: changed the tools list -->\n# Summary\n- Fact one\n<!-- multi\nline note -->\n- Fact two\n"
    facts = jev.parse_facts(text)
    assert [(f.id, f.heading, f.text) for f in facts] == [("f01", "Summary", "Fact one"), ("f02", "Summary", "Fact two")]


def test_parse_facts_widens_ids_past_99_and_caps_at_254():
    facts = jev.parse_facts("\n".join(f"- fact number {i}" for i in range(100)))
    assert facts[0].id == "f001" and facts[-1].id == "f100"
    assert len(jev.parse_facts("\n".join(f"- f {i}" for i in range(254)))) == 254
    with pytest.raises(ValueError):
        jev.parse_facts("\n".join(f"- f {i}" for i in range(255)))


def test_load_facts_reads_the_background_env_path(tmp_path, monkeypatch):
    p = tmp_path / "sheet.md"
    p.write_text(FACT_SHEET, encoding="utf-8")
    monkeypatch.setenv(judge2.BACKGROUND_ENV, str(p))
    assert jev.load_facts() == _facts()


def test_endpoint_model_and_key(monkeypatch):
    monkeypatch.delenv(Q.ENDPOINT_ENV, raising=False)
    assert jev.endpoint_from_env() == "typesafe"
    monkeypatch.setenv(Q.ENDPOINT_ENV, "vercel")
    assert jev.endpoint_from_env() == "vercel"
    monkeypatch.setenv(Q.ENDPOINT_ENV, "openai")
    with pytest.raises(ValueError):
        jev.endpoint_from_env()
    assert jev.model_for("typesafe") == Q.PINNED_MODEL
    assert jev.model_for("vercel") == Q.VERCEL_MODEL
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert jev.api_key("typesafe") == ""
    monkeypatch.setenv("TYPESAFE_API_KEY", "k1")
    assert jev.api_key("typesafe") == "k1"


def test_caps_from_env(monkeypatch):
    monkeypatch.delenv(Q.MAX_CALLS_ENV, raising=False)
    monkeypatch.delenv(Q.DAILY_CAP_ENV, raising=False)
    assert jev.caps_from_env() == Caps(Q.DEFAULT_MAX_CALLS_PER_RUN, Q.DEFAULT_DAILY_TOKEN_CAP)
    monkeypatch.setenv(Q.MAX_CALLS_ENV, "7")
    monkeypatch.setenv(Q.DAILY_CAP_ENV, "1000")
    assert jev.caps_from_env() == Caps(7, 1000)


# ---------------------------------------------------------------- 2. prompt version + requests
def test_prompt_version_is_stable_and_sensitive(monkeypatch):
    facts = _facts()
    pv = jev.prompt_version(facts, "typesafe")
    assert len(pv) == 12 and pv == jev.prompt_version(_facts(), "typesafe")
    assert jev.prompt_version(facts, "vercel") != pv
    assert jev.prompt_version(facts[:-1], "typesafe") != pv
    monkeypatch.setattr(Q, "READING_RULES", Q.READING_RULES + ["one more rule"])
    assert jev.prompt_version(facts, "typesafe") != pv
    monkeypatch.undo()
    monkeypatch.setattr(Q, "JD_CAP_CHARS", 5000)
    assert jev.prompt_version(facts, "typesafe") != pv
    monkeypatch.undo()
    monkeypatch.setattr(Q, "QUESTION_SET_VERSION", "changed")
    assert jev.prompt_version(facts, "typesafe") != pv


def test_role_request_shape_and_no_fact_text():
    body = jev.build_role_request(_posting(), Q.PINNED_MODEL)
    assert set(body) == {"state", "model", "questions"}
    assert set(body["state"]) == {"title", "employer", "posting"}
    assert body["model"] == Q.PINNED_MODEL
    assert body["questions"] == Q.ROLE_QUESTIONS
    dumped = json.dumps(body)
    for fact in _facts():
        assert fact.text not in dumped
    assert "reading_rules" not in body["state"] and "facts" not in body["state"]


def test_role_request_trims_the_jd():
    long_jd = "Intro paragraph. " * 2000 + "\n" + JD
    body = jev.build_role_request(_posting(jd=long_jd), Q.PINNED_MODEL)
    assert len(body["state"]["posting"]) <= Q.JD_CAP_CHARS + 10
    assert body["state"]["posting"] == judge2._trim_jd(long_jd, Q.JD_CAP_CHARS)


def test_select_lines_required_then_responsibility_never_preferred():
    lines = jev.select_lines(_posting(), log=lambda *a: None)
    assert [s for s, _ in lines] == ["required"] * 3 + ["responsibility"] * 2
    assert all("Six Sigma" not in t for _, t in lines)


def test_select_lines_caps_and_logs_drops(monkeypatch):
    monkeypatch.setattr(Q, "MAX_REQUIRED_LINES", 1)
    monkeypatch.setattr(Q, "MAX_RESPONSIBILITY_LINES", 1)
    logs = []
    lines = jev.select_lines(_posting(), log=logs.append)
    assert [s for s, _ in lines] == ["required", "responsibility"]
    assert any("dropped 2 required, 1 responsibility" in m for m in logs)


def test_lines_request_shape():
    facts = _facts()
    bodies = jev.build_lines_requests(_posting(), facts, Q.PINNED_MODEL, log=lambda *a: None)
    assert len(bodies) == 1
    body = bodies[0]
    state = body["state"]
    assert state["reading_rules"] == list(Q.READING_RULES)
    assert state["facts"] == [{"id": f.id, "heading": f.heading, "text": f.text} for f in facts]
    assert [l["id"] for l in state["lines"]] == ["L00", "L01", "L02", "L03", "L04"]
    assert state["lines"][3]["section"] == "responsibility"
    assert len(body["questions"]) == 3 * 5
    kid, vid, eid = Q.line_question_ids(3)
    assert body["questions"][kid] == Q.kind_question(3)
    assert body["questions"][vid] == Q.verdict_question(3, "responsibility")
    assert list(body["questions"][eid]["criteria"]) == [f.id for f in facts] + ["none"]


def test_lines_request_zero_lines_sends_nothing():
    posting = _posting(jd="We are a friendly company. Apply today.")
    assert jev.build_lines_requests(posting, _facts(), Q.PINNED_MODEL, log=lambda *a: None) == []


def test_lines_request_chunks_when_too_large(monkeypatch):
    facts = _facts()
    one = jev.build_lines_requests(_posting(), facts, Q.PINNED_MODEL, log=lambda *a: None)[0]
    single = jev._lines_body([("required", "x" * 80)], facts, Q.PINNED_MODEL)
    # room for roughly two lines per request
    monkeypatch.setattr(Q, "MAX_REQUEST_TOKENS_EST", jev.estimate_tokens(single) + 400)
    bodies = jev.build_lines_requests(_posting(), facts, Q.PINNED_MODEL, log=lambda *a: None)
    assert len(bodies) > 1
    for body in bodies:
        assert jev.estimate_tokens(body) <= Q.MAX_REQUEST_TOKENS_EST
        assert body["state"]["facts"] == one["state"]["facts"]
        assert body["state"]["reading_rules"] == one["state"]["reading_rules"]
        assert body["state"]["lines"][0]["id"] == "L00"
        assert set(body["questions"]) == {qid for i in range(len(body["state"]["lines"]))
                                          for qid in Q.line_question_ids(i)}
    flat = [item for chunk in jev.chunks_of(bodies) for item in chunk]
    assert flat == jev.select_lines(_posting(), log=lambda *a: None)


def test_lines_request_one_line_too_large_raises(monkeypatch):
    monkeypatch.setattr(Q, "MAX_REQUEST_TOKENS_EST", 10)
    with pytest.raises(ValueError):
        jev.build_lines_requests(_posting(), _facts(), Q.PINNED_MODEL, log=lambda *a: None)


# ---------------------------------------------------------------- 3. parsers
def test_smoke_fixture_shape_parses():
    resp = json.load(open(FIXTURE, encoding="utf-8"))["response"]
    dept = jev._choice(resp, "department", ["billing", "technical", "sales"])
    assert dept["choice"] == "technical" and dept["confidence"] == 0.72
    assert dept["probabilities"]["technical"] == 0.81
    assert jev._noul(resp, "is_urgent") == 0.99
    lens = jev._lens(resp, "frustration")   # string level keys, same as the lens Scores
    assert lens["score"] == 1.0 and lens["grade"] == "stretch"
    assert lens["probs"] == {"wrong": 0.0, "stretch": 1.0, "adjacent": 0.0}
    with pytest.raises(jev.JevResponseError):
        jev._choice(resp, "department", ["billing", "sales"])   # an option not asked for


def test_parse_role_response_fields():
    out = jev.parse_role_response(role_response(canary=0.61))
    assert out["lens_process_score"] == 2.8 and out["lens_process_grade"] == "bullseye"
    assert out["lens_technical_grade"] == "stretch" and out["lens_ai_grade"] == "wrong"
    assert out["lens_process_conf"] == 0.9
    assert out["lens_process_probs"] == {"wrong": 0.0, "stretch": 0.0, "adjacent": 0.0, "bullseye": 1.0}
    assert set(out["gates"]) == set(Q.GATE_QUESTIONS)
    assert out["gates"]["gate_clearance_active"] == 0.1
    assert set(out["gates"]["onsite_cadence"]) == {"choice", "confidence", "probabilities"}
    assert out["injection_p"] == 0.61


@pytest.mark.parametrize("score,grade", [(-0.3, "wrong"), (0.49, "wrong"), (0.5, "stretch"), (1.5, "adjacent"),
                                         (2.49, "adjacent"), (2.5, "bullseye"), (3.0, "bullseye"),
                                         (3.8, "bullseye")])
def test_score_to_grade_mapping_and_clamp(score, grade):
    assert jev._grade_for(score) == grade


def test_parse_role_response_missing_or_mistyped_answer_raises():
    resp = role_response()
    del resp["answers"]["gate_manufacturing"]
    with pytest.raises(jev.JevResponseError):
        jev.parse_role_response(resp)
    resp = role_response()
    resp["answers"]["lens_ai"] = {"type": "noul", "noul": 0.5}
    with pytest.raises(jev.JevResponseError):
        jev.parse_role_response(resp)
    resp = role_response()
    resp["answers"]["lens_ai"]["probabilities"] = {"7": 1.0}
    with pytest.raises(jev.JevResponseError):
        jev.parse_role_response(resp)
    with pytest.raises(jev.JevResponseError):
        jev.parse_role_response({"model": Q.PINNED_MODEL})


def _one_chunk_lines(verdicts):
    facts = _facts()
    bodies = jev.build_lines_requests(_posting(), facts, Q.PINNED_MODEL, log=lambda *a: None)
    resps = [lines_response(bodies[0], verdicts)]
    return jev.parse_lines_response(resps, jev.chunks_of(bodies), facts)


def test_evidence_resolved_from_fact_id():
    lines = _one_chunk_lines({0: ("skill", "met", "f02")})
    assert lines[0].verdict == "met" and lines[0].verdict_raw == "met"
    assert lines[0].evidence_fact_id == "f02"
    assert lines[0].evidence_text == "Built SQL reporting pipelines for a regional operations team."
    assert lines[0].evidence_p == 0.7 and lines[0].evidence_downgraded is False
    assert lines[0].line == lines[0].line_text and lines[0].evidence == lines[0].evidence_text
    assert [l.line_no for l in lines] == list(range(5))


def test_evidence_guard_downgrades_met_and_adjacent_without_evidence():
    lines = _one_chunk_lines({0: ("skill", "met", "none"), 2: ("degree", "adjacent", "none"),
                              3: ("skill", "unmet", "none")})
    assert (lines[0].verdict, lines[0].verdict_raw, lines[0].evidence_downgraded) == ("unclear", "met", True)
    assert (lines[2].verdict, lines[2].evidence_downgraded) == ("unclear", True)
    assert (lines[3].verdict, lines[3].evidence_downgraded) == ("unmet", False)
    assert lines[0].evidence_fact_id is None and lines[0].evidence_text is None


def test_adjacent_on_clearance_or_licence_is_unmet():
    lines = _one_chunk_lines({1: ("clearance", "adjacent", "f01"), 2: ("licence", "adjacent", "none")})
    assert (lines[1].verdict, lines[1].verdict_raw) == ("unmet", "adjacent")
    assert (lines[2].verdict, lines[2].evidence_downgraded) == ("unmet", False)


def test_unknown_evidence_id_raises():
    facts = _facts()
    bodies = jev.build_lines_requests(_posting(), facts, Q.PINNED_MODEL, log=lambda *a: None)
    resp = lines_response(bodies[0])
    resp["answers"]["evidence_L00"]["choice"] = "f99"
    with pytest.raises(jev.JevResponseError):
        jev.parse_lines_response([resp], jev.chunks_of(bodies), facts)
    with pytest.raises(jev.JevResponseError):
        jev.parse_lines_response([], jev.chunks_of(bodies), facts)   # response count mismatch


def test_line_numbers_are_global_across_chunks(monkeypatch):
    facts = _facts()
    single = jev._lines_body([("required", "x" * 80)], facts, Q.PINNED_MODEL)
    monkeypatch.setattr(Q, "MAX_REQUEST_TOKENS_EST", jev.estimate_tokens(single) + 400)
    bodies = jev.build_lines_requests(_posting(), facts, Q.PINNED_MODEL, log=lambda *a: None)
    lines = jev.parse_lines_response([lines_response(b) for b in bodies], jev.chunks_of(bodies), facts)
    assert [l.line_no for l in lines] == list(range(5))
    assert [l.line_text for l in lines] == [t for _, t in jev.select_lines(_posting(), log=lambda *a: None)]


def test_derive_via_judge2_matches_plain_dicts():
    lines = _one_chunk_lines({0: ("years_function", "met", "f01"), 1: ("clearance", "unmet", "none"),
                              2: ("degree", "met", "f03"), 3: ("skill", "met", "f01"),
                              4: ("skill", "adjacent", "f02")})
    dicts = [{"line": l.line_text, "section": l.section, "kind": l.kind, "verdict": l.verdict,
              "evidence": l.evidence_text, "years": None} for l in lines]
    title = "Process Manager"
    assert judge2.derive_required_fit(lines, title=title) == judge2.derive_required_fit(dicts, title=title)
    assert judge2.lines_fit(lines, title=title) == judge2.lines_fit(dicts, title=title)
    assert judge2.shape_fit(lines) == judge2.shape_fit(dicts)
    assert judge2.derive_required_fit(lines, title=title)[0] == "fails"   # the clearance hard gate


# ---------------------------------------------------------------- 4. client retries
def test_429_then_200_honors_retry_after():
    seq = [_FakeResponse(429, {"error": "slow down"}, {"Retry-After": "3"}), _FakeResponse(200, {"ok": 1})]
    sleeps = []
    meter = jev._Meter()
    out = jev.call_with_retry(lambda url, headers=None, json=None: seq.pop(0), sleeps.append, "u", "k",
                              {"x": 1}, log=lambda *a: None, meter=meter)
    assert out == {"ok": 1} and sleeps == [3.0] and meter.calls == 2


def test_529_without_retry_after_uses_backoff_and_cap_on_huge_retry_after():
    seq = [_FakeResponse(529, {}), _FakeResponse(429, {}, {"retry-after": "9999"}), _FakeResponse(200, {})]
    sleeps = []
    jev.call_with_retry(lambda url, headers=None, json=None: seq.pop(0), sleeps.append, "u", "k", {},
                        log=lambda *a: None)
    assert sleeps == [Q.BACKOFF_SECONDS[0], 60.0]


def test_exception_is_retried_then_gives_up():
    sleeps = []

    def boom(url, headers=None, json=None):
        raise ConnectionError("down")

    with pytest.raises(jev.JevAPIError) as err:
        jev.call_with_retry(boom, sleeps.append, "u", "k", {}, log=lambda *a: None)
    assert err.value.status is None
    assert sleeps == list(Q.BACKOFF_SECONDS)


def test_401_is_not_retried_and_carries_request_id():
    calls, sleeps = [], []

    def transport(url, headers=None, json=None):
        calls.append(1)
        return _FakeResponse(401, {"error": "bad key"}, {"x-typesafe-request-id": "req-123"})

    with pytest.raises(jev.JevAPIError) as err:
        jev.call_with_retry(transport, sleeps.append, "u", "secret-key", {}, log=lambda *a: None)
    assert len(calls) == 1 and sleeps == []
    assert err.value.status == 401 and err.value.request_id == "req-123"
    assert "bad key" in err.value.body_snippet
    assert "secret-key" not in str(err.value)


def test_key_goes_only_in_the_authorization_header(key_env):
    calls = []
    _run([_posting()], transport=fake_transport(calls=calls))
    for url, headers, body in calls:
        assert url == Q.ENDPOINTS["typesafe"]
        assert headers == {"Authorization": "Bearer fake-test-key", "Content-Type": "application/json"}
        assert "fake-test-key" not in json.dumps(body)


# ---------------------------------------------------------------- 5. run
def test_live_gate_refuses_without_approval(key_env):
    logs = []
    s = _run([_posting()], transport=_ExplodingTransport(), live_ok=False, log=logs.append)
    assert s.reviewed == 0 and s.calls == 0
    assert any("refused" in m for m in logs)


def test_live_gate_refuses_without_a_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    logs = []
    s = _run([_posting()], transport=_ExplodingTransport(), live_ok=True, log=logs.append)
    assert s.reviewed == 0 and any("TYPESAFE_API_KEY" in m for m in logs)


def test_dry_run_never_calls_transport_and_needs_no_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    logs = []
    s = _run([_posting("P1"), _posting("P2")], transport=_ExplodingTransport(), live_ok=False, dry_run=True,
             show=1, log=logs.append)
    assert s.dry_run is True and s.calls == 0 and s.reviewed == 0 and s.considered == 2
    text = "\n".join(logs)
    assert "jev dry-run role request: P1" in text and "jev dry-run lines request 1/1: P1" in text
    assert "dry-run role request: P2" not in text
    assert '"reading_rules"' in text and "to_send=2" in text


def test_live_run_records_a_review(key_env):
    persist = FakePersist()
    calls = []
    lines = lambda body: lines_response(body, {1: ("clearance", "unmet", "none")})  # noqa: E731
    s = _run([_posting()], transport=fake_transport(lines=lines, calls=calls), persist=persist)
    assert (s.reviewed, s.calls, s.errors, s.input_tokens) == (1, 2, 0, 1400)
    rec = persist.inserted[0]
    assert rec.posting_id == "P1" and rec.description_hash == "h-P1"
    assert rec.prompt_version == jev.prompt_version(_facts(), "typesafe") and rec.run_tag is None
    assert rec.endpoint == "typesafe" and rec.model_requested == rec.model_answered == Q.PINNED_MODEL
    assert rec.version_drift is False and rec.input_tokens == 1400
    assert rec.lens_process_grade == "bullseye" and rec.injection_p == 0.02
    assert rec.required_fit == "fails" and rec.derive_why.startswith("hard gate unmet")
    assert rec.lines_fit == "fails" and len(rec.lines) == 5
    assert set(rec.raw_response) == {"role", "lines"} and len(rec.raw_response["lines"]) == 1
    assert rec.reviewed_at.endswith("+00:00")


def test_run_tag_keys_the_cache_and_record(key_env):
    persist = FakePersist()
    _run([_posting()], persist=persist, run_tag="r2")
    base = jev.prompt_version(_facts(), "typesafe")
    assert persist.cache_queries == [("P1", "h-P1", f"{base}:r2", "r2")]
    assert persist.inserted[0].prompt_version == f"{base}:r2" and persist.inserted[0].run_tag == "r2"


def test_cache_skip_unless_forced(key_env):
    persist = FakePersist(reviewed={"P1"})
    s = _run([_posting()], transport=_ExplodingTransport(), persist=persist)
    assert s.skipped_cached == 1 and s.reviewed == 0 and s.calls == 0
    s = _run([_posting()], persist=persist, force=True)
    assert s.skipped_cached == 0 and s.reviewed == 1


def test_max_calls_cap_stops_cleanly(key_env):
    rows = [_posting("P1"), _posting("P2"), _posting("P3")]
    s = _run(rows, caps=Caps(max_calls_per_run=3, daily_token_cap=10**9))
    assert s.reviewed == 1 and s.calls == 2 and s.stopped_by_cap == "max_calls_per_run"


def test_daily_token_cap_counts_stored_and_run_tokens(key_env):
    rows = [_posting("P1"), _posting("P2")]
    est = sum(jev.estimate_tokens(b) for b in [jev.build_role_request(rows[0], Q.PINNED_MODEL)]
              + jev.build_lines_requests(rows[0], _facts(), Q.PINNED_MODEL, log=lambda *a: None))
    # 1000 spent today; the first posting fits (1000 + est), the second would not (2400 + est)
    persist = FakePersist(tokens_today=1000)
    s = _run(rows, persist=persist, caps=Caps(max_calls_per_run=100, daily_token_cap=1000 + est + 1000))
    assert s.reviewed == 1 and s.stopped_by_cap == "daily_token_cap"
    s = _run(rows, persist=FakePersist(tokens_today=10**9), transport=_ExplodingTransport(),
             caps=Caps(max_calls_per_run=100, daily_token_cap=5_000_000))
    assert s.reviewed == 0 and s.calls == 0 and s.stopped_by_cap == "daily_token_cap"


def test_version_drift_is_marked_on_typesafe_only(key_env):
    persist = FakePersist()
    lines = lambda body: lines_response(body, model="jev-1.14.0")  # noqa: E731
    s = _run([_posting()], transport=fake_transport(lines=lines), persist=persist)
    assert s.drift == 1 and persist.inserted[0].version_drift is True
    assert persist.inserted[0].model_answered == Q.PINNED_MODEL   # from the role response

    persist = FakePersist()
    role = lambda body: role_response(model="typesafe-ai/jev-x")  # noqa: E731
    s = _run([_posting()], transport=fake_transport(role=role), persist=persist, endpoint="vercel")
    assert s.drift == 0 and persist.inserted[0].version_drift is False
    assert persist.inserted[0].model_requested == Q.VERCEL_MODEL


def test_canary_flags_are_counted(key_env):
    role = lambda body: role_response(canary=Q.CANARY_FLAG_AT)  # noqa: E731
    s = _run([_posting("P1"), _posting("P2")], transport=fake_transport(role=role))
    assert s.canary_flags == 2
    s = _run([_posting("P1")])
    assert s.canary_flags == 0


def test_a_bad_posting_is_counted_never_fatal(key_env):
    def role(body):
        resp = role_response()
        if "BROKEN" in body["state"]["posting"]:
            del resp["answers"]["lens_ai"]
        return resp

    persist = FakePersist()
    s = _run([_posting("P1", jd=JD + "\nBROKEN"), _posting("P2")], transport=fake_transport(role=role),
             persist=persist)
    assert s.errors == 1 and s.reviewed == 1 and [r.posting_id for r in persist.inserted] == ["P2"]
    assert s.calls == 3   # the broken role call, then P2's two calls; no lines call after a bad role answer


def test_posting_with_no_lines_sends_only_the_role_request(key_env):
    persist = FakePersist()
    s = _run([_posting(jd="We are a friendly company. Apply today.")], persist=persist)
    assert s.calls == 1 and s.reviewed == 1
    rec = persist.inserted[0]
    assert rec.lines == [] and rec.required_fit is None and rec.raw_response["lines"] == []


# ---------------------------------------------------------------- 6. population helpers (tmp DB)
def _make_posting(con, req_id, *, jd=JD):
    job = N.base(req_id=req_id, title="Process Manager", url=f"https://x/{req_id}", workplace_type="remote",
                 description_text=jd)
    store.record_board(con, "Acme Corp", "greenhouse", [job], NOW, truncated=True)
    return con.execute("SELECT posting_id, description_hash FROM postings WHERE req_id = ?", [req_id]).fetchone()


def _screen(con, pid, *, fp=None, ft=None, fa=None, final_score=50, verdict="candidate"):
    con.execute("""INSERT INTO screens (posting_id, rules_version, model_version, screened_at, verdict, tier,
                   rule_score, final_score, band, fit_process, fit_technical, fit_ai)
                   VALUES (?, 'rv1', 'none', ?, ?, 1, 50, ?, 'strong', ?, ?, ?)""",
                [pid, NOW, verdict, final_score, fp, ft, fa])


def test_gated_postings_any_lens_at_or_above_the_gate(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    a, _ = _make_posting(con, "A")
    b, _ = _make_posting(con, "B")
    c, _ = _make_posting(con, "C")
    d, _ = _make_posting(con, "D")
    e, _ = _make_posting(con, "E")
    _screen(con, a, fa=0.95, final_score=90)
    _screen(con, b, fp=0.70, final_score=60)
    _screen(con, c, fp=0.5, ft=0.6, fa=0.69)                 # below the gate on every lens
    _screen(con, d, ft=0.99, verdict="reject")               # screen reject
    _screen(con, e, ft=0.9)
    con.execute("UPDATE postings SET status = 'closed' WHERE posting_id = ?", [e])
    got = jev.gated_postings(con)
    assert [p.posting_id for p in got] == [a, b]
    assert got[0].title == "Process Manager" and got[0].description_text and got[0].description_hash
    assert [p.posting_id for p in jev.gated_postings(con, limit=1)] == [a]
    assert {p.posting_id for p in jev.gated_postings(con, lens_min=0.6)} == {a, b, c}
    con.close()


def test_eval_set_postings_are_the_blind_graded_rows(tmp_path):
    con = store.connect(str(tmp_path / "t.duckdb"))
    pid, dh = _make_posting(con, "G1")
    other, _ = _make_posting(con, "G2")
    _screen(con, pid, fp=0.9)
    _screen(con, other, fp=0.9)
    feedback.record_mark(con, pid, dh, "pass", reason_code="requirement", required_fit="fails",
                         required_unmet="Active TS/SCI clearance required.", basis="blind", now=NOW)
    got = jev.eval_set_postings(con)
    assert [(p.posting_id, p.description_hash) for p in got] == [(pid, dh)]
    assert isinstance(got[0], Posting)
    con.close()


def test_line_record_properties_feed_judge2():
    rec = LineRecord(line_no=0, section="required", line_text="SQL", kind="tool", kind_conf=0.9,
                     verdict="met", verdict_raw="met", verdict_probs={}, verdict_conf=0.9,
                     evidence_fact_id="f01", evidence_p=0.8, evidence_text="Built SQL reports.",
                     evidence_downgraded=False)
    assert judge2._get(rec, "line") == "SQL" and judge2._get(rec, "evidence") == "Built SQL reports."
    assert judge2._get(rec, "years") is None
