# Labeling batch audit_001 — grade the WORK, not the candidate's odds


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
Return ONE JSON object per posting below, as a JSON array, written to `audit_001.result.json`. No prose, no markdown fence.
Grade every posting in this file, on ALL THREE lenses. Use the posting_id exactly as given.

## Postings (20)

### 860dc4c747dc9ba4fd6b
**RTX — Scrum Master (Puerto Rico)**

- [required] Experience working with software development life cycle models as well as expert knowledge of both SAFe or Agile and traditional project management principles and practices with the ability to blend them together properl
- [required] Prior experience with Scrum/Agile methodologies with enterprise-level development projects.
- [required] Experience overseeing Scrum and/or Project teams with at least 5 team members across Developers, Business Analysts, Product Owners, and QA engineers
- [role] Guide, facilitate, and educate Scrum teams on Agile and Scrum practices and values to successfully implement critical projects, leveraging cloud technologies, frameworks, and methodologies.
- [role] Improve the Scrum maturity of the teams and organization at a pace that is sustainable and comfortable for the team and organization.
- [role] Remove impediments and blockers or guide the team to remove impediments and blockers themselves; escalate impediments and blockers as necessary.
- [role] Apply Agile practices to facilitate better ways of working and improved execution of teams
- [role] Leverage SAFe/Scrum/Kanban frameworks.
- [role] Actions often include visualizing work, limiting work in progress, managing workflow, planning, and providing guidance for daily standups, sprint and Product Increment planning, short, mid, and long-range planning, backl
- [role] Ensure progress that is trackable toward objectives, KPIs, and commitments; guide assigned Hosting teams to deliver the required product increments and outcomes.
- [role] Partner with key stakeholders and management team (i.e.
- [role] Agile Coach, Release Train Engineer, and Team Leads) in backlog prioritization and product increment planning as well as tracking and reporting metrics, providing visibility to the team and stakeholders, ensuring a data-
- [role] Support and educate the Product Owner and Team Leads, especially on the product backlog and ongoing backlog refinement.
- [required] A University Degree or equivalent experience and prior relevant experience, or an Advanced Degree in a related field and
- [role] Assisting with internal and external communication, managing dependencies, and radiating information transparently.
- [role] This position will reference written work instructions for guidance with daily job activities.
- [role] Work instructions are available in English only.
- [role] US-PR-SANTA ISABEL-B1 ~ Felicia Industrial Park - St B1 ~ BLDG 1

### c4e2514ccd6f36c5794e
**Stripe — Strategic Business Performance, Central Strategy & Operations**

- [required] Product/data analytics within product organizations or strategic business performance teams at high-growth tech companies
- [required] Apply AI to enhance decision-making and operational efficiency, know when and how to use the right AI tools to move faster without sacrificing rigor or quality
- [required] Experience facilitating senior leadership/ stakeholder meetings to drive meaningful conversations regarding Stripe’s business performance
- [required] Experience in Product Strategy, Product Management, or GM-style roles where you owned a P&L or a specific business outcome
- [required] Deeply curious about business models and data, probing into signals, understanding trends and customer usage patterns to uncover novel insights
- [required] A proven track record of directly driving business growth through analytical insights and execution
- [required] Strong product sense and the ability to understand the technical mechanics of how software products are built and scaled
- [required] Experience navigating ambiguous, cross-functional problems and leading high-stakes interventions with senior stakeholders
- [role] In this role, you will work cross-functionally within our group and across Stripe’s technical and user-facing teams to solve problems and design workflows that benefit our users.
- [role] Own the "Signal" : You will define and maintain the metrics that actually matter for in-year performance.
- [role] You’ll develop a "morning read" discipline—a curated, high-signal view of the business that drives immediate action, moving far beyond standard dashboarding.
- [role] Deep-Dive Interventions : You will lead "surge" projects to diagnose and unblock critical goals at risk.
- [role] You aren't just identifying problems; you are staying in the arena until the trajectory has measurably changed.
- [role] Bridge Product & Strategy : You will translate long-horizon corporate theses into in-year operational reality.
- [role] You’ll take ambiguous opportunities that cut across product, risk, and marketing and decompose them into structured workstreams.
- [role] Synthesize for Leadership : You’ll think ahead of our executive team, preparing CEO/COO-level materials that translate unstructured signals into structured diagnoses they can act on.
- [role] Pressure-test our Assumptions : You’ll work closely with Corporate Strategy and Product teams to identify gaps between our external market forces and our internal execution.
- [required] Payments ecosystem, or experience in finance or fintech is a plus

### 242ffe7567a786279f0e
**Nebius — Sr. Manager, Quote-to-Cash Transformation **

