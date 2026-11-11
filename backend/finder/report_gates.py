"""Report gates for the Postgres snapshot (2026-10-30, user request): what the focus report needs to filter
on location and to find gems, computed here from the same code Top Jobs and the screen use, so the SQL views
never keep a second copy of the rules.

The user's split (2026-10-30): the OBJECTIVE gates filter every report (a role outside the US or outside the
commute area is a waste of time), while the JUDGMENT gates -- the Required verdict, level, Jev's demotion, a
screen reject on title or scope -- are where errant filtering hides, so they never filter the gem list; they
are shown beside the row as the reason it was filtered.

Per posting:
  us_primary        not non_us_primary(location_primary)
  location_ok       US primary, no logistics screen reason (commute, far commute, outside the US, remote
                    restricted, clearance, travel, comp floor, hard-avoid industry), and not an ATS hybrid /
                    on-site posting with no commutable location. The last check is stricter than the screen's:
                    the screen lets a remote phrase in the JD win over the ATS flag, which let "hybrid, 3 days
                    a week in Eagan, MN" through as remote (Solventum R01133897, 2026-10-30).
  location_note     why location_ok is false
  lens_strong       a judge grade of bullseye/adjacent on any lens, or a lens model at Jev's bar
  top_jobs          where the row sits on Top Jobs: apply | review | jev demoted | check required |
                    requirement unclear | NULL (not shown)
  gem_gate          for a lens-strong row NOT in Top Jobs Apply/Review: the judgment gate that kept it off
  judge_required, judge2_required, jev_required
                    the three Required reviewers' calls (judge, Gemma, Jev)
  two_reviewer_fail Jev demotes it on clear-cut lines AND the judge or Gemma also says fails: obviously not a
                    fit, left off the gem list
  screen_reasons    the screen's reject reasons, '; '-joined
  verify_note       (2026-11-11, #570) a SuccessFactors listing old enough to be a career-site leftover whose
                    requisition is closed: click Apply before building (backend/ats/listing_age.py)
"""
import json
import re
from typing import Optional

from .. import screen as S
from ..ats import listing_age
from . import jev_gate, report
from .judge import non_us_primary
from .rules import listing_from_row

LOGISTICS_REASON_RE = re.compile(
    r"^(?:not remote and outside the commute area|outside the US|far commute|remote restricted to|clearance|"
    r"travel|comp:|hard-avoid industry)", re.I)
GEM_EXCLUDED_SECTIONS = ("apply", "review")
COLUMNS = ("us_primary", "location_ok", "location_note", "lens_strong", "top_jobs", "gem_gate", "judge_required",
           "judge2_required", "jev_required", "two_reviewer_fail", "screen_reasons", "verify_note")


def _reasons(raw) -> list:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return [raw]
    return [str(r) for r in raw or []]


def location_check(row: dict, reasons: list) -> tuple:
    """(location_ok, note). Pure apart from the screen's place matching, so it is testable on a dict."""
    if non_us_primary(row.get("location_primary")):
        return False, "outside the US"
    logistics = [r for r in reasons if LOGISTICS_REASON_RE.match(r)]
    if logistics:
        return False, logistics[0]
    workplace = row.get("workplace_type")
    listing = listing_from_row(row)
    if workplace in ("hybrid", "onsite") and not S._GENERIC_LOCATION_RE.search(row.get("location_primary") or ""):
        if not S.is_commutable(listing):
            return False, f"{workplace} outside the commute area (ATS flag)"
    # 2026-11-10 (user: the Focus Report listed roles that were neither remote nor commutable). The stored screen
    # reasons are only as current as the last rescreen, and the screen's remote read had three holes (a "virtual
    # network" duties phrase, a remote-eligibility policy sentence, a multi-city spread on a JD that says
    # in-office), so Iselin NJ, San Antonio, Denver and a hybrid Cary role all passed. Read the place again here.
    local = bool(listing.is_local_pass) or S.is_commutable(listing)
    remote = S.is_remote(listing)
    if not remote and not local and not S.is_nationwide(listing):
        return False, "not remote and outside the commute area (live read)"
    # A spread of offices in 3+ states with no remote statement anywhere is the shape of a multi-site on-site
    # employer (Cisco 12 cities, BlinkRx 4). The screen keeps reading it as remote (Coinbase lists its remote
    # reqs that way), but a report that promises "remote or commutable" cannot rest on it alone.
    if remote and not local and _remote_by_spread_only(listing):
        return False, "multi-city listing with no remote statement"
    return True, None


