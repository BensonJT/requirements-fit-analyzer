"""Stale-listing flag for SAP SuccessFactors career sites (2026-11-11, the user: tracker #570).

A SuccessFactors employer runs two systems: the career site (Recruiting Marketing, "RMK": jobs.l3harris.com)
and the requisition system behind it (Recruiting Management, "RCM"). RCM pushes postings out to the career
site; when a recruiter closes the requisition, the career-site copy is supposed to come down, but it does not
always. L3Harris job code 32717 (RMK id 1355475200) was still listed on the search page and its job page (its
own validThrough said Jan 2027) while Apply answered "This job cannot be viewed at this time. It has either been
deleted or is no longer available for application." The board close-pass only sees the career site, so it
never closed. RCM's own pages (career-hcm20.ns2cloud.com/portalcareer) say which requisitions are closed, but
that host's robots.txt disallows everything, so the pipeline never asks it.

What we can read is the RMK job id. It comes from ONE counter shared by every SuccessFactors tenant (NTT Data,
L3Harris, Amtrak and NRG new postings land in the same range the same week), climbing about 300K a day
(measured 2026-11-11 from the weekly medians of newly seen postings: 1.4443e9 week of 10/26, 1.4487e9 week of
11/9). So `newest id - this id` dates the listing even when the career site refreshes its datePosted
(32717's page said 2026-10-29). Spot check the same day, 25 active L3Harris listings asked once by hand:
12 of the 15 oldest ids were closed behind the career site, 0 of 10 mid-range ids were.

This is a flag, never a close: an old id can be an evergreen requisition that is really open (3 of those 15
were). Reports print `verify_note` so the user clicks Apply before spending time on a package.
"""
from typing import Optional

RMK_PLATFORM = "successfactors_rmk"
RMK_IDS_PER_DAY = 300_000      # measured 2026-11-11; only sets the "~N mo" wording and the cut below
RMK_STALE_DAYS = 150           # ~5 months: all 12 closed listings in the spot check were older than this


def rmk_age_days(req_id, anchor_id: Optional[int]) -> Optional[int]:
    """Approximate listing age in days from the shared RMK id counter; None when either id is unusable."""
    try:
        rid = int(req_id)
    except (TypeError, ValueError):
        return None
    if not anchor_id or rid <= 0:
        return None
    return max(0, anchor_id - rid) // RMK_IDS_PER_DAY


def verify_note(age_days: Optional[int]) -> Optional[str]:
    """The report text for a listing old enough to be a career-site leftover, else None."""
    if age_days is None or age_days < RMK_STALE_DAYS:
        return None
    return (f"verify open: SuccessFactors listing ~{max(1, round(age_days / 30))} mo old; "
            "click Apply before building (career site can outlive the requisition)")


def rmk_anchor(con) -> Optional[int]:
    """The newest RMK id the store holds: the counter's present position."""
    row = con.execute("SELECT max(TRY_CAST(req_id AS BIGINT)) FROM postings WHERE platform = ?",
                      [RMK_PLATFORM]).fetchone()
    return row[0] if row else None


def stale_listings(con, ids) -> dict:
    """{posting_id: verify_note} for the SuccessFactors postings among `ids` old enough to need a check."""
    ids = list(ids or [])
    if not ids:
        return {}
    anchor = rmk_anchor(con)
    if not anchor:
        return {}
    rows = con.execute("SELECT posting_id, req_id FROM postings WHERE platform = ? "
                       "AND posting_id IN (SELECT unnest(?::VARCHAR[]))", [RMK_PLATFORM, ids]).fetchall()
    out = {}
    for pid, req_id in rows:
        note = verify_note(rmk_age_days(req_id, anchor))
        if note:
            out[pid] = note
    return out
