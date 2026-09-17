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

THREE LENSES. Grade each posting on all three, independently, against the lens descriptions below:
- grade_process   : how well the role matches the PROCESS / OPERATING-MODEL / CHANGE-MANAGEMENT lens
- grade_technical : how well it matches the TECHNICAL / DATA / ANALYTICAL lens
- grade_ai        : how well it matches the APPLIED-AI lens

Use the same four grades for all three. MOST ROLES ARE STRONG ON AT MOST ONE LENS, and a low score on the
other lenses is the normal, correct answer -- it is a statement about the ROLE, not a criticism of the
candidate. Do not inflate a weaker lens to be generous, and do not deflate one because the role is strong on
another. A role that genuinely demands more than one lens is rare and valuable, so record it honestly when you
see it. The overall positioning decision still comes from the process and technical lenses; `grade_ai` is
reported beside them, never folded into that decision.

A LENS WITH NO RELEVANT CONTENT IN THE POSTING IS `wrong` ON THAT LENS, NOT `stretch`. `stretch` requires
genuine partial overlap that you can NAME in your rationale. If you cannot point to the overlapping work, the
grade is `wrong`. Do not use `stretch` as a polite middle when the posting simply contains nothing for that
lens -- an HR business-partner role with no analysis in it is `wrong` on the technical lens, and a pure data
pipeline role with no process ownership in it is `wrong` on the process lens. Hedging destroys the signal: it
is the difference between "this role needs both capabilities" and "I was unsure".

OUTPUT: one JSON object per posting, nothing else:
{"posting_id": "<id>", "grade_process": "bullseye|adjacent|stretch|wrong",
 "grade_technical": "bullseye|adjacent|stretch|wrong", "grade_ai": "bullseye|adjacent|stretch|wrong",
 "lane": "primary|secondary|wrong", "confidence": "high|medium|low",
 "blocker": "<the single biggest gap, or empty>",
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

RUBRIC_LENS_AI = """
LENS 3 — APPLIED AI (grade_ai)
- `bullseye`: applied-AI delivery and enablement -- designing and shipping agentic workflows around a model
  (skills / procedures, memory, deterministic tool calls for the non-judgement steps), evaluation and
  regression discipline for knowledge work (reference sets, LLM-as-judge with calibration against humans,
  acceptance thresholds), human-in-the-loop grounding and review gates, model routing and token/cost judgement
  (tiering, fallback, graceful degradation), driving adoption -- teaching teams to build and use AI
  assistants inside an approved platform -- and EVALUATING AI / automation / RPA opportunities: deciding which
  workflows are ready, building the business case and the measurement baseline (hours and cost with and
  without the automation), and prioritising by value and feasibility. A process-transformation role whose
  Required block asks for AI/automation opportunity evaluation and business cases is a bullseye here, not
  merely adjacent: the judgement about where a model belongs IS the applied-AI work.
- `adjacent`: AI programme / transformation / governance leadership where the work above is directed rather
  than done and no evaluation or delivery duty is named; analytics or process roles where "AI" appears as a
  general expectation with no opportunity assessment, delivery or adoption duty behind it.
- `stretch`: named overlap only (e.g. prompt design or RAG configuration as a minor duty inside a role that is
  otherwise something else).
- `wrong`: building models (ML / NLP / RAG engineering, fine-tuning, model research), AI platform or
  infrastructure engineering, distributed services in Python as the job, AI security threat modelling as a
  primary duty, and "AI" as marketing vocabulary with no AI work in the duties. ALSO `wrong`: an engineering
  seat whose Required block asks for production code (Python / TypeScript), years of software or ML
  engineering, or building and deploying LLM / RAG / agent applications to production as core qualifications
  -- a forward-deployed or solutions engineer, however much the duties also mention evaluation suites,
  workflow mapping or adoption. Name that overlap in the rationale; do not credit it as `stretch` or
  `adjacent`. The Required block decides the seat; the duties describe it.
- The line is CREATING or ENGINEERING the AI system versus TEACHING an existing model to perform a job:
  skills, procedures, evals, tool calls and adoption around a model are applied AI; writing the production
  application or the model is engineering.
- Judge the duties and the Required block, not the title; "AI" in a title is not evidence.
"""

def rubric_version(extra: str = "") -> str:
    """Changes when any prompt text changes, so relabelled rows are distinguishable."""
    from . import requirements
    personal = "".join(globals().get(k, "") for k in
                       ("RUBRIC_PERSONAL", "RUBRIC_PERSONAL_PROCESS", "RUBRIC_PERSONAL_TECHNICAL",
                        "RUBRIC_PERSONAL_AI"))
    payload = (RUBRIC_PUBLIC + RUBRIC_LENS_PROCESS + RUBRIC_LENS_TECHNICAL + RUBRIC_LENS_AI + personal
               + "|".join(GRADES) + requirements.splitter_fingerprint() + extra)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


try:  # personal prose, gitignored
    from backend.finder.rubric_local import *  # noqa: F401,F403
except ImportError:
    pass
