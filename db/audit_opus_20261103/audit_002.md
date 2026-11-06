# Labeling batch audit_002 — grade the WORK, not the candidate's odds


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
Return ONE JSON object per posting below, as a JSON array, written to `audit_002.result.json`. No prose, no markdown fence.
Grade every posting in this file, on ALL THREE lenses. Use the posting_id exactly as given.

## Postings (20)

### f8ac1b009e6e5cd7ca57
**NTT Global Data Centers Americas — Service Delivery Program Manager, GID Operational Excellence**

- [required] Proven ability to contribute to performance strategies that drive operational excellence and business outcomes.
- [required] Extended knowledge of data center facilities, infrastructure, best practices, and industry standards
- [required] Effective leadership and team management skills when influencing without authority.
- [required] Extended communication and interpersonal skills for effective collaboration, stakeholder management, and reporting.
- [required] Extended problem-solving and analytical abilities to address complex challenges.
- [required] Commitment to continuous learning and staying updated with industry trends and best practices.
- [required] Good financial acumen, budgeting skills, and experience managing large construction project budgets.
- [required] Bachelor's degree or equivalent in Engineering, Construction Management, Compliance, Business Administration or related field.
- [required] Extensive experience gained in a similar role within a global services organization.
- [required] Extensive experience in project management methodologies, tools, and practices.
- [required] Experience managing data center construction projects.
- [required] Experience managing and leading data construction project resources.
- [required] Perform work from a remote location with stable internet connection.
- [role] Drive operational efficiency and standardization across global construction.
- [role] Identify and implement scalable process improvements.
- [role] Streamline workflows and eliminate inefficiencies.
- [role] Deploy best practices and digital tools to enhance performance.
- [role] Collaborate cross-functionally to align with strategic goals.

### c17382938a8beda95e0b
**AHEAD — Senior Consultant — Enterprise Change & Transformation**

- [required] Strong understanding of change management fundamentals and enterprise change capability building, backed by recent, relevant hands-on OCM or transformation experience; experience leading, driving, and facilitating change
- [required] Ability to assess organizational readiness for change and drive and adapt communication strategies for maximum impact while gaining support across all levels of the organization.
- [required] Ability to lead the design and stand-up of scalable change capability (Centers of Excellence, self-service playbooks, manager-led practices) rather than only executing a project-specific plan.
- [required] A bility to define and direct outcome- and value-based metrics, distinct from activity metrics, and to connect change adoption to business results.
- [required] Working proficiency with enterprise AI tools for communications drafting, sentiment analysis, and impact/stakeholder mapping, applied with sound judgment, and the ability to train other practitioners in their use.
- [required] Strong project management, critical thinking, problem-solving, data analysis, and information-structuring skills; must be able to translate plans/policies into specific goals, tactics, strategic action plans, and deliver
- [required] Ability to lead digital transformation road mapping and direct defined initiative workstreams in partnership with client stakeholders and project teams.
- [required] Working knowledge of project economics billing rates, utilization, and margin sufficient to support scope and staffing conversations with engagement leadership.
- [required] Proficient in Microsoft Office (Outlook, Word, Excel, PowerPoint, Visio, and Project); leverages these tools to develop work plans, articulate complex issues, and structure information.
- [required] Demonstrated ability and a proven track record of success in delivering client services in a collaborative, dynamic, and fast-paced environment.
- [required] Client relationship management, including building trust, managing expectations, and serving as a credible advisor to business and IT stakeholders at the manager and director level.
- [required] Business acumen, with the ability to understand client industries, operating models, and strategic priorities to frame recommendations in business terms.
- [required] Executive presence and polished presentation skills, with the ability to communicate complex ideas clearly to senior leadership and diverse audiences.
- [required] Analytical and structured problem-solving skills, with the ability to break down ambiguous business problems into clear, actionable workstreams and defensible hypotheses.
- [required] Effective time management and prioritization across multiple workstreams, deliverables, and competing deadlines in a fast-paced consulting environment.
- [required] Collaborative teaming skills, working effectively within cross-functional project teams and adapting style to diverse team and client cultures.
- [required] Formal coaching and mentorship skills — develops junior team members' OCM capability and project delivery expectations, with accountability for their growth.
- [required] Proficiency with core consulting toolkits (e.g., PowerPoint, Excel, Word) and enterprise AI tools to produce polished deliverables, executive-ready presentations, and data-driven analyses.

### e34cc44733bf8f5820ca
**Salesforce — Forward Deployed Process Senior/Principal Process Consultant**

