# Coding brief — sprint plan §20.2 (level fit), §20.3 (remote tags), §21 (AI lens)

**Written 2026-09-17 by Fable (designer/auditor). Built by Sonnet agents on disjoint files. Binding text is
`docs/SPRINT_PLAN.md` §20–21; this brief turns it into file-level work. Where this brief and the sprint plan
disagree, the sprint plan wins and the agent reports the conflict instead of choosing.**

## Ground rules for every agent

1. **Working tree:** `~/jobsearch`, branch `main`, run tests with `.venv/bin/python -m pytest -q`. Baseline is
   157 passing. Other agents are editing other files at the same time: if the full suite fails in a file you
   do not own, report it in your summary and do not fix it.
2. **Own only your files.** The file list per agent below is exhaustive. New test files are per agent
   (`tests/test_level_fit.py`, `tests/test_feedback.py`, `tests/test_ai_lens.py`, `tests/test_report_level.py`).
   Do not edit `tests/test_finder.py`.
3. **The live DB is read-only for you.** `db/jobsearch.duckdb` may be opened to read counts or sample rows; never
   run `screen`, `rescreen-all`, `judge import`, `feedback load` or any write against it. Tests use
   `store.connect(str(tmp_path / "t.duckdb"))` like `tests/test_finder.py` does. Note that `store.connect()`
   applies additive migrations and `CREATE OR REPLACE VIEW`s on open, so do not open the live DB from code you
   are mid-way through editing.
4. **This is a PUBLIC repo.** Nothing personal in any committed file: no first names, no home town, no pay
   figures, no vault paths (anything under `/mnt/c/...`), no employer + outcome pairs beyond what
   `docs/STATUS.md` already names. Personal prose goes in `backend/finder/rubric_local.py` and personal values
   in `backend/profile_local.py`; both are gitignored. Neutral defaults and test values go in
   `backend/profile.py` / `backend/profile_local.example.py`. Test fixtures use "Acme", "Candidate", etc.
5. **Do not commit.** Leave changes in the working tree; Fable audits every diff and commits once.
6. **Style:** match the codebase — explanatory docstrings that say WHY, rules return `(reasons, flags, notes)`,
   SQL views over Python constants where a threshold is read by both, `log=print` parameters, no new deps.
7. **Report back** with: files changed, the tests you added (names), the full-suite result, and every
   judgement call you made that the brief did not settle.

Vocabulary: **level_fit values**, ordered low → high: `too_low` < `in_range` < `stretch_up` < `out_of_reach`;
`unknown` is outside the order. **Grades**, best → worst: `bullseye` > `adjacent` > `stretch` > `wrong`.
"One step" is one position on either scale.

---

## Agent A — the level rule (§20.2 rule half) and the remote tags (§20.3)

**Files:** `backend/finder/rules.py`, `backend/profile.py`, `backend/profile_local.example.py`,
`backend/profile_local.py` (personal, gitignored — set the same new constants there), `backend/screen.py`,
`backend/finder/version.py`, new `tests/test_level_fit.py`.

### A1. `level_fit_rule(title, text, annual_top, reports_notes) -> RuleResult` in `rules.py`

