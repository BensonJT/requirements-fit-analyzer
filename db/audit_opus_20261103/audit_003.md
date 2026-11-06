# Labeling batch audit_003 — grade the WORK, not the candidate's odds


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
 "confidence": "high|medium|low",
 "blocker": "<the single biggest gap, or empty>",
 "required_fit": "meets|arguable|fails",
 "required_unmet": "<the unmet qualification lines, quoted briefly and separated by ' ; ', or empty>",
 "rationale": "<one sentence naming the actual work, and which lens it lands on>"}


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


CANDIDATE RECORD (ground truth; grade the work against this, never against the title)
- Lean Six Sigma Black Belt. 15+ years of enterprise operations process excellence at a Fortune 20 telecom,
  plus an independent process-excellence practice. Works cross-functionally without positional authority, and
  has been "embedded from the outside" in eight unrelated domains: release management, ISO 9000 quality
  systems, capital and expense governance, IT asset management, privileged access security, network
  deployment, field workforce capacity, and applied AI. Entering an unfamiliar industry is the PATTERN, not a
  gap.
- He is a process excellence practitioner and a quantitative analyst in equal measure. Neither half is a
  footnote to the other. That is why the two lenses are graded separately: a role can be a bullseye on one and
  `wrong` on the other, and saying so plainly is the point.
- Agentic AI and AI-assisted tools -- Claude and Claude Code, OpenAI Codex, GitHub Copilot, Gemini CLI and
  Gemini Antigravity -- are used daily and in production work, and count on BOTH lenses. The use is broad, not
  just coding: reasoning partners for process analysis and decomposition, strategy development, and
  human-in-the-loop workflows over curated databases (for example agentic resume crafting driven by a skills
  and evidence database, which is process design and technical build at once). On the process lens: designing
  the human-in-the-loop workflow, the governance and the organizational adoption of AI tooling. On the
  technical lens: directing the tools against written specifications to ship software, pipelines and analysis.
  He governs their output with architecture decision records, a three-layer automated test suite and a human
  sign-off gate on an autonomous build loop; designed a tiered LLM architecture reserving paid API calls for
  synthesis; used AI-assisted consultation to cut a production model's runtime 25%. A JD asking for
  AI-assisted development, agentic tooling, LLM-assisted analysis, prompt design or human-in-the-loop AI
  workflow design is a match on whichever lens it sits, never a gap. This is the "operator who codes with
  agentic AI assistance" pattern. Distinct from building ML / NLP / RAG models as a data scientist, which is
  `wrong` on the technical lens.
- Domain-agnostic by design, on BOTH lenses. The process capability applies to anyone who has a process -- any
  industry, any business function -- and the technical capability travels the same way. An unfamiliar industry
  as the SETTING is never a reason to lower either grade. Where a JD makes deep industry tenure a hard
  requirement, name it as the blocker: their expectation is the obstacle, not his capability.
- The one exception is the public rubric's domain-as-the-work test. He enters a domain as the process and
  analytics practitioner, never as its specialist. Specialisms he has NOT practised, where the specialist
  knowledge is the job rather than the setting: clinical, utilization and care management; actuarial, risk
  adjustment and medical-loss-ratio work; regulatory filing interpretation; public-accounting audit standards;
  accounting close and controllership; compensation design and market pricing; front-office investment data;
  M&A deal integration; software QA / test engineering; risk management as a discipline in ANY industry
  (enterprise or operational risk, GRC, risk and control self-assessment, model risk, regulatory exam and
  audit management -- he has never worked close to it); enterprise data governance as a practice (metadata,
  lineage, catalog, stewardship, FAIR -- his governed databases are his own builds, not an enterprise data
  governance programme). A role whose duties ARE one of these is `wrong` ON ALL
  THREE LENSES, not `stretch`, however much governance, analytics, AI or continuous-improvement vocabulary
  surrounds it -- this list overrides the public rubric's "`stretch` at best" for these specialisms, and it
  covers the analytics, governance or AI programme that serves such a function. A process-owner, measurement
  or transformation role that merely SITS in one of these settings keeps its grade.
- RISK, COMPLIANCE AND SECURITY ARE `wrong` ON ALL THREE LENSES when they are the core of the job: risk
  management of any kind (enterprise, operational, technology, data, model or AI risk), compliance and
  regulatory-controls work, and information security / cybersecurity as a discipline (security architecture,
  threat modelling, security operations). This is `wrong`, not `stretch`, on process, technical AND applied-AI
  alike, including the analytics, governance or AI programme that serves such a function -- he has not
  practised it and does not want it. The same setting-vs-work guard applies: a process-excellence,
  measurement or transformation seat that merely reports into a risk, compliance or security organization
  keeps its grade. The "privileged access security" entry in his record is NOT security experience: he
  rescued a troubled migration of a security product with DMAIC and SQL data engineering and zero security
  knowledge. Never credit it toward a security requirement.
