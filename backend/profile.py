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

# ---- Finder (backend/finder/): JD-text rules and scoring. Neutral structure, no personal values. ----
# Rule points feed rule_score (0-100). See docs/SPRINT_PLAN.md section 4.2.
# Weighted mean of the signals present (weights renormalized over what exists).
# ---- Final score (sprint plan §14). screens.rule_score holds the profile score -- level, location, pay and
# title weighted on 0-100 by the four non-content weights below, renormalized (profile_score drops "content").
# final = content and profile blended by "content" vs 1 - "content".
# Content (the fit model) dominates the rank. Location, pay, level and country are GATES at Level 1 -- a
# posting that survives has already passed them, so spending half the rank on them prices them twice. Measured
# 2026-09-15 over 720 graded survivors with out-of-fold fit: rule_score AUC 0.444 (mildly ANTI-correlated with
# fit, because pristine ATS metadata skews to big-corporate senior-generalist reqs), and sweeping this weight
# gave AUC 0.537 at 0.50 -> 0.589 at 0.90, precision@50 0.84 -> 0.90. Held at 0.90 rather than 1.00: level and
# title gradations that the gates let through are still worth a tiebreaker, and 0.90 -> 1.00 was worth 0.003.
SCORE_COMPONENT_WEIGHTS = {"content": 0.90, "level": 0.20, "location": 0.15, "pay": 0.10, "title": 0.05}
LEVEL_POINTS = {"senior": 100, "mid": 50, "unknown": 60}
LOCATION_POINTS = {"remote": 100, "commutable_hybrid": 80, "commutable_onsite": 70, "nationwide_unverified": 60}
PAY_POINTS = {"at_ask": 100, "at_floor": 75, "not_posted": 60}   # unposted pay is unknown until a screen, not bad
TITLE_POINTS = {1: 100, 2: 70, 3: 50, None: 30, "off_lane": 0}
FLAG_PENALTY = 5           # points off the final score per flag
FLAG_PENALTY_CAP = 25
# Flags that stay visible (and still send a posting to review) but cost no points, because a component
# already prices them (pay, location, level, content) or they are not a negative (faith signal).
UNPENALIZED_FLAG_PATTERNS = [r"^\$[\d,.]+K ask sits above", r"^local/hybrid", r"^nationwide listing", r"^mid level",
                             r"^few years asked", r"^content fit borderline", r"^faith-based signal"]
FIT_REJECT = 0.35          # content gate: a scored JD below this is rejected, whatever the title says
FIT_REVIEW = 0.50          # a scored JD below this is flagged for review
NO_CONTENT_CAP = 60        # no JD or no model: the profile alone cannot make a posting strong
SCORE_BANDS = [(85, "very_strong"), (70, "strong"), (50, "partial"), (30, "weak"), (0, "none")]
LLM_BLEND = 0.5
TIER_CAP = {3: 80}

# Tier 3: the secondary data / analytics lane, matched on title when no function term hits.
DATA_LANE_ENABLED = True
DATA_LANE_TERMS = ["business intelligence", " bi ", "data engineer", "analytics engineer", "data model", "reporting analyst",
                   "operations analyst", "data analytics", "insights"]
# Tier 3 only: a coding test or algorithmic take-home closes the secondary lane.
CODING_TEST_TERMS = ["hackerrank", "codility", "codesignal", "coding assessment", "coding challenge", "coding test",
                     "live coding", "technical assessment", "take-home assignment", "take-home exercise"]

# Business-process vocabulary that outweighs plant vocabulary in the discipline test.
LANE_PROCESS_TERMS = ["handoff", "hand-off", "approval", "decision rights", "cycle time", "sla", "intake", "workflow",
                      "stakeholder", "cross-functional", "service level", "process map", "value stream"]
# Employers that put every applicant through a timed cognitive test.
ASSESSMENT_GATE_TERMS = ["ccat", "cognitive aptitude", "criteria corp", "aptitude test", "cognitive assessment"]
# Sales / revenue operations scope. Case-insensitive except the bare acronym CRO. "pipeline" only in its
# sales sense, so data pipelines and talent pipelines do not trip it.
SALES_OPS_PATTERN = (r"(?i:\b(?:gtm|go-to-market|quota|cpq|sales enablement|revenue operations|chief revenue officer"
                     r"|(?:sales|revenue|deal|opportunity) pipeline|pipeline (?:management|generation|coverage))\b)|\bCRO\b")