- [required] Product Management, Process Automation, Business Analysis, or Solutions Consulting
- [required] Experience building external-facing products in enterprise B2B SaaS environments
- [required] Strong understanding of business workflow mapping, process modeling, and process improvement methodologies
- [required] Proven experience translating business requirements into structured automated workflows or solutions
- [required] Excellent spoken and written communication with ability to present complex ideas clearly to both technical and non-technical audiences
- [required] Proven ability to collaborate with virtual and global cross-functional teams including engineering, design, and customer success
- [required] Rigor and consistency in managing product backlogs and executing delivery
- [required] Hands-on technical experience with automation tools, workflow builders, AI prompting, or low/no-code platforms
- [role] Own the roadmap for process blueprints and automation solutions, balancing strategic direction with foundational execution needs
- [role] Own the feature lifecycle for specific components from discovery through launch and iteration
- [role] Identify operational inefficiencies, manual processes, and automation opportunities across customer and internal workflows
- [role] Partner with customer-facing team leaders to map and analyze customer requirements for new solutions
- [role] Build solution blueprints and workflow automations using current platform features
- [role] Work directly with AI agents and prompting tools to design automated, enterprise-grade solutions
- [role] Validate, test, and iterate blueprints for accuracy, consistency, and performance
- [role] Translate high-level strategic goals and customer requirements into clear Product Requirement Documents (PRDs), user stories, and technical acceptance criteria
- [role] Manage the product backlog, prioritize sprints, and drive consistent delivery using Agile methodologies
- [role] Document solution architecture, demonstrations, and implementation guidelines

### abbfa92341970a37c59a
**RTX — Business Analyst**

- [required] Experience supporting software applications throughout their lifecycle.
- [required] Experience gathering and documenting complex business requirements.
- [required] Experience facilitating discussions among technical and non-technical stakeholders.
- [required] Typically requires a Bachelors degree with or Advanced degree.
- [required] Experience working within Agile/Scrum delivery environments.
- [role] Collaborate with stakeholders to gather, analyze, and document complex business requirements.
- [role] Facilitate discussions between technical and non-technical stakeholders to ensure alignment and understanding.
- [role] Support internally developed software applications throughout their lifecycle, including requirements management and process improvement.
- [role] Conduct backlog refinement and prioritize requirements with product owners and development teams.
- [role] Utilize requirements management, process modeling, and documentation tools to create actionable solutions.
- [role] Analyze business processes and recommend improvements to enhance efficiency and effectiveness.
- [role] Ensure deliverables align with Agile/Scrum methodologies and organizational goals.
- [role] Strong record maintenance and attention to detail in support of ongoing application re-certifications process.
- [role] Work independently while collaborating effectively in a team environment.
- [required] We are seeking a Business Analyst to support mission-critical applications, serving as a vital liaison between business stakeholders, product owners, development teams, and end-users.
- [required] This role ensures applications continue to meet evolving business needs and align with strategic organizational objectives.
- [required] Familiarity with international trade processes, import/export compliance, or customs-related business functions.
- [required] Experience with system audits.

### d349ed4ae9f3ff1b054b
**U.S. Government (USAJobs) — Program Manager (Business Analysis & Formulation) (Budget Analysis and Formulation Group Team Manager)**

- [required] Experience with the budget formulation process to participate in the formulation of out year requirements and propose future funding needs
- [required] -Experience assessing operational needs, forecasting expenses, and developing a budget that aligns with program goals and funding constraints
- [required] AND -Experience developing program budget and operating plan and coordinating the different phases of the budget process to enhance strategic insight.
- [required] Answer all questions to the best of your ability.
- [required] DO NOT ASSUME THAT BECAUSE YOU HOLD, OR HAVE HELD THIS POSITION, YOU WILL AUTOMATICALLY BE FOUND ELIGIBLE.
- [required] You may be asked to provide evidence or documentation that you have this type of experience later in the selection process.
- [required] Your responses are subject to verification through job interviews, or any other information obtained during the application process.
- [required] Any exaggeration of your experience or any attempt to conceal information can result in disqualification. *Applicants who fail to demonstrate possession of any of the above criteria AND who do not provide the required do
- [required] You must demonstrate in your application that you possess specialized experience equivalent to the FV-J, FG/GS-14/15 level.
- [required] This experience is typically related to the line of work of the position to be filled and which has equipped the applicant with the particular knowledge, skills, and abilities to successfully perform the duties of the po
- [required] You should include relevant examples of the specialized experience in your work history.
- [required] qualification requirements must be met by the closing date of this vacancy announcement.**
- [required] A one-year probationary period may be required.
- [role] The Program Manager serves as a Team Manager within the Budget Analysis and Formulation Group (AJV-H6), overseeing a diverse and geographically distributed team of highly skilled analysts, typically through subordinate m
- [role] This role involves coordinating the efforts of multiple organizations to achieve the strategic objectives of the Federal Aviation Administration (FAA).
- [role] The Program Manager is responsible for making significant commitments regarding programs and budgets for a major organization and aligning programs and activities with the mission and goals of Enterprise Services, Missio
- [role] Key responsibilities include leading complex national financial and budget projects and programs that align with the objectives of AJV, the ATO, and the FAA.
- [role] The Program Manager must apply a deep understanding of the technical aspects of budget formulation and business analysis to guide the team in gathering, analyzing, classifying, and prioritizing ATO Operations program fun

### 99fd975b495ce4106cb4
**RTX — Process Transformation & Standard Work Principal Specialist**

