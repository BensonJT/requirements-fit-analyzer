"""USAJobs location (2026-11-02): RemoteIndicator decides remote, and a "Multiple Locations" posting is judged
on its listed duty stations. Shapes taken from live postings: VA Aurora CO "Virtual: This is not a virtual
position", State Dept NYC telework boilerplate, DHS "Multiple Locations" Atlanta + Kansas City."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import screen as S  # noqa: E402
from backend.finder import rules  # noqa: E402

OUTSIDE = "not remote and outside the commute area (per listing)"
NATIONWIDE_FLAG = "nationwide listing -- remote status unverified"


def _row(location, locations, workplace, description, title="Supervisory Management and Program Analyst"):
    return dict(posting_id="u" * 20, employer="U.S. Government (USAJobs)", platform="usajobs", req_id="800001",
                title=title, url="https://www.usajobs.gov/job/800001", location_primary=location,
                locations=json.dumps(locations), country=None, workplace_type=workplace,
                employment_type="full_time", job_level=None, pay_min=150000, pay_max=191900, pay_interval="year",
                posted_at=None, description_text=description, first_seen_at=None, description_fetched_at=None)


def _listing(*a, **kw):
    return rules.listing_from_row(_row(*a, **kw))


def test_federal_telework_boilerplate_is_not_remote():
    for text in ("Work Schedule: Monday- Friday, 8:00am - 4:30pm Virtual: This is not a virtual position.",
                 "Compressed/Flexible Schedule: Not Available\nTelework: Not Available",
                 "If eligible, telework and remote work agreements may be permitted with supervisory approval.",
                 "Other benefits may include flexible work schedules; telework; tuition reimbursement."):
        assert not S.is_remote(_listing("Aurora, Colorado", ["Aurora, Colorado"], "onsite", text)), text


def test_usajobs_remote_indicator_still_reads_remote():
    lst = _listing("Anywhere in the U.S. (remote job)", ["Anywhere in the U.S. (remote job), United States"],
                   "remote", "Telework: Not Available")
    assert S.is_remote(lst)


def test_out_of_area_federal_posting_is_rejected_on_location():
    rec = rules.screen_row(_row("Aurora, Colorado", ["Aurora, Colorado"], "onsite",
                                "Virtual: This is not a virtual position."))
    assert OUTSIDE in rec.reasons


def test_multiple_locations_is_judged_on_its_sites():
    far = _listing("Multiple Locations", ["Atlanta, Georgia", "Kansas City, Missouri"], "hybrid", "")
    assert not S.is_nationwide(far)
    rec = rules.screen_row(_row("Multiple Locations", ["Atlanta, Georgia", "Kansas City, Missouri"], "hybrid", ""))
    assert OUTSIDE in rec.reasons and NATIONWIDE_FLAG not in rec.flags

    dc = _listing("Multiple Locations", ["Atlanta, Georgia", "Washington, District of Columbia"], "hybrid", "")
    assert not S.is_nationwide(dc) and S.is_commutable(dc)
    rec = rules.screen_row(_row("Multiple Locations", ["Atlanta, Georgia", "Washington, District of Columbia"],
                                "hybrid", ""))
    assert OUTSIDE not in rec.reasons


def test_multiple_locations_without_a_site_list_stays_nationwide():
    assert S.is_nationwide(_listing("Multiple Locations", [], "hybrid", ""))
    assert S.is_nationwide(_listing("Multiple Locations", ["Multiple Locations"], "hybrid", ""))


def test_other_platforms_keep_the_nationwide_placeholder():
    lst = rules.listing_from_row(dict(_row("Multiple Locations", ["Atlanta, Georgia"], "hybrid", ""),
                                      platform="workday"))
    assert S.is_nationwide(lst)