- [role] You will lead cross-functional transformation initiatives that eliminate friction, standardize processes, strengthen governance, and embed automation across quoting, approvals, contracting, billing setup, and activation.
- [role] You will assess the current state, define the target operating model and transformation roadmap, and drive implementation across processes, policies, systems, data, and ways of working.
- [role] You will partner closely with Sales, Commercial Ops, Legal, Finance, GTM Ops, GTM Systems, Billing Engineering, and Billing Product .
- [role] Lead the transformation of the end-to-end Quote-to-Cash lifecycle , initially focusing on deal initiation, structuring, approval, contracting, billing setup, and customer activation.
- [role] Assess current-state processes, identify root causes of friction and rework, and define a scalable future-state operating model .
- [role] Develop and execute a prioritized transformation roadmap spanning processes, policies, systems, data, controls, and organizational responsibilities .
- [role] Redesign complex, fragmented, or manual workflows to improve speed, accuracy, automation, and the customer and seller experience.
- [role] Define and implement policies, governance frameworks, approval authorities, and deal guardrails that enable scale without unnecessarily slowing the business.
- [role] Partner with Sales and Commercial Ops to improve deal quality and enable first-time-right deal structuring.
- [role] Collaborate with Legal and Finance to simplify approvals, standardize contract terms, and maintain appropriate commercial and financial controls.
- [role] Work with GTM Systems to embed workflows, policies, data requirements, and controls into CRM, CPQ, contract management, and related tools.
- [role] Partner with Billing Engineering and Billing Product to create a reliable and increasingly automated flow from order form to billing and customer activation.
- [role] Standardize the handling of complex deal constructs, including usage-based pricing, multi-year agreements, ramp structures, prepayments, and custom commercial terms .
- [role] Establish clear ownership, handoffs, and service expectations across teams involved in the Quote-to-Cash lifecycle.
- [role] Develop playbooks, guidance, and change-management programs that drive adoption of new processes and tools.
- [role] Establish transformation metrics and track improvements in cycle time, approval time, rework, policy adherence, billing accuracy, activation accuracy, and time-to-revenue .
- [role] Lead cross-functional programs from design through implementation, adoption, and measurable business impact.
- [role] A fragmented deal lifecycle into a scalable, integrated Quote-to-Cash operating model .

### a6b29637534358e7879a
**Cvent — Director/Senior Manager, Events**