- NOT in the record (name as the blocker, never grade `wrong` for it alone): Prosci certification, PMP, Azure.
- Light in the record (cap that requirement's credit, name it as the blocker): Power BI, Tableau, Looker, Qlik,
  ThoughtSpot, AWS, Azure and GCP as solutions-engineering platforms (BigQuery as a warehouse is in the record;
  cloud work is otherwise light and task-driven), ServiceNow, CSM (holds PSM I from Scrum.org).
- NOT in the record, platform edition (name as the blocker, never `wrong` on its own): administering or
  configuring an enterprise BPM or workflow suite -- Appian, Pega, Nintex -- and Microsoft Power Automate. The
  depth is in the process standards, not in a named BPM product.
- People leadership: has led small teams (about 4 direct reports, roughly a year as Scrum Master). A JD asking
  for large-team management is a blocker to name, not a `wrong` grade on either lens.


PROCESS LENS -- what he has actually done (grade_process)
- Mapping and decomposing end-to-end processes (IDEF0, SIPOC, BPMN 2.0, value stream mapping); removing
  handoffs between teams that report to different leaders; standing up process ownership and governance where
  none existed; building the measurement baseline before the improvement case; DMAIC projects with verified
  savings; operating-model and work-area design.
- Change management belongs on THIS lens. ADKAR-based adoption work: stakeholder readiness, moving people
  through the change-acceptance curve, driving a new way of working into an organization and getting it owned.
  Won buy-in without positional authority repeatedly -- the signature of the whole record. A JD about
  organizational change management, adoption, enablement for a new operating model, or resistance management
  is a process-lens `bullseye`. ITIL / ITSM change control is not this and is `wrong`.
- Decomposing a business problem and designing the model that answers it is process work, and he has done it
  across unrelated domains. That breadth is the point: read these as evidence the capability is
  domain-portable, not as a workforce-analytics specialism.
  * Field service operations (a 1,000-person geographically distributed field workforce): decomposed
    automated-dispatch performance to its real drivers when a year of rule-tweaking had failed, designed the
    supply-to-demand capacity model and the service-territory structure underneath it, and restructured 41,326
    postal codes into 521 work areas across 50 territories in 2.5 months working with every front-line
    supervisor -- design, governance and adoption together. Generalizes to any organization that has to match a
    workforce or a capacity pool to demand.
  * A product line (the program office for a consumer broadband product): framed the five-year demand and cost
    problem for the product, designed the forecast and the what-if structure that the program office, its
    vendors and finance all ran on, and defined the service-interval KPI that made an SLA risk visible before
    it broke. Read this as operational and financial analytics in support of a product or service line --
    demand planning, budget forecasting, vendor cost structure and delivery commitments -- which is how it
    generalizes to any product, service or programme. This is not "product operations" in the launch,
    go-to-market or stage-gate sense, which remains `wrong`. It is also his only exposure to supporting a
    commercial product line, so treat product-line support as real but narrow and name it as the blocker when
    a JD wants a product-operations career. The other things he has productized are internal products: the
    models, pipelines, dashboards and tooling he built and then supported for the teams that used them daily.
  * Finance (budget and forecasting operations): reengineered the financial planning and budget cycle itself
    with DMAIC, cutting cycle time 70% -- process redesign applied to a finance operating routine.
  Designing the framework, the model or the operating routine is process-lens primary even when the role sits
  inside HR, Finance, Operations or any other department; the department is not the object of the work. Only
  running the staffing cycle (requisitions, headcount approvals, scheduling) is out of lane.
- His own SaaS product, built in his independent practice (process side): deep process engineering and Lean Six
  Sigma methodology translated into software requirements -- a thirty-year methodology encoded so it renders
  six process notations from one captured model, with a structural validation engine and a standards-faithful
  BPMN export.
- Built the operating standards two organizations still ran on: an ISO 9000 quality system for 24x7 production
  operations, and an enterprise release planning standard.


TECHNICAL LENS -- what he has actually done (grade_technical)
The through-line: he takes the decomposition, the design and the plan and builds it himself -- the data
engineering pipelines and the automated analytics that yield and deliver business intelligence through
dashboards. The same flagship projects appear on the process lens for their design work; here they count for
the build.
- Supply-to-demand ("force-to-load") capacity model for a 1,000-person geographically distributed field
  workforce: the underlying demand analytics per postal code that sized total demand per work area, then demand
  modelled by day, hour and shift against available supply; work priced in minutes using a modified PERT
  distribution instead of averages; surfaced $18-22M of capacity variance and $3-4M of avoidable overtime;
  iterated across four versions in three years with the team that used it daily.
- Five-year demand and budget forecast model for a product line (consumer broadband; field installation and
  repair volume): re-ingested sales projections whenever they changed and allocated volume down to market level
  through tunable logic, against a vendor contract whose pricing turned on minimum-volume commitments; 20+
  what-if simulations against a projected budget overrun, quantifying $2M of upward pressure from a mandated
  product mix. Generalizes to demand planning, capacity commitment and budget forecasting for any product,
  service line or programme.
- Financial forecasting and budget automation: $3B of demand against a $2.2B machine expense budget, carrying
  the committed budget and the working forecast as two separate views and tracking every intervention's net
  effect against both; automated the cycle to produce output that would have taken 4.5x the staff by hand.
- Production data pipelines and BI delivery: Python and SQL over Oracle milestone tables assembling thousands
  of daily rows into aligned durations with automated SPC control charts (replacing Minitab); the ingestion
  pipeline for an entire enterprise transformation portfolio feeding VP-level benefit reporting; a self-healing
  12-source market data pipeline into PostgreSQL with gap-fill and reconciliation, delivered as an integrated
  daily dashboard; an end-to-end validation pipeline reconciling ~10 million rows field by field between a
  legacy claims system and its replacement. Supplied the validated, transformed data architecture a data
  science team trained a prediction model on.
- The same SaaS product (technical side): shipped 18+ sprints of a multi-tenant product on Python, FastAPI,
  React and TypeScript over row-level-secured PostgreSQL, contributing to the architecture and directing an
  agentic AI assistant against written specifications.
- Workforce and capacity analytics and demand forecasting are `bullseye` here, including when the tooling is
  spreadsheets. Advanced Excel modelling is squarely in the record; do not treat it as beneath him.

PLATFORMS AND STACK ACTUALLY USED (weigh these on this lens; judge the stack, not the job title)
  * Languages and frameworks: Python, SQL, FastAPI, React, TypeScript, HTML/CSS.
  * Databases and warehouses: PostgreSQL, Oracle, MS SQL Server, GCP BigQuery, MS Access, DBeaver.
  * Analysis and visualization: Streamlit, Plotly, Grafana, Excel (advanced), Minitab, Visio, iGrafx.
  * Enterprise systems worked inside as data sources: IT asset / CMDB, SAP, PeopleSoft. Also Jira, Vercel.
  * A JD naming a stack he has used is evidence FOR the fit; one naming a stack he has not is a blocker to
    name, not by itself a `wrong` grade.

Engineering-titled roles (data engineer, analytics engineer, BI engineer, ETL developer) are decided here by
the stack and the expectations, never the title: the record's stack is `adjacent`, and `bullseye` when the
object of the work is an operating decision; dbt or Snowflake as the primary tool, Databricks, or Airflow at
platform scale is `stretch`; genuine platform or software engineering is `wrong`.


APPLIED-AI LENS -- what he has actually done (grade_ai)
- Chose the build tool by testing, not by reputation: built early app versions with three different AI coding
  tools inside an editor, then settled on an agentic CLI for daily work.
- Agentic build workflow (Forester, his own SaaS product): every sprint stage is assigned either to an
  autonomous agent build loop or to an interactive session, with the model and effort level chosen per stage
  so tokens are spent where judgement earns them. Governs the AI-written code with 30 architecture decision
  records, a three-layer automated test suite (unit, component, end-to-end), and a human sign-off gate on
  every stage of the loop. Written specifications drive the agent: a governed evidence pipeline was built by
  an agentic coding session against a binding written plan he wrote and amended himself.
- Context engineering: 19 written agent skills (co-authored with the agent) that every session follows, so the
  same steps and guardrails apply run to run, plus persistent memory of 100+ prior rulings and corrections so a
  correction made once becomes a standing rule (counts as of 2026-09-16). Deterministic steps are moved out of
  the model into plain code the skills call -- term extraction, record export, document conversion, a two-way
  markdown/document sync verified by round trip -- so the model spends tokens only on judgement. A five-layer
  extraction pipeline filters candidate terms in code before any model call, matching over 90% of a commercial
  tool's term list by coverage (not accuracy) before the model is asked to do anything.
- LLM APIs, routing and cost control: integrated multiple LLM providers directly, migrated a production term-
  classification job between providers to cut run time (say "about 5 minutes to about 2", never a multiplier).
  Designed a tiered architecture for a paid client's analytics platform -- a local model on client hardware for
  routine work, paid API calls reserved for synthesis, and the design fails loudly rather than silently
  escalating cost. Built a free-tier-resilient scoring lane that rotates across LLM APIs on rate-limit signals
  and defers cleanly when every quota is spent, rather than failing or paying.
- Evaluation -- lead with this; it is the strongest part of the record. Qualified candidate LLMs for production
  against a frozen, human-adjudicated reference set and versioned go/no-go bars before shipping any of them;
  production went to a free model at 93.3% sentiment-direction agreement against the reference set (that
  number is one model's own agreement rate, never a comparison used to knock a competitor's as-run result).
  Separately, paid Claude Code subagents to grade thousands of documents against a written rubric in resumable
  batches, with rows validated on import; the labels retrained a fit model to AUC 0.973, meaning a strong fit
  ranks above one graded wrong 97 times in 100 -- never "97% accurate". Benchmarked two model sizes as judges on
  the same document set before choosing the cheaper one, because the larger model was systematically more
  generous. Compared the judge against his own prior human decisions and excluded every contested case from
  training on both sides rather than letting either side win by default. Found a bug in the rubric itself, not
  the model, when one rule made an entire category impossible to grade a top fit; split the grading into two
  lenses and recovered the misrated documents -- the discipline behind this very lens split. Also profiled an
  AI agent's own proposed performance optimizations rather than accepting them on faith, using DMAIC to find
  the two root causes that actually governed a pipeline's runtime and cutting it substantially.
- Grounding and human in the loop: a content pipeline pairs a grounded knowledge base with generation and a
  mandatory human review gate before anything publishes -- grounded generation with a human gate, not an
  agentic pipeline. A separate spreadsheet-analysis tool follows a stated method of deterministic extraction
  first, the model only on structured output, and human review of anything that becomes a source of truth; run
  live against a real take-home, the tool confirmed a subset of defects found by hand and surfaced nothing
  missed, which is itself evidence for where a structural check's limits are. On a healthcare-claims project he
  documents explicitly what stays with the human: the data model, the comparison logic, the underlying method,
  and the interpretation of results.
- MCP and browser agents: configures and uses an in-editor browser-automation MCP tool and a browser-based
  research skill for sites that block scripts, handing verified documents to analysis skills -- configured and
  used, not built. A read-only MCP server over his own evidence database, with tools to search entries and
  check guardrails and exclusions, is a candidate build, not a shipped one.
- Data governance underneath the AI: a governed evidence database (over a hundred entries, dozens of contexts
  and projects, as of 2026-09-16) where every entry carries its basis, time window and what it may not claim,
  so AI analysis runs against one verified source; rules enforced in the schema itself (constraints, a lint
  view, exclusion pairs for shared figures, a verb audit) rather than in prompts alone; dozens of versioned SQL
  scripts drafted with an agent and reviewed before commit.
- Adoption and teaching: waited for an approved enterprise AI platform before any company code touched a model,
  then adopted it deeply and taught teammates to build their own assistants, spreading the practice from one
  reporting workflow into coding, debugging and executive communication. For a paid client, delivered usable
  analytics partway through a build and then taught the client to query the delivered database through an AI
  assistant rather than wait on reports.
- Evaluating automation and AI opportunities is squarely in the record even where the specific tool is not:
  the method is a measured baseline first -- time the task by hand and with the automation, convert the
  difference to hours at loaded wage rates, net the build and run cost -- then a go/no-go bar written before
  the pilot. He qualified candidate models exactly this way (frozen reference set, agreement thresholds set in
  advance) and profiled an agent's own optimisation proposals before accepting them. RPA and process-mining
  platforms themselves are NOT in the record: name them as the blocker, never grade `wrong` for them alone.
  Enterprise context: adopted an approved enterprise AI platform inside a Fortune 20 operations organisation in
  2025 and spread it across teams -- the adoption evidence is not only independent work.
- Boundary, restated from the lens definition: building models (ML / NLP / RAG engineering, fine-tuning, model
  research), AI platform or infrastructure engineering, distributed Python services as the job, and AI security
  threat modelling as a primary duty are `wrong` on this lens even when "AI" is in the title. No fine-tuning or
  model training beyond a classical statistical classifier is in the record, and no agent framework (LangGraph
  or similar) -- his "agents" are skills, subagents and loops inside an existing CLI product, not a framework he
  built. The coding-assessment constraint stays outside this rubric, as it does on the other two lenses.

## How to answer
Return ONE JSON object per posting below, as a JSON array, written to `audit_003.result.json`. No prose, no markdown fence.
Grade every posting in this file, on ALL THREE lenses. Use the posting_id exactly as given.

## Postings (20)

### 93595d5b16922dfdfc08
**GE Vernova — Lean and Quality Operations Leader**

- [required] Lean, project management, quality, and KPI management.
- [required] Strong operations background: Manufacturing, Logistics, Materials, Fulfillment, etc.
- [required] Prior leadership role or leadership experience in an influencing role.
- [required] Strong business acumen and cross-functional experience and awareness.
- [required] Clear, concise, and effective communicator across all levels of the organization, including executives
- [required] Strong alignment with GE Vernova culture and values: Innovation, Customers, Lean, One team, and Accountability
- [required] Comfortable working in a matrixed environment, influencing and motivating diverse teams to achieve a unified goal.
- [role] Lead operating cadences Lead the development and execution of the team’s weekly, monthly, quarterly, and annual meeting rhythms, including agenda design, content coordination, and follow-up on action items.
- [role] Track and report key performance indicators (KPIs) to measure team effectiveness and progress against goals.
- [role] Support the development and execution of action plans to address gaps and drive continuous improvement.
- [role] Partner with cross-functional teams to standardize shared processes, ensuring consistency and scalability.
- [role] Act as a change agent by planning, communicating, and supporting the adoption of new processes, tools, and behaviors.
- [role] Ensure team readiness and engagement through clear communication, training, and feedback loops.
- [role] Lean Operating Method Deployment Facilitate and maintain the monthly Community of Practice call and its content, which is a network of Lean leaders that share good practices to accelerate performance improvements in SQDC
- [role] Support the development of new Lean tools as the Business system develops.
- [role] Maintain updates to existing content and track revision changes as needed.
- [role] Manage content on the Lean Watt Page, ensuring relevant content is shared across the GE Vernova organization, providing visibility and information pertaining to the Lean Operating Method, company-wide Lean events, and Le
- [role] Kaizen Facilitation and Project Management Own the end-to-end planning, logistics, and reporting for all corporate Kaizen events, with a focus on driving impactful outcomes across the enterprise.

### 9a39b34971d0a1208102
**Salesforce — Forward Deployed Engineer (FDE) (Mid/Senior Level)**

- [required] Production experience required.
- [required] Ship real customer solutions, supported by best-in-class AI tools — Cursor, Claude, and Salesforce coding products like Vibes — embedded into your day-to-day workflow.
- [required] You'll work alongside Senior Forward Deployed Engineers and a Deployment Strategist.
- [required] You'll own real components, benefit from structured mentorship from people who've shipped agentic systems at scale, and have a clear path to leading complex deliveries yourself.
- [required] Our agentic AI platform is evolving in real time.
- [required] You'll work on capabilities most engineers won't touch for months — spanning Agentforce, data, platform, and headless architectures and your field insight will feed directly to the product teams building them.
- [required] You'll be expected to stay at the forefront, experimenting with emerging AI tools, models, and frameworks, and sharing what you learn with your team and customers.
- [required] What You'll Actually Be Building
- [required] Develop AI agents and experiences that take real actions for real organizations.
- [required] Our solutions are in production at some of the largest enterprises on the planet.
- [required] The work matters because the customers depend on it.
- [required] Design agent intelligence: prompts, reasoning, tool calls, and integration with customer systems, leveraging our Agentic AI platform and current LLM techniques.
- [required] Own technical components end-to-end: from architecture choice to deployed, validated, and observable in production.
- [required] Build and integrate data pipelines on Salesforce Data 360, Snowflake, Databricks, and customer data platforms.
- [required] Model the data, ship the pipeline, validate the output
- [required] Build and maintain agent performance dashboards and customer KPI reporting to track deployment health and business outcomes.
- [required] Contribute to proofs-of-concept and MVPs that move from sketch to deployable in days, not months.
- [required] Co-build alongside customers and partners, sharing best practices and enablement as you go.

### 5087ada9d3a5f51c76b9
**Airbnb — Principal, Strategic Finance & Analytics - Membership**

- [required] Highly proficient in business analytics.
- [required] You will need to independently extract, analyze, and synthesize large datasets.
- [required] Proven ability to influence cross-functional partners and drive alignment in a fast-paced, ambiguous environment where frameworks are still being built.
- [required] High ownership, strong judgment, and attention to detail.
- [required] Superb financial modeling skills and extensive experience with quantitative and qualitative financial analysis, including building business cases and ROI frameworks.
- [required] You enjoy working with fragmented data and insights and synthesizing them into a story with recommendations.
- [required] AI proficient or able to become AI proficient (Claude or Gemini).
- [role] As a Strategic Finance & Analytics Principal supporting Membership, you will partner closely with leaders across business operations, product, analytics, data science, and adjacent Strategic Finance teams to help build a
- [role] Strategic Insights: Conduct analytical and financial deep dives to inform and shape key decisions.
- [role] You should be comfortable both leading the work independently and partnering with the cross-functional teams to deliver a joint output.
- [role] Investment Decision Support: Build the business cases behind program investments.
- [role] Performance Management: Own performance measurement for Membership programs, including defining KPIs, setting goals and OKRs, and building reporting and forecasting that connect early signals to guest and host outcomes, 
- [role] Support the planning cadences (annual planning, long-range planning, and monthly or quarterly forecasts).
- [role] Program Economics and ROI: Develop and pressure-test the economics of programs, including incentive and benefit costs, guest and host response, and incremental impact.
- [role] Size the opportunity, model the returns, and frame tradeoffs so leadership can decide where to place bets and how large they should be.
- [role] Presentations to Leadership: Synthesize complex data into clear narratives and recommendations for senior leadership.
- [role] Influence cross functional stakeholders to drive alignment on high impact investment and operating decisions.
- [required] Excellent written and verbal communication skills with audiences at various levels.

### b05b1333beebaba1a07d
**SAIC — Senior Program Control Analyst**

- [required] Or more, depending on level) of experience in program/project control or financial analysis.
- [required] Financial discipline and attention to detail.
- [required] Proactive problem-solving and continuous improvement mindset.
- [required] Ability to communicate complex financial concepts clearly.
- [required] Proficiency with Excel and financial analysis tools.
- [required] Strong collaboration and stakeholder management.
- [role] Develop, maintain, and monitor program budgets, forecasts, and financial plans.
- [role] Track actual costs, labor, and material expenditures; analyze variances and provide actionable insights.
- [role] Support monthly program financial reviews, revenue recognition, and EAC updates.
- [role] Prepare and present financial reports for internal leadership and external stakeholders as required.
- [role] Ensure compliance with FAR, DFARS, CAS, internal policies, and customer contract requirements.
- [role] Partner with Program Managers to support decision-making, risk analysis, and resource planning.
- [role] Assist in preparing cost proposals, pricing models, and BOEs for new business and contract modifications.
- [role] Coordinate with accounting, contracts, procurement, and operations to ensure program alignment.
- [role] Support audits, internal reviews, and financial system data integrity efforts.
- [role] Identify process improvements to enhance program financial management effectiveness.
- [required] Ability to work in a fast-paced environment and manage multiple priorities.
- [required] Bachelor’s degree in Finance, Accounting, Business Administration, Economics, or related field.

