"""Screening profile for sweep.py: target title phrases, exclusions, and scoring rules.

The title and exclusion lists below reflect the author's target roles; edit them for yours.
Personal values (pay thresholds, home location) live in backend/profile_local.py, which
is gitignored, so they never enter the repository.
"""

# Search phrases -- the Mode 2 default string, one phrase per API call.
# "BPO" and bare "Lean" were removed 2026-08-31 (outsourcing / McLean / plant-floor noise).
# "Workforce Planning" and "Chief of Staff" are excluded by the author's decision.
FUNCTION_PHRASES = [
    "Process Excellence",
    "Operational Excellence",
    "Business Process Optimization",
    "Global Process Owner",
    "Business Transformation",
    "Lean Six Sigma",
    "Lean Transformation",
    "Change Management",
    "Continuous Improvement",
    "Process Improvement",
    "Capacity Planning",
    "Operating Model",
    "DMAIC",
    "IDEF0",
]

# Runs as its own pass and every hit gets the discipline test (plant vs. business process).
SEPARATE_PASS_PHRASES = ["Process Engineer"]

# A title must contain one of these to count as a function match.
TITLE_FUNCTION_TERMS = [
    "process excellence", "operational excellence", "operations excellence",
    "business excellence", "business process", "process optimization",
    "process owner", "process ownership", "transformation", "lean six sigma",
    "six sigma", "black belt", "lean ", "change management", "change manager",
    "organizational change", "continuous improvement", "process improvement",
    "capacity planning", "capacity strategy", "operating model", "dmaic", "idef0",
    "process engineer", "process engineering", "opex", "process design",
    "process architect", "business architect", "performance improvement",
]

# Tier 1 -- high precision, near-zero false positives (HANDOFF_filter_sync_20260914 section 1).
# Everything else that passes TITLE_FUNCTION_TERMS is Tier 2 and reports below Tier 1.
PRECISE_TITLE_TERMS = [
    "global process own", "business process optimi", "process excellence",
    "operational excellence", "operations excellence", "lean six sigma",
    "lean transformation", "dmaic", "idef0", "operating model", "business transformation",
]

# Title words that mark a level well below Principal / Senior.
JUNIOR_TITLE_TERMS = [
    "intern", "internship", "coordinator", "technician", "assistant ", "junior",
    "entry level", "entry-level", "apprentice", "part time", "part-time",
    "specialist i ", "analyst i ", "co-op", "clerk",
]
# "associate" is junior unless it is Associate Director / Associate Partner.
ASSOCIATE_OK = ["associate director", "associate partner", "associate vice president"]

# Off-lane titles: sales/revenue ops (profile exclusion), frontline, clinical, plant floor.
OFF_LANE_TITLE_TERMS = [
    "sales", "account manager", "account executive", "revenue operations", "revops",
    "go-to-market", "gtm", "business development", "customer success",
    "nurse", " rn ", "physician", "clinical", "pharmacist", "therapist",
    "driver", "store ", "retail", "restaurant", "cashier", "barista",
    "warehouse", "forklift", "maintenance", "mechanic", "welder", "machinist",
    "plant ", "manufacturing", "production ", "quality engineer", "quality inspector",
    "cell therapy", "chemical", "packaging", "teacher", "coach ", "counselor",
    "software engineer", "developer", "devops", "site reliability",
    "product manager", "product owner", " ux ", "sox ", "audit", "case reviewer",
    "operator", " bd ", "credentialing", "tax ", "instructor",
    # discipline collisions from the handoff noise list
    "wastewater", "epoxy", "ceramic", "facilities", " mep ", "hvac", "industrial engineer",
    "supplier quality", "reliability engineer", "validation engineer", "process technician",
    "sales enablement", "sales operations",
]

# Titles naming a platform the author has not implemented -- flag, the gate may sit under Preferred.
PLATFORM_GATED_TERMS = ["servicenow", "s/4hana", "sap ", "coupa", "oracle health", "workday hcm", "salesforce", "pega"]

# Aggregators / reposters -- the listing is not the requisition; find the employer's posting.
AGGREGATOR_POSTERS = ["virtual vocations", "jobgether", "remotehunter", "swooped", "ladders", "actively hiring", "lensa", "cybercoders"]