- [required] Experience with supply chain information systems, tools, and technologies.
- [required] Experience in data analytics, including analyzing trends and presenting actionable insights.
- [required] Hands-on experience with CORE principles, Lean Six Sigma methodologies, or a similar process improvement framework.
- [required] Experience managing complex, cross-functional projects.
- [role] Collaborate with stakeholders, internal customers, and cross-functional teams to implement supply chain process improvements and standard work initiatives.
- [role] Design, develop, and deliver standardized supply chain training programs aligned with RTX Corporation’s strategic objectives.
- [role] Utilize supply chain tools, data analytics, and information systems to enhance training programs and operational efficiency.
- [role] Analyze supply chain data to identify trends, inefficiencies, and opportunities for improvement, and recommend actionable solutions.
- [role] Apply CORE principles or Lean Six Sigma methodologies to promote continuous improvement in supply chain processes.
- [role] Lead and manage complex, cross-functional projects, ensuring alignment with timelines, budgets, and deliverables.
- [role] Provide guidance and mentorship to team members and stakeholders on best practices and standardized processes.
- [role] Monitor and report on the effectiveness of training initiatives and process improvements, ensuring alignment with organizational goals.
- [role] Stay updated on industry trends and emerging technologies to identify opportunities for innovation in supply chain operations
- [required] Typically requires a university degree or equivalent experience and prior relevant experience, or an advanced degree in a related field and
- [required] Raytheon brings the strength of more than 100 years of experience and renowned engineering expertise to meet the needs of today’s mission and stay ahead of tomorrow’s threat.
- [required] We deliver solutions that help our nation and allies defend freedoms and deter aggression, creating a safer, more secure world.
- [required] Raytheon is seeking a Process Transformation & Standard Work Principal Specialist to lead the development, delivery, and optimization of standardized supply chain processes and training.
- [required] This role will leverage expertise in CORE (Customer Oriented Results and Excellence) or Lean Six Sigma methodologies, supply chain systems, and project management to drive innovation and efficiency in supply chain operat

### 82723b14effd062c7f60
**Oracle — Lead Principal Technical Program Manager**

- [role] The Low Voltage Vendor team is seeking experienced program managers who have worked directly in infrastructure deployment or supported continuous improvement initiatives focused on delivering data center capacity at scal
- [role] Dashboard Creation and Reporting: Own the definition, adherence, and delivery of team and org wide goals relating to quality, savings, and delivery.
- [role] You will own the creation and fidelity of large data sets used to drive the decision making of the organization to improve vendor throughput.
- [role] Experience in programming languages, modeling, and dashboard creation is key.
- [required] Proven experience leading large-scale data center or infrastructure programs.
- [required] Strong ability to operate in ambiguous, fast-changing environments.
- [required] Deep understanding of cloud infrastructure, networking, and hardware deployment.
- [required] Demonstrated success driving cross-functional alignment and execution at scale.
- [required] Technical program management experience.
- [role] In this role, you will be part of the Low Voltage Vendor Management (LVV) team and will collaborate with internal OCI teams and external cabling vendors to deliver new sites, support space and power expansion, and add cu
- [role] Project Management: Oversee a broad portfolio of global data center projects, with the goal of delivering capacity to customers on time and as efficiently as possible.
- [role] Provide regular project status updates, conduct monthly business reviews, and address concerns promptly.
- [role] Frequent communication with executive leadership is expected.
- [role] This is a high-impact role responsible for leading, driving, and ensuring the successful delivery of critical data center infrastructure builds on our roadmap.
- [role] Your ability to anticipate and mitigate risks to data center delivery will be critical to maintaining project timelines.
- [role] The position offers a high degree of personal accountability and autonomy and requires effective engagement with a wide range of stakeholders, including design, engineering, operations, supply chain, external vendors, an
- [required] Executive communication and stakeholder management skills.
- [role] Risk Management: Identify potential risks and develop mitigation strategies to minimize their impact on project timelines and costs.

### bb74879f59f6dcf7c325
**GDIT — Cloud FinOps Lead**