- [required] Event planning, marketing, or a related field
- [required] Leadership experience where you have coached and worked on career development
- [required] Must be located near Tyson's Corner, Virginia and have the ability to commute to the office 2 days per week and on days with live events or in-office prep for upcoming events
- [required] Experience managing large and complex contracts, budgets, and financial analysis
- [required] Ability to analyze situations, identify and troubleshoot problems, and follow up until a resolution is achieved
- [required] Strong project management and communication skills, ability to excel in a fluid, cross-functional team, deadline driven environment
- [required] Ability to operate and provide value at both a macro/strategy-level and a micro/execution level
- [required] Strong time management skills, ability to manage multiple time-sensitive, high priority projects simultaneously
- [required] Strong negotiation and bid comparison experience with vendors
- [required] Knowledge of the total marketing/sales mix, including campaign execution, program management, event marketing, logistics management and event technology, etc.
- [required] Experience with managing a cross-functional team & working with international resources
- [required] Strong executive presence and ability to interact with all levels of management within the company and experience working directly with C-Level/senior leadership
- [required] Self-directed, self-motivation, and disciplined team player capable of hands-on execution and long-term business planning
- [required] Quick learner and enthusiastic about ongoing development to better support the team & growing event program
- [required] Strong knowledge of the MS Office suite, with expertise in Excel
- [required] Strong knowledge & experience with using Event Marketing & Management solutions (Cvent)
- [role] Collaborate with the Team's Senior Director of Events to execute a strategic corporate event & experiential events strategy
- [role] Manage the North America Event Design team based in the HQ (Tyson's office) responsible for producing 500+ events annually.

### 5733a2fdcbfecbd3a708
**ServiceNow — Senior Principal Inbound Product Manager, CRM Voice AI **

- [required] Product management experience, with recent hands-on work shipping voice AI or conversational AI products.
- [required] What you've shipped in the last 12-18 months matters more than total years.
- [required] This is the core of the job; the voice stack expertise below layers on top of it.
- [required] Fluency with the voice stack: ASR, TTS, streaming pipelines, latency optimization, endpointing, barge-in, turn-taking.
- [required] You've made real tradeoff decisions here, not just evaluated vendor demos.
- [required] Experience with telephony integration: SIP, call transfers, IVR systems, or CCaaS platforms.
- [required] Experience working directly with enterprise customers on technical deployments - not from a distance, but in their environment, on their calls, solving their problems.
- [required] Strong opinions on what makes a voice conversation feel human, backed by experience building or evaluating conversational systems.
- [required] Technical depth in LLM orchestration for real-time conversational use cases.
- [required] Experience defining voice-specific evaluations such as task completion, latency, transcription accuracy, and conversational quality.
- [required] Hands-on experience building and evaluating AI agents - conversation design, prompt engineering, testing and iteration against real user interactions.
- [required] A track record of productizing customer-specific solutions into repeatable, industry-level assets.
- [required] Industry expertise in verticals with high contact center volume: telecommunications, financial services, travel, healthcare.
- [required] Sales positions generally offer a competitive On Target Earnings (OTE) incentive compensation structure.
- [required] We also offer health plans, including flexible spending accounts, a 401(k) Plan with company match, ESPP, matching donations, a flexible time away plan and family leave programs.
- [required] Compensation is based on the geographic location in which the role is located and is subject to change based on work location.
- [required] To determine eligibility for a work persona, ServiceNow may confirm the distance between your primary residence and the closest ServiceNow office using a third-party service.
- [required] We approach our distributed world of work with flexibility and trust.

### cf996cddd7a2e327d7c6
**Deloitte — Digital Release Manager – Global Employer Services Technology Center**

- [required] Bachelor's degree in engineering, computer science, or other relevant discipline
- [required] In Project Management Office (PMO), and/or strategy/consulting experience with Azure DevOps/JIRA (or similar software development management tool)
- [required] Proven experience in strategy/consulting and/or Technology Project Management
- [required] Proven experience with Azure DevOps / JIRA (or similar Software product development management tool)
- [required] One of the following active accreditations obtained: Licensed CPA in state of practice/primary office if eligible to sit for the CPA; Licensed Attorney (or foreign equivalent); Enrolled Agent; Project Management Professi
- [role] As a Manager within the GES Technology Group, you will have the opportunity to work directly with the Digital Delivery team and be responsible for planning, coordinating, and overseeing the deployment of software release
- [role] This role ensures that releases are delivered on time, within scope, and with minimal disruption to business operations that impact a global audience.
- [role] This role collaborates with development, QA, operations, risk and business teams to manage the end-to-end release lifecycle, including planning, risk management, and communication.
- [role] Develop and manage detailed release plans, schedules, and milestones for software deployments.
- [role] Coordinate release activities across multiple teams, including development, QA, operations, risk and business stakeholders.
- [role] Ensure all release deliverables meet quality standards and compliance requirements.
- [role] Identify, document, and mitigate risks associated with software releases.
- [role] Manage and communicate release status, issues, and changes to stakeholders.
- [role] Oversee the execution of release processes, including code branching, merging, build management, and deployment.
- [role] Facilitate release readiness reviews, go/no-go meetings, and post-release retrospectives.
- [role] Continuously improve release management processes, tools, and documentation.
- [role] Maintain release documentation, including release notes, deployment guides, and rollback procedures.
- [role] Support incident management and root cause analysis for release-related issues.

### 742855a99e9d7d69e7d6
**Gartner — Associate Director - AI Product Lead - Growth and Retention**

- [required] Bachelor’s Degree or Equivalent
- [required] Enterprise sales-process expertise: Demonstrated understanding of enterprise selling, including account planning, opportunity management, QBRs, seller workflows, Salesforce, and sales enablement.
- [required] AI product fluency: Practical experience applying AI to business processes, including agentic workflows, prompts, data grounding, orchestration, evaluations, guardrails, context management, and human-in-the-loop design.
- [required] Process-transformation skills: Ability to analyze an existing business process, identify opportunities for simplification or redesign, and determine whether the right solution is agent-led, automated, guided, or platform
- [required] Product discovery and requirements skills: Ability to facilitate discovery with business leaders and end users, identify underlying needs, define future-state workflows, and translate findings into clear product requirem
- [required] End-to-end product delivery experience: Demonstrated ability to take a digital, data, or AI product from an ambiguous problem through solution design, backlog development, build, testing, pilot, launch, adoption, and ite
- [required] Technical product judgment: Sufficient technical fluency to collaborate effectively with Engineering, Data Science, Architecture, Salesforce, and data teams; assess solution options; understand dependencies; and make inf
- [required] Prioritization and roadmap management: Ability to prioritize work based on user value, business impact, technical feasibility, risk, scalability, and delivery speed.
- [required] User-centered problem solving: Ability to understand user needs through interviews, observation, feedback sessions, demonstrations, and pilots rather than relying solely on stated requirements.
- [required] Outcome measurement: Experience defining and tracking success measures such as product quality, adoption, user experience, seller efficiency, process effectiveness, and business impact.
- [required] Stakeholder influence: Demonstrated ability to align and influence cross-functional teams—including Sales, Operations, Product, Technology, Data Science, Legal, Information Security, and Change Management—without direct 
- [required] Executive communication: Ability to communicate product direction, recommendations, risks, dependencies, trade-offs, decisions, delivery progress, and business impact clearly to senior stakeholders.
- [required] Enterprise governance awareness: Experience incorporating privacy, security, access, legal, regulatory, and responsible AI considerations into product decisions and delivery.
- [required] Gartner is the world authority on AI
- [required] At Gartner, you’ll join a company at the very center of the AI revolution.
- [required] Gartner has proactive, objective guidance throughout clients’ AI journeys.
- [required] We set the standard for how organizations leverage artificial intelligence to drive meaningful impact.
- [required] You’ll have access to unmatched resources, expertise, and technology, and play a key role in helping Gartner and our clients innovate and grow as we leverage AI to transform business and technology landscapes.

### bd9ed5b7762f49c15757
**L3Harris — Senior Associate, Finance - Balance Sheet Analyst**

- [required] Bachelor’s Degree and prior related experience.
- [required] Graduate Degree or equivalent with prior related experience.
- [required] Lieu of a degree, prior related experience.
- [required] Must have strong planning, organizational, and analytical skills; be self-directed, proactive, and possess good communication skills
- [required] Must be able to work in a team environment.
- [role] Ability to work independently with regular direction and guidance on non-routine assignments requiring sound judgment, ownership, and initiative.
- [role] Intermediate to advanced proficiency in Microsoft Excel, including complex formulas, data manipulation, spreadsheet development, and financial modeling.
- [role] Intermediate to advanced proficiency in Microsoft Office applications, including PowerPoint, Word, SharePoint, and Teams.
- [role] Ability to manage, consolidate, and analyze large volumes of financial data to support forecasting and reporting activities.
- [role] Prepare and support Balance Sheet and Working Capital forecasting and modeling activities for select Divisions within the Sector using established systems (PeopleSoft, HFM, UDL) and processes.
- [role] Coordinate as needed on a weekly and monthly basis.
- [role] Evaluate and apply appropriate methodologies for forecasting Direct and Indirect Cash Flow, ensuring approaches are accurate, consistent, and well-supported.
- [role] Support monthly financial forecasting activities, as well as the annual Joint Strategic Plan (JSP) and Annual Operating Plan (AOP) processes.
- [role] Analyze and explain Quarter-over-Quarter (QoQ), Year-over-Year (YoY), and variance-to-prior-forecast changes to identify key drivers and trends.
- [role] Ability to obtain a U.S. security clearance.
- [role] Job Title: Senior Associate, Finance – Balance Sheet Analyst
- [role] Job Location: Mason, OH; Amityville, NY; Richardson, TX; Plano, TX
- [role] Job Schedule: 9/80 - Employees work 9 out of every 14 days – totaling 80 hours worked – and have every other Friday off

### fa520d06fe9e77f771dc
**Cooley LLP — Associate Director of Technology Collaboration  & AI Services**

- [required] Bachelor’s Degree
- [required] Deep knowledge of enterprise AI platforms (ChatGPT, Claude, etc.) and Microsoft 365, productivity and collaboration tools
- [required] Experience managing external development teams and vendors
- [required] After orientation at Cooley LLP, proficiency in the use of the AI, the Microsoft Office suite, iManage and other firm applications
- [required] Ability to work extended and/or weekend hours, as required
- [role] Cooley Technology embraces a culture of customer-service excellence, and all members of the department are expected to advance this commitment.
- [role] The Associate Director of Technology Collaboration & AI Services will provide strategic and operational leadership for two enterprise service areas: Digital Workplace Collaboration and Enterprise AI.
- [role] Through two direct reports, one leading each area, the Associate Director sets strategy, establishes priorities, develops talent, and ensures coordinated delivery across the portfolio.
- [role] The role is accountable for the implementation, adoption, governance, optimization, and lifecycle management of the firm’s general-purpose enterprise AI platforms and digital workplace collaboration ecosystem.
- [role] This includes, but is not limited to, Microsoft 365, Outlook and Exchange Online, Teams, SharePoint Online, OneDrive, Power Platform, the firm’s intranet, enterprise search, workflow solutions, and core productivity appl
- [role] Working across Technology and the Firm, the Associate Director ensures these platforms deliver secure, reliable, and intuitive experiences; improve how work is performed; and maximize the value of the firm’s technology i
- [role] Own the enterprise AI portfolio from strategy and platform selection through configuration, governance, launch, adoption, support, and continuous improvement
- [role] Lead M365 strategy, adoption, and optimization across Exchange Online, Teams, Power Platform, SharePoint Online, OneDrive, core productivity applications, and the firm's intranet to reduce friction and improve digital wo
- [role] Drive workflow automation and low-code/no-code initiatives to automate and improve
- [role] Build and lead high-performing teams focused on user experience, change enablement, and continuous improvement
- [role] Establish and maintain operating models, playbooks, and decision frameworks for enterprise AI, Power Platform, Teams/SharePoint provisioning, intranet content and workflow solutions
- [role] Evaluate and guide the controlled introduction of emerging enterprise AI capabilities, e.g., agents, connectors and plugins, early-access models, and AI-enabled workflows
- [role] Collaborate with Practice Innovation, Legal Education and Technology Learning & Adoption to ensure cohesive organizational change management planning and execution to drive adoption and build our users’ digital dexterity

### fb34044c99c448e15094
**Microsoft — Global Manager of Quality Assurance - Construction**

- [role] Support the Global Director in defining the enterprise QA strategy for construction, ensuring alignment with engineering, design, safety, and project execution priorities.
- [role] Develop and maintain the global QA operating model, including roles, responsibilities, decision rights, and interfaces with regional execution teams.
- [role] Establish scalable frameworks for contractor quality management, inspection readiness, commissioning quality, and defect prevention.
- [role] Create, refine, and govern global QA standards, procedures, checklists, and verification protocols used across all regions.
- [role] Lead the development of digital QA systems, dashboards, and reporting tools that provide visibility into quality performance, trends, and systemic risks.
- [role] Partner with Engineering, Design, Supply Chain, and Construction to ensure QA processes are integrated throughout the project lifecycle—from design through commissioning.
- [role] Stand up and manage global QA governance forums that drive accountability, transparency, and continuous improvement.
- [role] Establish global metrics, KPIs, and scorecards that measure quality performance across contractors, regions, and project types.
- [role] Conduct global quality audits, maturity assessments, and compliance reviews to ensure adherence to standards and identify systemic gaps.
- [role] Develop and maintain global contractor QA requirements, qualification criteria, and performance expectations.
- [role] Partner with Supply Chain and regional teams to evaluate contractor quality capability and drive corrective action plans when needed.
- [role] Create frameworks for defect tracking, root‐cause analysis, and lessons‐learned integration.
- [role] Provide training, coaching, and guidance to regional QA managers and construction teams on global standards and best practices.
- [role] Facilitate global knowledge‐sharing, including playbooks, case studies, and technical guidance.
- [role] Support regional execution teams in implementing QA systems, resolving complex quality issues, and improving contractor performance.
- [role] Lead global initiatives to reduce rework, improve first‐time‐quality, and strengthen construction readiness.
- [role] Identify systemic risks and drive cross‐functional solutions that improve quality outcomes across the portfolio.
- [role] Integrate lessons learned into standards, processes, and contractor requirements.

### dc39e28762428097e149
**AT&T — Assistant Store Manager**

- [required] Strategic perspective and the ability to champion change.
- [required] Inspiring your team through high performance, collaboration, and teamwork
- [required] Utilizing professional expertise to solve problems and analyze issues.
- [required] Taking initiative and striving and creating results
- [required] Not to mention all the other amazing rewards that working at AT&T offers.
- [required] Joining our team comes with amazing perks and · Medical/Dental/Vision coverage
- [required] 401(k) plan
- [required] Tuition reimbursement program
- [required] Paid Time off and holidays (at least 23 days of vacation each year and 9 company-designated holidays)
- [required] Paid Parental Leave
- [required] Additional sick leave beyond what state and local law require may be available but is unprotected.
- [required] Adoption Reimbursement
- [required] Life and Accidental Death Insurance
- [required] Supplemental benefit programs: critical illness/accident hospital indemnity/group legal
- [required] Employee Assistance Programs (EAP)
- [required] Extensive employee wellness programs
- [required] Employee discounts up to 50% off on eligible AT&T mobility plans and accessories, AT&T internet (and fiber where available) and AT&T phone
- [required] Relevant Material Job Duties for which Criminal History may have a direct adverse, and negative relationship potentially resulting in the withdrawal of the Conditional Offer of Employment

### cf2dd2c44e6f2b497e9d
**Mastercard — Senior Business Analyst**

- [required] Bachelor's degree in Business, Finance, Economics, Accounting, Information Systems, or related field, or equivalent work experience.
- [required] Experience in financial services, banking, payments, or related fields.
- [required] Strong analytical and problem-solving skills with the ability to interpret data and evaluate business processes.
- [required] Experience supporting cross-functional projects or product initiatives.
- [required] Ability to document business requirements and translate stakeholder needs into actionable deliverables.
- [required] Strong written and verbal communication skills.
- [required] Excellent organizational skills and attention to detail.
- [required] Ability to work effectively in a collaborative, cross-functional environment.
- [role] All activities involving access to Mastercard assets, information, and networks comes with an inherent risk to the organization and, therefore, it is expected that every person working for, or on behalf of, Mastercard is
- [role] Abide by Mastercard’s security policies and practices;
- [role] Ensure the confidentiality and integrity of the information being accessed;
- [role] Report any suspected information security violation or breach, and Complete all periodic mandatory security trainings in accordance with Mastercard’s guidelines.
- [role] Mastercard benefits for interns generally include: 56 hours of Paid Sick and Safe Time; jury duty leave; and on-site fitness facilities in some locations.
- [role] THIS POSTING RELECTS ONE OR MORE CURRENT VACANCIES ON OUR TEAM, AND MASTERCARD INTENDS TO FILL THIS POSITION BY 01/03/2027.
- [role] If the fill date has passed, Mastercard intends to fill this position as soon as possible.
- [role] Mastercard powers economies and empowers people in 200+ countries and territories worldwide.
- [role] Together with our customers, we’re helping build a sustainable economy where everyone can prosper.
- [role] We support a wide range of digital payments choices, making transactions secure, simple, smart and accessible.

### 86bd06c2d0b9c57a5ebb
**Oracle — Principal Program Manager — Business Operations**

- [required] Business operations, program management, strategy, or related roles in a complex, fast-paced environment.
- [required] Demonstrated experience leading cross-functional programs, business operations initiatives, or operational improvement programs within a complex organization.
- [required] Proven ability to independently manage multiple programs or workstreams involving diverse stakeholders, competing priorities, dependencies, and evolving requirements.
- [required] Experience evaluating business processes, identifying operational gaps, implementing practical improvements, and supporting sustained adoption.
- [required] Ability to develop governance mechanisms, establish clear ownership, document decisions, and drive accountability for deliverables.
- [required] Experience using operational data, reporting, and tracking tools to monitor progress, identify risks, and support business decisions.
- [required] Demonstrated ability to influence stakeholders without direct authority, navigate ambiguity, exercise sound judgment, and drive initiatives to completion.
- [required] Excellent organizational skills, attention to detail, and consistent follow-through, with the ability to balance recurring operational responsibilities and strategic improvements.
- [required] Strong program management fundamentals, including planning, execution, risk management, issue resolution, stakeholder coordination, and commitment tracking.
- [role] In this role, you will drive cross-functional initiatives spanning operational governance, third-party reviews, purchasing workflows, internal business tools, and business commitment tracking.
- [role] You will partner with technical and business stakeholders to simplify complex processes, improve operational visibility, and establish clear ownership and accountability.
- [role] Lead cross-functional business operations programs, managing priorities, dependencies, risks, and deliverables across multiple stakeholder groups.
- [role] Drive operational governance and recurring business reviews, ensuring clear decisions, assigned ownership, documented actions, and timely follow-through.
- [role] Coordinate purchasing workflows, including complex purchase request consolidation, stakeholder alignment, issue resolution, and process improvements.
- [role] Partner with business and technical teams to gather requirements, prioritize enhancements, and improve the usability and effectiveness of internal business operations tools.
- [role] Establish and maintain processes for tracking business commitments, monitoring progress, identifying risks, and providing actionable operational reporting.
- [role] Analyze operational data, workflow performance, and stakeholder feedback to identify inefficiencies and recommend practical improvements.
- [role] Develop standardized processes, governance documentation, training materials, and self-service resources to improve consistency and reduce manual effort.

### b5aebd424c48827d0d3c
**CACI — HCM Analytics & BI Developer**

- [required] Business intelligence, analytics, or enterprise reporting
- [required] Experience developing accessible analytics meeting
- [required] Section 508 / WCAG standards
- [required] Familiarity with Agile/SAFe development practices
- [required] Experience building interactive dashboards with drill-downs, parameters, and dynamic visualizations
- [required] Knowledge of core HR/workforce analytics topics such as headcount, attrition, time-to-fill, workforce planning, and span of control
- [required] Oracle OTBI, Oracle BI Publisher, or similar enterprise BI tools (Power BI, Tableau, SAP BO)
- [required] Strong SQL proficiency with experience writing analytical queries against large-scale HR/HCM datasets
- [role] CACI as a Principal Consultant (HCM Analytics & BI Developer) and play a pivotal role in one of the largest federal Human Capital Management modernization initiatives in the country.
- [role] This enterprise-wide program will implement
- [role] In this high-impact role, you will architect and deliver the analytics ecosystem that informs federal HR leaders, agency executives, and government decision-makers.
- [role] You will build real-time workforce dashboards, compliance reporting solutions, predictive analytics models, and enterprise data visualizations that shape human capital strategies across the federal landscape.
- [role] You will work independently on complex technical and functional assignments, drive reporting innovations, and serve as a trusted expert within the program.
- [role] This role offers long-term project stability, exceptional technical breadth, and the chance to make a measurable difference in government workforce modernization.
- [role] Oracle OTBI dashboards delivering real-time workforce metrics including headcount, hiring pipelines, turnover trends, vacancy rates, and demographic insights across multiple federal agencies
- [role] Oracle BI Publisher (BIP) reports and templates for structured submissions such as EHRI, GHRR, SF-113, and agency-driven HR compliance requirements
- [role] Oracle Analytics Cloud (OAC) dashboards with predictive modeling, trend analysis, and executive-level insights across agencies
- [role] HCM Extract processes for bulk data extraction and downstream analytics workflows

### 0cdfb5091a778710bf98
**Capital One — Senior Associate, Data Scientist - People Strategy & Analytics**

- [role] As a Data Scientist on Capital One’s People Strategy & Analytics team, you’ll be on the leading edge of applying analytics to talent, combining artificial intelligence, machine learning, and social science to build model
- [role] Support development of natural language processing and machine learning models through all phases, from design through training, evaluation, validation, and implementation
- [role] Apply expertise in using open source large language models (LLMs) through prompt engineering, retrieval-augmented generation (RAG) and evaluation metric frameworks for business specific applications
- [role] Partner with a cross-functional team of data scientists, software engineers, business analysts, and product managers to deliver industry leading HR tools and AI-powered products
- [role] Leverage a broad stack of technologies — Python, SQL, AWS, LangChain, Hugging Face Transformers, VectorDBs, Pytorch/TensorFlow, and more — to reveal the insights hidden within large volumes of numeric and textual data
- [role] Flex your interpersonal skills to collaborate with internal stakeholders, translating complex data science work into tangible, aligned business outcomes.
- [required] Currently has, or is in the process of obtaining one of the following with an expectation that the required degree will be obtained on or before the scheduled start date:
- [required] A Master's Degree in a quantitative field (Statistics, Economics, Operations Research, Analytics, Mathematics, Computer Science, or a related quantitative field) or an MBA with a quantitative concentration
- [required] A Bachelor's Degree in a quantitative field (Statistics, Economics, Operations Research, Analytics, Mathematics, Computer Science, or a related quantitative field) plus performing data analytics
- [role] Senior Associate, Data Scientist - People Strategy & Analytics
- [role] Data is at the center of everything we do.
- [role] As a Data Scientist at Capital One, you’ll be part of a team that’s leading the next wave of disruption at a whole new scale, using the latest in computing and machine learning technologies and operating across billions 
- [role] People Strategy & Analytics brings data, analytics, and insights to shape critical talent decisions and strategy at Capital One.
- [role] We work closely with HR partners and senior executives in shaping talent policy, automating real-time data, and improving talent decision-making.
- [role] The team is comprised of people with diverse skills and backgrounds including: data analysts, data scientists, business analysts, HR specialists, project managers, and industrial/organizational psychologists.
- [required] Master’s Degree in “STEM” field (Science, Technology, Engineering, or Mathematics), or PhD in “STEM” field (Science, Technology, Engineering, or Mathematics)
- [required] Experience working with AWS
- [required] Passionate about human capital: You are excited by the value we can add to our company and our associates, and are inspired to help make a large positive impact.

### f099114fb674b910d8c4
**Accenture — Enterprise & Client Solutions Manager, AI, Song Life - part of Accenture Song**

- [required] AI solution development, digital transformation, creative technology, product innovation, or a related field
- [role] You'll help reinvent how Accenture Song works and what it sells — turning emerging AI capabilities into real, repeatable offerings that create genuine value for clients.
- [role] Act as a creative engineer and maker — not just a recommender.
- [role] If you can imagine it, you should be able to rough it out.
- [role] Prototype rapidly: turn a brief, a conversation, or a napkin idea into something tangible that a team can react to and a client can see
- [role] Design AI-native solutions from scratch — not retrofitted workflows, but experiences and systems built around what AI actually makes possible
- [role] Bring creative instincts to technical problems and technical instincts to creative ones
- [role] Work with Song leadership to identify which prototypes have the legs to become formal, repeatable offerings
- [role] Define what an offering actually is: the problem it solves, the value it delivers, how it's built, and how it gets sold
- [role] Develop the reusable assets, playbooks, and reference models that let teams deliver it again and again
- [role] Ensure everything you build is designed for scale — not one-off heroics
- [role] Serve as the connective tissue between strategy, creative, data, technology, and operations — fluent in all of them, siloed in none
- [role] Help teams choose the right tools for the right job, without attachment to any one platform or approach
- [role] Make the complex feel simple: translate AI capabilities into plain language that inspires action at the leadership level
- [role] Facilitate working sessions, sprints, and incubators that move quickly from "what if" to "here's how"
- [role] Question established processes — especially the ones people have stopped questioning
- [role] Advocate for AI-first approaches rather than AI bolted onto the way things have always been done
- [role] Help the organization distinguish between process optimization and genuine reinvention

### 87933d4d2f593af71687
**Lyric — Forward Deployed Solution Strategist  (US)**

- [required] Adept knowledge of supply chain domains with the ability to engage in both technical and business-level discussions
- [required] Deep fluency in at least one core domain: Modeling & Analysis, Planning & Execution, or Frontier Use Cases (AI / Machine Learning)
- [required] Proven ability to drive platform adoption and measurable value through stakeholder engagement, value quantification, and quarterly success review leadership
- [required] Excellent communication skills across business and technical audiences; capable of building high-trust relationships from end users to senior executives
- [required] Skilled at identifying expansion opportunities, shaping growth narratives, and feeding structured insights back to internal cross-functional teams (including Sales, Product, and Engineering) for continuous improvement
- [required] Comfortable training and enabling customers on a dynamic, technical platform; able to instill confidence in less-technical users; and can get hands-on with the platform to prototype alongside customers
- [required] Highly adaptable, coachable, and comfortable navigating ambiguity in a dynamic environment
- [required] Even if you don’t meet 100% of the qualifications, we recommend applying to the role!
- [required] Up to 30% travel expected, primarily within the US.
- [required] Base cash will be determined based on years of experience with a 30% variable compensation that is earned based on individual & business performance.
- [required] You will also have the opportunity to receive equity in Lyric.
- [role] Lyric is hiring a Forward Deployed Solution Strategist within its Customer Success organization to drive adoption, retention, and expansion across some of our most strategic customer relationships.
- [role] This is a Director-level, customer-facing role that blends deep supply chain expertise, strong technical fluency, and exceptional relationship-building to navigate customer organizations.
- [role] As a Forward Deployed Solution Strategist, you are both core strategic advisor and business partner to Lyric’s enterprise customers.
- [role] Lyric Studio is a platform built to “start anywhere, scale everywhere” with the vision to transform all parts of supply chain operations.
- [role] We call this lighting up the supply chain decision mesh.
- [role] The Forward Deployed Solution Strategist (FDSS) plays a key role in shaping how customers realize value from their partnership with Lyric and expand into new parts of the decision mesh.
- [role] The ideal candidate is T-shaped, capable of engaging broadly across business and technical topics, and brings deep expertise in one of three areas: advanced modeling, operational planning, or AI and machine learning–driv

### d039b3ff3f6c30302fc9
**Sedgwick — VP Business Transformation - Finance**

- [required] Certification one of the following areas required (e.g., Project Management Professional (PMP), Program Management Professional (PgMP), Certified Business Transformation Professional (CBTP), Change Management Certified P
- [required] Business transformation, with several years in a management role having significant business impact or equivalent combination of education and experience required.
- [required] Experience in management consulting and/or enterprise transformation roles within B2B environments required.
- [required] Proven track record in strategy execution, analytics initiatives, and large-scale change programs
- [required] Strong collaboration, influence, and communication skills with ability to operate at senior leadership level
- [required] Applied knowledge of technology-enabled operating models and data dependencies
- [required] Proven management/leadership and negotiating skill
- [required] Ability to create and complete comprehensive, accurate, and constructive written reports
- [required] Ability to work in a team environment and meet or exceed performance competencies
- [required] Licenses as needed.
- [required] Analytical, innovative, and comfortable navigating ambiguity in complex organizations
- [required] The statements contained in this document are intended to describe the general nature and level of work being performed by a colleague assigned to this description.
- [required] They are not intended to constitute a comprehensive list of functions, duties, or local variances.
- [required] Management retains the discretion to add or to change the duties of the position at any time. #LI-TS1 #remote
- [role] Leads transformation initiatives that support business unit growth objectives and deliver measurable impact through structured execution and strategic alignment.
- [role] Partners with divisional and functional leaders to identify and focus on high-value, enterprise-critical initiatives that advance organizational priorities.
- [role] Designs and delivers transformation programs by coordinating cross-functional teams, ensuring seamless execution and alignment with strategic goals.
- [role] Leads process redesign efforts with an emphasis on data-driven thinking and technology enablement to enhance efficiency and unlock innovation.

### 4ac72130dc4c72f3047e
**Capital One — Director, Product Development, AI Enablement**

- [role] A Director of AI Enablement Product Management is a strategic hands-on product leader who will fundamentally rethink how the eData product organization - and ultimately Capital One - works with AI.
- [role] You will act as the tip of the spear in driving enterprise-wide adoption of AI workflows, transforming our operations at every level: the tools we build, the processes we follow, and the mechanisms we use to measure succ
- [role] Unlike traditional product managers who focus on deterministic software features, or AI-native product managers who train foundational models, an enablement product manager focuses on the operational layer: transforming 
- [role] AI Platform Strategy: Define and execute a multi-year product roadmap for internal AI infrastructure tailored to enterprise operations.
- [role] Intelligent Workflows: Architect and deploy autonomous AI agents and advanced RAG systems to transform high-friction processes.
- [role] Blueprint & Standard Rollout: Create and deploy enterprise patterns, guardrails, and AI architectures across engineering and non-engineering functions for a faster, better quality outcome of the product development proce
- [role] Workflow Transformation: Replace manual toil and high-friction processes with reusable platform components and AI agents.
- [role] Data Democratization & Integration: Build intuitive AI-driven semantic layers and integrate agentic AI/ML capabilities into core front-to-back workflows via flexible integration techniques (API, MCP, CLI).
- [role] Governance, Compliance & Security: Establish ironclad LLM evaluation pipelines and guardrails that ensure strict data privacy, compliance, and zero-hallucination thresholds for reporting.
- [role] Adoption & Change Management: Partner with internal champions and partners to ensure behavioral adoption rather than just feature deployment.
- [role] Cross-Functional Synergy: Act as the primary strategic translator between core data engineering teams, corporate finance executives, legal teams, and business unit leaders.
- [role] Feedback Loops: Serve as the structured bridge between business users and core infrastructure or engineering teams to refine future product strategy.
- [required] Bachelor's degree in any field and Product Management experience
- [role] In this role, you’ll be expected to demonstrate proficiency in five key areas which we consider to be the foundation for successful Product management:
- [role] Human Centered - Obsesses about internal and external customer needs to reimagine and innovate product solutions
- [role] Business Focused - Delivers game-changing outcomes by focusing on leverage and execution excellence
- [role] Technology Driven - Leverages technology to deliver innovative and resilient solutions that enable both near term and long term value
- [role] Integrated Problem Solving - Identifies and resolves complex problems to deliver outcomes while mitigating product risks

### 864fd10fbbf2a8d9d44a
**BDO USA — Change Management Senior Manager - Public Sector, Intel**

- [required] Organizational change management (OCM), business transformation, or project delivery, required
- [required] Advanced proficiency in the use of Excel (pivot tables, VLOOKUP, raw data manipulation), required
- [required] Proficiency in the use of Microsoft Office Suites, required
- [role] BDO is seeking a senior manager level professional to do change management/training support for Intelligence Community (IC) Agencies.
- [role] This role involves guiding organizations through complex system and process transformations.
- [role] This person must be skilled in stakeholder engagement, impact assessments, and training strategy design that reduce user resistance.
- [role] Proven track record of increasing project adoption rates through structured change frameworks.
- [role] Creates/contributes to structured training programs
- [role] Writes clear user guides, manuals, and e-learning content
- [role] Develops plans to accelerate employee adoption of new processes
- [role] Drafts communications to brief stakeholders on project changes
- [role] Facilitates workshops, webinars, and instructional sessions for diverse audiences
- [role] Evaluates how changes affect daily operations, map key stakeholders, and measure organizational readiness
- [role] Designs and delivers transparent communication materials to keep staff informed
- [role] Partners with training teams to build guides, workshops, and programs that prepare employees for new skills or tools
- [role] They will also be responsible for answering urgent taskers requesting group specific information.
- [role] Interprets management requests and autonomously develop relevant financial reports
- [role] Delivers a full range of services that uses their educational background, experience, and organizational skills while serving BDO’s dynamic public sector client base