### a075b2c7c08bdcae146b
**GitLab — Principal Technical Program Manager**

- [required] Proven experience leading technical program initiatives in product, engineering, data, or security environments, with demonstrated success delivering complex programs from inception through scaled adoption
- [required] Demonstrated ability to define strategy and create clarity within extreme ambiguity
- [required] Strong background in SaaS, cloud infrastructure, or DevOps technology companies, with ability to quickly master the GitLab platform and AI/ML initiatives
- [required] Proven experience influencing and driving impact at a division and company-wide scale, affecting market position and revenue
- [required] Excellence in cross-functional leadership, with experience partnering with engineering, product, security teams and engaging effectively with senior and C-level stakeholders
- [required] Outstanding communication skills, including asynchronous collaboration, executive reporting, and the ability to translate technical complexity into clear business impact
- [required] Track record of success in fast-paced, dynamic environments with evolving technical requirements and market opportunities
- [required] Strong facilitation and influence skills, with ability to drive consensus across diverse stakeholder groups while building trust through transparency and delivery excellence
- [required] Expertise in high-level stakeholder management, including masterful communication with Executive Boards and enterprise Customers
- [required] Collaborative leadership style aligned with GitLab's values, including respect, openness to feedback, and commitment to team success
- [required] Proven track record of redefining organizational operations and serving as an escalation point to resolve highly complex, enterprise-level challenges
- [required] Proven experience leading complex reliability programs across multiple engineering organizations
- [required] Welcome candidates from diverse backgrounds, including those with consulting, enterprise technology, or technical leadership experience who have owned outcomes and delivered transformational initiatives
- [required] Ability to act as a visionary leader, shaping R&D direction and fostering technical excellence across the organization
- [required] About the team: The Technical Program Management team sits within GitLab's R&D Program Management Office and drives product, engineering, data, and security initiatives across our AI-powered DevSecOps platform.
- [required] You will join our growing, distributed team of Technical Program Managers currently spanning North America and Europe.
- [required] We own the end-to-end delivery of complex, cross-functional programs including AI platform capabilities, enterprise customer initiatives, and strategic technology transformations.
- [required] As an all-remote team, we excel at asynchronous communication to coordinate across time zones while partnering closely with executive leaders and stakeholders to deliver business-critical outcomes.