LARGE_TEAM_MARKERS = ["performance reviews", "headcount growth", "build the org", "scale the team", "build and scale",
                      "hiring plan", "build the practice", "grow the team"]
BOILERPLATE_PATTERNS = [r"equal opportunity employer.*", r"eeo statement.*", r"benefits?:.*", r"about (us|the company).*",
                        r"reasonable accommodation.*"]   # applied per line/paragraph
# Heading lines that open and close the Required block of a JD ("requirements" / "qualifications" added:
# they are the two most common plain-text headings in the corpus).
REQUIRED_HEADINGS = (r"required|requirements|basic qualifications|minimum qualifications|qualifications|what you.ll need"
                     r"|must have|what you have|what you bring")
PREFERRED_HEADINGS = r"preferred|nice to have|desired|bonus|sets you apart"

# ---- Level (finder). The pay band and the most years any JD line asks for decide level; a title decides
# it only when the word is unambiguous. "Associate" / "assistant" are deliberately absent: Associate
# Director is senior at one employer and Associate is entry level at another.
LEVEL_YEARS_SENIOR = 8     # the JD's highest "N+ years ... experience" at or above this = senior
LEVEL_YEARS_MID = 5        # below this = junior; between the two = mid
LEVEL_YEARS_CAP = 25       # larger numbers are company history ("100 years serving clients"), not a requirement
SENIOR_LEVEL_TITLE_TERMS = ["director", "vice president", "vp", "svp", "evp", "principal", "head of", "chief"]
EARLY_CAREER_TITLE_TERMS = ["intern", "internship", "summer associate", "co-op", "apprentice", "apprenticeship",
                            "new grad", "graduate program", "entry level", "entry-level", "junior", "jr"]

# ---- Outside the US (finder hard reject). A location segment naming one of these, with no US state or
# "United States" in any segment, is outside the US. Place names that are also common US places with no
# state attached (Dublin, Paris, Athens, Birmingham, Manchester, Cambridge, Georgia, Jersey, Jordan) are left out.
NON_US_TERMS = [
    "india", "canada", "united kingdom", "uk", "england", "scotland", "wales", "ireland", "germany", "france", "spain",
    "portugal", "italy", "netherlands", "belgium", "switzerland", "austria", "poland", "czech republic", "czechia",
    "hungary", "romania", "bulgaria", "greece", "sweden", "norway", "denmark", "finland", "israel", "turkey",
    "türkiye", "egypt", "south africa", "nigeria", "kenya", "morocco", "united arab emirates", "uae", "qatar",
    "saudi arabia", "kuwait", "bahrain", "oman", "pakistan", "bangladesh", "sri lanka", "china", "hong kong",
    "taiwan", "japan", "south korea", "korea", "singapore", "malaysia", "indonesia", "thailand", "vietnam", "viet nam",
    "philippines", "australia", "new zealand", "mexico", "brazil", "argentina", "chile", "colombia", "peru",
    "costa rica", "guatemala", "dominican republic", "maldives", "luxembourg", "slovakia", "serbia", "croatia",
    "ukraine", "lithuania", "latvia", "estonia", "ghana", "uruguay", "ecuador",
    "bengaluru", "bangalore", "hyderabad", "chennai", "mumbai", "pune", "noida", "gurugram", "gurgaon", "new delhi",
    "delhi", "kolkata", "ahmedabad", "kochi", "coimbatore", "toronto", "mississauga", "vancouver", "montreal",
    "montréal", "calgary", "ottawa", "london", "são paulo", "sao paulo", "mexico city", "guadalajara", "monterrey",
    "bogota", "bogotá", "buenos aires", "krakow", "kraków", "warsaw", "prague", "budapest", "bucharest", "munich",
    "frankfurt", "amsterdam", "madrid", "barcelona", "lisbon", "zurich", "geneva", "stockholm", "copenhagen", "oslo",
    "helsinki", "tel aviv", "dubai", "abu dhabi", "doha", "riyadh", "jeddah", "cairo", "johannesburg", "cape town",
    "nairobi", "lagos", "kuala lumpur", "jakarta", "bangkok", "phuket", "bali", "manila", "taguig", "cebu",
    "ho chi minh", "hanoi", "taipei", "tokyo", "osaka", "seoul", "shanghai", "beijing", "shenzhen", "hangzhou",
    "guangzhou", "sydney", "melbourne", "brisbane", "auckland",
]

