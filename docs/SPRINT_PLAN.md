# Sprint Plan — the job-finding layer (`backend/finder/`)

_Binding build contract, written 2026-09-15 by the design session (Claude Fable). Built by Opus (high or xhigh effort), audited by Fable afterward against §11. Execute phases in order; do not reorder or skip without the user's confirmation. Personal values (pay thresholds, places, travel limit, headcount limit, rubric prose) are NOT in this file: they live in the vault appendix `Tools/Finder_Build_Personal_Appendix.md` under `$JOBSEARCH_VAULT_DIR/Professional/Areas/Job_Search/`. Read that file before Phase 1 and copy its values into `backend/profile_local.py` and `backend/finder/rubric_local.py` (both gitignored). Never put any of its contents into a tracked file._

## 0. Builder rules

- Read `CLAUDE.md`, `docs/STATUS.md`, this file, and the vault appendix first. Then read `backend/ats/store.py`, `backend/ats/sweep.py`, `backend/screen.py`, `backend/profile.py`, `sweep.py` (its `load_tracker`, `load_recent_jobs_found`, and the CSV/markdown writers at the end of `main`), and `tests/test_ats.py`. Reuse what is there; do not fork `screen.py` into a second rule engine.
- **The live DuckDB may be locked** by a running backfill (`ps -eo pid,cmd | grep -E '[s]weep_ats.py'`; `grep '^=== DONE' output/backfill_20260915_day.log`). Until it is free, develop and test against a scratch copy: `cp db/jobsearch.duckdb /tmp/finder_scratch.duckdb` once the lock is released, or a tiny `tmp_path` DB in tests. Every CLI in this spec takes `--db`.
- Python 3.14 venv at `.venv`. New dependencies are allowed and verified to have cp314 wheels: `scikit-learn`, `numpy`, `scipy`, `joblib`, `sentence-transformers`, `torch`, `fastembed` (fallback). Install into `.venv` only. The finder must import lazily so `sweep_ats.py` still runs with none of them installed.
- Commit locally after each phase (imperative first line ≤72 chars, 2–5 bullets). **Do not push.** Before every commit run `git grep --untracked -n -i -E -f .personal_patterns -- . ':!.personal_patterns'` and require empty output.
- Add to `.gitignore`: `db/models/`, `db/snapshots/`, `backend/finder/rubric_local.py`.
- Tests: `.venv/bin/python -m pytest -q` must stay green (19 existing). New tests go in `tests/test_finder.py`, same style as `tests/test_ats.py` (tmp DuckDB via `store.connect(str(tmp_path / "t.duckdb"))`, rows via `backend.ats.normalize.base(**kw)` + `store.record_board`). Tests use neutral values only (the ones in `profile_local.example.py`), never the appendix values.
- Keep functions small, typed, docstringed. Boring beats clever.
- Update `docs/STATUS.md` at the end of each session (overwrite; it is the state file).

## 1. What this builds and why

The ATS sweep (`sweep_ats.py`) holds ~78k live postings across ~137 boards, ~60k with full JD text, 2–5k new per day, with truthful open/closed status. The missing step is the one the user does by hand with Cowork / Claude Code: read the corpus and say which postings match. The four reserved `postings.screen_*` columns exist for this and are empty.

Deliverable: a deterministic, auditable, re-runnable scorer that runs inside the daily sweep, produces a daily `Jobs_Found_YYYYMMDD_HHMM.md` in the vault in the exact wrapper the vault skills already parse, exports lock-free snapshots, and feeds review state (surfaced / built / applied / passed) back into the DB so nothing is shown twice.

Locked decisions (do not relitigate):

| Topic | Decision |
|---|---|
| LLM | Off by default. Optional Gemma stage on the top N behind `--llm-top N`. Reads `GEMINI_API_KEY` and `GEMINI_API_MODEL` (comma-separated, first = primary, rest = fallbacks on 429/5xx) from the repo's `.env`. Both are already set. |
| Output | `Jobs_Found_*.md` in the vault (§7 format) + `db/snapshots/*.parquet` and `shortlist.csv`. No per-application folder creation. |
| Tracker | `Application_Tracker.md` (vault markdown) stays the source of truth. A trailing `Posting ID` column is appended to its tables by the user; the finder mirrors the tracker into a `tracker` table each run and never writes the markdown. |
| Passed state | Both: a `Decision` column in the Jobs_Found summary table read back next run, and `finder.py mark`. |
| Lock | No second DB. Snapshots after every run. |
| NLP | Rules = gate + tier. TF-IDF + logistic regression on the user's own labels = `fit_prob` + top terms. Sentence embeddings = `embed_sim`. All three combined per §6. |
| Public/private | Everything personal goes through the existing `profile.py` → `from backend.profile_local import *` override, plus `rubric.py` → `rubric_local.py` for LLM prose. |

## 2. Repo facts the build depends on

