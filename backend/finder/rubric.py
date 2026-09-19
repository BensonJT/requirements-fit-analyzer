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
              bullseye. Judge the verbs and the objects of the work, not the sector -- unless the sector's
              specialist knowledge IS the work (see DOMAIN AS THE SETTING vs DOMAIN AS THE WORK below).
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

DOMAIN AS THE SETTING vs DOMAIN AS THE WORK
- An unfamiliar industry is never a gap when it is the SETTING: the duties would read the same with the
  industry nouns removed (process ownership inside a health plan is still process ownership).
- It IS a gap when practitioner knowledge of a specialism is itself the WORK: the duties cannot be performed
  without it (applying risk-adjustment or actuarial models, interpreting regulatory filing rules, clinical
  criteria, audit standards, compensation market pricing, front-office investment data), or the Required block
  asks for that expertise rather than for tenure. A licence or credential that gates practice (CPA, RN,
  actuarial) signals this.
- TEST: strike the industry nouns from the duties. If the candidate's kind of work remains, grade that work and
  name the tenure as the blocker. If little remains beyond leadership and governance vocabulary, the grade is
  `stretch` at best, and `wrong` when you cannot name the overlapping work.

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

REQUIRED-BLOCK FIT -- A SECOND, SEPARATE QUESTION. Answer it AFTER the three lens grades and do NOT let it
change them: the lens grades say what KIND of work the role is; this says whether the candidate would clear the
posting's own stated requirements.
- Find every line that states a required QUALIFICATION -- years of experience in something, a named tool or
  platform, a credential, a track record -- wherever it appears (some postings put them under a role heading, and
  some lines tagged [required] are boilerplate or duties: ignore those). Ignore soft skills and generic degree lines.
- Mark each one against the candidate record:
    met    : the record shows it, OR the line is a list of alternatives ("program management, product management,
             service delivery, or a related field") and the record satisfies any one of them.
    partly : the same kind of experience but short on years; a named tool that is light in the record while the
             skill behind it is strong; an adjacent function; industry-only tenure ("experience in healthcare").
    unmet  : years of experience IN a business function, domain, specialism or platform practice that is not in the
             record (HR operations, claims, capital planning, content strategy, a vendor platform's implementation
             practice, leading software-engineering teams), a gating licence, or people-leadership scale well
             beyond the record.
- required_fit is the overall call:
    `meets`    : every qualification line is met or partly, and the central experience line is met.
    `arguable` : the central experience line is met or partly, with one or two unmet lines that are not the core
                 of the seat, or several partly lines. A recruiter could be persuaded.
    `fails`    : the central experience requirement -- usually the first years-of-experience line -- is unmet, or
                 most qualification lines are unmet. Be strict: a confident candidate reading this block would
                 say "they are not looking for me".
- If the posting states no real qualifications, required_fit is `arguable` and say so.

OUTPUT: one JSON object per posting, nothing else:
{"posting_id": "<id>", "grade_process": "bullseye|adjacent|stretch|wrong",
 "grade_technical": "bullseye|adjacent|stretch|wrong", "grade_ai": "bullseye|adjacent|stretch|wrong",
 "lane": "primary|secondary|wrong", "confidence": "high|medium|low",
 "blocker": "<the single biggest gap, or empty>",
 "required_fit": "meets|arguable|fails",
 "required_unmet": "<the unmet qualification lines, quoted briefly and separated by ' ; ', or empty>",
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
- ALSO `wrong`: RUNNING one function's operations -- the seat is accountable for that function's own daily
  output, staffing, vendors and budget (creative or marketing operations, desktop support or a service desk,
  application / ERP support operations, a compensation or payroll cycle). Intake, workflow, governance,
  resource-planning and continuous-improvement vocabulary inside such a seat describes how that manager runs
  the shop; it is not process-excellence work. TEST: is the role accountable for CHANGING how work is done,
  across teams it does not run (this lens), or for DELIVERING one function's work (`wrong`, or `stretch` when
  a named improvement mandate is a real share of the duties)? A process-management or transformation team is
  itself this lens: leading process managers, or owning a framework other teams run on, is not "running a
  function" in this sense.
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
- ALSO `wrong`: analytics, reporting or modelling IN SERVICE OF a specialism where the specialist knowledge is
  the work (DOMAIN AS THE WORK above: risk, compliance, clinical or utilization analytics, actuarial work, an
  enterprise data-governance programme). The `adjacent` line above covers a different business OBJECT
  (marketing, product, commercial); it does not cover a specialist discipline's own analytics.
- FINANCE-PROFESSION SEATS: "budget and financial modelling" is bullseye when it models an OPERATION (its
  capacity, workforce, cost-to-serve, the business case for a change). A seat inside the finance profession --
  owning the FP&A planning cycle, finance business-partnering, controllership, investor relations, corporate
  strategy or M&A evaluation -- is `stretch` at best, and `wrong` when the Required block asks for experience
  in a finance function.
- A modelling or reporting duty that is a minor share of a seat accountable for RUNNING one function's
  operations is `stretch`, not `adjacent` or `bullseye`: grade the centre of gravity of the seat.
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
- ALSO `wrong`: a technologist, architect, scientist or subject-matter-expert seat whose Required block asks
  for deep AI / ML technical expertise as a core qualification (years in AI / ML technology, NLP, computer
  vision, deep learning, AI / ML architecture), even when no production code is named and the duties include
  use-case evaluation or business cases. If your own `blocker` would say that expertise is missing from the
  record, the grade cannot be `adjacent` or `bullseye`.
- AI INSIDE A SPECIALIST FUNCTION: DOMAIN AS THE SETTING vs DOMAIN AS THE WORK applies to this lens too. An
  AI programme, execution or strategy seat that sits inside one specialist function (investment management,
  marketing, FP&A, risk, security) and whose Required block asks for practitioner experience IN that function
  is that function's work with AI as its object: `stretch` at best, `wrong` when the AI duties are directed
  programme management. An enterprise AI adoption, transformation or portfolio seat that merely happens to be
  at an asset manager, a bank or a health plan is the SETTING and is graded on the AI work as usual.
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