# ---- Fit model vocabulary. The model scores function and content; level, logistics and career-site page
# text are the rules' job, and letting the model learn them teaches it where a JD was copied from.
MODEL_STOP_WORDS = [
    # level words (level comes from years + pay)
    "associate", "associates", "assistant", "senior", "sr", "junior", "jr", "intern", "internship", "entry", "level",
    "director", "directors", "manager", "managers", "vp", "vice", "president", "principal", "lead", "head", "chief",
    "executive", "staff", "officer", "years", "year", "yrs",
    # logistics (location, pay, schedule)
    "remote", "hybrid", "onsite", "site", "office", "location", "locations", "relocation", "travel", "salary",
    "pay", "compensation", "range", "base", "bonus", "hourly", "annual", "time", "week", "days",
    # career-site page and benefits boilerplate
    "benefits", "benefit", "career", "careers", "candidate", "candidates", "applicant", "applicants", "apply",
    "application", "applications", "posting", "requisition", "req", "policy", "policies", "eeo", "equal",
    "opportunity", "employer", "disability", "disabilities", "veteran", "veterans", "accommodation",
    "accommodations", "discrimination", "race", "religion", "gender", "sexual", "orientation", "national", "origin",
    "protected", "status", "age", "medical", "dental", "vision", "pto", "vacation", "holidays", "parental", "leave",
    "retirement", "wellness", "perks", "click", "job", "jobs", "hiring", "hire", "offer",
    # EEO / privacy / recruiting-notice words that survive line stripping
    "regard", "regardless", "applicable", "color", "identity", "email", "personal", "considered", "consideration",
    "recruiting", "recruiter", "recruiters", "recruitment", "law", "laws", "eligible", "eligibility", "employee",
    "employees", "xa", "nbsp", "amp",
]
# A JD line naming two or more of these is EEO / privacy / benefits / recruiting-notice text and is dropped
# before the model reads the JD (career-site pages carry it; JDs pasted into notes usually do not).
BOILERPLATE_MARKERS = [
    "equal opportunity", "equal employment", "affirmative action", "race", "color", "religion", "national origin",
    "sexual orientation", "gender identity", "disability", "veteran", "protected", "applicable law",
    "reasonable accommodation", "e-verify", "background check", "drug test", "privacy", "personal information",
    "pay transparency", "salary range", "base pay", "benefits", "401(k)", "paid time off", "dental", "parental leave",
    "recruiting fraud", "recruitment fraud", "unsolicited", "staffing agencies", "fraudulent", "eligible",
]

# ---- Personal settings: NOT in the repo. ----
# Pay thresholds and home location are set in backend/profile_local.py (gitignored).
# Copy backend/profile_local.example.py to backend/profile_local.py and fill it in.
# With no local file, pay and commute rules are simply skipped.
COMP_FLOOR = None          # reject when a posted band's TOP is under this (annual)
COMP_ASK = None            # small score bonus when the band top reaches this
HOURLY_ANNUALIZE = 2_000   # hourly x this = annual
HOME = None                # e.g. "Springfield, IL"; used for local search lanes
LOCAL_RADIUS_KM = 80
COMMUTABLE_PLACES = []     # lowercase place names; "name, st" pins the US state, "name, st?" also accepts no state

TRAVEL_MAX_PCT = None      # travel percent limit; None skips the travel rule
MAX_DIRECT_REPORTS = None  # direct-report limit; None skips the team-size rule
DOMAIN_TENURE_TERMS = []   # industries where "N+ years in <industry>" is a gate worth flagging
CORRIDOR_PLACES = []       # places where a Required-block plant term means a manufacturing role; "name, st" pins the state
FAITH_COMP_FLOOR = None    # comp floor when FAITH_SIGNALS fire

# Posting ids (or "employer|title") the user has read and called wrong-function: coverage calibration negatives
# (sprint plan §16.1). A calibration input, exempt from the rules version.
AUDIT_NEGATIVES = []

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
