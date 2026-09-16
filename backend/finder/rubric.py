"""The judge's rubric (sprint plan §10, §15.5, §16.8).

`RUBRIC_PUBLIC` and `RUBRIC_LANE` are committed and contain nothing personal: the grading scale, the output
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

OUTPUT: one JSON object per posting, nothing else:
{"posting_id": "<id>", "grade": "bullseye|adjacent|stretch|wrong", "lane": "primary|secondary|wrong",
 "confidence": "high|medium|low", "blocker": "<the single biggest gap, or empty>",
 "rationale": "<one sentence naming the actual work, not the title>"}
"""

# Neutral target description, safe to send on a free API tier (§16.8 `public` profile).
RUBRIC_LANE = """
TARGET FUNCTION (neutral description)
- Primary: enterprise operations process excellence — process improvement, operational excellence, business
  process optimization, global process ownership, business transformation, Lean Six Sigma, continuous
  improvement, operating-model design, change management in the adoption-and-ownership sense.
- Equally primary: workforce / capacity / demand modelling, operations forecasting, and the analytics and data
  work that produces an operating decision. This is NOT a lesser lane and carries no grade cap. Judge analytics
  roles by the OBJECT of the analysis — operations decisions are primary; marketing, product or risk analytics
  are `adjacent`.
- Engineering-titled roles (data / analytics / BI engineer) are decided by the STACK and the expectations in the
  JD, never by the title. Building models, pipelines and analysis on a familiar stack is in lane; owning the
  platform — streaming infrastructure, cloud build-out, CI/CD, ML model engineering — is not.
- Wrong: plant-floor / manufacturing continuous improvement, product operations, sales or revenue operations,
  IT service-management change control, and roles whose real requirement is industry tenure.
"""


def rubric_version(extra: str = "") -> str:
    """Changes when any prompt text changes, so relabelled rows are distinguishable."""
    from . import requirements
    personal = globals().get("RUBRIC_PERSONAL", "")
    payload = RUBRIC_PUBLIC + RUBRIC_LANE + personal + "|".join(GRADES) + requirements.splitter_fingerprint() + extra
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


try:  # personal prose, gitignored
    from backend.finder.rubric_local import *  # noqa: F401,F403
except ImportError:
    pass