- `store.connect(db_path)` runs `SCHEMA` (CREATE TABLE IF NOT EXISTS …) and `VIEWS` (CREATE OR REPLACE …) on every open. `SCHEMA_VERSION` is 2; bump to 3. No `postings` column changes are needed, so `_migrate` is untouched.
- `postings` columns: posting_id (sha1(employer|platform|req_id)[:20]), employer, platform, req_id, title, url, location_primary, locations (JSON array text), country, workplace_type (remote|hybrid|onsite|NULL), employment_type, job_family, job_level, pay_min, pay_max, pay_currency, pay_interval (year|hour), pay_source, posted_at, posting_end_at, description_text, description_hash, description_fetched_at, raw_json, screen_verdict, screen_score, screen_reasons, screened_at, first_seen_at, last_seen_at, closed_at, status (active|closed). The upsert (`POSTING_COLUMNS`) never touches the screen_* or lifecycle columns.
- All DuckDB writes happen on the main thread; batch with `BEGIN` … `COMMIT` (see `fetch_details`, CHUNK=100).
- `backend/ats/sweep.py::run()` stage order: `load_registry` → `store.connect` → `sweep()` → `fetch_details(new, since=stats["started"])` → `fetch_details(backlog)` → `store.log_run` → close. The finder stage goes after the backlog details and before `log_run`, on the same connection.
- `backend/screen.py::screen(job: Listing, tracker_rows=None, recent_titles=None) -> Listing`: verdict = reject if any reason else review if any flag else candidate; `job.extra["tier"]` 1 if a `PRECISE_TITLE_TERMS` hit else 2; `job.score` is ordering-only. Reason/flag producers are listed in the module. `is_remote`, `is_commutable`, `annual_top`, `company_keys`, `company_matches`, `similar_title`, `norm_company`, `norm_title` are reusable.
- `backend/profile.py` constants (public): FUNCTION_PHRASES, SEPARATE_PASS_PHRASES, TITLE_FUNCTION_TERMS, PRECISE_TITLE_TERMS, JUNIOR_TITLE_TERMS, ASSOCIATE_OK, OFF_LANE_TITLE_TERMS, PLATFORM_GATED_TERMS, AGGREGATOR_POSTERS, NATIONWIDE_LOCATIONS, FEDERAL_FUNCTION_TITLES, ITSM_CHANGE_TERMS, PLANT_DISCIPLINE_TERMS, HARD_AVOID_INDUSTRY_TERMS, CLEARANCE_TERMS, BLOCKED_POSTERS (dict), AI_GIG_TITLE_TERMS, COMP_FLOOR/COMP_ASK/HOURLY_ANNUALIZE/HOME/LOCAL_RADIUS_KM/COMMUTABLE_PLACES (None/empty publicly), REMOTE_TERMS, SENIOR_TITLE_TERMS, FAITH_SIGNALS. Ends with the `profile_local` star import.
- `sweep.py` (old aggregator sweep) has `load_tracker()` (calls the vault's `Tools/tracker_lookup.py`, builds `company_keys`) and `load_recent_jobs_found()`; its `main()` tail shows the CSV and markdown conventions (`"; ".join(...)` for lists, `stamp = %Y%m%d_%H%M`).
- Vault paths (via `JOBSEARCH_VAULT_DIR` in `.env`): `Professional/Areas/Job_Search/Application_Tracker.md` (tables under `## Active`, `## On Hold`, `## Consultant Networks` (different columns; skip), `## Closed` (ragged trailing cells); columns `Date Applied | Company | Role | Status | Status Date | Recruiter | Next Action | Follow-Up Due`); `Tools/tracker_lookup.py` (positional parse of cells 0..2; appending a column is safe); `Applications/<Company_Role>/index.md` (YAML frontmatter with `status`, `role`, `company`; a `## Job Description` section holding the verbatim JD, often starting with an `Apply: <url>` line and a metadata line before the first blank line); `Search_Results/Jobs_Found_YYYYMMDD_HHMM.md`.
- Label inventory: ~317 application folders with a real `## Job Description` (statuses mostly evaluating/applied; a handful `pass` / `not-pursuing` / `declined` / `rejected`), 187 `# Company:` blocks across 44 Jobs_Found files, ~488 `## Passed / Filtered Out` rows (company, `[title](url)` mostly LinkedIn links, reason).

## 3. Schema additions (`backend/ats/store.py`)

Append to `SCHEMA` (all `CREATE TABLE IF NOT EXISTS`):

```sql
CREATE TABLE IF NOT EXISTS screens (
    posting_id      VARCHAR NOT NULL,
    rules_version   VARCHAR NOT NULL,          -- finder.version.rules_version()
    model_version   VARCHAR NOT NULL DEFAULT 'none',
    screened_at     TIMESTAMP NOT NULL,
    verdict         VARCHAR NOT NULL,          -- candidate | review | reject
    tier            INTEGER,                   -- 1 precise | 2 broad | 3 data lane | NULL no function hit
    rule_score      INTEGER NOT NULL,          -- 0-100
    fit_prob        DOUBLE,                    -- Phase 2
    embed_sim       DOUBLE,                    -- Phase 3 (raw cosine)
    llm_score       INTEGER,                   -- Phase 4
    final_score     INTEGER NOT NULL,          -- 0-100
    band            VARCHAR NOT NULL,          -- very_strong | strong | partial | weak | none
    reasons         JSON,
    flags           JSON,
    top_terms       JSON,                      -- [["lean six sigma", 0.41], ...]
    llm_notes       VARCHAR,
    PRIMARY KEY (posting_id, rules_version, model_version)
);
CREATE TABLE IF NOT EXISTS tracker (
    section VARCHAR NOT NULL, date_applied VARCHAR, company VARCHAR NOT NULL, role VARCHAR NOT NULL,
    status VARCHAR, posting_id VARCHAR, matched_posting_id VARCHAR, match_kind VARCHAR, synced_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    posting_id VARCHAR NOT NULL, decision VARCHAR NOT NULL,   -- build | pass | hold
    reason VARCHAR, source VARCHAR NOT NULL,                  -- file | cli | tracker
    source_ref VARCHAR, decided_at TIMESTAMP NOT NULL,
    PRIMARY KEY (posting_id, source, decided_at)
);
CREATE TABLE IF NOT EXISTS embeddings (
    posting_id VARCHAR NOT NULL, description_hash VARCHAR NOT NULL, model VARCHAR NOT NULL,
    vector FLOAT[384] NOT NULL, embedded_at TIMESTAMP NOT NULL, PRIMARY KEY (posting_id, model)
);
CREATE TABLE IF NOT EXISTS label_docs (
    label_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL,    -- application | jobs_found_escalated | jobs_found_passed | tracker | pseudo_neg
    source_ref VARCHAR, posting_id VARCHAR, company VARCHAR, title VARCHAR, text VARCHAR,
    label INTEGER NOT NULL, weight DOUBLE DEFAULT 1.0, loaded_at TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS models (
    model_version VARCHAR PRIMARY KEY, kind VARCHAR NOT NULL,  -- tfidf_lr | embed_centroid
    trained_at TIMESTAMP NOT NULL, n_pos INTEGER, n_neg INTEGER, cv_auc DOUBLE, cv_precision_at_20 DOUBLE,
    embed_lo DOUBLE, embed_hi DOUBLE, path VARCHAR, notes VARCHAR
);
CREATE TABLE IF NOT EXISTS readback_log (file VARCHAR PRIMARY KEY, mtime DOUBLE NOT NULL, read_at TIMESTAMP NOT NULL);
```

Append to `VIEWS`:

```sql
CREATE OR REPLACE VIEW vw_screen_latest AS
    SELECT * FROM screens QUALIFY row_number() OVER (PARTITION BY posting_id ORDER BY screened_at DESC) = 1;
CREATE OR REPLACE VIEW vw_decisions AS
    SELECT * FROM decisions QUALIFY row_number() OVER (PARTITION BY posting_id ORDER BY decided_at DESC) = 1;
CREATE OR REPLACE VIEW vw_shortlist AS
    SELECT p.posting_id, p.employer, p.title, p.location_primary, p.workplace_type, p.employment_type,
           p.pay_min, p.pay_max, p.pay_interval, p.url, p.posted_at, p.first_seen_at,
           date_diff('day', p.first_seen_at, now()) AS days_since_first_seen,
           s.final_score, s.band, s.tier, s.verdict, s.rule_score, s.fit_prob, s.embed_sim, s.llm_score,
           s.reasons, s.flags, s.top_terms, s.rules_version, s.model_version, s.screened_at
    FROM postings p
    JOIN vw_screen_latest s USING (posting_id)
    LEFT JOIN vw_decisions d USING (posting_id)
    LEFT JOIN tracker t ON t.matched_posting_id = p.posting_id
    WHERE p.status = 'active' AND s.verdict != 'reject' AND d.posting_id IS NULL AND t.matched_posting_id IS NULL
    ORDER BY s.final_score DESC, p.first_seen_at DESC;
CREATE OR REPLACE MACRO vw_scored_new(days) AS TABLE
    SELECT * FROM vw_shortlist WHERE days_since_first_seen <= days::INTEGER ORDER BY final_score DESC;
CREATE OR REPLACE VIEW vw_label_set AS
    SELECT label_id, source, posting_id, company, title, text, label, weight FROM label_docs WHERE text IS NOT NULL
    UNION ALL
    SELECT 'dec:' || d.posting_id, 'decision', p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN d.decision = 'build' THEN 1 ELSE 0 END, 1.0
    FROM vw_decisions d JOIN postings p USING (posting_id)
    WHERE d.decision IN ('build', 'pass') AND p.description_text IS NOT NULL;
```

Also, after every screening batch, write the latest values into the four reserved columns: `screen_verdict`, `screen_score = final_score`, `screen_reasons = '; '.join(reasons + flags)`, `screened_at`. Existing macros then support `WHERE screen_verdict != 'reject'`.

Verified in DuckDB 1.5.5: `FLOAT[384]` columns, `array_cosine_similarity`, `JSON` columns, `QUALIFY`, table macros, `COPY (SELECT …) TO 'x.parquet' (FORMAT PARQUET)`. Bind vectors as `?::FLOAT[384]`; prove it in the Phase 3 test.

## 4. Package layout (`backend/finder/`)

```
backend/finder/__init__.py
backend/finder/version.py      RULES_CODE_VERSION, rules_version()
backend/finder/rules.py        listing_from_row, JD-text rules, screen_row
backend/finder/pipeline.py     candidates, screen, combine, daily
backend/finder/tracker_sync.py parse_tracker, sync
backend/finder/report.py       write_jobs_found, snapshots, parse_decisions, read_back
backend/finder/labels.py       (Phase 2) vault label loaders, strip_boilerplate, sync_labels
backend/finder/features.py     (Phase 2) TF-IDF + LR train/predict/top_terms
backend/finder/embed.py        (Phase 3) encoder, embed_missing, centroid, similarity, calibrate
backend/finder/llm.py          (Phase 4) Gemini REST client, score_top
backend/finder/rubric.py       (Phase 4) public rubric skeleton; rubric_local.py gitignored
finder.py                      CLI (repo root, sibling of sweep_ats.py)
tests/test_finder.py
```

### 4.1 `version.py`
```python
RULES_CODE_VERSION = "2026-09-15.1"   # bump whenever rules.py logic changes without a constant change
def rules_version() -> str:
    """sha1 of RULES_CODE_VERSION + repr(sorted uppercase constants of backend.profile) [:12]."""
```
Iterate `vars(backend.profile)` for names that `isupper()`; `repr` dicts with sorted keys. Because `profile.py` star-imports `profile_local`, personal overrides are included automatically.

### 4.2 `rules.py`
- `listing_from_row(row: dict) -> Listing`: `source="ats"`, `search_pass=row["platform"]`, `company=employer`, `location=location_primary or ""`, `locations=json.loads(locations or "[]")`, `description=description_text or ""`, `salary_min/max` from `pay_min/pay_max` (if `pay_interval == "hour"` leave as-is; `screen.annual_top` treats `< 500` as hourly), `salary_predicted=False`, `extra={"posting_id", "platform", "workplace_type", "employment_type", "job_level"}`.
- Small additive edits in `backend/screen.py`: `is_remote` returns True when `job.extra.get("workplace_type") == "remote"`; `is_commutable` returns True when `extra["workplace_type"] == "hybrid"` **and** a commutable place matches (hybrid alone is not commutable); add a keyword `skip_tracker: bool = False` to `screen()` that bypasses section 9 (the finder handles the tracker via SQL). `sweep.py` behaviour must not change (run its tests / a `--from-raw` smoke if a raw file exists).
- JD-text rules, each `(title: str, text: str) -> tuple[list[str], list[str], dict]` (reasons, flags, notes). Regexes case-insensitive over the JD; the Required block (below) is used where stated:
  - `travel_rule`: find `(\d{1,2})\s*%` near `travel` (within 40 chars). A single number or "up to N%": if N > `TRAVEL_MAX_PCT` → flag `travel ceiling N% (limit M%)`; if N > 2·M → reason. A span "A–B%" / "A to B%": if B > 2·M → reason `travel span A-B% doubles the limit`; elif B > M → flag. Skip when `TRAVEL_MAX_PCT` is None.
  - `direct_reports_rule`: `(\d{1,3})\s*(direct reports|direct-reports|team of|staff of|people managers?)` → notes["reports"]=N; N > 2·`MAX_DIRECT_REPORTS` → reason `large team (N direct reports)`; N > MAX → flag. Any `LARGE_TEAM_MARKERS` hit (performance reviews, headcount growth, build/scale the org/practice/team, hiring plan) → flag `large-team markers`. Skip when MAX is None.
  - `domain_tenure_rule`: `(\d{1,2})\+?\s*years?[^.]{0,60}?\b(<DOMAIN_TENURE_TERMS alternation>)` in the Required block → flag `domain-tenure gate (term, N yrs)`; never a reason.
  - `discipline_rule`: counts `PLANT_DISCIPLINE_TERMS` hits vs `LANE_PROCESS_TERMS` hits (handoff, approval, decision rights, cycle time, SLA, intake, workflow, stakeholder, cross-functional, service level). If `"engineer" in title` and plant > lane → reason `different discipline (plant/industrial: <first term>)`; if plant ≥ 3 and plant > lane (any title) → reason `plant-floor scope (<term>)`; elif plant > 0 → flag `plant vocabulary (<term>) -- read REQUIRED quals`.
  - `corridor_rule`: only when `location_primary`/`locations` match `CORRIDOR_PLACES` and the Required block contains a plant/manufacturing term → reason `corridor manufacturing (required: <term>)`.
  - `assessment_gate_rule`: `ASSESSMENT_GATE_TERMS` anywhere → reason `assessment-gated (<term>)`.
  - `hours_cap_rule`: `part[- ]time|(\d{1,2})\s*[-–to]+\s*(\d{1,2})\s*hours?\s*(per|a|/)\s*week|\$\d[\d,]*\s*(per|/)\s*week` → notes["hours_capped"]=True, flag `hours capped -- do not annualize`; when set, the hourly annualization for comp is suppressed (treat comp as not posted).
  - `sales_ops_rule`: Required block contains `GTM|go-to-market|CRO|quota|pipeline|CPQ|sales enablement|revenue operations` (word-bounded) → reason `sales/revenue ops scope`; anywhere else in the JD → flag.
  - `required_block(text) -> str`: slice from the first heading matching `REQUIRED_HEADINGS` (`required|basic qualifications|minimum qualifications|what you.ll need|must have`) to the next heading matching `preferred|nice to have|desired|bonus` or 2,500 chars, whichever first; falls back to the whole text when no heading is found.
- `screen_row(row: dict, rv: str) -> ScreenRecord` (dataclass: posting_id, verdict, tier, rule_score, reasons, flags, notes): call `screen(listing, skip_tracker=True)`, then the JD rules, merge lists (dedupe, keep order). Tier: 1 if `PRECISE_TITLE_TERMS` hit in title; else 2 if `TITLE_FUNCTION_TERMS` hit; else 3 if `DATA_LANE_ENABLED` and `DATA_LANE_TERMS` hit in title (and remove the `off-function title` reason in that case); else NULL. Tier 3 + assessment gate → reject. Rule points (`RULE_POINTS` dict in profile): tier1 30, tier2 15, tier3 8, each extra function hit 5 (cap 10), senior 5, remote 10, commutable hybrid 5, comp top ≥ `COMP_ASK` 10 / ≥ `COMP_FLOOR` 5 / posted 2, each flag −5 (floor −25); `rule_score = clamp(sum, 0, 100)`.

### 4.3 `pipeline.py`
```python
RESCREEN_SQL = """
SELECT p.* FROM postings p LEFT JOIN vw_screen_latest s USING (posting_id)
WHERE p.status = 'active' AND (s.posting_id IS NULL OR s.rules_version != ? OR s.model_version != ?
      OR s.screened_at < p.description_fetched_at)"""
def candidates(con, rv, mv, since=None, limit=None) -> list[dict]      # dict rows, ordered first_seen_at DESC
def screen(con, *, since=None, full=False, model=None, encoder=None, log=print) -> dict   # counts by verdict/band
def combine(rule_score, fit_prob, embed_sim, llm_score, calib) -> tuple[int, str]
def daily(con, *, since, vault_dir, llm_top=0, report=True, log=print) -> dict
```
`screen()` processes 500-row batches inside `BEGIN`/`COMMIT`: rules → (predict fit_prob + top_terms when a model is loaded) → (embed_sim when an encoder + centroid exist) → `combine` → `INSERT OR REPLACE INTO screens` → `UPDATE postings SET screen_* …`. `full=True` ignores the predicate and re-screens every active row. `daily()` order: `tracker_sync.sync` → `report.read_back` → `screen(since=…)` → `llm.score_top(llm_top)` if > 0 → `report.write_jobs_found` → `report.snapshots`. Log one line per stage with counts and seconds.

### 4.4 `tracker_sync.py`
- `parse_tracker(path) -> list[TrackerRow]` (section, date_applied, company, role, status, posting_id). Reuse the section/separator logic of the vault's `tracker_lookup.py`. Cells 0..2 positional; `status` = cell 3 when present. `posting_id` = the cell under a header named `Posting ID` when the header row has one, else the first match of `\b[0-9a-f]{20}\b` anywhere in the row. Skip a section whose header's second cell is `Platform` (Consultant Networks).
- `sync(con, vault_dir) -> dict`: `DELETE FROM tracker`; for each row, exact match when `posting_id` exists in `postings`; else fuzzy: `screen.company_keys(company)` vs distinct employers via `screen.company_matches`, then `screen.similar_title(role, title)` over that employer's postings (active and closed), newest `first_seen_at` wins; record `match_kind` exact|fuzzy|none. For every matched row, insert `decisions(source='tracker', decision='build', source_ref=section)` if no decision from source `tracker` exists for that posting. Return counts (rows, exact, fuzzy, none) and print unmatched rows on `--verbose`.

### 4.5 `report.py`
- `write_jobs_found(con, vault_dir, run_meta, *, max_blocks=15, block_min_band="strong", table_min=50, passed_cap=200, out_path=None) -> Path`. Output format in §7. Rows come from `vw_shortlist` (not decided, not tracked). Blocks for `band in (very_strong, strong)` up to `max_blocks`; summary-table rows for everything with `final_score >= table_min`; Passed section for rejected rows with a non-NULL tier screened in this run (cap `passed_cap`). After writing, insert a `decisions` row? **No.** Surfacing is not a decision. Instead record surfaced postings in `readback_log`-style bookkeeping: add table `surfaced(posting_id, file, surfaced_at, PRIMARY KEY (posting_id, file))` to §3 and exclude postings surfaced within the last 14 days from new blocks (they may still appear in the summary table with an `(shown YYYY-MM-DD)` note).
- `snapshots(con, out_dir="db/snapshots") -> list[Path]`: `COPY (SELECT * FROM vw_shortlist) TO 'shortlist.parquet'`, same for `vw_screen_latest`, `decisions`, `tracker`; plus `shortlist.csv` and `shortlist_YYYYMMDD.csv`. Overwrite the undated files.
- `parse_decisions(path) -> list[Decision]`: read the `## Summary — decide here` table; a row counts when the `Decision` cell is one of build/pass/hold (case-insensitive, trimmed); `reason` = the Reason cell.
- `read_back(con, vault_dir) -> int`: every `Search_Results/Jobs_Found_*.md` whose mtime is newer than `readback_log.mtime` (or unseen) is parsed; decisions inserted with `source='file'`, `source_ref=<filename>`, `decided_at=now`; skip a (posting_id, decision) already present from the same file. Update `readback_log`.

### 4.6 `finder.py` (CLI, argparse subcommands; every subcommand accepts `--db`)
`screen [--full] [--since-hours N] [--limit N] [--no-model] [--no-embed]` · `rescreen-all` (= `screen --full`, then prints a verdict-count diff vs the previous latest) · `report [--max-blocks N] [--table-min N] [--out PATH] [--no-snapshots]` · `mark <posting_id|url|"employer|title"> <build|pass|hold> [--reason TEXT]` (resolve URL by `url` or `req_id` substring, `employer|title` by `company_matches` + `similar_title`; refuse on 0 or >1 hits) · `sync [--verbose]` · `shortlist [--days 7] [--n 30]` (prints a fixed-width table from `vw_scored_new`) · `labels [--report]` · `train [--cv 5] [--C 4.0] [--report]` · `embed [--all] [--limit N] [--backend st|fastembed]` · `llm --top N [--dry-run]`.
Load `.env` with `python-dotenv` from the repo root; `sys.path.insert(0, ".")` like `sweep_ats.py`.

### 4.7 `sweep_ats.py` / `backend/ats/sweep.py`
New `run()` kwargs `screen=True, report=True, llm_top=0, full_screen=False`; CLI flags `--no-screen`, `--no-report`, `--llm-top N`, `--full-screen`. After the backlog detail pass: `if screen: from backend.finder import pipeline; pipeline.daily(con, since=stats["started"] if stats else None, vault_dir=os.getenv("JOBSEARCH_VAULT_DIR"), llm_top=llm_top, report=report and bool(vault_dir), log=log)`. `report` silently becomes False when `JOBSEARCH_VAULT_DIR` is unset. Wrap the finder call so an exception logs and does not lose the sweep's `log_run`.

## 5. Public/private split (`backend/profile.py` additions)

Public, neutral defaults:
```python
RULE_POINTS = {"tier1": 30, "tier2": 15, "tier3": 8, "extra_hit": 5, "extra_hit_cap": 10, "senior": 5, "remote": 10,
               "commutable_hybrid": 5, "comp_ask": 10, "comp_floor": 5, "comp_posted": 2, "flag": -5, "flag_floor": -25}
SCORE_WEIGHTS = {"rule": 0.40, "fit": 0.35, "embed": 0.25}
SCORE_BANDS = [(85, "very_strong"), (70, "strong"), (50, "partial"), (30, "weak"), (0, "none")]
LLM_BLEND = 0.5
TIER_CAP = {3: 80}
DATA_LANE_ENABLED = True
DATA_LANE_TERMS = ["business intelligence", " bi ", "data engineer", "analytics engineer", "data model", "reporting analyst", "operations analyst", "data analytics", "insights"]
LANE_PROCESS_TERMS = ["handoff", "hand-off", "approval", "decision rights", "cycle time", "sla", "intake", "workflow", "stakeholder", "cross-functional", "service level", "process map", "value stream"]
ASSESSMENT_GATE_TERMS = ["ccat", "cognitive aptitude", "criteria corp", "aptitude test", "cognitive assessment"]
LARGE_TEAM_MARKERS = ["performance reviews", "headcount growth", "build the org", "scale the team", "build and scale", "hiring plan", "build the practice", "grow the team"]
BOILERPLATE_PATTERNS = [r"equal opportunity employer.*", r"eeo statement.*", r"benefits?:.*", r"about (us|the company).*", r"reasonable accommodation.*"]   # applied per line/paragraph
REQUIRED_HEADINGS = r"required|basic qualifications|minimum qualifications|what you.ll need|must have"
PREFERRED_HEADINGS = r"preferred|nice to have|desired|bonus"
TRAVEL_MAX_PCT = None
MAX_DIRECT_REPORTS = None
DOMAIN_TENURE_TERMS = []
CORRIDOR_PLACES = []
FAITH_COMP_FLOOR = None
```
`profile_local.example.py` gains the same keys with neutral example values (`TRAVEL_MAX_PCT = 25`, `MAX_DIRECT_REPORTS = 5`, `DOMAIN_TENURE_TERMS = ["banking", "pharma"]`, `CORRIDOR_PLACES = ["springfield"]`, `FAITH_COMP_FLOOR = 85_000`). The real `profile_local.py` gets the appendix values. `.env.template` gains `GEMINI_API_KEY=`, `GEMINI_API_MODEL=gemma-4-31b-it`, `JOBSEARCH_EMBED_BACKEND=` (blank = auto). `requirements.txt` gains two commented sections: `# Finder (optional): scikit-learn numpy scipy joblib` and `# Embeddings (optional): sentence-transformers torch  (or: fastembed)`. CLAUDE.md line "No LLM call anywhere" → "No LLM call unless `--llm-top` is passed"; add `backend/finder/` and `finder.py` to the repo map.

## 6. Scoring

- Hard reject → `final_score = 0`, `band = "none"`, reasons kept.
- Otherwise: `rule = rule_score`; `fit = 100 * fit_prob` if present; `embed = 100 * clamp((cos - embed_lo) / (embed_hi - embed_lo), 0, 1)` if present (lo/hi from the latest `models` row of kind `embed_centroid`; if none, `embed = None`). Weighted mean of the signals present, weights renormalized. Apply `TIER_CAP` (tier 3 ≤ 80). If `llm_score` present: `final = (1 - LLM_BLEND) * final + LLM_BLEND * llm_score`. Round to int; band by the first threshold met in `SCORE_BANDS`.
- `finder train --report` prints single-signal AUCs (rule_score, fit_prob, embed_sim) and the blend's AUC over `vw_label_set` rows that have a screen, so the weights can be tuned later.

## 7. Jobs_Found file format (exact; Batch Mode parses it)

```
---
node_id: JOBS:found-YYYYMMDD-HHMM
node_type: search_results
tags: [#job-search #pipeline #ats]
---
# Jobs Found — ATS pipeline (`jobsearch/finder.py`), YYYY-MM-DD HH:MM

**Run type:** ATS pipeline screen. **Sources:** N boards (platform counts). **Window:** postings first seen since <since>. **The bar:** rule engine + model score; every block is live on the employer's own ATS at run time. **Dedup:** tracker N rows (M matched), decisions N, surfaced-in-last-14-days N. **Funnel:** N screened → N candidate / N review / N reject → N blocks. **Versions:** rules <rv> · model <mv>.

## Coverage Log

| Source / step | Status | Notes |
|---|---|---|
| <platform> boards | ✅ | N ok, N failed (from vw_board_health) |
| Screen | ✅ | N rows, S s |
| Model / Embed / LLM | ✅ or ⏭️ | ... |

## Summary — decide here

| Posting ID | Company | Title | Score | Band | Tier | Decision | Reason |
|---|---|---|---|---|---|---|---|
| <20 hex> | <employer> | [<title>](<url>) | 88 | very_strong | 1 |  |  |

## Escalated Roles (pipeline-scored)

---

# Company: <employer>
## Title: <title>
Apply: <url>
<description_text verbatim; any line beginning with one or more '#' has the '#'s stripped; no other edits>

---

**Fit: ~88%.** rule 72 · fit 0.81 · embed 0.63 · top terms: lean six sigma, operating model, kaizen · flags: <flags joined by '; '>

---

## Passed / Filtered Out

| Company | Role | Reason |
|---|---|---|
| <employer> | [<title>](<url>) | <reasons joined by '; '> `pid:<20 hex>` |
```
Rules: a blank line before and after every `---`; one block per escalated row; the `**Fit:**` stanza sits in its own `---` fence after the block; no markdown headings inside a JD body; every Passed row has an employer and a link. File name `Jobs_Found_YYYYMMDD_HHMM.md` in `Search_Results/`; never overwrite an existing file (append `_2` etc.).

## 8. Phase 2 details (labels + TF-IDF/LR)

- `labels.load_applications(vault_dir)`: for each `Applications/*/index.md`, frontmatter `status`, `company`, `role`; text = section between `## Job Description` and the next `## `, minus lines before the first blank line when they start with `Apply:` or contain ` · ` metadata; label 0 when status ∈ {pass, not-pursuing, not_pursuing, passed}, else 1 (`declined`, `rejected`, `applied`, `evaluating` are all fit-positives); skip when text < 800 chars. `label_id = sha1("application|" + folder)[:20]`.
- `load_jobs_found_escalated`: `# Company:` / `## Title:` / `Apply:` blocks → label 1, weight 0.7, dedup against applications by `company_keys` + `similar_title`.
- `load_jobs_found_passed`: `## Passed / Filtered Out` rows → label 0, text NULL; `match_to_postings` fills `posting_id` and text by URL (`req_id` substring or exact url) else company + title over `postings` (active and closed).
- `pseudo_negatives(con, n=1500, seed=7)`: random active postings with a JD whose `screen_row` verdict is reject on `off-function title` or `off-lane title`; label 0, weight 0.5.
- `sync_labels(con, vault_dir)` upserts `label_docs` and prints counts per source and how many have text.
- `features.train(con, C=4.0, min_df=3, ngram=(1,2), max_features=50_000, cv=5, seed=7)`: text = `title + "\n" + title + "\n" + strip_boilerplate(text)`; `TfidfVectorizer(ngram_range, min_df, sublinear_tf=True, stop_words="english", max_features)` + `LogisticRegression(class_weight="balanced", C, max_iter=2000)`; `StratifiedKFold(cv)` report (AUC, precision@20 on held-out, confusion at 0.5), fit on all, `joblib.dump({"vec", "clf", "version"})` to `db/models/<version>.joblib`, insert `models`. `model_version = sha1(sorted label_ids + params)[:12]`. Warn when positives or non-pseudo negatives with text < 150 and set a note; in that case `pipeline.combine` uses weight 0.15 for `fit`.
- `predict(model, texts)`, `top_terms(model, text, k=6)` (tf-idf row × `coef_`, top positive contributions), `load_latest(con)`.
- Also print a **hard-negatives** list: top 20 `fit_prob` among label 0 docs, with source refs.

## 9. Phase 3 details (embeddings)

- `EMBED_MODEL = "BAAI/bge-small-en-v1.5"`, `EMBED_DIM = 384`. `load_encoder(backend=None)`: try `sentence_transformers.SentenceTransformer`, else `fastembed.TextEmbedding`, else raise with an install hint; `JOBSEARCH_EMBED_BACKEND` overrides.
- `chunks(text, size=1500, max_chunks=4)` after `strip_boilerplate`; encode chunks, mean-pool, L2-normalize.
- `embed_missing(con, encoder, only_screen_survivors=True, batch=64, limit=None)`: rows with a JD and no `embeddings` row for this model or a changed `description_hash`. Insert with `?::FLOAT[384]`.
- `positive_centroid(con, encoder)`: mean of label-1 `vw_label_set` texts (encode on the fly; cache `db/models/centroid_<version>.npy`). `similarity(con, posting_ids, centroid)` via `array_cosine_similarity(vector, ?::FLOAT[384])`. `calibrate(con, centroid)`: `embed_lo` = 10th percentile of label-0 cosines, `embed_hi` = 90th percentile of label-1 cosines; insert a `models` row of kind `embed_centroid`.
- Full-corpus embed (`--all`) is a one-time background job (~45–90 min CPU); the daily path embeds only screen survivors.

## 10. Phase 4 details (optional LLM)

- `llm.py`: `httpx` POST to `https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent?key=…` with `response_mime_type: application/json`; prompt = `rubric.RUBRIC_PUBLIC` + `rubric_local.RUBRIC_PERSONAL` + title/employer/JD (trimmed to 12k chars); parse `{"score": int, "lane": str, "level": str, "blockers": [str], "notes": str}`. Models from `GEMINI_API_MODEL` in order; on 429/5xx/timeout back off (2 s, 8 s) then fall to the next model. Pace ≤ 30 requests/min.
- `score_top(con, n, rules_version, model_version)`: top n of `vw_shortlist` without `llm_score` for the current versions; `UPDATE screens SET llm_score, llm_notes, final_score, band`. `--dry-run` prints the prompts and calls nothing.
- `rubric.py` holds the schema, scoring anchors (90+ bullseye function and level; 70 function match with one mitigable gap; 50 adjacent; <30 wrong lane) and placeholders; `rubric_local.py` supplies `RUBRIC_PERSONAL` from the appendix. `rubric.py` ends with the same `try: from backend.finder.rubric_local import *` pattern.

## 11. Acceptance criteria and the Fable audit checklist

Phase 1 (rules, report, snapshots, tracker, decisions):
- [ ] `pytest -q` green: existing 19 + new tests covering `listing_from_row`, every JD rule (both the flag and the reason branch), `rules_version()` stability/change, `pipeline.screen` writes `screens` + `postings.screen_*`, the re-screen predicate selects only new/changed/version-changed rows, `vw_shortlist` excludes decided and tracked rows, `combine` renormalization + band edges + tier cap + hard reject, tracker parser (row without id, id in a trailing cell, ragged Closed row, Consultant Networks skipped, fuzzy match), report writer output re-parses (count of `# Company:` == blocks, blank lines around every `---`, no `#` inside JD bodies), `parse_decisions` round-trip, `read_back` idempotent on an unchanged file.
- [ ] `finder.py screen --full --db <scratch>` over the corpus finishes in < 10 min and prints verdict and band counts; candidate+review in the low thousands.
- [ ] `finder.py report --db <scratch> --out <tmp>` produces a file that `Skill_Job_Application_Analysis.md` Batch Mode could parse, ≤ 15 blocks.
- [ ] `finder.py sync --db <scratch>`: ~348 tracker rows, > 100 matched.
- [ ] `sweep_ats.py --skip-sweep --detail-budget 0 --db <scratch>` runs the finder stage end to end and writes snapshots; `--no-screen` skips it; the sweep still runs with sklearn uninstalled (test by running in a shell with `PYTHONPATH` tricks or by mocking the import).
- [ ] Eyeball on the scratch DB (record the SQL and results in STATUS.md for the audit): the Henry Schein `R134977` Senior Manager AI Transformation & Process Excellence row and the PFG Global Process Owner Director row are tier 1 and in the top 20; QIAGEN / Lonza plant-floor rows in the corridor are `reject` with `different discipline` / `corridor manufacturing`; Equinix Director Business Process Excellence carries `sales/revenue ops scope` only if GTM/CRO is in its Required block, else a flag; Crossover rows reject `assessment-gated`; a USAA "10+ years banking" row is flagged `domain-tenure gate`, not rejected; a Verizon hybrid CX Transformation row is flagged local/hybrid, not rejected on comp.
- [ ] `git grep --untracked … .personal_patterns` empty; `git status` shows no `db/models`, `db/snapshots`, `rubric_local.py`, `profile_local.py`.

Phase 2: `finder labels --report` shows ≥ 250 positive docs with text and ≥ 150 non-pseudo negatives with text (or the warning path is exercised); `finder train --report` 5-fold AUC ≥ 0.85; positives' mean `fit_prob` > 0.7; the hard-negative list is printed and plausible; `top_terms` in the Fit stanza reads sensibly.
Phase 3: `FLOAT[384]` insert and cosine query proven by a test with a fake encoder; Applications JDs' cosine to the centroid clearly above the corpus median (print both); `--no-embed` works.
Phase 4: `finder llm --top 5 --dry-run` prints prompts without a call; a real run stores five `llm_score`s; model fallback path unit-tested with a mocked 429.
Phase 5: vault edits listed in §12 done; STATUS.md rewritten; README section "Finding" added.

Audit method for Fable: read `docs/STATUS.md` (the builder's log), run the test suite, run `finder.py shortlist --days 30 --n 30`, open the newest pipeline Jobs_Found file, spot-check 10 rows against the rules in the vault skills, run the eyeball SQL above, diff `profile.py` against this spec, and check that no personal value leaked into a tracked file.

## 12. Vault edits (Phase 5; the vault is at `$JOBSEARCH_VAULT_DIR`)

- `Professional/Areas/Job_Search/Application_Tracker.md`: append `| Posting ID |` to the header and separator rows of the Active, On Hold and Closed tables only (existing rows are left short; the parser tolerates it).
- `System/Context/Skills/Skill_Job_Application_Analysis.md`, Batch Mode Phase 0: pipeline-produced files (`# Jobs Found — ATS pipeline`) carry a `## Summary — decide here` table above the blocks; fan out only rows whose Decision is `build` (blank = undecided, ask); copy the row's Posting ID into the tracker row's new column; Fit-stanza handling unchanged; the pipeline reads the Decision column back on its next run.
- `System/Context/Skills/Skill_CoWork_Job_Search.md`, Output Format and Deduplication: the summary table and `pid:` tokens are pipeline-only additions Cowork may ignore; `db/snapshots/shortlist.csv` and the latest pipeline Jobs_Found are dedup inputs alongside `tracker_lookup.py`.
- `System/Context/Skills/Skill_Agentic_Job_Search.md`, Execution Workflow: the ATS pipeline runs the Filtering Criteria as code in `backend/profile*.py`; a rule change there gets a note back in this skill.
- `Professional/Areas/Job_Search/Tools/README.md`: `finder.py mark`, snapshots, and the Decision column.
