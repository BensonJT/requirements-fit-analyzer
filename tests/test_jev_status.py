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
