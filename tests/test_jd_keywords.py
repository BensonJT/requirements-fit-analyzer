"""backend/jd_keywords: section trim, quality filter, token pacing, bucket merge and the Groq fallback (no network)."""
import pytest

from backend.finder.requirements import section_lines
from backend.jd_keywords import classify, extract

JD = """Overview
Contoso builds things for customers everywhere and is a great place to work.

Responsibilities
Business Operations & Governance
Owns and continuously improves the Rhythm of Business, including leadership and planning forums.
Leads budget planning, forecasting and variance analysis across operational spend.

Qualifications
Required Qualifications
Bachelor's Degree AND 4+ years experience in business planning or operations management.

Preferred Qualifications
Strong systems thinking and executive communication skills.
#ContosoCareers
The typical base pay range for this role across the U.S. is USD $97,600 - $188,400 per year.
This position will be open for a minimum of 5 days, with applications accepted on an ongoing basis.

Contoso is an equal opportunity employer. All qualified applicants will receive consideration without regard
to race, religion, sexual orientation or any other characteristic. Request a reasonable accommodation here.
"""


def test_section_lines_flags_subheadings():
    rows = section_lines(JD)
    assert ("responsibility", "Business Operations & Governance", True) in rows
    assert all(len(r) == 3 for r in rows)


def test_duties_and_requirements_drops_boilerplate_keeps_subheadings():
    text, sizes = extract.duties_and_requirements(JD)
    assert "Business Operations & Governance" in text
    assert "Rhythm of Business" in text
    assert "4+ years experience" in text
    assert "systems thinking" in text
    for gone in ("base pay", "equal opportunity", "reasonable accommodation", "#ContosoCareers",
                 "open for a minimum", "great place to work"):
        assert gone not in text, gone
    assert sizes.get("responsibility", 0) > 0


def test_duties_and_requirements_falls_back_when_nothing_kept():
    text, sizes = extract.duties_and_requirements("")
    assert (text, sizes) == ("", {})


def test_filter_candidates_drops_fragments_and_rare_single_words():
    kws = [
        {"keyword": "variance analysis", "count": 1},
        {"keyword": "provide routine updates", "count": 1},      # verb-led fragment
        {"keyword": "refreshing sharepoint sites", "count": 1},  # gerund-led fragment
        {"keyword": "budgets and forecasts", "count": 1},        # connector
        {"keyword": "forecasting", "count": 1},                  # single word, rare, not whitelisted
        {"keyword": "governance", "count": 1},                   # whitelisted single word
        {"keyword": "cross‑functional execution", "count": 2},   # unicode hyphen normalized
    ]
    out = [k["keyword"] for k in extract.filter_candidates(kws)]
    assert out == ["variance analysis", "governance", "cross-functional execution"]


def test_process_jd_layers_find_taxonomy_and_noun_phrases():
    pytest.importorskip("spacy")
    try:
        extract._get_nlp()
    except OSError:
        pytest.skip("en_core_web_sm not installed")
    found = {k["keyword"].lower() for k in extract.extract_keywords(extract.duties_and_requirements(JD)[0])}
    assert {"business planning", "business operations"} <= found
    assert not {k for k in found if "opportunity" in k or "accommodation" in k}


def test_token_pacer_waits_for_the_window():
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    pacer = classify.TokenPacer(10000, window=60.0, clock=lambda: now[0], sleep=sleep)
    assert pacer.wait(6000) == 0
    now[0] = 10.0
    waited = pacer.wait(6000)            # 12K would exceed 10K inside the minute
    assert waited == pytest.approx(50.5)
    assert pacer.wait(100) > 0 or now[0] >= 60.5   # first call has aged out by now
    assert classify.TokenPacer(10000).wait(50000) == 0   # an oversized single call still goes, alone


def test_bucketize_merges_conservatively_and_assigns_leftovers(monkeypatch):
    monkeypatch.setenv("JD_BUCKETIZE_PROVIDER", "aistudio")
    reply = '{"bucket1": ["variance analysis", "ROB", "variance analysis"], "bucket2": ["ROB"], "bucket3": []}'
    monkeypatch.setattr(classify, "_aistudio_call", lambda *a, **k: reply)
    out = classify.bucketize([{"keyword": k} for k in ("variance analysis", "ROB", "forgotten")], log=lambda *a: None)
    assert out["bucket1"] == ["variance analysis"]
    assert out["bucket2"] == ["ROB", "forgotten"]


def test_bucketize_falls_back_to_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.delenv("JD_BUCKETIZE_FALLBACK", raising=False)
    monkeypatch.setattr(classify.time, "sleep", lambda s: None)

    def down(*a, **k):
        raise classify.ProviderError("aistudio: HTTP 503", status=503)

    monkeypatch.setattr(classify, "_aistudio_call", down)
    monkeypatch.setattr(classify, "_groq_call", lambda *a, **k: '{"bucket1": ["governance"]}')
    lines = []
    out = classify.bucketize([{"keyword": "governance"}], provider="aistudio", log=lines.append)
    assert out["bucket1"] == ["governance"]
    assert any("falling back to groq" in l for l in lines)


def test_bucketize_no_fallback_when_disabled(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setenv("JD_BUCKETIZE_FALLBACK", "none")
    monkeypatch.setattr(classify, "_aistudio_call",
                        lambda *a, **k: (_ for _ in ()).throw(classify.ProviderError("bad", status=400)))
    with pytest.raises(classify.ProviderError):
        classify.bucketize([{"keyword": "x"}], provider="aistudio", log=lambda *a: None)


def test_server_errors_are_refunded_to_the_pacer(monkeypatch):
    monkeypatch.setattr(classify.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3:
            raise classify.ProviderError("aistudio: HTTP 500", status=500)
        return '{"bucket1": ["governance"]}'

    monkeypatch.setattr(classify, "_aistudio_call", flaky)
    waits = []
    real = classify.TokenPacer.wait
    monkeypatch.setattr(classify.TokenPacer, "wait", lambda self, t: waits.append(real(self, t)) or waits[-1])
    monkeypatch.setenv("JD_BUCKETIZE_TPM", "6000")   # one call's worth: without refunds attempt 2 would pace
    out = classify.bucketize([{"keyword": "governance"}], provider="aistudio", log=lambda *a: None)
    assert out["bucket1"] == ["governance"] and calls["n"] == 3 and waits == [0, 0, 0]
