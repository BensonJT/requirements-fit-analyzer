"""Copy to backend/profile_local.py (gitignored) and fill in your own values."""

COMP_FLOOR = 90_000        # reject when a posted band's top is under this
COMP_ASK = 110_000         # score bonus when the band top reaches this
HOME = "Springfield, IL"   # used for the local search lanes
LOCAL_RADIUS_KM = 80
COMMUTABLE_PLACES = ["springfield, il", "chatham, il?", "rochester, il"]   # "name, st" pins the state; "st?" also accepts none
# Of those, the ones you would live near or visit for a REMOTE role but not commute to several days a
# week. Never carries an office role (one that does not read as remote); counted everywhere else.
COMMUTE_REMOTE_ONLY_PLACES = []
# Of those, the long drives: the JD's in-office days decide (reject at FAR_COMMUTE_REJECT_DAYS+, flag below).
COMMUTE_FAR_PLACES = []
FAR_COMMUTE_OK_DAYS = 1        # a day a week or less at a far place is fine
FAR_COMMUTE_REJECT_DAYS = 3    # three or more days a week at a far place is a no-go
# Places that are a plain commute for ONE employer only, e.g. {"acme payments": ["chatham, il?"]}.
COMMUTE_EMPLOYER_PLACES = {}
# Private, per-employer residence knowledge the JD text never states (§23 amendment). Uncomment and
# fill in only what you actually know; leave it out entirely and the rule is simply skipped.
# EMPLOYER_RESIDENCE_NOTES = {
#     "acme payments": {"hubs": ["chicago, il"], "note": "remote roles must live within commuting distance of a hub office -- not stated in their postings, known from a past screen"},
# }
TRAVEL_MAX_PCT = 25
MAX_DIRECT_REPORTS = 5
DOMAIN_TENURE_TERMS = ["banking", "pharma"]
# Fields you HAVE: a years-requirement listing one of these as an alternative is not a gate.
CANDIDATE_TENURE_FIELDS = ["operations", "consulting", "process improvement", "transformation"]
CORRIDOR_PLACES = ["springfield, il"]
FAITH_COMP_FLOOR = 85_000

# ---- Level fit (finder sec 20.2) -- mirrored here (not just in profile.py) so tests that monkeypatch this
# example file always see a known value, regardless of what a real profile_local.py sets.
LEVEL_MANAGERIAL_YEARS_MAX = 3     # tighten in your own profile_local.py, e.g. 2, if your bar is lower
# At or above this many years of PEOPLE management the ask is two scope hits (out_of_reach on its own);
# between the two limits it is one (stretch_up). The grey zone the user named: sell 15+ years of cross-functional
# leadership, or not.
LEVEL_MANAGERIAL_YEARS_OUT = 5
LEVEL_OUT_OF_REACH_TITLE_TERMS = [
    "senior director", "sr. director", "sr director", "executive director", "vice president", "vp", "svp",
    "evp", "avp", "assistant vice president", "head of", "chief", "cxo", "general manager", "managing director",
]
LEVEL_STRETCH_TITLE_TERMS = ["director", "chief of staff"]
LEVEL_IN_RANGE_TITLE_TERMS = [
    "principal", "senior", "sr.", "sr ", "lead", "staff", "manager", "consultant", "analyst", "specialist",
    "architect", "engineer", "program manager", "project manager", "owner",
]
ORG_BUILDING_TERMS = [
    "build and lead", "build and scale", "build the org", "build the organization", "build a team",
    "build the team", "build out the team", "scale the organization", "global teams", "global organization",
    "spans of control", "span of control", "executive leadership team", "member of the executive",
    "leaders of leaders", "manager of managers", "managers of managers", "org design",
]
