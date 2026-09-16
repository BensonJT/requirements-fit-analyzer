"""The judge's rubric (sprint plan §10, §15.5, §16.8).

`RUBRIC_PUBLIC` and the two `RUBRIC_LENS_*` blocks are committed and contain nothing personal: the grading scale, the output
contract, and a neutral description of the target function. `rubric_local.py` (gitignored) supplies
`RUBRIC_PERSONAL`, the candidate's own profile prose, and is only sent on the `personal` prompt profile
(paid tier or a local model) or in Claude Code batch files, which never leave the machine.
"""
import hashlib

GRADES = ("bullseye", "adjacent", "stretch", "wrong")

RUBRIC_PUBLIC = """
You grade one question only: IS THIS THE SAME KIND OF WORK THE CANDIDATE HAS DONE?

Not whether they would get the job. Not whether the pay, the location, the seniority or the travel suit them —
a separate rule engine already decided all of that, and weighing it here corrupts the label.

GRADES
- bullseye  : the core of the role is work the candidate has demonstrably done. A different industry is NOT a
              gap: if the JD asks for work they have done, in a domain they have not worked in, it is still a
              bullseye. Judge the verbs and the objects of the work, not the sector.
- adjacent  : substantially overlapping work with one real difference in the object of the work (a different
              function's process, a specialism they have touched but not owned). They could do it; someone with
              direct experience would be preferred.
- stretch   : some genuine overlap, but the centre of gravity of the role is something they have not done.
- wrong     : fundamentally different work. Shared vocabulary ("transformation", "governance", "operational
              excellence") is NOT overlap when the actual work is a different discipline.

RULES
- Judge the REQUIREMENTS and RESPONSIBILITIES text supplied, not the title. Titles mislead in both directions.
- A missing certification is never by itself worse than `adjacent`; name it as the blocker instead.
- Industry tenure requirements ("10+ years in banking") are a blocker to name, not a reason for `wrong`.
- If the supplied text is too thin to judge, grade `stretch` and set confidence "low".
- Be decisive. A corpus where everything is `adjacent` is useless.

TWO LENSES. Grade each posting TWICE, independently, against the two lens descriptions below:
- grade_process   : how well the role matches the PROCESS / OPERATING-MODEL / CHANGE-MANAGEMENT lens
- grade_technical : how well it matches the TECHNICAL / DATA / ANALYTICAL lens

Use the same four grades for both. MOST ROLES ARE STRONG ON AT MOST ONE LENS, and a low score on the other lens
is the normal, correct answer -- it is a statement about the ROLE, not a criticism of the candidate. Do not
inflate the weaker lens to be generous, and do not deflate it because the role is strong on the other. A role
that genuinely demands both is rare and valuable, so record it honestly when you see it.

OUTPUT: one JSON object per posting, nothing else:
{"posting_id": "<id>", "grade_process": "bullseye|adjacent|stretch|wrong",
 "grade_technical": "bullseye|adjacent|stretch|wrong", "lane": "primary|secondary|wrong",
 "confidence": "high|medium|low", "blocker": "<the single biggest gap, or empty>",
 "rationale": "<one sentence naming the actual work, and which lens it lands on>"}
"""

# Neutral lens descriptions, safe to send on a free API tier (§16.8 `public` profile).
RUBRIC_LENS_PROCESS = """
LENS 1 — PROCESS / OPERATING MODEL / CHANGE MANAGEMENT (grade_process)
- `bullseye`: process improvement, operational excellence, business process optimization, global process
  ownership, business transformation, Lean Six Sigma / Lean, continuous improvement, operating-model and
  process design, process governance and ownership, CHANGE MANAGEMENT in the adoption-and-ownership sense
  (stakeholder readiness, resistance, driving a new way of working into an organization), and designing the
  framework or operating routine for how work gets planned and run.
- `adjacent`: programme / project / portfolio management, business operations or chief-of-staff work where the
  real job is coordinating across functions, or consulting delivery of the above.
- `wrong`: plant-floor / manufacturing continuous improvement, product operations, sales or revenue operations,
  IT service-management change control (ITIL change tickets are NOT change management in this sense).
"""

RUBRIC_LENS_TECHNICAL = """
LENS 2 — TECHNICAL / DATA / ANALYTICAL (grade_technical)
- `bullseye`: quantitative modelling of a business -- capacity, workforce, demand, throughput, cost-to-serve;
  forecasting and scenario analysis; budget and financial modelling; the measurement baseline itself; building
  the data pipelines, reporting and dashboards an operating decision rests on.
- `adjacent`: senior analytics / BI / insights work whose object is something else (marketing, product, risk,
  commercial), or analytics governance and methodology validation.
- `wrong`: platform and software engineering as the job -- streaming or distributed infrastructure, cloud
  build-out, CI/CD and data-platform ownership, microservice or API product engineering -- and data-scientist
  seats whose work is building ML / NLP / RAG models.
- Judge the STACK and the expectations named in the JD, never the job title.
"""

def rubric_version(extra: str = "") -> str:
    """Changes when any prompt text changes, so relabelled rows are distinguishable."""
    from . import requirements
    personal = "".join(globals().get(k, "") for k in
                       ("RUBRIC_PERSONAL", "RUBRIC_PERSONAL_PROCESS", "RUBRIC_PERSONAL_TECHNICAL"))
    payload = (RUBRIC_PUBLIC + RUBRIC_LENS_PROCESS + RUBRIC_LENS_TECHNICAL + personal + "|".join(GRADES)
               + requirements.splitter_fingerprint() + extra)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


try:  # personal prose, gitignored
    from backend.finder.rubric_local import *  # noqa: F401,F403
except ImportError:
    pass
