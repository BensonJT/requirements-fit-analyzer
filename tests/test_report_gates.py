"""report_gates: the location gate and the gem gate (2026-10-30). Pure functions on dicts; fake data only."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backend.finder import report_gates as G  # noqa: E402


def _row(location, workplace=None, locations=None):
    return {"location_primary": location, "workplace_type": workplace, "locations": locations,
            "title": "Program Manager", "employer": "Acme", "description_text": "", "platform": "workday"}


@pytest.mark.parametrize("row, reasons, ok, note_start", [
    (_row("Mexico"), [], False, "outside the US"),
    (_row("Remote - United States", "remote"), [], True, None),
    (_row("Remote - United States", "remote"), ["not remote and outside the commute area (per listing)"], False, "not remote"),
    (_row("Remote - United States", "remote"), ["clearance must already be held -- likely unreachable (ts/sci)"], False, "clearance"),
    (_row("Remote - United States", "remote"), ["comp: band top $120,000 under the $150,000 floor"], False, "comp:"),
    # 10/30 Solventum: the ATS says hybrid in Eagan, MN; a remote phrase in the JD must not carry it
    (_row("US, Minnesota, Eagan", "hybrid", '["US, Minnesota, Eagan"]'), [], False, "hybrid outside the commute area"),
    (_row("Leesburg, VA", "hybrid", '["Leesburg, VA"]'), [], True, None),
    (_row("United States", "hybrid"), [], True, None),                   # too generic to judge: left to the screen
    # judgment reasons never fail the location gate
    (_row("Remote - United States", "remote"), ["junior level (at most 3 yrs required)"], True, None),
])
def test_location_check(row, reasons, ok, note_start):
    got_ok, note = G.location_check(row, reasons)
    assert got_ok is ok
    assert (note is None) if note_start is None else note.startswith(note_start)


@pytest.mark.parametrize("kw, expect", [
    (dict(verdict="reject", reasons=["junior level (at most 3 yrs required)", "travel 50% (limit 25%)"]),
     "screen: junior level (at most 3 yrs required)"),
    (dict(verdict="review", level="out_of_reach"), "level: out_of_reach"),
    (dict(top_jobs="jev demoted"), "Jev demoted"),
    (dict(top_jobs="check required"), "Jev: check the Required block"),
    (dict(judge_required="fails"), "judge: Required fails"),
    (dict(fit_required=0.12), "model: Required 0.12 (unclear below 0.40)"),
    (dict(), "below the Top Jobs bars or caps"),
])
def test_gem_gate_names_the_first_judgment_gate(kw, expect):
    args = dict(verdict="review", reasons=[], level="in_range", top_jobs=None, judge_required=None, fit_required=None)
    args.update(kw)
    assert G.gem_gate(**args) == expect