Returns `([], [], {"level_fit": <value>, "level_fit_hits": [<short strings>]})`. **Never a reason or a flag**:
the level rule does not change `verdict` or `rule_score` (sprint plan: "Score is untouched until agreement is
measured"). The existing `level_rule` (years/pay → senior/mid/junior) stays as it is; this is a second,
separate rule that writes its own note. `screen_row` calls it after the other rules so it can read
`notes["reports"]` and the split below; the value lands in `rec.notes["level_fit"]` (Agent B stores it).

**Where signals are read (sprint plan: "title and Required block only").** Title signals from `title`. Scope
signals from `required_block(text)` — which already falls back to the whole JD when no Required heading is
found. Team size comes from `direct_reports_rule` (whole text, as today) — see A2.

**Signals and constants** (constants in `profile.py` with neutral defaults, mirrored in
`profile_local.example.py`; each is a list/int so `version.rules_version()` picks it up):

- `LEVEL_OUT_OF_REACH_TITLE_TERMS = ["senior director", "sr. director", "sr director", "executive director",
  "vice president", "vp", "svp", "evp", "avp", "assistant vice president", "head of", "chief", "cxo",
  "general manager", "managing director"]` — word-bounded via `find_terms`. ("executive director" is Fable's
  call: it is the band above director in most large companies. Record it in your summary so the user can veto.)
- `LEVEL_STRETCH_TITLE_TERMS = ["director"]` — a Director title with no scope hit is `stretch_up`
  ("Director-type promotion"). "senior director" must match out-of-reach, not this (longest-match first, or
  check out-of-reach before stretch).
- `LEVEL_IN_RANGE_TITLE_TERMS = ["principal", "senior", "sr.", "sr ", "lead", "staff", "manager", "consultant",
  "analyst", "specialist", "architect", "engineer", "program manager", "project manager", "owner"]` — senior IC or
  small-team manager. `manager` includes "senior manager".
- `ORG_BUILDING_TERMS = ["build and lead", "build and scale", "build the org", "build the organization",
  "build a team", "build the team", "build out the team", "scale the organization", "global teams",
  "global organization", "spans of control", "span of control", "executive leadership team", "member of the
  executive", "leaders of leaders", "manager of managers", "managers of managers", "org design"]` plus the existing
  `LARGE_TEAM_MARKERS` (hiring plan, headcount growth, ...). Each distinct term hit = one scope hit, counted at
  most once per term; cap the org-building family at 2 hits so a wordy JD cannot pile up.
- `PNL_TERMS`: regex in `rules.py`, `\bP\s*&\s*L\b|\bprofit\s+(?:and|&)\s+loss\b` — one scope hit when it appears
  within 80 characters of `own|ownership|responsib|accountab|manag|deliver`.
- **Managerial years:** regex over the required block for a years phrase (reuse `required_years`'s number
  grammar — factor a helper if needed) whose sentence contains `manag|people leadership|leading teams|
  supervis|leadership experience|leading (?:a )?team`. Read the LARGEST such number. If it exceeds
  `LEVEL_MANAGERIAL_YEARS_MAX` (profile.py default `3`; set `2` in `profile_local.py` and say so in the example
  file's comment) → one scope hit. Written minimums are scored separately from scope: "Master's + 10 yrs" alone
  is never a scope hit.
- **Team size** (from A2's notes): explicit direct reports `> 2 * MAX_DIRECT_REPORTS` → two scope hits (that is
  `out_of_reach` on its own); explicit direct reports `> MAX_DIRECT_REPORTS` → one hit; program/org headcount
  ("team of N") counts one hit only when `N > 2 * MAX_DIRECT_REPORTS` AND the phrase is preceded within 30
  characters by `lead|manage|run|own|direct|build` (CACI's "support a team of 250+ professionals" is zero hits).
  `MAX_DIRECT_REPORTS is None` → team size contributes nothing.
- **Junior signals:** `EARLY_CAREER_TITLE_TERMS` in the title; `max(required_years(text)) < LEVEL_YEARS_MID`;
  `annual_top is not None and COMP_FLOOR and annual_top < COMP_FLOOR`.

**Decision order** (first match wins):
1. Early-career title term and no senior/in-range/stretch/out-of-reach title term → `too_low`.
2. Out-of-reach title term → `out_of_reach`.
3. Scope hits ≥ 2 → `out_of_reach`; scope hits == 1 → `stretch_up`.
4. Stretch title term (Director) → `stretch_up`.
5. Band top posted and below `COMP_FLOOR` → `too_low`.
6. Max required years below `LEVEL_YEARS_MID`: if band top is posted and `>= COMP_FLOOR` → `in_range` (the
   user's rule: pay in range overrides low years); if pay is not posted and no in-range/senior title term →
   `too_low`; with an in-range title term → `in_range`.
7. Any in-range title term, or `level_rule`'s `senior` note, or a pay band at/above the floor, or required years
   at/above `LEVEL_YEARS_MID` → `in_range` ("assume a small team when the team size is not specified").
8. Otherwise → `unknown`.

`level_fit_hits` lists what fired, short and readable, e.g. `["title: senior director", "p&l",
"managerial years 5 > 2", "direct reports 12"]`, so the report and the agreement listing can show why.

### A2. Split `direct_reports_rule` notes

Keep `notes["reports"]` as the max of both (existing tests rely on it) and add `notes["direct_reports"]` (max
from `_REPORTS`, explicit) and `notes["team_headcount"]` (max from `_TEAM_SIZE`) plus
`notes["team_headcount_led"]: bool` (the lead/manage/run/own/direct/build prefix test above). Do not change
reasons/flags behaviour; `tests/test_finder.py::test_direct_reports_rule_*` must still pass unchanged.

### A3. Remote tags (§20.3) in `screen.py` / `profile.py`

- Add to `REMOTE_TERMS`: `"us off-site"`, `"#li-remote"`, `"#bi-remote"` (matching is lowercase substring via
  `_has`; make sure `#li-remote` inside `#LI-Remote, #BI-REMOTE` matches, and that the `_REMOTE_DUTY_RE`
  exclusion cannot swallow a tag line).
- In `is_remote`: when the JD (title+location+description) contains BOTH `#li-hybrid` and `#li-remote`, the
  posting is **remote** (user ruling). A lone `#li-hybrid` must NOT make anything remote or non-remote —
  absence of a tag says nothing; a hybrid tag alone is left to the existing ATS-flag/JD logic (do not add a
  new negative signal).
- The location segment `US Off-Site` (any case, with or without hyphen) counts as remote when it appears in
  `location` or `locations` or the JD.
- Tests (in `tests/test_level_fit.py`, section "remote tags"): a location `"US Off-Site"`; a JD footer with
  `#LI-Remote`; `#LI-Hybrid #LI-Remote` together → remote; `#LI-Hybrid` alone with an ATS `hybrid` flag and a
  specific city → not remote; the existing Blue Yonder / Microsoft shapes still pass (they live in
  `test_finder.py`, just run it).

### A4. Version bump and tests

- Bump `RULES_CODE_VERSION` in `version.py` (e.g. `"2026-09-17.1"`); the new profile constants change the hash
  anyway, but the code changed too.
- `tests/test_level_fit.py`: one test per decision branch above with a neutral JD each (Senior Director title;
  Director + P&L → out_of_reach; Director alone → stretch_up; Senior Manager with "5+ years of people management"
  → stretch_up under the example profile's `LEVEL_MANAGERIAL_YEARS_MAX`; Principal with nothing else → in_range;
  "3+ years experience" Analyst with a posted band at the floor → in_range; the same with no band → too_low;
  Intern → too_low; a bare title with no signals → unknown; CACI-shape "support a team of 250+" → no scope hit;
  "12 direct reports" → out_of_reach; plus the `screen_row` integration test asserting
  `rec.notes["level_fit"]` and that `verdict`/`rule_score` are unchanged by the new rule).

---

## Agent B — schema v10, screens write path, `report_feedback` load, agreement, re-export, CLI

**Files:** `backend/ats/store.py`, `backend/finder/pipeline.py`, new `backend/finder/feedback.py`, `finder.py`,
new `tests/test_feedback.py`, new fixture `tests/fixtures/report_feedback_sample.csv` (neutral, ~8 rows).

### B1. Schema v10 (additive)

- `SCHEMA_VERSION = 10`, comment in the same style as v9.
- `_add_missing_columns`: `screens.level_fit VARCHAR`, `screens.fit_ai DOUBLE`, `llm_labels.grade_ai VARCHAR`.
- New table `report_feedback` exactly per the DDL in the vault spec, reproduced here so you need not read it:

```sql
CREATE TABLE IF NOT EXISTS report_feedback (
    posting_id VARCHAR NOT NULL, description_hash VARCHAR NOT NULL,
    report_files VARCHAR, snapshot_final_score INTEGER, snapshot_band VARCHAR,
    snapshot_grade_process VARCHAR, snapshot_grade_technical VARCHAR,
    human_grade VARCHAR,                 -- bullseye|adjacent|stretch|wrong; NULL = capability not assessed
    level_fit VARCHAR,                   -- in_range|stretch_up|out_of_reach|too_low; NULL = not assessed
    verdict VARCHAR NOT NULL,            -- build|consider|pass
    reason_code VARCHAR, reason_detail VARCHAR, positioning VARCHAR, confidence VARCHAR,
    basis VARCHAR NOT NULL,              -- jd_read|metadata|rule_screen
    assessor VARCHAR NOT NULL,           -- 'user' / 'human-override' outrank 'claude-*-review'
    confirmed_by_user BOOLEAN NOT NULL DEFAULT false,
    note VARCHAR, grade_before_split VARCHAR, needs_confirm BOOLEAN, split_reason VARCHAR,
    assessed_at TIMESTAMP NOT NULL, loaded_at TIMESTAMP NOT NULL,
    PRIMARY KEY (posting_id, description_hash, assessor)
);
```

- Views:
  - `vw_report_feedback_latest`: one row per posting for its CURRENT `description_hash` (join `postings` like
    `vw_llm_labels_latest`), precedence `assessor IN ('user','human-override')` > `confirmed_by_user` > newest
    `assessed_at`. Rows whose hash no longer matches are simply absent (= expired).
  - `vw_level_agreement`: `posting_id, employer, title, human_level_fit, rule_level_fit, agree BOOLEAN,
    steps_apart INTEGER` over `vw_report_feedback_latest` (rows with `level_fit IS NOT NULL AND
    confirmed_by_user`) joined to `vw_screen_latest.level_fit`. Use a `CREATE OR REPLACE MACRO level_fit_rank(v)`
    = too_low 0, in_range 1, stretch_up 2, out_of_reach 3, unknown NULL.
  - `vw_label_set_ai`: copy of `vw_label_set_technical` reading `grade_ai`.
  - `vw_lens_grades` and `vw_lens_fit`: add `grade_ai`, `fit_ai`, `ai_strong`, `ai_standout` following the
    process/technical pattern exactly. **`lens_bucket` is unchanged** (sprint plan: `ai` is reported beside the
    others, never folded in); `lens_source` unchanged. Add `level_fit` to `vw_lens_fit.base` and to
    `vw_shortlist`.
- Add `screens.level_fit` and `screens.fit_ai` to the `vw_screen_latest`-derived selects where the other lens
  columns are listed (`vw_shortlist`, `vw_lens_fit`).

### B2. Pipeline write path

`_BATCH_SHAPE`, `_BATCH_KEYS`, the INSERT column list and SELECT in `_write_batch` gain `level_fit` (VARCHAR)
and `fit_ai` (DOUBLE). In `screen()`, `level_fit` comes from `rec.notes.get("level_fit")` (Agent A writes that
note; until A lands the value is simply NULL — code defensively) and `fit_ai` from `lens_probs.get("ai", …)`
exactly like `fit_process`. The `models` metadata in the run summary already lists lens versions; nothing else.

### B3. `backend/finder/feedback.py`

- `load_csv(con, path, log=print) -> dict`: reads the golden-source CSV (columns listed below), upserts into
  `report_feedback`. Booleans arrive as `TRUE`/`FALSE`/blank. Empty strings → NULL. Rows with a blank
  `posting_id`: look up `postings.url = row.url`; if found use it, else skip and count. Never re-key
  `description_hash`: load what the CSV says. Returns counts `{loaded, skipped_no_posting, matched_by_url}`.
  CSV columns: `posting_id, report_files, employer, title, url, description_hash, snapshot_final_score,
  snapshot_band, snapshot_grade_process, snapshot_grade_technical, human_grade, level_fit, verdict, reason_code,
  reason_detail, positioning, judge_agreement, confidence, basis, assessor, assessed_at, confirmed_by_user, note,
  grade_before_split, needs_confirm, split_reason` — `employer`, `title`, `url`, `judge_agreement` are not stored.
- `rule_level_fit(con, posting_ids) -> dict`: computes the level rule LIVE for each posting by calling
  `rules.screen_row(row)` on the postings row (reuse `pipeline.fetch_rows`) and reading
  `rec.notes.get("level_fit")` / `level_fit_hits`. This works before any rescreen, which is the point.
- `agreement(con, log=print) -> dict`: over `vw_report_feedback_latest` rows with `level_fit` and
  `confirmed_by_user`, compares human vs live rule. Prints: n, exact agreement %, agreement restricted to human
  `in_range`/`out_of_reach` (the ≥ 80% target), a confusion table human × rule, and every disagreement as
  `posting_id · employer · title · human · rule · hits`. Returns the numbers.
- `export(con, out_path, log=print) -> Path`: writes a CSV with every `report_feedback` row (all assessors, latest
  hash or not — include an `expired` column) plus `rule_level_fit`, `rule_level_hits`, `judge_grade_process`,
  `judge_grade_technical`, `judge_grade_ai`, `judge_grade` (from `vw_llm_labels_latest`), and `needs_you`
  (BOOLEAN) = `confidence = 'low'` OR (human `level_fit` present and differs from rule by > 1 step) OR (human
  `level_fit` absent and rule ∈ {out_of_reach, too_low} and verdict ∈ {build, consider}) OR (`human_grade` present
  and differs from `judge_grade` by > 1 step) OR `needs_confirm`. Sort `needs_you` first, then by
  `snapshot_final_score` desc.
- `sample_ai_titles` does NOT live here (Agent C owns the judge pools).

### B4. `finder.py`

- New subcommand `feedback` with `--load CSV`, `--agreement`, `--export OUT`; any combination, in that order.
- `train` gains `--lens {process,technical,ai}` passed to `features.train(lens=...)` (Agent C makes `ai` valid
  in `features.LENS_VIEWS`; until then argparse accepts it and `features` raises — fine).
- `lenses`: print an `ai` count line (`ai_strong AND verdict != 'reject'` from `vw_lens_fit`) after the three
  buckets; the fourth list itself is Agent D's.

### B5. Tests (`tests/test_feedback.py`)

Temp DB; insert a few postings + screens rows (copy the pattern from `test_screen_writes_screens_and_postings_columns`);
load the fixture CSV (include: a normal row, a blank-`posting_id` row matched by URL, a blank-`posting_id` row
with no match, a row whose hash is stale, a `human-override` row and a review row for the same posting); assert
counts, `vw_report_feedback_latest` precedence and expiry, `vw_level_agreement` steps, that `screens.level_fit`
and `fit_ai` round-trip through `pipeline.screen` (monkeypatch `rules.screen_row` to return a record whose notes
carry `level_fit`, and pass a fake `lens_models={"ai": …}` like the existing lens tests do), and that `export`
sets `needs_you` on each branch.

---

## Agent C — the applied-AI lens (§21): rubric, personal prose, judge, features

**Files:** `backend/finder/rubric.py`, `backend/finder/rubric_local.py` (gitignored, personal), `backend/finder/judge.py`,
`backend/finder/features.py`, new `tests/test_ai_lens.py`.

### C1. `rubric.py` (public, neutral)

- `RUBRIC_PUBLIC`: "TWO LENSES" → "THREE LENSES"; add `grade_ai : how well it matches the APPLIED-AI lens`; the
  output JSON gains `"grade_ai": "bullseye|adjacent|stretch|wrong"`; keep every existing rule sentence. Add one
  sentence: the overall positioning decision still comes from process and technical; `grade_ai` is reported
  beside them.
- New `RUBRIC_LENS_AI` block, neutral, same shape as the other two:
  - `bullseye`: applied-AI delivery and enablement — designing and shipping agentic workflows around a model
    (skills / procedures, memory, deterministic tool calls for the non-judgement steps), evaluation and
    regression discipline for knowledge work (reference sets, LLM-as-judge with calibration against humans,
    acceptance thresholds), human-in-the-loop grounding and review gates, model routing and token/cost
    judgement (tiering, fallback, graceful degradation), and driving adoption — teaching teams to build and use
    AI assistants inside an approved platform.
  - `adjacent`: AI programme / transformation / governance leadership where the work above is directed rather
    than done; analytics or process roles where AI-assisted delivery is one named expectation among several.
  - `stretch`: named overlap only (e.g. prompt design or RAG configuration as a minor duty inside a role that
    is otherwise something else).
  - `wrong`: building models (ML / NLP / RAG engineering, fine-tuning, model research), AI platform or
    infrastructure engineering, distributed services in Python as the job, AI security threat modelling as a
    primary duty, and "AI" as marketing vocabulary with no AI work in the duties.
  - Judge the duties, not the title; "AI" in a title is not evidence.
- `rubric_version()` payload gains `RUBRIC_LENS_AI` and `RUBRIC_PERSONAL_AI`. (Yes, this changes the version for
  every existing label; that is expected — §21 regrades in steps.)

### C2. `rubric_local.py` — `RUBRIC_PERSONAL_AI` (personal, gitignored, never committed)

Read the coaching brief in the vault: `Professional/Resources/AI_Experience_Coaching_Brief_20260916.md`, section
5 (5.1–5.8) and section 6, under the Keystone vault root the orchestrator gives you. Write
`RUBRIC_PERSONAL_AI` as **rubric prose in the voice of the existing `RUBRIC_PERSONAL_PROCESS` block** — "what he
has actually done", evidence organised the way the brief sorts it (agentic build workflow; context engineering
with skills, memory and deterministic tools; LLM APIs, routing and cost control; evaluation — lead with it;
grounding and human in the loop; MCP and browser agents configured-not-built; data governance under the AI;
adoption and teaching). Rules that are binding, copied from the brief's own italics:
- No token-savings figure anywhere. "About 5 minutes to about 2", never a multiplier.
- 93.3% is one model's sentiment-direction agreement only; do not use a competitor's as-run number to knock it.
- AUC 0.973 is "a strong fit ranks above one graded wrong 97 times in 100", never "97% accurate".
- Never name the paid client; say "a paid client's analytics platform".
- The content pipeline with the human gate is grounded generation, NOT "agentic".
- Counts (skills, rulings, entries) carry "as of 2026-09-16".
- The MCP server over the evidence record is a CANDIDATE build, not done.
- Generalise employer and product names as the other blocks do.
- End with the boundary in §21 verbatim in spirit: building models, AI platform/infra engineering, distributed
  Python services and AI security threat modelling are `wrong` on this lens; the coding-assessment constraint
  is not in the rubric.
Employer names inside the brief that the other blocks already generalise (a Fortune 20 telecom, etc.) stay
generalised. Keep it to roughly the length of `RUBRIC_PERSONAL_TECHNICAL`.

### C3. `judge.py`

- `BATCH_HEADER` gains `{lens_ai}` after `{lens_technical}` and `{personal_ai}` after `{personal_technical}`;
  "on BOTH lenses" → "on ALL THREE lenses". `write_batches` passes them (`getattr(rubric, "RUBRIC_PERSONAL_AI", "")`).
- `_validate`: `grade_ai` is required when present in the manifest's rubric (i.e. always for new exports);
  keep the two existing back-compat paths (single-grade files; two-lens files without `grade_ai` load with
  `grade_ai = NULL` and a counted warning, so the existing `db/batches*` results still import).
- `load_results`: write `grade_ai`; the "by lens" log line gains `ai-strong N`; `overall()` unchanged.
- `to_csv`: add `grade_ai`.
- `agreement()`: add lens independence for `ai` — % of judged rows where `grade_ai` differs from `grade_process`
  and from `grade_technical`, over rows where `grade_ai IS NOT NULL` (§18.7's bar).
- New pool for §21's live validation: `AI_TITLE_RE` (module constant) matching `\bAI\b|artificial intelligence|
  machine learning|\bML\b|\bLLM\b|GenAI|generative|agentic|copilot|intelligent automation` in `postings.title`;
  `pools(..., only=["ai_title"])` returns active, non-rejected postings with such a title, newest first, and an
  `--pools ai_title` export works through the existing CLI (`finder.py` is Agent B's file; the `--pools` option
  already exists and passes a list, so no CLI edit is needed). Also `ai_term_estimate(con) -> dict` counting
  active postings whose JD text matches the same regex plus `prompt engineering|large language model|RAG|
  retrieval-augmented`, printed by `judge report` (add the line in `agreement()`).

### C4. `features.py`

`LENSES = ("process", "technical", "ai")`; `LENS_VIEWS["ai"] = "vw_label_set_ai"` (view is Agent B's; until it
lands, a train on `ai` raises a DuckDB error — acceptable). `load_lens_models` needs no change.

### C5. Tests (`tests/test_ai_lens.py`)

`rubric_version()` changes when `RUBRIC_LENS_AI` changes (monkeypatch); a batch file contains the AI lens text and
the "ALL THREE" instruction; `_validate` accepts a three-grade object, accepts a two-grade object with a NULL
`grade_ai`, rejects a bad `grade_ai`; `load_results` round-trips `grade_ai` into `llm_labels` on a temp DB
(follow the existing judge test in `test_finder.py` for the manifest shape); `AI_TITLE_RE` matches "Principal
AI Engineer (Agentic AI)" and "Senior Manager, AI Transformation" and does not match "Retail Associate" or
"Maintenance"; `pools(only=["ai_title"])` returns only such titles from a temp DB.

---

## Agent D — reports (wave 2, after A–C land): level ordering, collapsed tail, fourth lens list

**Files:** `backend/finder/report.py`, `finder.py` (the `lenses` command only), new `tests/test_report_level.py`,
`docs/STATUS.md` (a new NOW section drafted for Fable to edit), `docs/SPRINT_PLAN.md` (acceptance checkboxes in
§20.2 / §20.3 / §21 only).

- `write_jobs_found`: the summary table and the escalated blocks order `in_range` → `stretch_up` → `unknown`/NULL
  → then score; `out_of_reach` and `too_low` rows never take a block and never appear in the top table — they
  go to a new collapsed section `<details><summary>Out of level range (N)</summary>` with the same table columns
  plus `Level` and the hits. Add a `Level` column to the summary table. `read_back` / `parse_decisions` must
  still reparse the file (there is a test for that — run it).
- `write_lens_lists`: same ordering within each list, a `Level` column, and a fourth list
  `("ai", "Strong on APPLIED AI", "agentic workflow delivery, evals, enablement — reported beside the other lenses,
  never folded in")` reading `ai_strong` from `vw_lens_fit` (a row can appear in `ai` and in another list; that
  is intended). Keep the three existing lists first.
- `finder.py lenses`: print the `ai` count with the others.
- Tests: extend the report fixture pattern from `test_finder.py::_report_fixture` (copy, do not import private
  helpers if they are not importable) with rows in each level bucket; assert ordering and the collapsed tail;
  assert the `ai` list renders and that a row strong on `ai` and `process` appears in both.

---

## Acceptance (Fable runs these after the audit; agents do not)

1. Full suite green; `git grep --untracked -n -i -E -f .personal_patterns -- . ':!.personal_patterns' ':!LICENSE'`
   shows only the known 3-line baseline.
2. `finder.py feedback --load <vault CSV> --agreement` on the live DB: agreement reported against the confirmed
   human rows (target ≥ 80% exact on `in_range` / `out_of_reach`), disagreements listed.
3. `finder.py feedback --export` re-exports the 232 rows with the rule's answer and `needs_you` beside the human
   columns.
4. `finder.py judge export --pools ai_title --limit 40` produces the first live-validation batch for §21; grading
   and rubric adjustment happen with the user (sprint plan §21), not in this build.
5. Then §20.4's order: fresh `sweep_ats.py` ingestion → `rescreen-all` → coverage re-baseline → Phase 4.