- [required] Continuous Improvement, Financial Management, Key Performance Indicators (KPI) Certifications:
- [required] Demonstrated expertise with multi‐cloud environments (Azure, AWS, VAEC) and cloud financial management tools
- [required] Ability to evaluate cloud cost reports, usage models, allocation data, tagging strategies, and forecasting outputs for accuracy and completeness
- [required] Excellent written, analytical, and stakeholder communication skills
- [required] Ability to collaborate across cloud engineering, finance, cybersecurity, hosting operations, and federal organizations
- [required] Experience leading cloud financial operations (FinOps), cloud cost management, or enterprise financial governance
- [required] Strong understanding of ITSM, cloud service delivery, financial analytics, forecasting, and governance frameworks
- [required] Experience developing financial performance plans, optimization dashboards, reporting packages, unit economics models, and corrective‐action processes
- [role] Join GDIT to support the Department of Veterans Affairs (VA) through the Infrastructure Operations Support Services program.
- [role] As the FinOps Lead, you will oversee VA’s enterprise cloud financial operations ensuring accurate forecasting, cost optimization, transparent reporting, and strong financial governance across all VA Enterprise Cloud (VAE
- [role] In this role, you will lead cloud financial management processes, maintain cost models and dashboards, strengthen tagging and allocation strategies, identify optimization opportunities, and ensure VA meets key FinOps per
- [role] You will collaborate with cloud engineers, architects, financial analysts, cybersecurity teams, and VA stakeholders to drive financial efficiency, improve cloud spend visibility, and support data-driven decision making.
- [role] You will manage cloud cost reporting, unit economics analytics, automated financial feeds, showback/chargeback workflows, and monthly/quarterly performance packages for leadership.
- [role] You will monitor cost anomalies, refine allocation models, enhance optimization pipelines, and streamline financial operations across hosting and multi-cloud environments.
- [role] You will also lead customer engagement with VA leadership and service line managers—delivering cost reviews, briefings, forecasting insights, modernization roadmaps, and optimization recommendations that strengthen FinOp
- [role] Finally, you will drive continuous improvement by refining tagging standards, enhancing dashboards, modernizing reporting structures, embedding automation, addressing inefficiencies, and maturing VA’s enterprise cloud fi
- [required] Bachelor’s degree or equivalent and
- [role] Seize your opportunity to make a personal impact as a CLOUD FINOPS LEAD supporting the Department of Veterans Affairs .

### fe9c5728710c789372fd
**Summit Therapeutics — Director, Clinical Operations Analytics & Performance Excellence**

- [required] Bachelor's degree in Life Sciences, Data Analytics, Business, Information Systems, Public Health, or related discipline.
- [required] Clinical Operations, Clinical Systems, Clinical Analytics, RBM, or related functions.
- [required] Leadership experience managing teams and complex cross-functional initiatives.
- [required] Deep understanding of clinical trial execution processes and operational metrics.
- [required] Demonstrated experience with Clinical Operations technology platforms and reporting.
- [required] Experience building executive dashboards and performance management frameworks.
- [required] Strong knowledge of ICH-GCP, regulatory requirements, and inspection readiness expectations.
- [required] Proven ability to lead organizational change initiatives.
- [role] Develop and execute the Clinical Operations analytics roadmap aligned with organizational objectives, including priorities for AI, automation, predictive analytics, and advanced reporting.
- [role] Establish the portfolio performance measurement framework, defining leading and lagging indicators to assess study performance, operational health, quality, and execution risk.
- [role] Translate the performance measurement framework into standardized Clinical Operations KPIs, portfolio health metrics, and reporting across clinical programs.
- [role] Establish operational scorecards for studies, sites, countries, vendors, and functions to monitor performance and quality.
- [role] Develop and maintain portfolio dashboards and executive reporting that provide visibility into Clinical Operations performance.
- [role] Partner with Clinical Operations leaders to interpret portfolio trends, identify risks, and recommend actions to improve study execution.
- [role] Present actionable portfolio insights and strategic recommendations to leadership and executive teams to support performance reviews and data-driven decision-making.
- [role] Clinical Operations Data Governance & Data Quality
- [role] Serve as the functional leader for Clinical Operations data governance, defining ownership and accountability for operational data quality.
- [role] Serve as the SME in data and processes in Veeva Vault Clinical, including processes within CTMS, SSU and Site Engagement capabilities.

### 1f9b85d4109d6ea69b64
**Amgen — Associate Director, Digital Product Operations - Measurement & Reporting**

- [required] Leadership, Digital Transformation, Technology Strategy, or related experience
- [role] The Associate Director, Product Operations - Measurement & Reporting will guide and lead the enterprise measurement and reporting problem space for the Digital Product Operating Model (DPOM), helping Amgen measure, under
- [role] Reporting to the Senior Director, Digital Product Operations, this individual contributor role will serve as a product management subject matter expert for DPOM measurement.
- [role] The role will shape the strategy, standards, decision systems, dashboards, and leadership-ready insights that connect DPOM adoption to enterprise operating rhythms and measurable product management outcomes.
- [role] This leader will translate the DPOM roadmap into coherent measurement strategy, integrate quantitative and qualitative signals, guide senior cross-functional partners through trade-offs, and ensure reporting drives bette
- [role] In addition, this role will lead regular maturity assessments of the Digital Product Management organization to identify where progress is being made in building product management capabilities and where gaps to address 
- [role] Lead the enterprise measurement strategy for DPOM adoption, product management maturity, team health, and product artifact quality across multiple teams and organizational areas.
- [role] Translate Product Management Hub priorities into a coherent measurement framework, maturity model, executive reporting approach, and roadmap for continuous improvement.
- [role] Define the standards, dimensions, and indicators used to assess adoption, maturity, artifact quality, operating health, and realized value of product ways of working.
- [role] Ensure the measurement approach distinguishes activity metrics from meaningful signals of adoption, maturity, decision quality, user value, and enterprise impact.
- [role] Frame ambiguous measurement problems, test assumptions, and determine which signals merit investment, instrumentation, leadership attention, and operating-model change.
- [role] Own the strategy and evolution of DPOM dashboards, scorecards, reporting mechanisms, and executive readouts for MBRs, QBRs, and other enterprise operating cadences.
- [role] Guide analytics, technology, reporting, and product operations partners in building scalable, trusted reporting solutions.
- [role] Integrate quantitative data, qualitative field insights, artifact reviews, and leadership feedback into clear findings, trends, risks, and recommendations.
- [role] Create leadership-ready narratives that help Product Management Hub leadership, the VP of Product Management, the CTO, technology leaders, and business leaders understand progress, barriers, and trade-offs.
- [role] Use reporting to surface cross-product dependencies, adoption barriers, maturity gaps, and opportunities to improve DPOM effectiveness across the enterprise.
- [role] Lead the assessment approach for product management maturity across teams, including outcome orientation, roadmap quality, OKR quality, decision-making, discovery, prioritization, value measurement, and lifecycle managem
- [role] Identify recurring maturity gaps, anti-patterns, adoption friction, and capability needs across product teams, product groups, and business functions.

### 743ec8a75a41f681e8ca
**McKesson — Product Manager, Intelligent Platforms - Healthcare**

- [required] Product management experience delivering digital products, platform products, APIs, or internal tooling solutions.
- [required] Strong understanding of modern architecture patterns, including APIs, API gateways, microservices, BFF architectures, event-driven systems, cloud platforms, and identity/access management.
- [required] Experience launching and managing AI or machine learning capabilities such as conversational AI, generative AI assistants, document processing, or intelligent automation solutions.
- [required] Proven success leading legacy modernization initiatives through phased migration strategies.
- [required] Strong product discovery, roadmap prioritization, stakeholder management, and agile delivery experience.
- [required] Ability to communicate effectively with both executive business stakeholders and technical teams.
- [required] Demonstrated use of data and analytics to inform decisions and measure outcomes.
- [required] Experience leveraging AI tools to improve productivity and product management practices.
- [role] Own the vision, roadmap, and backlog for role-based digital workspaces that unify workflows, communications, data, and AI-powered assistance.
- [role] Lead user discovery with internal and external stakeholders to identify pain points and translate insights into prioritized product investments.
- [role] Define and execute phased delivery strategies through MVPs, pilots, and scalable release plans that maximize user value and platform reuse.
- [role] Define requirements for shared platform services, including API management, backend-for-frontend (BFF) services, event-driven integrations, and identity and access management.
- [role] Manage APIs and platform capabilities as products, establishing clear service definitions, documentation, adoption strategies, and performance expectations.
- [role] Guide modernization efforts that enable legacy and cloud-native systems to operate together while minimizing disruption to business operations.
- [role] Drive requirements for reusable communication services such as SMS, email, fax, voice, and chat capabilities.
- [role] Shape and deliver AI-driven solutions including conversational AI, virtual agents, knowledge search, question-answering experiences, document AI, and generative AI assistants.
- [role] Define and measure success metrics such as containment rates, accuracy, operational efficiency, time savings, and business impact.
- [role] Partner with engineering, data science, security, and compliance teams to ensure AI solutions are governed, explainable, and appropriate for regulated environments.

### 00429bffcf5b85b484ff
**Affirm — Senior Manager, Programs - Global Expansion and New Initiatives**

- [required] Cluding teams and/or managing other managers or senior professionals.
- [required] Experience building and scaling global customer experience operations and working in global environments across cultures and geographies.
- [required] Deep program and process management expertise (PMP and/or Lean Six Sigma Black Belt a plus), with a track record of defining methods and standards — not just applying them.
- [required] Strong knowledge of how shared services operate; experience in services, operations, and/or sales.
- [required] Proven ability to work on unique, poorly defined problems that lack strategic direction — evaluating intangibles, applying conceptual thinking, and creating the approach and evaluation criteria.
- [required] Demonstrated ability to influence and build alignment among diverse, senior stakeholders in complex or sensitive situations.
- [required] Sound judgment to make discretionary decisions with significant latitude, balancing research, creativity, and business impact.
- [required] Self-starter with a strong sense of urgency, accountability, and results orientation, and a genuine enthusiasm for developing people and building new capabilities.
- [role] As we scale into new products, partners, geographies, and channels, we need an exceptional leader to own the strategy of how our servicing operating model evolves globally.
- [role] In this role you'll set the customer operations strategy for a portfolio of complex, cross-functional expansion programs, lead a team of program and process managers, and be accountable for adapting the servicing model t
- [role] In addition, you will be the leader who represents operations across international business teams providing transparency into performance and partnering across product, engineering, and operations to help drive continuou
- [role] The ideal candidate pairs strong business acumen and judgment with the ability to lead teams, shape ambiguous problem spaces into clear operating plans, and influence senior partners across the company.
- [role] A deep passion for growing international businesses and working to understand the localization needs for customer experience and being able to implement with scale and efficiency will be key.
- [role] This role requires deep expertise in the customer/merchant servicing space and a strong grounding in financial services.
- [role] You'll own a portfolio that spans a multitude of international servicing landscapes — launching, testing and learning in operations, and scaling geo expansions, new product scaling, and new channel expansion — and you'll
- [role] Own the strategy and roadmap for how Customer Operations scales to support global expansion — new products, merchants/partners, geo launches, and channels — translating ambiguous, undefined business direction into struct
- [role] Establish and modify the team's operational objectives, policies, procedures, and work plans; define the methods, frameworks, and success criteria used to run initiatives across regions.
- [role] Lead, coach, and develop a team spanning two or more disciplines (e.g., program management and service delivery via process/continuous improvement); oversee professionals and, where applicable, other people leaders; dele

### 72088b010daa5b5cb649
**Gartner — Director, People Analytics - Talent Acquisition**

- [required] Demonstrated strong analytical, organizational, communication and leadership skills at all levels
- [required] Demonstrated ability to solve complex/unstructured business problems.
- [required] Uses data and the economics of the business to analyze problems and recommend solutions (can translate insights into actionable solutions).
- [required] Progressive experience in analytics and HR or consulting.
- [required] Ability to clearly and succinctly communicate with senior leadership to drive decisions.
- [required] Proven ability to influence and coordinate with global team members, anticipate and manage risks, set appropriate expectations and consistently deliver strong results.
- [required] Proven track record of establishing trusted partner relationships, including collaborating with and influencing leaders and teams across multiple business functions.
- [required] Don’t meet every single requirement?
- [required] We encourage you to apply anyway.
- [required] You might just be the right candidate for this, or other roles.
- [required] Competitive compensation.
- [required] Limitless growth and learning opportunities.
- [required] Ongoing mentorship and apprenticeship; Leadership courses, development programs, technical courses, certification opportunities and more!
- [required] A collaborative and positive culture - join a diverse team of professionals that are as smart and driven as you.
- [required] A chance to make an impact – your work will contribute directly to our strategy.
- [required] 20+ PTO days plus holidays and floating holidays in your first year.
- [required] Extensive medical, dental insurance and vision plan.
- [required] 401K with corporate match, immediate vesting.

### ddcd7d43fd78099d0c22
**TribalScale — Transformation Engineer**

- [required] Transformation Experts: We transform traditional business models into agile, AI-powered ecosystems.
- [required] Strategic Visionaries: We navigate the uncharted waters of technological evolution.
- [required] Product Virtuosos: We orchestrate the creation of world-class digital solutions.
- [required] Code Optimizers: We leverage AI to refine and perfect digital systems.
- [required] Mobile & Web Development: (iOS, Android, React Native, React, Node.js)
- [required] Voice-Activated Platforms: (Amazon Alexa, Google Home)
- [required] Connected Ecosystems: (Cars and Homes)
- [required] Streaming Platforms: (Roku, Fire TV, Android TV, tvOS)
- [required] You thrive in challenging environments where innovation is key.
- [required] Your passion for technology is matched only by your drive to redefine its limits.
- [required] You seek more than a job; you seek a calling—a chance to make a lasting impact on the digital landscape.
- [required] Strong hands-on software or AI engineering experience, with proficiency in Python, TypeScript, JavaScript, or comparable languages, and experience delivering production systems.
- [required] Demonstrable experience building LLM knowledge management or retrieval systems, such as RAG, LLM wikis, enterprise search, or related context-engineering solutions.
- [required] Practical expertise designing agentic memory, retrieval strategies, context management, and permission-aware knowledge access.
- [required] Deep, demonstrable Claude Code expertise, with specific examples of workflows, skills, subagents, MCP integrations, or harnesses you have personally built.
- [required] Proven ownership of production agent harnesses, including evaluations, observability, guardrails, debugging, maintenance, and continuous improvement—not just familiarity with the concepts.
- [required] Strong AWS experience, including relevant services such as Lambda, Bedrock, IAM, S3, and vector search or OpenSearch, with attention to security, scalability, and cost.
- [required] Experience working within an organization undergoing significant technology or AI transformation, including the human side of adoption and change management.

### e1c9190b0bf011fdff71
**Mastercard — Vice President, Enterprise Order Management**

- [required] The ideal candidate for the Vice President, Enterprise Order Management position should:
- [required] Bring significant senior leadership experience: building or transforming enterprise capabilities in a complex, global, matrixed organization, with accountability extending beyond a single platform implementation.
- [required] Demonstrate deep expertise in Enterprise Order Management and Order-to-Cash: including the relationships among commercial processes, contracting, order orchestration, fulfillment, billing, invoicing, revenue, payments, c
- [required] Have a proven record of establishing enterprise disciplines and operating models: including governance, decision rights, global standards, engagement models, service ownership, performance measures, and adoption across m
- [required] Show success shaping and executing multi-year transformation roadmaps: with experience aligning vision, investment, architecture, organizational change, delivery sequencing, and measurable value realization.
- [required] Possess strong enterprise systems and integration acumen: with experience influencing cloud and on-premise ecosystems, master and transactional data, APIs and event-based integrations, reconciliation, observability, secu
- [required] Demonstrate executive presence and influence: with the ability to engage senior and executive leaders, frame enterprise decisions, navigate competing priorities, challenge constructively, and create alignment without rel
- [required] Translate complexity into clear strategic choices: by connecting business models, customer needs, technology architecture, operational impacts, financial outcomes, and risk into concise recommendations for executive audi
- [required] Lead through ambiguity and enterprise change: while creating structure, momentum, accountability, and confidence across a broad and evolving stakeholder environment.
- [required] Build and lead global, multidisciplinary teams: with a track record of developing leaders, attracting critical capabilities, and establishing sustainable operating and support models.
- [required] Bring strong commercial and financial judgment: including experience with business cases, investment governance, vendor negotiations, portfolio prioritization, benefits realization, and total-cost-of-ownership considerat
- [required] Hold a Bachelor’s degree or equivalent experience: in Information Systems, Engineering, Finance, Business, Operations, or a related field, or equivalent practical experience
- [required] We hire the most qualified candidate for the role.
- [required] Do not include any medical or health information in this email.
- [role] All activities involving access to Mastercard assets, information, and networks comes with an inherent risk to the organization and, therefore, it is expected that every person working for, or on behalf of, Mastercard is
- [role] Abide by Mastercard’s security policies and practices;
- [role] Ensure the confidentiality and integrity of the information being accessed;
- [role] Report any suspected information security violation or breach, and Complete all periodic mandatory security trainings in accordance with Mastercard’s guidelines.

### 71b6b67f859b9c2d55a2
**Guidehouse — Senior Analyst - Data Profiling Specialist**

- [required] Data profiling, data analysis, or data quality assessment
- [required] Prior experience in child welfare, human services, or related social programs (state agency, consulting, or nonprofit)
- [required] Experience identifying data quality issues and recommending remediation actions
- [required] Experience supporting data preparation for analytics, reporting, or system modernization efforts
- [required] Familiarity with system modernization or data migration readiness efforts
- [required] 10% travel may be required during key client deliverable milestones
- [required] Strong proficiency in SQL and data analysis for querying, analyzing, and profiling data across complex, multi-system environments
- [required] Based on our contractual obligations, candidate must be located within the United States and US Citizen
- [required] Ability to clearly communicate findings to both technical and non-technical stakeholders
- [role] You will play a key role in helping the agency understand its data landscape and prepare for the transition to platforms that better support decision-making, reporting, and improved outcomes for children and families.
- [role] Evaluate and analyze data from legacy systems and disparate sources to assess quality, completeness, and consistency
- [role] Develop and apply practical approaches to profile complex datasets, including structured and semi-structured data
- [role] Identify data gaps, anomalies, and structural issues that may impact reporting, analytics, or system migration
- [role] Recommend data quality improvements and remediation actions that are aligned to both current-state use and future-state system needs
- [role] Support data readiness efforts for modernization and system replacement, including migration considerations
- [role] Collaborate with stakeholders to validate findings and align on data priorities and remediation strategies
- [role] Document data issues, patterns, and recommendations in a clear, actionable format
- [role] Partner with program and technical teams to improve data usability for reporting and analytics

### 164a77211fa7e7228c22
**Capital One — Business Manager - Talent, Development, and Org Insights Intelligence Analyst**

- [required] engineering, Information Systems or a related quantitative field)
- [role] Intelligence leadership: Serve as the analytical brain for the Performance Assessment, Associate Growth, and Organizational Planning experiences — framing the questions that matter, leading the analysis, and translating 
- [role] Shape the product agenda: Use data to influence what we build — identify unmet needs, size opportunities, and bring an evidence-based point of view that directly informs product strategy, prioritization, and roadmap deci
- [role] Insight to action: Move beyond describing what happened to explaining why and recommending what to do — build the metrics, models, and narratives that help leaders drive talent accountability and build high-performing, d
- [role] Platform-minded analytics: Partner with product and engineering to turn one-off analyses into durable, reusable data products and insight experiences — reducing duplicative work and building a trusted single source of tr
- [role] Execution: Manage and sequence the delivery of analysis across multiple experiences and stakeholders concurrently, bringing clear accountability and consistent delivery in a fast-moving environment
- [role] Partnership: Work closely across Capital One — product, engineering, design, People Science & Analytics, HRBPs, and executive leaders — to ensure insights are trusted, understood, and acted on Strategic & analytic orient
- [role] Strong business judgment and integrity: A tenacious, responsible decision maker who can turn ambiguity into a clear point of view and hold partners accountable to it
- [required] Currently has, or is in the process of obtaining one of the following with an expectation that the required degree will be obtained on or before the scheduled start date: A Bachelor's Degree in a quantitative field (Busi
- [required] A Master's Degree in a quantitative field (Business, Finance, Accounting, Statistics, Economics, Operations Research, Analytics, Mathematics, Computer Science, Computer Engineering, Software Engineering, Mechanical engin
- [role] Business Manager - Talent, Development, and Org Insights Intelligence Analyst
- [role] Do you want your analysis to shape how a Fortune 100 company develops its people?
- [role] Talent Development and Org Insights (TDOI) builds the platform and experiences that empower every associate to discover their unique path and unlock their full potential.
- [role] You'll join TDOI Intelligence — the analytical engine behind three connected experiences: Performance Assessment, Associate Growth, and Organizational Planning.
- [role] Our charter is to develop data-driven insights that illuminate the path to high-impact performance and growth, and to turn complex talent and organizational data into decisions leaders can act on.
- [role] This is not reporting for its own sake — the questions we frame and the patterns we surface directly shape the product agenda, defining what we build next for associates, people leaders, and organizational leaders across
- [role] As a Business Analysis Manager at Capital One, you will apply your strategic and analytical skills to major company challenges.
- [role] You'll team with world-class professionals to develop and test strategies that ultimately impact the bottom line.