def _remote_by_spread_only(listing) -> bool:
    """True when the only thing making the screen read this posting as remote is the multi-city spread."""
    blob = "\n".join([listing.location or "", *(listing.locations or []), listing.description or ""])
    return (listing.extra.get("workplace_type") != "remote" and not S._remote_location_entry(listing)
            and not S._US_OFFSITE_RE.search(blob)
            and not S._remote_in_context(f"{listing.title}\n{listing.location}\n{listing.description}")
            and S.multi_city_signature(listing))


def judgment_reasons(reasons: list) -> list:
    return [r for r in reasons if not LOGISTICS_REASON_RE.match(r)]


def gem_gate(*, verdict: Optional[str], reasons: list, level: Optional[str], top_jobs: Optional[str],
             judge_required: Optional[str], fit_required: Optional[float], levels=report.TOP_LEVELS) -> str:
    """The judgment gate that kept a lens-strong row out of Top Jobs Apply / Review, first gate first."""
    if verdict == "reject":
        return "screen: " + "; ".join(judgment_reasons(reasons) or reasons)
    if level not in levels:
        return f"level: {level or 'unknown'}"
    if top_jobs == "jev demoted":
        return "Jev demoted"
    if top_jobs == "check required":
        return "Jev: check the Required block"
    if judge_required == "fails":
        return "judge: Required fails"
    if judge_required is None and fit_required is not None and fit_required < report.MODEL_REQ_REVIEW:
        return f"model: Required {fit_required:.2f} (unclear below {report.MODEL_REQ_REVIEW:.2f})"
    if top_jobs == "requirement unclear":
        return "model: requirement unclear"
    return "below the Top Jobs bars or caps"


def top_jobs_sections(con, levels=report.TOP_LEVELS) -> dict:
    """{posting_id: section} for every row Top Jobs shows, as write_top_jobs places it."""
    shown = jev_gate.top_subset_ids(con, levels=levels)
    out = dict(shown)
    for pid, d in jev_gate.demotions(con, shown).items():
        out[pid] = "check required" if d.check else "jev demoted"
    for r in report.unclear_rows(con, levels=levels)[:report.TOP_UNCLEAR_CAP]:
        out.setdefault(r[0], "requirement unclear")
    return out


def compute(con, ids: list, jev: Optional[dict] = None, *, levels=report.TOP_LEVELS) -> dict:
    """{posting_id: {column: value}} for `ids`. `jev` is jev_status.statuses' output (for two_reviewer_fail)."""
    if not ids:
        return {}
    jev = jev or {}
    sections = top_jobs_sections(con, levels=levels)
    jev_req = dict(con.execute("SELECT posting_id, required_fit FROM vw_jev_latest").fetchall())
    cur = con.execute(f"""
        SELECT p.posting_id, p.employer, p.title, p.url, p.platform, p.location_primary, p.locations,
               p.workplace_type, p.description_text, p.pay_min, p.pay_max, p.posted_at,
               f.verdict, s.reasons, f.level_fit, f.required_fit, f.judge2_required, f.fit_required,
               ({jev_gate._ELIGIBLE_LENS_SQL}) AS strong
        FROM postings p LEFT JOIN vw_lens_fit f USING (posting_id) LEFT JOIN vw_screen_latest s USING (posting_id)
        WHERE p.posting_id IN (SELECT unnest(?::VARCHAR[]))""", [list(ids)])
    names = [d[0] for d in cur.description]
    fetched = cur.fetchall()
    stale = listing_age.stale_listings(con, ids)
    out = {}
    for t in fetched:
        row = dict(zip(names, t))
        pid, reasons = row["posting_id"], _reasons(row["reasons"])
        ok, note = location_check(row, reasons)
        strong = bool(row["strong"])
        section = sections.get(pid)
        jres = (jev.get(pid) or (None, None))[0]
        out[pid] = {
            "us_primary": not non_us_primary(row["location_primary"]),
            "location_ok": ok, "location_note": note, "lens_strong": strong, "top_jobs": section,
            "gem_gate": (gem_gate(verdict=row["verdict"], reasons=reasons, level=row["level_fit"], top_jobs=section,
                                  judge_required=row["required_fit"], fit_required=row["fit_required"], levels=levels)
                         if strong and section not in GEM_EXCLUDED_SECTIONS else None),
            "judge_required": row["required_fit"], "judge2_required": row["judge2_required"],
            "jev_required": jev_req.get(pid),
            "two_reviewer_fail": jres in ("Demoted", "Would demote")
                                 and "fails" in (row["required_fit"], row["judge2_required"]),
            "screen_reasons": "; ".join(reasons) or None,
            "verify_note": stale.get(pid),
        }
    return out