### 0e29d4e3b09d606b291f
**Capital One — Business Director, Global Acceptance Strategy (Global Payment Network)**

- [role] You will lead a high performing team supporting their professional growth and development.
- [role] As a Business Director at Capital One, you will apply your strategic and analytical skills to major company challenges.
- [required] Performing quantitative analysis
- [required] Performing qualitative analysis
- [required] Performing people management
- [required] Performing project management
- [role] Business Director, Global Acceptance Strategy (Global Payment Network)
- [role] The Global Acceptance Strategy team is looking for a talented and experienced Business Director to partner with senior leaders and help set the strategic direction for our global acceptance strategy.
- [role] As a Business Director you will leverage your strong analytical rigor to shape and drive both short- and long-term strategic initiatives, ranging from “is this partnership a good opportunity” to “where should we focus ou
- [role] Lead to development and analysis of long-term and short-term global acceptance strategies
- [role] Evaluate major deal opportunities, and strategic growth initiatives
- [role] market research, and use internal data to support your projects.
- [role] Conduct deep-dive competitive research and market analyses to identify industry trends, benchmark network performance, and surface actionable business insights
- [role] Deliver executive-level narratives, strategic briefings, and recommendations to senior leadership
- [role] Translate complex business questions into structured recommendations while partnering with cross functional teams
- [role] Leadership: May manage and develop multiple analysts.
- [role] Strategic leadership : Develop business strategies that will drive growth, profitability, and competitive success for Capital One in the face of shifting consumer and regulatory demands
- [role] Execution: Manage and sequence delivery of business intent, build business requirements and execute against the strategy.

### 905570ce533aa8962867
**Oracle — Senior Program Manager – OCI Business Operations Analytics & Insights**