### 1de80533811669b8a422
**Oracle — Director, Program Management**

- [required] Proven success leading cross-functional process improvement initiatives with measurable results.
- [required] Ability to work across business and technical teams and communicate clearly with executive leadership.
- [required] Experience leading organizational change and establishing consistent, scalable ways of working.
- [required] Experience managing and developing technical program managers and program managers.
- [required] Strong program management skills, including planning, governance, risk management, and resource allocation.
- [role] Drive process improvement: Identify operational gaps, simplify workflows, and implement repeatable processes, standards, and automation.
- [role] Oversee program delivery: Guide planning and execution across internal initiatives, ensuring clear scope, milestones, ownership, and outcomes.
- [role] Align stakeholders: Partner with business and technical leaders to prioritize initiatives, resolve dependencies, and make timely decisions.
- [role] Manage risks and resources: Anticipate delivery risks, remove barriers, and align staffing and resources with business priorities.
- [role] Establish governance and reporting: Create consistent practices for program reviews, status reporting, escalations, and decision-making.
- [role] Measure results: Define KPIs and use data to track progress, evaluate impact, and guide further improvements.
- [role] Lead change: Build stakeholder support, communicate changes clearly, and ensure teams adopt and sustain improved processes.
- [role] Lead and develop the team: Manage, coach, and grow technical program managers and program managers, setting clear priorities and expectations.
- [role] We are seeking a Director of Program Management to drive transformational change across Oracle.
- [role] Leading a team of technical program managers and program managers, this leader will spearhead cross-functional initiatives that simplify complex processes, remove barriers, and reshape how work gets done.
- [role] This role offers the opportunity to influence how Oracle operates at scale—partnering with senior leaders to challenge established practices, turn ambitious goals into actionable programs, and deliver lasting, measurable
- [role] The ideal candidate has a track record of leading process transformation, developing strong program management teams, and bringing people together to move complex initiatives forward.