# Listing locations that mean "nationwide" rather than a specific on-site city.
NATIONWIDE_LOCATIONS = {"us", "usa", "united states", "nationwide", "multiple locations", "anywhere"}

# USAJobs titles rarely carry the function; accept these only when the duties do.
FEDERAL_FUNCTION_TITLES = ["program analyst", "management analyst", "business process", "process improvement",
                           "change management", "transformation", "operations research", "performance improvement"]

# ITIL / ITSM change control -- ticket-queue work, not adoption (Change Management caution).
ITSM_CHANGE_TERMS = ["itil", "itsm", "change control", "change advisory", "cab ", "servicenow change", "release manager"]

# Discipline test for "Process Engineer" / CI Engineer hits: a process with a temperature,
# pressure, flow rate or plant floor is a different profession.
PLANT_DISCIPLINE_TERMS = [
    "manufacturing", "production line", "plant", "chemical", "semiconductor", "wafer",
    "yield", "gmp", "fda-regulated", "kaizen event", "tpm", "smed", "6s", "5s",
    "shop floor", "assembly", "machining", "extrusion", "molding", "refinery",
    "water treatment", "utilities", "biologics", "fill/finish", "cgmp", "process safety",
    "site leader", "plm", "fab ",
]

# Hard-avoid industries (profile): tobacco, gambling, manufacturing, industrial chemicals.
HARD_AVOID_INDUSTRY_TERMS = ["tobacco", "casino", "gambling", "sportsbook", "betting", "vape"]

# Clearance-gated titles -- flagged, since an active clearance is required.
CLEARANCE_TERMS = ["ts/sci", "top secret", "secret clearance", "active secret", "security clearance", "polygraph", "clearance required"]

# Hard skips -- fraud / assessment-gated / AI-data gig posters. Match on company name.
BLOCKED_POSTERS = {
    "crossover": "assessment-gated employer (CCAT on all applicants)",
    "crossing hurdles": "unverified poster -- likely fraudulent, hard skip",
    "impel-consultants": "confirmed fraudulent poster -- verified 2026-09-10",
    "impel consultants": "confirmed fraudulent poster -- verified 2026-09-10",
    "fulchester": "Advisor/Coach fraud template",
    "fitpoint talent": "Advisor/Coach fraud template",
    "ethos": "AI-data gig, not an employed or consulting engagement",
    "mercor": "AI-data gig, not an employed or consulting engagement",
    "outlier": "AI-data gig, not an employed or consulting engagement",
    "alignerr": "AI-data gig, not an employed or consulting engagement",
    "micro1": "AI-data gig, not an employed or consulting engagement",
}
AI_GIG_TITLE_TERMS = ["ai trainer", "train ai", "ai training", "expert opportunity", "data annotation"]

# ---- Personal settings: NOT in the repo. ----
# Pay thresholds and home location are set in backend/profile_local.py (gitignored).
# Copy backend/profile_local.example.py to backend/profile_local.py and fill it in.
# With no local file, pay and commute rules are simply skipped.
COMP_FLOOR = None          # reject when a posted band's TOP is under this (annual)
COMP_ASK = None            # small score bonus when the band top reaches this
HOURLY_ANNUALIZE = 2_000   # hourly x this = annual
HOME = None                # e.g. "Springfield, IL"; used for local search lanes
LOCAL_RADIUS_KM = 80
COMMUTABLE_PLACES = []     # lowercase place names that count as commutable

REMOTE_TERMS = ["remote", "work from home", "telework", "anywhere in the u", "virtual"]

# Seniority words that earn a small bonus (Principal IC primary; Sr Mgr/Dir JD-gated).
SENIOR_TITLE_TERMS = ["principal", "senior", "sr.", "sr ", "lead", "director", "head of", "manager", "vice president", "vp", "staff"]

# Faith-based / mission-positive signals (comp floor differs, flag for review).
FAITH_SIGNALS = ["ministry", "ministries", "christian", "church", "gospel", "bible", "faith-based"]


# Personal overrides, kept out of git.
try:
    from backend.profile_local import *  # noqa: F401,F403
except ImportError:
    pass
