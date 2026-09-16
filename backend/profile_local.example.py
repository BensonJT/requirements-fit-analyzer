"""Copy to backend/profile_local.py (gitignored) and fill in your own values."""

COMP_FLOOR = 90_000        # reject when a posted band's top is under this
COMP_ASK = 110_000         # score bonus when the band top reaches this
HOME = "Springfield, IL"   # used for the local search lanes
LOCAL_RADIUS_KM = 80
COMMUTABLE_PLACES = ["springfield, il", "chatham, il?", "rochester, il"]   # "name, st" pins the state; "st?" also accepts none
TRAVEL_MAX_PCT = 25
MAX_DIRECT_REPORTS = 5
DOMAIN_TENURE_TERMS = ["banking", "pharma"]
# Fields you HAVE: a years-requirement listing one of these as an alternative is not a gate.
CANDIDATE_TENURE_FIELDS = ["operations", "consulting", "process improvement", "transformation"]
CORRIDOR_PLACES = ["springfield, il"]
FAITH_COMP_FLOOR = 85_000