### 42d1f15f538af9fca3ae
**AHEAD — AI Principal Consultant, AI Services**

- [required] Proven experience leading client engagements, workstreams, or project teams with senior business and technology stakeholders.
- [required] Excellent executive communication and facilitation skills, including experience creating and presenting senior-leadership-ready materials.
- [required] Experience in one or more of the following areas: AI strategy, digital transformation, use case prioritization, business case development, process redesign, automation, operating model design, AI governance, or technolog
- [required] Sufficient AI and technology fluency to work credibly with business leaders, CIO / CTO organizations, architects, engineers, data teams, and risk/governance stakeholders.
- [required] Demonstrated ability to lead teams, coach talent, manage timelines, and maintain a high bar for client-ready work.
- [required] Typically management consulting, technology advisory, digital transformation, enterprise technology, AI strategy, or related client-facing roles.
- [required] Strong structured problem-solving skills, including the ability to break down ambiguous problems, develop hypotheses, conduct analysis, synthesize insights, and communicate recommendations.
- [role] In this role, you will lead high-impact client engagements with senior business and technology executives.
- [role] You will help clients identify where AI can create value, prioritize use cases, build business cases and roadmaps, design operating models and governance, and mobilize implementation with AHEAD’s broader teams of consult
- [role] Lead client engagements from strategy through execution
- [role] Lead complex AI and digital transformation engagements from initial diagnostic through roadmap design, implementation planning, and execution support.
- [role] Structure ambiguous client problems into clear hypotheses, workplans, analyses, decision points, and executive-ready deliverables.
- [role] Manage day-to-day engagement delivery, including workstream planning, team leadership, synthesis, quality control, issue resolution, timelines, and client communication.
- [role] Facilitate executive workshops and align senior stakeholders across business, technology, data, risk, and operations.
- [role] Serve as a trusted advisor to client leaders on where AI can create measurable value and what is required to scale it responsibly.
- [role] Shape AI strategy, transformation roadmaps, and business cases
- [role] Identify and prioritize AI opportunities across productivity, growth, customer experience, decision-making, software delivery, and operational efficiency.
- [role] Conduct AI maturity and readiness assessments across strategy, data, platforms, governance, talent, process, and change management.

