"""jev_status.exclusion_note: the first gate that kept a posting out of Jev's subset, in write_top_jobs order."""
from backend.finder import jev_status

BASE = dict(track="fit", verdict="review", level="in_range", decided=False, in_tracker=False, listed=None,
            judge_tier=None, n_lenses_good=None, judge_required=None, unclear=False, band="strong", fit_required=0.5)


def note(**kw):
    return jev_status.exclusion_note(**{**BASE, **kw})


def test_gates_in_report_order():
    assert note(track="bridge").startswith("bridge track")
    assert note(decided=True, verdict="reject").startswith("already decided")      # decided wins over reject
    assert note(in_tracker=True).startswith("already in the tracker")
    assert note(verdict=None).startswith("never screened")
    assert note(verdict="reject", level="junior").startswith("screen rejected")    # reject wins over level
    assert note(level="junior").startswith("level gate: junior")


def test_listed_rows_name_the_cap_or_the_location_gate():
    assert "below the Top_Jobs cap: Jev reviews only the first 40" in note(listed="judge review")
    assert "first 100" in note(listed="model apply")
    assert note(listed="model review (non-US)").startswith("non-US primary location")


def test_unlisted_rows_say_why():
    hidden = note(judge_tier="hidden", n_lenses_good=0, judge_required="meets")
    assert hidden.startswith("judge tier hidden: 0 lens(es)") and "Required meets" in hidden
    assert "required-fit 0.18" in note(unclear=True, fit_required=0.18)
    assert note(band="weak").startswith("score band weak")


def test_hard_gates_are_recognised_and_soft_gates_mean_no_lens_at_the_bar():
    for kw in (dict(decided=True), dict(verdict="reject"), dict(level="junior"), dict(track="bridge"),
               dict(listed="model review (non-US)"), dict(in_tracker=True), dict(verdict=None)):
        assert note(**kw).startswith(jev_status.HARD_GATES), kw
    for kw in (dict(listed="judge review"), dict(judge_tier="hidden"), dict(unclear=True), dict(band="weak")):
        assert not note(**kw).startswith(jev_status.HARD_GATES), kw
    assert "no lens at Jev's bar" in jev_status.NO_LENS_NOTE and "technical 0.80" in jev_status.NO_LENS_NOTE


def test_a_hidden_row_carrying_the_users_own_grade_says_so():
    n = note(judge_tier="hidden", n_lenses_good=0, judge_required="meets", own_grade="stretch")
    assert n.startswith("your own grade on record (stretch)")