- [required] Experience managing analytics, business reporting, or data-driven projects involving multiple stakeholders, deadlines, and dependencies.
- [required] Strong hands-on SQL skills, including extracting, joining, transforming, and validating complex datasets.
- [required] Practical experience using Python or R for data cleaning, manipulation, and analysis.
- [required] Experience defining business metrics, documenting calculation logic, and performing data quality validation.
- [required] Ability to translate ambiguous business requests into clear analytical requirements and actionable deliverables.
- [required] Strong organizational and program management skills, with the ability to coordinate parallel workstreams and follow through on commitments.
- [required] Business analytics, data analysis, business operations, program management, or a related field, including experience delivering analytical or reporting initiatives.
- [required] Excellent written and verbal communication skills, including the ability to explain analytical findings to nontechnical audiences.
- [role] In this role, you will combine hands-on data analysis with cross-functional program management to drive recurring reporting and analytical initiatives.
- [role] You will partner with business stakeholders and data owners to translate requirements into analytical solutions, validate metrics, develop dashboards, and deliver clear, actionable insights.
- [role] You will also help scale established reporting and analytics processes by developing reusable datasets, standardizing metrics, and identifying opportunities to improve efficiency and reporting reliability.
- [role] Manage recurring reporting and analytics initiatives, coordinating stakeholders, timelines, dependencies, and deliverables to ensure timely completion.
- [role] Partner with business stakeholders to clarify analytical requests, define requirements, and establish measurable outcomes.
- [role] Write and optimize SQL queries to extract, join, reconcile, and validate data from multiple sources.
- [role] Define and document business metrics, calculation logic, data definitions, and reporting assumptions to improve consistency across reporting.
- [role] Perform data quality checks to ensure reporting accuracy, consistency, and reliability.
- [role] Develop and maintain reusable datasets, dashboards, visualizations, and recurring reports that communicate operational performance.
- [role] Translate analytical findings into concise written updates, presentations, and actionable insights that help business leaders understand operational trends and make informed decisions.

### b66614d42d37221bad23
**Amgen — Director, Field Effectiveness, Obesity and Related Conditions**