### c7aa36bb49598e8a2385
**Riveron — Business Performance Improvement - Associate Director (Finance Transformation)**

- [required] Extensive experience and subject matter expertise in at least one of the following: Tier 1-3 ERPs, Finance Governance tools, CPM technologies, Supply Chain Management applications, or Data Visualization and Analysis solu
- [required] A consulting/advisory or corporate role
- [required] You have a proven talent for sustaining long-term client relationships
- [required] You are skilled at leading and developing teams and individuals
- [required] You believe that exceeding client expectations and having fun at work are mutually dependent
- [required] Bachelor’s or Master’s in Business or related field of study
- [required] You thrive in an ever-changing, dynamic work environment
- [required] You readily identify problems and instinctively look for solutions
- [required] You enjoy participating in internal and external company initiatives such as community service, training, recruiting, committees, conference panels, and firm events
- [required] You have the ability and desire to travel as required based on client location
- [required] You are willing and able to travel as needed to meet client and business needs, as travel is an expected part of this role.
- [role] Use functional leadership and expertise to advise clients and develop solutions to a variety of strategic problems related to improving business processes, enabling technology, and aligning the enterprise operating model
- [role] Run smaller projects autonomously
- [role] Lead market-facing practice development initiatives such as thought leadership and white papers
- [role] Contribute actively to the development of people leaders into future practice leaders
- [role] Provide guidance and issue resolution support to teams across multiple concurrent client engagements
- [role] Partner with the practice’s executive leaders to conduct business development activities
- [role] Applications for this evergreen opportunity will be accepted through Dec 31, 2026.
