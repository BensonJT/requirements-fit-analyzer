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
    (_row("Remote - United States", "remote"), ["comp: band top $120,000 under the $130,000 floor"], False, "comp:"),
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


def _desc_row(location, description, locations, workplace=None):
    r = _row(location, workplace, locations)
    r["description_text"] = description
    return r


@pytest.mark.parametrize("row, ok, note_start", [
    # 11/10: a multi-campus employer lists its offices because the job is in one of them; the JD says in-office
    (_desc_row("Dallas, TX", "We require being in the office 4 days per week.",
               '["Dallas, TX", "Phoenix, AZ", "Charlotte, NC"]'), False, "not remote and outside"),
    (_desc_row("Newark, NJ", "This position offers a hybrid work schedule.",
               '["Newark, NJ", "Irving, TX", "Charlotte, NC"]'), False, "not remote and outside"),
    # silent spread: kept by the screen, but the report cannot call it remote
    (_desc_row("Denver, CO", "Program role.", '["Denver, CO", "Austin, TX", "Atlanta, GA"]'), False, "multi-city"),
    # a spread that also says remote stays
    (_desc_row("Denver, CO", "This is a fully remote role.", '["Denver, CO", "Austin, TX", "Atlanta, GA"]'), True, None),
    # "virtual network" is a duties phrase; the ATS flag says hybrid, three days in the office
    (_desc_row("Cary, United States", "Lead a virtual network of champions. This is a hybrid role requiring "
               "a minimum of 3 days per week in office.", '["Cary, United States"]', "hybrid"), False, "not remote and outside"),
    # a remote-eligibility POLICY sentence is not an offer
    (_desc_row("Plano, TX", 'A role is remote-eligible only when it is listed as "Remote" in the job location.',
               '["Plano, TX"]'), False, "not remote and outside"),
    # the posting itself says remote
    (_desc_row("Remote", "Join us.", '["Remote"]', "remote"), True, None),
    # a nationwide listing with nothing against it stays (unverified, the screen flags it)
    (_desc_row("United States", "Strategy role.", '["United States"]'), True, None),
])
def test_location_check_reads_the_place_live(row, ok, note_start):
    got_ok, note = G.location_check(row, [])
    assert got_ok is ok
    assert (note is None) if note_start is None else note.startswith(note_start)