- [required] Doctorate degree and sales analytics and/or field effectiveness experience
- [required] Or Master’s degree and sales analytics and/or field effectiveness experience
- [required] Or Bachelor’s degree and sales analytics and/or field effectiveness experience
- [role] Strong preference for flexible commuter role to Thousand Oaks, CA office.
- [role] Deerfield, IL or Remote option will be considered.
- [role] This position reports to the Senior Director, US Field Excellence, Obesity and Related Conditions and will be responsible for the following:
- [role] Manage and develop a team, and providing them with guidance, coaching, and training
- [role] In this vital role you the Director, Field Effectiveness will have responsibility for leading a team instrumental in driving commercial success by enabling, advising, and supporting the sales force to achieve performance
- [role] The US Field Excellence team provides guidance and support to the sales organization by optimizing the field experience through Field Effectiveness, Incentive Compensation, Field Operations, and Field Systems and Capabil
- [role] Accountable for program management and oversight of functional plan and execution of all field effectiveness programs and initiatives
- [role] Develop department’s organizational design, roles and responsibilities, and goals and objectives to deliver outstanding Field Effectiveness capabilities
- [role] Lead/ co-lead special initiatives and projects (e.g. sales force size & structure, launch readiness
- [role] Proactively monitor field performance and execution; identify issues/ opportunities and recommend solutions
- [role] Facilitate coordinating execution and pull-through of field force projects – customer targeting, field reporting, field alerts / suggestions, omnichannel / digital, field transformation, etc.
- [role] Collaborate with the broader GCCI (Global Commercial Capabilities & Innovation) organization to ensure alignment and support to deliver timely and accurate field effectiveness processes and deliverables
- [role] Monitor industry trends and marketplace to stay ahead of competition, enhancing and evolving capabilities to adapt to changing business needs
- [role] Proactively monitor business performance and execution; identify issues/opportunities and recommend solutions/course correction tactics as needed
- [role] Optimizing the field insight process, ensuring efficient and high-quality reporting

### bbd2f0cc83d9a9f3675c
**BD — Senior Manager, BD Excellence – Innovation and R&D**

- [required] R&D, Innovation, Engineering, Program Management, Product Management, or product-development transformation.
- [required] Demonstrated success leading complex, cross-functional product-development or innovation initiatives from insight and concept through design, transfer, launch, and learning.
- [required] Experience improving development speed, schedule predictability, technical readiness, resource effectiveness, or portfolio execution while maintaining quality and compliance.
- [required] Working knowledge of phase-gate product development, design controls, risk management, critical-path management, and the trade-offs among customer value, scope, time, cost, quality, and risk.
- [required] Experience applying structured problem solving, Kaizen, visual management, Obeya, Lean product development, Agile, or related continuous improvement methods.
- [required] Proven ability to influence senior leaders, technical experts, project teams, and external partners without direct authority.
- [required] Experience working across innovation, product development, and commercialization functions to accelerate successful product launches and adoption.
- [required] Innovation and consumer mindset: starts with observed and unspoken user needs, converts insight into testable concepts, and values rapid learning and iteration.
- [required] Systems thinker: connects portfolio strategy, product roadmaps, technical work, governance, resources, manufacturing, commercialization, and customer outcomes.
- [required] Execution oriented: creates clarity on scope, milestones, decision rights, risks, owners, and next actions, and maintains urgency without sacrificing rigor.
- [required] Technically credible and commercially aware: can engage engineering and scientific teams while keeping customer value, market readiness, and business impact visible.
- [required] Curious and analytical: challenges assumptions, uses data to expose constraints, and focuses teams on root causes and the critical path.
- [required] Pragmatic change leader: simplifies work, builds followership, and balances thoughtful risk-taking with quality, safety, and regulatory requirements.
- [required] Coach and facilitator: develops others, creates constructive challenge, and builds cross-functional accountability in complex situations.
- [required] Learning agile and resilient: adapts thinking as evidence changes and helps teams turn lessons learned into better standards and faster execution.
- [required] Ability to work effectively in a global, matrixed environment and engage teams at the Gemba.
- [role] The Senior Manager, BD Excellence - R&D and Innovation, UCC will partner directly with the UCC R&D leadership team to accelerate innovation, product-development velocity, and predictable execution while maintaining quali
- [role] Reporting to the Senior Director, BD Excellence - UCC, this leader will help design and deploy the operating system, capabilities, and continuous improvement practices required to move priority products from consumer and

### d3370eed67d7bf250afd
**Guidehouse — Business Process Transformation Manager**

- [required] Business process improvement, reengineering, modernization, or transformation
- [required] Experience working with executive (C-suite) and/or senior leadership (SES, GS-15) clients and developing executive-ready reports and briefings
- [role] Guidehouse is seeking a Managing Consultant who will lead and support key business process improvement, reengineering, modernization, and transformation initiatives/studies to enhance efficiency and effectiveness across 
- [role] This role will gather, analyze, and synthesize quantitative and qualitative data; and identify opportunities to improve organizational outcomes through process redesign, automation, technology-enabled, and/or financial r
- [role] This role will facilitate engagements with stakeholders and leadership to define goals, objectives, requirements, and outcomes; conduct interviews, surveys, workshops, and focus groups to gather information and analyze d
- [role] Serve as a key member and lead multidisciplinary consulting engagements supporting business process improvement, reengineering, modernization, and transformation initiatives/studies for a federal law enforcement client.
- [role] Conduct information gathering activities with senior federal clients, including interviews, workshops, focus groups, and working sessions.
- [role] Ability to quickly synthesize information, learn new skills, approach problems with a range of solutions, and lead client-facing discussions and meetings.
- [role] Contribute to business process improvement efforts to enhance efficiency and effectiveness across the organization.
- [role] Deliver customer‐focused end-user support by identifying issues, conducting root-cause analysis, and helping troubleshoot solutions.
- [role] Apply analytical and critical thinking skills to collect and interpret qualitative and quantitative data to inform client decision-making, develop, comprehensive reports, and present findings and recommendations.
- [role] Lead the development of client deliverables such as gap analyses, findings and recommendations reports, requirements documents, process flows, strategic plans, and knowledge management documentation.
- [role] Deliver high-quality client service in dynamic environments while balancing competing priorities and deadlines.
- [role] Develop trusted, consultative relationships with clients at multiple levels.
- [role] Provide mentorship, feedback, and guidance to junior consultants.
- [role] Support team culture through collaboration, knowledge-sharing, and inclusive engagement practices.
- [role] Contribute to internal initiatives such as communities of practice, business development, and proposal efforts.
- [required] Must be able to OBTAIN and MAINTAIN a CBP "PUBLIC TRUST"; candidates must obtain approved adjudication of their CBP PUBLIC TRUST prior to onboarding with Guidehouse.

### 5a142291897d017438a1
**Deloitte — Data and AI Policies & Enablement Senior Manager**

- [required] Data and AI governance, data policy development, and risk management and monitoring
- [required] Strong understanding of data and AI policies (privacy, security, governance), risk frameworks and control design, and regulatory environment impacting data and AI e.g.
- [required] GDPR, ISO 27001, SOC2, NIST Standards, and the EU AI Act
- [required] Experience implementing risk and compliance processes at scale, ideally in a global or federated organization
- [required] Familiarity with data and AI governance tools (catalog, quality, lineage, model decisioning etc.), cloud data platforms, and AI/data governance considerations
- [required] Ability to interpret complex policies and translate into execution
- [required] Stakeholder management across business and technical teams
- [required] Process design and operationalization expertise
- [required] Communication skills to simplify complex governance concepts
- [required] Strong analytical and problem-solving capabilities
- [role] The Data and AI Policies & Enablement Senior Manager role is a unique role that will drive immediate and global impact to help lead the formation and build the foundations of the new Data Strategy & Governance Office (DS
- [role] The Senior Manager, Data Policies & Enablement is responsible for developing enterprise data and AI policies and monitoring organizational compliance by supporting the execution of data risk management practices, working
- [role] The role acts as a bridge between policy and execution, ensuring that data and AI governance frameworks are effectively embedded into day-to-day operations and risks related to lifecycle management including but not limi
- [role] Identify requirements for policy definition to align with the requirements of internal cyber security standards, and external standards and frameworks like GDPR, ISO 27001, SOC2, NIST Standards, and the EU AI Act
- [role] Develop and maintain global data and AI policies, standards, and data and AI policy frameworks in alignment with data and AI maturity across the enterprise
- [role] Deloitte Global is the engine of the Deloitte network.
- [role] Our professionals reach across disciplines and borders to develop and lead global initiatives.
- [role] We deliver strategic programs and services that unite our organization.

### 30475c6c7eb9386aba10
**Guidehouse — Senior Data Automation Specialist**

- [required] Experience should include: Performing data analytics to address management and operational challenges
- [required] Independently conducting analysis and recommending actions to take based on analysis.
- [required] Using various data visualization programs such as Tableau, Power BI, SQL/VBA, etc.
- [role] Guidehouse is seeking a Data Automation Specialist to join our Defense and Security Segment, where the individual will collect, transform, integrate, and analyze data to identify trends, assess performance, and support d
- [role] Knowledge and experience working on multiple projects simultaneously in or like a Contact Center environment
- [role] Leads data research and analysis tasks.
- [role] Prepares data formats and compiles data to produce reports.
- [role] Prepares reports and presentations that convey trends and analysis.
- [role] Performs data research and analysis tasks related to database planning and design, systems analysis, integration, programming, conversion, and implementation support.
- [role] May use VBA, SQL and other tools to provide analysis and use of data sets, systems, and software.
- [role] Supports DHS business process improvements and modernization projects.
- [role] Analyzes and evaluates data to provide administrative and operational support, and to identify and resolve problems, in a variety of non-technical functional areas such as logistics, finance, programs, and business opera
- [role] Identifies, defines, interprets, clarifies scope, and provides guidance regarding existing technical difficulties and potential enhancement solutions.
- [role] Works with cross-functional teams to achieve goals.
- [role] Develops and implements policies, procedures, and solutions to improve efficiency and effectiveness.
- [role] Provides expert analysis and recommendations on current and emerging approaches and technology for big data management and analytics.
- [role] Ability to conduct detailed research and analysis of technical information.
- [role] Knowledge of industry leading data quality and data protection management practices.

### 9dea8641904e414d9499
**Wells Fargo — Principal Engineer - AI Enablement & Adoption**

- [required] Engineering experience, or equivalent demonstrated through one or a combination of the following: work experience, training, military experience, education
- [required] Large-scale transformation, Learning & Development, Organizational Effectiveness, change management, adoption and workforce enablement programs across multiple organizations.
- [required] Supporting Artificial Intelligence (AI), Generative AI (GenAI), data, or emerging technology initiatives.
- [role] Design, prototype, and build AI, Generative AI, Agentic AI, automation, and intelligent workflow solutions that demonstrate practical business applications.
- [role] Develop engaging AI technical content, including demonstrations, learning experiences, reference implementations, playbooks, use cases, labs, templates, and implementation guidance.
- [role] Design and deliver workshops, technical demonstrations, hands-on training, and enablement sessions for technical and business audiences.
- [role] Partner with teams to identify meaningful AI opportunities and translate business needs into practical AI-enabled solutions and workflows.
- [role] Create reusable AI assets, including agents, prompts, workflows, prototypes, technical examples, and implementation patterns that accelerate adoption.
- [role] Coach teams through the AI adoption lifecycle, helping them explore capabilities, build and test solutions, refine approaches, and incorporate AI into existing ways of working.
- [role] Translate emerging AI technologies into clear, actionable guidance, communicating capabilities, limitations, implementation considerations, and appropriate use cases.
- [role] Evaluate emerging AI technologies, platforms, and frameworks and recommend approaches based on business value, technical requirements, risk, and scalability.
- [role] Incorporate security, data protection, access controls, testing, evaluation, observability, governance, and human oversight into AI solutions and enablement materials.
- [role] Develop reusable standards, best practices, and lessons learned that enable successful AI approaches to scale across teams.
- [role] Partner with engineering, architecture, product, cybersecurity, risk, governance, and business stakeholders to enable responsible AI implementation.
- [role] Serve as a technical advisor and advocate for AI adoption, helping leaders and teams understand emerging capabilities, technical tradeoffs, and implementation opportunities.
- [role] Mentor engineers and practitioners and contribute to a culture of experimentation, continuous learning, innovation, and responsible AI adoption
- [required] Please be prepared to provide accurate information and supporting documentation in connection with any lawful employment and education verification conducted by Wells Fargo.
- [required] Third-Party recordings are prohibited unless authorized by Wells Fargo. b.

### 79b4079017391d5726ed
**Deloitte — Rural Health Data Standardization Senior Consultant**

- [required] Analytics (data cleansing, business intelligence & health analytics, financial analytics, Microsoft Excel, data extraction)
- [role] As a Rural Health Data Standardization Delivery Senior Consultant on the Operations Transformation team, you will:
- [role] Convert hospital financial statements and related source documents into standardized, analysis-ready data to support Rural Health Transformation engagement teams.
- [role] Extract financial and operational data from hospital cost reports, audited financial statements, and other source PDFs at scale
- [role] Standardize extracted data into consistent, analysis-ready formats aligned with engagement data models
- [role] Perform quality control checks to identify and resolve data discrepancies, gaps, or extraction errors
- [role] Maintain documentation of data sources, standardization rules, and assumptions for audit and reproducibility purposes
- [role] Collaborate with team members to ensure standardized data meets analytical needs
- [role] Support the development and refinement of extraction templates and standardization processes to improve efficiency over time
- [required] Hybrid Work Model: Operate under a hybrid system requiring residence within a commutable distance to one of the US Delivery Center locations (Gilbert, Lake Mary, or Mechanicsburg) or Geo-Hub locations (Atlanta, Charlotte
- [required] Co-location Expectation: Spend up to 30% of working time co-located at an assigned office for orchestrated opportunities, including projects, practice sessions, training, and Moments That Matter at a Deloitte Delivery Ce
- [required] Ability to lead projects or workstreams
- [required] Ability to provide clear guidance to others
- [required] Ability to meet deadlines
- [required] Ability to build and sustain professional relationships
- [required] Strong interpersonal skills and professional demeanor
- [required] Meticulous attention to detail and quality of work product
- [required] Ability to manage and prioritize multiple tasks in a fast-paced and dynamic environment

### a5544bf7860aef304307
**NTT Data — MMIS Project Manager**

- [required] Running multiple concurrent projects
- [required] Managing projects in their assigned area
- [required] Managing multi-vendor projects
- [required] Strong hands-on experience with Microsoft Project
- [required] Certification and CMS procurement requirements
- [required] Performing procurement work (developing RFIs/RFPs, vendor selection and evaluation, etc.)
- [required] Must be a team player and a consistent, dependable performer with an excellent work ethic, flexible "can-do" attitude, and a results-driven commitment to success
- [required] PMP or equivalent certification
- [required] Bachelor’s degree in computer science, information systems or similar field or equivalent work experience.
- [role] NTT DATA strives to hire exceptional, innovative and passionate individuals who want to grow with us.
- [role] If you want to be part of an inclusive, adaptable, and forward-thinking organization, apply now.
- [role] We are currently seeking a MMIS Project Manager to join our team in Montgomery, Alabama (US-AL), United States (US).
- [role] NTT DATA strives to hire exceptional, innovative, and passionate individuals who want to grow with us.
- [role] We are currently seeking a Project Manager to join our team in Montgomery, Alabama.
- [role] The Project Manager will play a key role in the successful cutover and modernization of an MMIS to a cloud environment with major project enhancements .
- [role] The Project Manager will be primarily responsible for the health of their cohort project.
- [role] The Project Manager will be responsible for performing, delegating and managing the activities within the cohort project team, working closely with direct business partners and vendor teams.
- [role] He/she will actively participate in the development and delivery of program and quality management services and associated deliverables.

### 752404d4a61c12728816
**Capital One — Sr. Manager- Data Analytics**

- [required] Developing with open source data technologies
- [required] analytics with an expectation that required degree will be obtained on or before the scheduled start date.
- [required] Currently has, or is in the process of obtaining a Bachelor’s Degree in quantitative field (Statistics, Economics, Operations Research, Analytics, Mathematics, Computer Science or a related quantitative field) plus perfo
- [role] As a Senior Manager, you will partner cross-functionally with teams throughout the company to validate business impacting analytical tools.
- [role] You will enhance your technical and analytical skills, while also working with leaders to influence business strategies.
- [required] Performing professional data analysis work
- [role] With a network of hundreds of Data Analysts, Quants, and Data Scientists, challenging projects with an eye on the bottom line and a focus on work/life balance, we’ve created a dynamic environment with plenty of room for 
- [role] Refining a new Enterprise-wide analytical tools validation framework
- [role] Assessing the quality and risk of analytical tool methodologies across Capital One, and the nature of tools usage within those processes
- [role] Understanding technical issues in analytical tools and assessing tools risks and opportunities
- [role] Independently researching, identifying, and prototyping industry best practices for emerging analytical tools
- [role] Communicating results clearly and concisely both verbally and through written communications via validation reports and presentations
- [role] Leveraging education, colleagues and training opportunities to develop solutions to business problems
- [role] An ideal candidate will possess strong problem solving and conceptual thinking abilities in addition to communication, interpersonal and leadership skills.
- [role] This position will be in a fast-paced and entrepreneurial environment where you will be handling multiple concurrent projects while working independently and in teams.
- [role] At Capital One, data is at the center of everything we do.
- [required] Master’s Degree or PhD in a Finance, Economics, Statistics, Mathematics, Industrial Engineering, Operations Research, or a related field
- [required] Statistical or econometrics hands-on work

### 3f268443a6e3504c344c
**CACI — Cybersecurity Assessment Data Analyst**

- [required] The selected candidate must meet eligibility requirements for access to sensitive information and be able to obtain a Public Trust fitness determination (High Risk).
- [required] Ability to work in customer-provided remote environments, use customer-approved tools, and comply with rules of engagement, data-handling requirements, evidence controls, deconfliction procedures, and stop-work criteria.
- [required] Data analysis, cybersecurity analytics, reporting, business intelligence, or data-engineering experience, including work with technical security data.
- [required] Strong SQL and Python skills and practical experience with data cleaning, joins, APIs, structured files, quality validation, reproducible analysis, and version control.
- [required] Experience building dashboards and reports in Power BI, Tableau, Splunk, Elastic, or comparable tools.
- [required] Working knowledge of vulnerability, asset, finding, evidence, ticket, and remediation data and of concepts such as CVE, CVSS, CISA KEV, EPSS, false positives, and attack paths.
- [required] Ability to define metrics precisely, reconcile conflicting sources, explain limitations, and communicate results to technical and executive audiences.
- [role] CACI is seeking a Cybersecurity Assessment Data Analyst to turn assessment, scan, asset, evidence, and remediation data into trusted operational insights.
- [role] The analyst will support penetration testing, red team, and attack-surface evaluation by normalizing data, validating quality, identifying trends and risk concentrations, and producing repeatable dashboards and reporting
- [role] This is a shared analytical capability; it does not replace the technical judgment of assessment SMEs.
- [role] This position will provide continuous technical assessment support for full‐time, proactive testing of externally and internally visible federal cyber assets.
- [role] This expands federal visibility by conducting continual assessments of .gov domains, subdomains, and internet‐facing assets to uncover unknown exposures, validate vulnerabilities, and provide agencies with actionable rem
- [role] This role supports the Continuous Diagnostics and Mitigation (CDM) Program’s mission to safeguard and secure cyberspace in an environment where the threat of cyber-attack is continuously growing and evolving and is respo
- [role] The CDM Program defends the United States (U.S.) Federal Information Technology (IT) networks from cybersecurity threats by providing continuous monitoring sensors (tools), diagnosis, mitigation tools, and associated ser
- [role] Ingest, normalize, reconcile, and quality-check data from customer-approved assessment tools, scanners, APIs, repositories, tickets, operator evidence, and remediation workflows.
- [role] Develop repeatable data models, queries, scripts, metrics, dashboards, and reporting packages that show asset coverage, finding trends, attack paths, severity, remediation status, and delivery performance.
- [role] Correlate vulnerability and exposure data with CISA KEV, CVSS, EPSS, asset criticality, exploit evidence, threat context, and customer priorities.
- [role] Investigate duplicates, conflicting identifiers, missing fields, stale records, outliers, and reconciliation issues; document source lineage and metric definitions.

### bdef1c4e7820fcabfc61
**BD — Director, Marketing Data & Analytics**

- [required] Strategic analytics leader with a proven ability to define and execute a marketing analytics vision that drives measurable commercial outcomes.
- [required] Deep expertise in marketing measurement, including multi-touch attribution, marketing mix modeling, omnichannel performance frameworks, and KPI governance.
- [required] Expert-level knowledge of digital analytics platforms – GA4 and Adobe Analytics – including enterprise-scale governance, SDR design, and cross-market implementation.
- [required] Strong command of tag management strategy (Adobe Launch / GTM) and digital data collection best practices across paid, owned, and earned channels.
- [required] Proven capability in advanced analytics and machine learning applied to marketing – attribution, segmentation, forecasting, lead scoring, and experimentation.
- [required] Clear strategic point of view on AI’s role in marketing analytics, with practical experience identifying and delivering high-value use cases.
- [required] Sophisticated data governance mindset – able to set enterprise standards for data quality, privacy compliance, and first-party data strategy.
- [required] Exceptional ability to translate analytical complexity into executive-level strategic narrative that drives decisions and investment.
- [required] Experienced people leader and function builder – able to attract, develop, and retain analytical talent and build a high-performing team culture.
- [required] Senior stakeholder influencer – credible, proactive, and commercially astute in interactions with VP-level and BU leadership audiences.
- [required] Strong understanding of marketing data infrastructure – data mesh, cloud platforms (Azure), pipelines, and data modelling – sufficient to set strategic direction for engineering teams.
- [required] Proficiency with visualization platforms (Tableau, Power BI); working knowledge of SQL; familiarity with Python or R for advanced analytics.
- [required] Marketing analytics, digital analytics, or commercial data roles, with senior leadership positions.
- [required] Demonstrated track record of defining and delivering a marketing analytics strategy with measurable business impact at enterprise scale.
- [required] Expert-level experience with GA4, Adobe Analytics, and tag management platforms (Adobe Launch and/or GTM) in a global, multi-market context.
- [required] Proven experience leading advanced analytics programs in a marketing context, including attribution modeling, ML-based segmentation, and large-scale experimentation.
- [required] Experience defining or significantly contributing to an enterprise data strategy, data mesh, or first-party data program.
- [required] Demonstrated success building, scaling, and leading global analytics teams across diverse disciplines (data science, engineering, digital analytics).

### cd958561742ba452f91c
**Zillow — Senior Business Systems Analyst**

- [required] Solutions consulting, technical account management, implementation, integration support, or related work.
- [required] Skilled in XML, JSON, data mapping, and feed-based integrations, including the Multifamily Information and Transactions Standards (MITS) schema.
- [required] Familiar with application programming interfaces (APIs), REST, webhooks, and tools such as Postman.
- [required] Able to translate technical information into clear explanations for partners, engineers, and business leaders.
- [required] Experienced in identifying patterns across issues and turning findings into practical process or system improvements.
- [required] Able to coordinate work across teams, manage competing priorities, and follow issues through resolution while documenting and sharing findings.
- [required] Interested in developing and improving AI-assisted workflows that help teams work effectively.
- [role] As a Senior Business Systems Analyst, you’ll provide technical leadership as an individual contributor on the Integration Solutions team.
- [role] Working across product, engineering, support, and enterprise partners, you’ll resolve complex integration issues and develop documentation, tools, and monitoring that improve reliability and help teams support partners e
- [role] You Will Get To Lead the investigation and resolution of complex integration issues across systems, providing engineering teams with clear problem statements, reproduction steps, and supporting evidence.
- [role] Lead partner discovery conversations, recommend integration approaches, and explain technical findings to technical and nontechnical audiences.
- [role] Partner with Sales on technical discussions with enterprise prospects.
- [role] Develop and improve AI-assisted knowledge and diagnostic tools to support troubleshooting and knowledge sharing.
- [role] Create and maintain integration guides, escalation playbooks, onboarding resources, and troubleshooting documentation.
- [role] Improve operational tools and workflows, including Zendesk, Slack automations, Airtable, and monitoring dashboards.
- [role] Analyze support trends and integration health signals to identify recurring issues, recommend root-cause improvements, and strengthen proactive monitoring.
- [role] Define launch support plans—including documentation, escalation paths, monitoring, and known limitations—and support partner onboarding and post-launch reviews.
- [required] Here at Zillow - we value the experience and perspective of candidates with non-traditional backgrounds.

### 1bcad1a1a2d3ed74ff14
**Cisco — Sr. Manager, Cloud Capacity Management**

- [required] Bachelor's degree in business, finance, engineering, supply chain, data analytics or a related field; and , or a master's degree and
- [required] Experience in cloud or infrastructure capacity planning with AWS, Microsoft Azure or Google Cloud, including quotas and reservations
- [required] Experience building demand forecasts and scenario models
- [required] Experience building reports and dashboards
- [required] Experience working with FinOps or finance teams on cloud cost management, reservation coverage or budgets
- [role] Build and maintain a rolling capacity forecast by CSP, region, workload and resource type to help leaders make confident investment and growth decisions worldwide.
- [role] Compare demand from sales pipeline, bookings and product roadmaps against available supply, then work with CSPs on reservations so customers get capacity on time without wasted spend.
- [role] Collaborate with sales on realistic lead times, with engineering on protecting capacity during maintenance windows and with FinOps on reconciling reservations to keep costs healthy.
- [role] Mentor fellow capacity planners and introduce AI-enabled tools to make planning faster and more accurate across every region.
- [role] Measure your success by sharper forecasts, on-time deliveries and a clear owner and plan for every capacity gap.
- [role] The Cloud Capacity Planning team makes sure Cisco's cloud platforms have the compute, storage and network capacity customers need, in the right region at the right time.
- [role] We turn demand from sales, product and engineering into confirmed supply with our cloud service providers (CSPs), which keeps customer launches, migrations and maintenance windows on track while managing cloud spend resp
- [role] We're a supportive team of 7 and analysts across the US and Canada who share what we know, learn from each other and celebrate wins together.
- [role] What makes this team exciting is timing: we're bringing AI-powered forecasting into how Cisco plans for the future of cloud!
- [required] Hands-on experience using AI or machine learning tools to automate forecasting and reporting
- [required] Knowledge of how geopolitical, regulatory and data sovereignty factors affect regional capacity and hardware supply chains
- [required] Clear communicator who enjoys explaining trade-offs to stakeholders at every level
- [required] Passion for mentoring teammates and building processes that make everyone's work easier
