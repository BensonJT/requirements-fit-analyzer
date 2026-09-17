# Sprint Plan — the job-finding layer (`backend/finder/`)

_Binding build contract, written 2026-09-15 by the design session (Claude Fable). Built by Opus (high or xhigh effort), audited by Fable afterward against §11. Execute phases in order; do not reorder or skip without the user's confirmation. Personal values (pay thresholds, places, travel limit, headcount limit, rubric prose) are NOT in this file: they live in the vault appendix `Tools/Finder_Build_Personal_Appendix.md` under `$JOBSEARCH_VAULT_DIR/Professional/Areas/Job_Search/`. Read that file before Phase 1 and copy its values into `backend/profile_local.py` and `backend/finder/rubric_local.py` (both gitignored). Never put any of its contents into a tracked file._

## 0. Builder rules

- Read `CLAUDE.md`, `docs/STATUS.md`, this file, and the vault appendix first. Then read `backend/ats/store.py`, `backend/ats/sweep.py`, `backend/screen.py`, `backend/profile.py`, `sweep.py` (its `load_tracker`, `load_recent_jobs_found`, and the CSV/markdown writers at the end of `main`), and `tests/test_ats.py`. Reuse what is there; do not fork `screen.py` into a second rule engine.
- **The live DuckDB may be locked** by a running backfill (`ps -eo pid,cmd | grep -E '[s]weep_ats.py'`; `grep '^=== DONE' output/backfill_20260915_day.log`). Until it is free, develop and test against a scratch copy: `cp db/jobsearch.duckdb /tmp/finder_scratch.duckdb` once the lock is released, or a tiny `tmp_path` DB in tests. Every CLI in this spec takes `--db`.
- Python 3.14 venv at `.venv`. New dependencies are allowed and verified to have cp314 wheels: `scikit-learn`, `numpy`, `scipy`, `joblib`, `sentence-transformers`, `torch`, `fastembed` (fallback). Install into `.venv` only. The finder must import lazily so `sweep_ats.py` still runs with none of them installed.
- Commit locally after each phase (imperative first line ≤72 chars, 2–5 bullets). **Do not push.** Before every commit run `git grep --untracked -n -i -E -f .personal_patterns -- . ':!.personal_patterns' ':!LICENSE'` and require empty output.
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

## 9. Phase 3 details (embeddings) — SUPERSEDED by §15 (requirement coverage); the encoder loader and FLOAT[384] storage carry over, the centroid does not

- `EMBED_MODEL = "BAAI/bge-small-en-v1.5"`, `EMBED_DIM = 384`. `load_encoder(backend=None)`: try `sentence_transformers.SentenceTransformer`, else `fastembed.TextEmbedding`, else raise with an install hint; `JOBSEARCH_EMBED_BACKEND` overrides.
- `chunks(text, size=1500, max_chunks=4)` after `strip_boilerplate`; encode chunks, mean-pool, L2-normalize.
- `embed_missing(con, encoder, only_screen_survivors=True, batch=64, limit=None)`: rows with a JD and no `embeddings` row for this model or a changed `description_hash`. Insert with `?::FLOAT[384]`.
- `positive_centroid(con, encoder)`: mean of label-1 `vw_label_set` texts (encode on the fly; cache `db/models/centroid_<version>.npy`). `similarity(con, posting_ids, centroid)` via `array_cosine_similarity(vector, ?::FLOAT[384])`. `calibrate(con, centroid)`: `embed_lo` = 10th percentile of label-0 cosines, `embed_hi` = 90th percentile of label-1 cosines; insert a `models` row of kind `embed_centroid`.
- Full-corpus embed (`--all`) is a one-time background job (~45–90 min CPU); the daily path embeds only screen survivors.

## 10. Phase 4 details (optional LLM) — EXTENDED by §15.5 (review ledger, rate limits, Claude Code batch path); where they differ §15.5 wins

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
Phase 3 (superseded by §15.7): `FLOAT[384]` insert and cosine query proven by a test with a fake encoder; Applications JDs' cosine to the centroid clearly above the corpus median (print both); `--no-embed` works.
Phase 4 (superseded by §15.8): `finder llm --top 5 --dry-run` prints prompts without a call; a real run stores five `llm_score`s; model fallback path unit-tested with a mocked 429.
Phase 5: vault edits listed in §12 done; STATUS.md rewritten; README section "Finding" added.

Audit method for Fable: read `docs/STATUS.md` (the builder's log), run the test suite, run `finder.py shortlist --days 30 --n 30`, open the newest pipeline Jobs_Found file, spot-check 10 rows against the rules in the vault skills, run the eyeball SQL above, diff `profile.py` against this spec, and check that no personal value leaked into a tracked file.

## 12. Vault edits (Phase 5; the vault is at `$JOBSEARCH_VAULT_DIR`)

- `Professional/Areas/Job_Search/Application_Tracker.md`: append `| Posting ID |` to the header and separator rows of the Active, On Hold and Closed tables only (existing rows are left short; the parser tolerates it).
- `System/Context/Skills/Skill_Job_Application_Analysis.md`, Batch Mode Phase 0: pipeline-produced files (`# Jobs Found — ATS pipeline`) carry a `## Summary — decide here` table above the blocks; fan out only rows whose Decision is `build` (blank = undecided, ask); copy the row's Posting ID into the tracker row's new column; Fit-stanza handling unchanged; the pipeline reads the Decision column back on its next run.
- `System/Context/Skills/Skill_CoWork_Job_Search.md`, Output Format and Deduplication: the summary table and `pid:` tokens are pipeline-only additions Cowork may ignore; `db/snapshots/shortlist.csv` and the latest pipeline Jobs_Found are dedup inputs alongside `tracker_lookup.py`.
- `System/Context/Skills/Skill_Agentic_Job_Search.md`, Execution Workflow: the ATS pipeline runs the Filtering Criteria as code in `backend/profile*.py`; a rule change there gets a note back in this skill.
- `Professional/Areas/Job_Search/Tools/README.md`: `finder.py mark`, snapshots, and the Decision column.

## 13. Amendments (2026-09-15, user decisions after the Phase 2 build; binding over §5, §6 and §8 where they differ)

- **Labels (§8).** Nothing in the vault is a negative. A JD that reached the vault had language that fit; a pass there was about nuance. Application folders are always label 1 (pass / not-pursuing = near miss, weight 0.5); `## Passed / Filtered Out` rows and `pass` decisions stay in `label_docs` / `decisions` as context but are never trained on. Negatives are pseudo-negatives only: random active postings with a JD and no function term in the title (level and location play no part). The low-data warning checks positives and pseudo-negatives.
- **Source invariance (§8).** The model must not learn where a JD was copied from. Text is HTML-unescaped and NFKC-normalized; lines naming two or more `BOILERPLATE_MARKERS` are dropped; the employer's name is removed; tokens are letters only; `MODEL_STOP_WORDS` removes level words (associate, senior, director…), logistics and career-site boilerplate; `max_df = 0.5`. A vault positive matched to a posting by URL or req id is trained on the posting's text; any other matched vault positive is trained on both copies, grouped in CV (`StratifiedGroupKFold`). `train` prints the paired held-out gap (same jobs, vault vs career-site copy).
- **Level (§4.2).** The pay band and the MOST years any JD line asks for decide level; a low-years exception line never lowers it. `LEVEL_YEARS_SENIOR = 8`, `LEVEL_YEARS_MID = 5`: under 5 = `junior level` reason unless an unambiguous senior title (`SENIOR_LEVEL_TITLE_TERMS`: director, VP, principal, head of, chief) or a band top at the floor (then a flag); 5–7 = `mid level` flag unless a senior title or a band top at the ask. `EARLY_CAREER_TITLE_TERMS` (intern, summer associate, entry level, junior…) = reason. "Associate" / "assistant" never imply level; `screen.py`'s `below target level` output is superseded in the finder. The `senior` rule point comes from this level (years ≥ 8, senior title, or band top at the ask), not from title words.
- **Workplace (§4.2).** A JD that states in-office days per week (or fully on-site) sets `workplace_type` when the ATS left it empty, and a stated hybrid / onsite workplace is never treated as remote (the multi-state "remote" guess no longer overrides it).
- **Outside the US (§4.2).** Hard reject when no location segment is in the US: the ATS `country` code is not US, or every segment names a `NON_US_TERMS` place. One US segment keeps the posting; a bare "Remote" never rejects.
- **Rule score (§4.2, §6).** `rule_score` is rescaled to 0–100 against `rule_max()` (tier1 + extra_hit_cap + senior + max(remote, commutable_hybrid) + comp_ask), so it blends with fit on the same scale.

## 14. Amendment — content-first scoring (2026-09-15, user decision; replaces §6 and the §4.2 rule points)

- **Three questions, three kinds of evidence.** Function fit is decided by the JD content (the fit model; embeddings join it in Phase 3). Level is decided by required years + pay band (§13). "Can I take it" is decided by location, pay floor, travel and hours. The title is supporting evidence only.
- **Content gate** (`pipeline.apply_content_gate`, runs after the model scores a row): when a JD was scored, `off-function title` / `off-lane title` reasons are dropped (off-lane stays visible as a flag); fit < `FIT_REJECT` (0.35) → reason `content does not fit (fit x)`; fit < `FIT_REVIEW` (0.50) → flag `content fit borderline (fit x)`. With no JD or no model the title gate stands. Every other hard reason (location, outside the US, comp floor, junior / early-career, travel span, large team, discipline / plant floor, sales ops in Required, assessment-gated, ITIL change, hard-avoid industry, blocked posters) still rejects.
- **Final score** (`pipeline.combine`): `SCORE_COMPONENT_WEIGHTS` = content **0.90** (was 0.50 until the 2026-09-15 sweep, see §18.9) · level 0.20 · location 0.15 · pay 0.10 · title 0.05. `screens.rule_score` holds the profile score = the four non-content components weighted on 0–100 (`rules.profile_score`); final = 0.9·content + 0.1·profile (a low-data `fit_weight` replaces the content weight). Content = 100·fit, averaged with the calibrated embedding score when present. No content → profile score capped at `NO_CONTENT_CAP` (60). Then `TIER_CAP`, minus `FLAG_PENALTY` (5) per flag up to `FLAG_PENALTY_CAP` (25), then the LLM blend. Bands unchanged.
- **Component points:** `LEVEL_POINTS` senior 100 (years ≥ 8, band top at the ask, or unambiguous senior title) · mid 50 · not stated 60. `LOCATION_POINTS` remote 100 · commutable hybrid 80 · commutable on-site or unstated 70 · nationwide unverified 60. `PAY_POINTS` band top at the ask 100 · at the floor 75 · not posted 60 (unposted pay is unknown until a screening conversation). `TITLE_POINTS` tier 1 100 · tier 2 70 · tier 3 50 · no function term 30 · off-lane 0.
- `RULE_POINTS`, `SCORE_WEIGHTS`, `rules.rule_points` and `rules.rule_max` are removed. The Fit stanza reads `profile N · fit x · …`.
- **Fixes (same day, found on the Henry Schein R134977 bullseye):** (1) `required_years` reads "10 or more years", "15 years or more of experience", "a minimum of ten (10) years", "at least eight years'", "five to seven years", "(8) years" and "Experience: 12+ years" (number phrase before the year word, "experience" in the same sentence). (2) Flags already priced by a component cost no points (`UNPENALIZED_FLAG_PATTERNS`: ask above band top, local/hybrid, nationwide listing, mid level, few years asked, content fit borderline, faith signal); they stay visible and still send a posting to review.

## 15. Amendment — Phase 3 becomes requirement coverage; Phase 4 gets a review ledger; context is configuration (2026-09-15, user decision; replaces §9 and extends §10)

**Why.** The TF-IDF fit model (§8, §13) scores shared vocabulary, not context: senior roles in another function that use the same words (transformation, governance, change management) score as high as bullseyes. Context comes from sentences. Phase 3 therefore matches each requirement in a JD, by meaning, against evidence of what the user has actually done. Industry is never a negative by itself: a healthcare JD that asks for work the user has done covers well. The fit model stays as the cheap first gate (`FIT_REJECT`).

### 15.1 Evidence (the user's background as data)

- **Manifest, not code.** `evidence.local.toml` at the repo root (gitignored; path overridable with `JOBSEARCH_EVIDENCE`), parsed with `tomllib`. Committed `evidence.example.toml` uses neutral fixture files under `tests/fixtures/evidence/`. Sample shape:
  ```toml
  embed_model = "BAAI/bge-small-en-v1.5"
  not_in_record = ["Tool X", "Certification Y"]     # never counted as covered; listed as a gap when a requirement names one

  [[source]]
  name = "resume_bullets"
  type = "csv"                  # csv | markdown | pdf | html | text
  path = "~/evidence/resume_bullets.csv"
  text_columns = ["context_statement", "bullet"]   # joined per row
  ref_columns = ["position", "project"]            # shown in the report as the match reference
  kind = "achievement"          # achievement 1.0 | duty 0.9 | narrative 0.8 | method 0.7 (unit weight)

  [[source]]
  name = "articles"
  type = "markdown"
  path = "~/evidence/articles"
  include = ["**/*.md"]
  exclude = ["BACKLOG.md"]
  kind = "method"

  [[guard]]                     # never embedded as evidence; Phase 4 prompt material only
  name = "claim_guards"
  path = "~/evidence/guards.csv"
  text_columns = ["do_not_claim"]
  ```
- **`backend/finder/evidence.py`**: `load_manifest(path)`, `iter_units(manifest)` → `EvidenceUnit(unit_id, source, kind, ref, text, weight)`. CSV = one unit per row (text columns joined; long rows split at sentence ends); markdown = paragraphs and list items, headings kept as `ref`; pdf = `pdftotext -layout` when on PATH, else optional `pypdf`, then paragraphs; html = `beautifulsoup4` visible text (nav, footer, script, style dropped), then paragraphs. Units 40–600 chars (longer split at sentence ends; shorter merged with the next in the same section). `unit_id = sha1(source|ref|text)[:20]`. Exact-duplicate text across sources is kept once (highest weight).
- **Table** (append to `SCHEMA`): `evidence_units(unit_id PK, source, kind, ref, text, weight, model, vector FLOAT[384], content_hash, embedded_at)`. `evidence_version = sha1(sorted unit_ids + embed_model)[:12]`; a changed file changes the version, and only new unit_ids are embedded.
- **CLI**: `finder.py evidence --check` (every source: path found, units, chars, 3 sample units, the `not_in_record` list; exit 1 on a missing path) · `finder.py evidence --rebuild` (embed missing units).

### 15.2 Requirement units (the JD as claims to cover)

- **`backend/finder/requirements.py`**: `split_requirements(text) -> list[Requirement(text, section, weight, klass)]`. HTML-unescape and `strip_boilerplate` first. Sections by heading (reuse `rules._heading_lines`): `REQUIRED_HEADINGS` → weight 1.0; new `RESPONSIBILITY_HEADINGS` (`responsibilit|what you.ll do|duties|the role|key accountabilities|in this role`) → 0.8; `PREFERRED_HEADINGS` → 0.4; about-the-company, benefits, pay, EEO sections dropped; no headings → every sentence at 0.7. Units = list items, else sentences; 25–400 chars (split on `;`); at most 40 per JD (highest weight first).
- **Classes** (excluded from the coverage denominator, never scored as gaps; the rules already handle them): `level` (a years-of-experience line with no other skill content), `logistics` (location, travel, schedule, clearance, pay, work authorization), `domain` (an industry-tenure line matching `domain_tenure_rule`, shown as a note). Everything else is `work`.

### 15.3 Coverage (`backend/finder/coverage.py`, `backend/finder/embed.py`)

- **Encoder** (`embed.py`, the §9 loader): `EMBED_MODEL = "BAAI/bge-small-en-v1.5"`, 384-d, normalized; sentence-transformers, else fastembed, else a clear install hint; `JOBSEARCH_EMBED_BACKEND` overrides. bge query instruction on the requirement side only.
- **Match**: each `work` requirement → cosine to every evidence unit (in memory: numpy matrix of evidence vectors; evidence is small); best score times the unit weight → `strong` (≥ `COVER_STRONG`), `partial` (≥ `COVER_PARTIAL`), else `gap`. A requirement naming a `not_in_record` term is a `gap` regardless. Keep the best evidence `ref` per requirement.
- **Score**: `coverage = 100 · Σ w·credit / Σ w` over `work` units (strong 1.0, partial 0.5, gap 0). Fewer than 3 `work` units → coverage NULL (not enough to judge).
- **Calibration** (`finder.py coverage --calibrate`): score the labeled positives (vw_label_set, career-site text where matched) and 1,500 pseudo-negatives; pick `COVER_STRONG` / `COVER_PARTIAL` to maximize the positives-vs-pseudo AUC of `coverage` on a grid (defaults 0.72 / 0.62 until calibrated); then `COVERAGE_REJECT` = positives' 5th percentile, `COVERAGE_REVIEW` = 15th. Stored as a `models` row of kind `coverage` (notes JSON holds the thresholds); `pipeline.combine` reads the newest.
- **Table**: `coverage(posting_id, description_hash, evidence_version, model, coverage DOUBLE, n_work, n_strong, n_partial, gaps JSON, matches JSON, notes JSON, scored_at, PRIMARY KEY (posting_id, description_hash, evidence_version, model))`. `gaps` = the three highest-weight uncovered requirement texts; `matches` = up to five `[requirement, evidence ref, score]`; `notes` = domain lines. Screens pick up coverage by join; a posting is re-covered only when its JD hash or the evidence version changes.
- **Which rows**: only rows that survive every hard rule and the fit gate (hundreds to low thousands). `finder.py coverage [--all] [--limit N]`; the daily sweep covers new or changed survivors after `screen`, then re-screens those rows so the final score uses coverage.

### 15.4 Scoring (updates §14)

- Content = `CONTENT_BLEND` (`{"coverage": 0.75, "fit": 0.25}`) over the signals present; coverage NULL → fit alone (the §14 behaviour). The §9 centroid similarity is dropped.
- Content gate: coverage < `COVERAGE_REJECT` → reason `requirements not covered (N of M; gaps: …)`; < `COVERAGE_REVIEW` → flag `coverage borderline (N of M)`. The fit gate (§14) still runs first and decides which rows get coverage at all.
- Fit stanza: `**Fit: ~88%.** profile 95 · coverage 84 (12 of 14 strong, 1 partial) · fit 0.97 · gaps: <gap 1>; <gap 2> · matched: <requirement> ← <evidence ref> · flags: …`.

### 15.5 Phase 4 review ledger (extends §10)

- **Table** `llm_reviews(posting_id, description_hash, rubric_version, scorer, score INTEGER, lane, level, blockers JSON, notes, reviewed_at, PRIMARY KEY (posting_id, description_hash, rubric_version, scorer))`. `rubric_version = sha1(RUBRIC_PUBLIC + RUBRIC_PERSONAL + not_in_record)[:12]`. **A posting whose (description_hash, rubric_version) already has a review from any scorer is never sent again** unless `--force`.
- **Daily (free Gemma)**: `finder.py llm --top N` reviews the top N by final score among new or changed survivors with no review. Throttle from `.env`: `GEMINI_RPM`, `GEMINI_TPM` (tokens estimated as chars / 4, prompt + expected output); sleep to stay under both; 429 / 5xx back off (2 s, 8 s) then fall to the next model in `GEMINI_API_MODEL`. The JD is trimmed to its requirement and responsibility units (§15.2) to cut tokens.
- **Backlog (Claude Code on the user's subscription, human-run)**: `finder.py llm-batch export --n 200 --out <dir>` writes numbered batch files (rubric, the claim guards, and per posting: id, title, employer, requirement units, the coverage gaps) plus the exact JSON schema to return; the user runs a Claude Code session over the batch; `finder.py llm-batch import <dir>` validates each JSON result and writes `llm_reviews` with `scorer = 'claude-code'`. No API calls are made for this path.
- `combine` blends `llm_score` last (§6 `LLM_BLEND`) using the newest review for the current rubric version.

### 15.6 Public users: context is configuration

- **Four personal files, each with a committed example**: `.env` (paths, keys, rate limits), `backend/profile_local.py` (pay, places, limits), `backend/finder/rubric_local.py` (candidate profile prose for the LLM), `evidence.local.toml` (what the user has done). Code never names a person, employer or path.
- **`docs/SETUP_CONTEXT.md`**: what to gather (resume achievements as CSV or markdown, a bio, a LinkedIn PDF export, articles or portfolio pages), how to write evidence that matches well (one accomplishment per unit, the context included, the verb and the object explicit), a `not_in_record` list for tools and certifications the user does not hold, and a minimum setup (a single resume PDF works; coverage improves with achievement bullets).
- **Privacy statement in the guide and the README**: the DuckDB file, embeddings, models and snapshots stay local and gitignored; the fit model and coverage run offline; the LLM tier sends the JD requirement units, the rubric and the claim guards, and never evidence text.
- **`finder.py setup-check`**: `.env` keys present, `profile_local.py` imports, the evidence manifest parses and every path exists, optional dependencies (scikit-learn, sentence-transformers or fastembed, pdftotext or pypdf) with the install line for each missing one, DB schema version. Exit 1 on anything blocking.
- **Contributors**: the `.personal_patterns` pre-commit scan stays documented in CLAUDE.md; tests use only `tests/fixtures/evidence/` and the example profile.

### 15.7 Phase 3 acceptance (replaces the §11 Phase 3 line)

- [ ] Tests (fake encoder, no model download): manifest parsing and every source type, unit splitting bounds, duplicate collapse, `FLOAT[384]` insert and `array_cosine_similarity`; `split_requirements` sections, weights, the three excluded classes; coverage credit math, `not_in_record` gap, NULL under 3 work units; combine with coverage + fit, coverage NULL fallback, the coverage gate; ledger never re-sends a reviewed (hash, rubric) pair; batch export / import round trip with a malformed result rejected.
- [ ] `finder.py setup-check` and `evidence --check` pass on the builder's machine (every source found, unit counts printed) and fail clearly on a broken example.
- [ ] `coverage --calibrate` prints thresholds and the positives-vs-pseudo AUC for coverage, fit and the blend; the paired vault-copy vs career-site-copy coverage gap is below 5 points (coverage should not care where a JD was copied from).
- [ ] Eyeball (record in STATUS): the Henry Schein R134977 bullseye covers ≥ 80 with its gaps listed; CVS "Vice President & Chief Operating Officer, Medical Affairs", Novartis "Director, AI Foundations Engineer" and Centene "Senior Director, Medical Economics" each cover at least 20 points below it, with gaps that name the missing work; a JD in an unfamiliar industry asking for familiar work covers well (find one and record it); the top 30 of vw_shortlist before and after is listed, with the count of rows whose title has no function term.
- [ ] Coverage of all survivors finishes in < 15 min on CPU; the sweep still runs with no embedding library installed (coverage skipped, logged).

### 15.8 Phase 4 acceptance (replaces the §11 Phase 4 line)

- [ ] Tests (no network): prompt assembly from `rubric.RUBRIC_PUBLIC` + `rubric_local.RUBRIC_PERSONAL` + claim guards + requirement units; JSON result validation (score 0–100, lane, level, blockers list) with a malformed reply rejected and logged; the throttle never exceeds `GEMINI_RPM` or `GEMINI_TPM` over a simulated minute; 429 → back off → next model in `GEMINI_API_MODEL` (mocked); a (posting, description_hash, rubric_version) already in `llm_reviews` from any scorer is never selected again, and a changed JD hash or rubric version makes it eligible; `llm-batch export` → hand-written result files → `import` round trip writes `scorer = 'claude-code'` rows and refuses ids that were not exported.
- [ ] `finder.py llm --top 5 --dry-run` prints the five prompts and their estimated tokens and makes no call; a real run with the free key stores five `llm_reviews` rows and re-running it selects five different postings.
- [ ] `finder.py llm-batch export --n 20` writes files a Claude Code session can work through without other context (rubric, guards, schema, per-posting units and coverage gaps); importing the results updates final scores through the §6 `LLM_BLEND`, visible in `vw_shortlist`.
- [ ] Privacy: a test asserts no evidence-unit text appears in any Gemma prompt or batch file (only requirement units, the rubric and the guards).
- [ ] Eyeball (record in STATUS): for the top 20 after coverage, the LLM `lane` agrees with the user's read on the bullseye (Henry Schein R134977 → primary) and on the three context misfires named in §15.7; disagreements listed.

## 16. Amendment — Phase 3/4 audit findings (2026-09-16, Fable audit of §15; binding over §15 where they differ)

**Verdict.** §15's direction stands: sentence-level requirement coverage against the user's evidence is the right lever for the near-miss problem, the review ledger is sound, and context-as-configuration is sound. Seven changes below make it measurable and keep it from reproducing the vocabulary flaw one level down.

### 16.1 Calibrate against near misses, not random postings (replaces the §15.3 calibration paragraph)

- **Hard-negative set** (`vw_hard_negatives`, new view): (a) postings the user marked `pass --reason function` (new `--reason` code `function`; nuance / logistics / comp passes stay context only, honouring §13); (b) an `audit_negatives` list in `profile_local.py` of posting ids the user has read and called wrong-function (seed: CVS VP & COO Medical Affairs, Novartis Dir AI Foundations Engineer, Centene Sr Dir Medical Economics, Humana Creative Operations Director, Centene VP Medicare Care Management, GE Vernova Plant Leader); (c) the 200 highest-fit unlabeled postings under the current fit model that carry no function term in the title (the model's own confident mistakes; refreshed each calibration, listed in the report so the user can promote any real fit to a label).
- **Calibration target**: choose `COVER_STRONG` / `COVER_PARTIAL` on the grid to maximize AUC of **positives vs hard negatives**. Report positives-vs-pseudo AUC as a sanity figure only. `COVERAGE_REJECT` / `COVERAGE_REVIEW` = positives' 5th / 15th percentile as before, but the report must also print the hard-negative median so the user sees the separation, not just the thresholds.
- **Precondition**: `finder.py mark` gains `--reason function|nuance|logistics|comp|other`; existing pass rows default to `other`.

### 16.2 Kind weight scales credit, not similarity (replaces "best score times the unit weight" in §15.3)

- Compare the **raw cosine** to `COVER_STRONG` / `COVER_PARTIAL`. Credit = band credit × evidence kind weight (strong 1.0 · partial 0.5, × achievement 1.0 / duty 0.9 / narrative 0.8 / method 0.7). Rationale: bge-small cosines for related sentences sit in a narrow band (~0.75–0.90); multiplying by 0.7 pushed every article and bio unit below the strong threshold by construction, which made seven of the nine evidence sources decorative.
- `matches` JSON records the evidence **source and kind** per requirement; the §15.7 eyeball adds a source histogram (which sources drive coverage on the top 30) so the user can see whether bullets or narrative carry the score.

### 16.3 Specific requirements count more than generic ones (adds to §15.2 and §15.3)

- **Specificity weight.** After requirement units are embedded for the survivor set, each unit's weight is multiplied by `spec = clamp(log(N / df) / log(N), 0.2, 1.0)` where `df` = the number of distinct postings holding a near-duplicate unit (cosine ≥ `REQ_DUP` = 0.92 to any of that posting's units; computed with a single matrix product over the survivor units, cached in `requirement_units(posting_id, description_hash, unit_hash, text, section, weight, klass, vector)`). "Lead cross-functional teams" appears in thousands of JDs and drops to ~0.2; "map value streams for a shared-services function" stays ~1.0. Stored per unit so the report can show it.
- **Two figures, one gate.** `coverage_required` (Required + Preferred sections) and `coverage_role` (Responsibility sections; every-sentence JDs count as role) are both stored. **The gate and the blend use `coverage_required`**; `coverage_role` is reported in the fit stanza and used only when `coverage_required` is NULL (< 3 work units in Required). Rationale: responsibilities describe the seat and match every senior generalist; requirements describe the person.
- Cap stays 40 units per JD, chosen by section weight × specificity.

### 16.4 Years lines keep their skill content (replaces the `level` class in §15.2)

- A years-of-experience line is **not dropped**. `required_years` (rules) still reads the number for level. For coverage, strip the years phrase (`\b\d+\+?\s*(or more\s*)?years?\b[^,;:]*?(of|in)?\s*(experience|exp)\b(\s*(in|with|of))?`, the phrasings §14 parses) and keep the remainder as a `work` unit at the section weight when ≥ 25 chars remain ("10+ years in process improvement leading cross-functional teams" → "process improvement leading cross-functional teams"). Only a line with nothing left ("10+ years of experience.") is class `level`.

### 16.5 Three tiers of record, not two (adds to §15.1 and §15.3)

- `evidence.local.toml` gains `light_in_record = ["AWS", "Azure", "GCP", …]`: a requirement naming a light term can score at most **partial**, whatever the cosine (the user has touched the tool, task-driven, and is not a solutions engineer for it). `not_in_record` stays a forced gap. Both lists are term-matched on the requirement text with word bounds, case-insensitive; a requirement naming both a light and a not-in-record term is a gap. The report names the tier that capped the requirement.

### 16.6 Coverage is proven before it is weighted (replaces the §15.4 blend rollout)

- Phase 3 ships in two steps. **3a**: coverage computed and stored, shown in the fit stanza and `vw_shortlist` as columns, **weight 0** in content; the §15.7 eyeball runs on 3a. **3b** (only after the eyeball passes: Henry Schein R134977 ≥ 80 and each named misfire ≥ 20 points below it on `coverage_required`): set `CONTENT_BLEND` from the calibration report (the blend weight that maximizes positives-vs-hard-negative AUC on the grid {0.5, 0.6, 0.75, 0.9}, default 0.75) and turn on the coverage gate. Phase 4 does not start until 3b is on.

### 16.7 Ledger: a rubric edit must not strand the backlog (replaces the last line of §15.5)

- `combine` blends the **newest review for the posting's current description_hash from any rubric version**; a review whose rubric_version is not current is flagged `llm review stale (rubric)` and is not re-sent unless `--force` or `finder.py llm --refresh-stale --top N` (which re-reviews only the top N stale rows under the daily throttle). Rationale: with "never re-review" as the default, a wording change to the rubric would otherwise zero the LLM signal for every reviewed posting with no path back short of `--force` over the whole backlog.
- `rubric_version` also hashes `RESPONSIBILITY_HEADINGS` and the unit-splitter version, so a splitter change is visible as staleness rather than silently reviewing different prompts under one version.

### 16.8 Privacy: what leaves the machine, by path (replaces the privacy statement in §15.6)

- **Google AI Studio, unpaid tier:** Google's Gemini API terms allow prompts and responses to be used to improve Google products and to be read by human reviewers. Treat anything sent on a free key as retained. **Paid tier** (billing enabled on the Cloud project): inputs are not used for training and are held only for abuse monitoring.
- Therefore two prompt profiles, chosen in `.env` by `LLM_PROMPT_PROFILE`:
  - `public` (default; safe on the free tier): sends the JD's requirement units, `rubric.RUBRIC_PUBLIC`, and a **neutral role description** (`RUBRIC_LANE`: the target function, level and lane in generic terms, committed in `rubric.py`). Never sends `RUBRIC_PERSONAL`, the claim guards, `not_in_record` / `light_in_record`, or evidence text.
  - `personal` (paid tier, or a local model): adds `RUBRIC_PERSONAL` and the claim guards. `finder.py llm` refuses `personal` unless `GEMINI_PAID_TIER=1` is set, and says why.
  - The **Claude Code batch path** (§15.5) always uses `personal`; the files stay on disk, gitignored under `db/batches/`.
- **Owner's setting (2026-09-16):** the key in use has billing enabled, so `personal` is allowed; data terms follow the project's billing status, not the volume. Cost stays zero because Gemma models are priced at $0 on the Gemini API and `GEMINI_RPM` / `GEMINI_TPM` / `GEMINI_DAILY_CAP` (new, requests per day) hold usage inside the free quota. `finder.py llm` prints requests used today against the cap. The builder verifies the $0 Gemma price on the pricing page at build time; a non-zero price makes `--top` default to 0 until the user sets it.
- The README privacy paragraph states the three paths in one line each. `git grep` of `.personal_patterns` stays the pre-commit guard; `db/batches/`, `evidence.local.toml`, `rubric_local.py`, `profile_local.py` are gitignored and `setup-check` confirms it.

### 16.9 Acceptance additions

- §15.7 adds: hard-negative view populated (count printed); calibration report prints positives-vs-hard-negative AUC for `coverage_required`, `coverage_role`, fit, and each blend weight; specificity weights spot-checked (the five most generic and five most specific units on the survivor set listed); a years line with content yields a work unit (test); a `light_in_record` term caps at partial (test); step 3a runs with weight 0 and the stanza shows both coverage figures.
- §15.8 adds: the `public` profile prompt contains no string from `RUBRIC_PERSONAL`, the guards file, or the evidence store (test); `personal` refuses without `GEMINI_PAID_TIER=1` (test); a stale-rubric review still blends and is flagged (test).
- §12 (Phase 5) adds: Batch Mode skill documents the new fit-stanza fields (coverage required / role, gaps, matched, review staleness).

## 17. Amendment — the labeling run: buy the labels the ML layer never had (2026-09-15, user decision; binding, and it reorders §16.6)

**Why this exists.** Phase 3a shipped, was measured, and its §15.7 eyeball failed: the bullseye covered 53.8 where the bar was 80, and the audited misfires landed within 4 points of it. Option A (contrast scoring) was prototyped on the stored vectors and rejected. The diagnosis in `docs/STATUS.md` is that bge-small cosines sit near 0.65 whether or not a requirement genuinely matches, so 93% of requirements fall in one band and every posting scores ~50. Underneath that sits a harder constraint: the finder has **362 positives and 7 audited near misses**. No scoring scheme — embeddings, cross-encoders or an LLM — can be tuned or honestly measured against 7 examples. The labeling run buys that missing evidence. A strong model grades the corpus; the cheap models learn from the grades. That is distillation, and the postings that clear Level 1 are exactly the hard examples where labels carry the most information.

### 17.1 What is graded, and what is deliberately not

One question only: **is this the same kind of work the candidate has done?** Pay, location, level and travel already have a rule engine; letting the judge weigh them corrupts the label. Industry is never a gap by itself — entering an unfamiliar domain is the candidate's pattern, not a risk.

Grades: `bullseye` · `adjacent` · `stretch` · `wrong`, plus `lane`, `confidence`, one `blocker` and a one-sentence `rationale` naming the actual work. Graded, not binary: binary labels filter, graded labels rank.

### 17.2 The queue (`backend/finder/judge.py`)

| Pool | Live count | Why it is in the set |
|---|---|---|
| `high` — very_strong + strong | 622 | the confusable band, where ordering fails today |
| `low` — partial + weak | 1,362 | without it the model only ever sees rows that already scored well |
| `reject` — content-gate rejects (100), logistics rejects with fit ≥ 0.5 (100), random rejects (50) | 250 | the content-gate slice measures Level 1's **false-negative rate**, which has never been measured |

`interleave()` mixes them 17 / 5 / 3 per 25 exported, so **stopping at any point leaves a label set that spans the range** — the token budget can run out without biasing the data.

**Dedupe: identical JD text only.** Cosine 0.97 collapsed Zillow's "Principal Analytics Engineer" with its "Principal Business Intelligence Manager"; 0.995 plus a title check still collapsed AHEAD's "Senior Manager, Enterprise Transformation" with its "Senior Software Engineer". An employer's boilerplate makes unrelated roles look alike, so text similarity alone is never evidence of a repost. On the live corpus this leaves 34 duplicates out of 2,234 — they copy their representative's grade on import.

### 17.3 The batch protocol

`judge export` writes `db/batches/batch_NNN.md` (gitignored): the public rubric, the neutral lane description, `RUBRIC_PERSONAL` from the gitignored `rubric_local.py`, and up to 25 postings trimmed to their 18 highest-weight requirement units at 220 characters each. **Evidence text is never included** — the judge grades the JD against a description of the record, not against the record itself. A Claude Code subagent per batch writes `batch_NNN.result.json`; `judge import` validates every row against the manifest (unknown posting id or invalid grade is refused and logged) and writes `llm_labels` (schema v5).

**Resume is the design, not a feature bolted on.** Each batch result is written the moment that batch finishes, so a crash, a token limit or a change of plan costs at most one batch. `judge status` prints exactly what is left.

```bash
# Where did we stop?
.venv/bin/python finder.py judge status --dir db/batches

# Re-export (only if the rubric, the splitter or the queue design changed — it renumbers the batches):
.venv/bin/python finder.py judge export --dir db/batches --batch-size 25

# Grade the pending batches: one Claude Code subagent per batch, ~6-8 in parallel, Sonnet.
#   Prompt: read db/batches/batch_NNN.md, grade every posting, write batch_NNN.result.json as a JSON array of
#   {posting_id, grade, lane, confidence, blocker, rationale}; reply with only the count and the grade tally.

# Load whatever has come back (safe to run repeatedly, mid-run included):
.venv/bin/python finder.py judge import --dir db/batches --scorer claude-sonnet-batch

# Check the judge against the user's own behaviour, and write a CSV to eyeball:
.venv/bin/python finder.py judge report --csv db/snapshots/llm_labels.csv
```

Cost observed on the pilot: ~70k subagent tokens and ~100 s per 25-posting batch, so the full 78 batches are roughly 5M tokens. That is why the queue is interleaved and the run is resumable across sessions and plans.

### 17.4 Validation before the labels are trusted

1. **Against the user's own behaviour** (`judge agreement`, automatic): postings in the tracker, or marked `build`, should not be graded `wrong`; postings passed with reason code `function` should not be graded `bullseye`. The overlap on the live corpus is thin (28 tracker-matched, 22 build decisions), so it is a smoke test, not proof.
2. **By hand**: the user eyeballs ~100 rows from the CSV, which carries the grade, the blocker, the rationale, the score it already had and the URL.
3. **The user's decisions always win.** LLM labels apply to unlabelled postings; where the judge contradicts something the user actually pursued, the conflict is surfaced, never silently written over the user's call.

Pilot evidence (batches 001-002, 50 postings): Henry Schein R134977 → `bullseye`; CVS "VP & COO, Medical Affairs", Centene "Senior Director, Medical Economics" and Novartis "Director, AI Foundations Engineer" → `stretch`; Dandy's NetSuite/order-to-cash role and CVS "Medicaid Risk Adjustment Analytics" → `wrong`. That is the separation Phase 3a could not produce, from the same JD text.

### 17.5 What the labels are for (in priority order)

1. **Retrain the fit model with real negatives.** Today its negatives are 1,500 random postings, which is why it cannot tell a bullseye from a senior generalist that shares its vocabulary. `wrong` and `stretch` rows from the confusable band are the hard negatives it has never had. Highest value, smallest change.
2. **Re-calibrate coverage honestly** (§16.1): `vw_hard_negatives` stops depending on 7 audited rows.
3. **Settle the Level 2 question.** The supervised-embedding test ran on 28 rows and proved nothing; with hundreds it either beats TF-IDF or it does not. If it does not, Level 2 becomes a cross-encoder re-ranker over the top few hundred, or nothing.
4. **Measure Level 1's miss rate** from the graded reject slice, which decides whether the screen's gates are too tight.

### 17.6 Ordering (replaces §16.6's "Phase 4 does not start until 3b is on")

3b stays off: coverage keeps weight 0 until something makes it rank. The labeling run and the retraining it feeds come first; Phase 4's daily Gemma path is unblocked to start after that, since the batch machinery, the rubric split and the `llm_labels` ledger built here are its foundation. Nothing about the §16.8 privacy rules changes: the batch path is `personal` and stays on disk, and evidence text is sent nowhere.

### 17.7 Acceptance

- [ ] ≥ 600 postings graded with the grade distribution reported (the distribution itself is a finding: it says what fraction of Level 1 survivors are genuinely relevant).
- [ ] `judge agreement` printed; every disagreement with a pursued posting listed for the user.
- [ ] ~100 rows eyeballed by the user against the CSV.
- [ ] Fit model retrained with the graded negatives; report AUC and, specifically, whether the named misfires drop below the bullseye.
- [ ] Coverage re-calibrated against the real hard-negative set; the §15.7 eyeball re-run and its verdict recorded.
- [ ] Level 1 miss rate reported from the graded rejects.

## 18. Amendment — two lenses, and how the single-lens rubric failed (2026-09-16, user decision; binding, supersedes §17.1's "one question only")

**Why this exists.** §17 bought 3,009 graded labels and they worked: retraining the fit model on them moved AUC-vs-`wrong` to 0.946 and recovered the content-gate's false negatives (Amentum "Business Process Specialist", a graded bullseye, went from fit 0.12 to 0.744). But reading those labels exposed a defect in the rubric itself, and fixing it exposed a design limit that no amount of rubric prose can fix.

### 18.1 The defect: one axis could not carry two capabilities

`RUBRIC_PERSONAL` said *"SECONDARY lane (cap at `adjacent`): senior, strategic, leadership-facing data / analytics / BI work"* and described the analytics record in a single clause. Measured consequence: **bullseye by lane was primary 208, secondary 0** — not one analytics posting in 3,009 could ever be a bullseye, by construction. The cap was binding on 247 secondary-lane rows and pushed others into `wrong`.

It also lost specific, checkable grades. Capital One "Principal Associate, Financial Planning & Analysis" was graded `wrong` with the rationale *"the core work is corporate FP&A, a distinct finance discipline"* — the judge called FP&A unfamiliar work because the record it was shown did not contain FP&A, though the candidate reengineered a $3B/$2.2B FP&A forecasting and budget cycle with DMAIC.

**Root cause, and it generalizes:** the cap was never detecting what it claimed to. It encoded a *positioning* decision (data/AI is the surprise value-add, not the headline) and a *gate risk* (timed coding assessments) inside a *capability* label. Three different questions compressed into one grade. Every fix attempted was another prose arbitration rule — "judge the object of the analysis", "the stack decides" — and each one traded one misgrading for another.

### 18.2 Corrections made to the single-lens rubric first (all measured)

Rewritten from the §5 evidence sources (`Resume_Bullets.csv`, `Resume_Blocks.csv`, `Bio_Jeff_Professional.md`) rather than paraphrase: the Force-to-Load capacity model, the FWA five-year truck-roll forecast (**FWA = Fixed Wireless Access, not fraud/waste/abuse** — verified before writing), FP&A forecasting and budget management, the production data pipelines, the platform list from the TorchStone resume, and the negative platform evidence (no Appian/Pega/Nintex, no Power Automate, light ServiceNow). Engineering titles became stack-conditional rather than a blanket `wrong`. `RUBRIC_LANE`, the committed half, carried the identical cap and was fixed too.

**Validation, clean proportional sample n=575:** 79% of grades unchanged · good-fit 9.4% → 19.3% · `wrong` 79.0% → 70.6% · only 3 of 45 bullseyes demoted (one correctly: Voya "AI Security Architect" → `wrong`) · 31 rows recovered from `wrong`, concentrated in FP&A and hybrid process/BI roles. Every recovered row's rationale named actual duties rather than citing the rubric — the test for whether the judge had begun pattern-matching the instructions.

### 18.3 The two-lens design (supersedes the single grade)

Each posting is graded **twice in one judging pass**: `grade_process` (process excellence, operating model, **change management** in the adoption-and-ownership sense) and `grade_technical` (quantitative modelling, forecasting, data engineering, BI delivery). `grade` remains the overall label and is **the better of the two lenses** — a role reachable through either capability is work the candidate has done — so `vw_label_set`, the fit model and the reports keep working unchanged.

**One pass, two grades, not two passes.** The cost is reading the JD; a second grade off the same read measured 78KB per batch against 74KB single-lens.

Allocation rules that are user decisions, recorded so they are not relitigated:
- **Change management sits on the process lens only.** ITIL/ITSM change control is not change management and stays `wrong`.
- **Agentic AI tools count on BOTH lenses**, because the use is broad and not just coding: reasoning partner for process analysis and decomposition, strategy development, and human-in-the-loop workflows over curated databases — the "operator who codes with agentic AI assistance" pattern. Process lens = designing the HITL workflow, governance and organizational adoption; technical lens = directing the tools against written specifications to ship software, pipelines and analysis. Still distinct from *building* ML/NLP/RAG models, which stays `wrong` on the technical lens.
- **Technical platforms weigh on the technical lens.**
- **The three flagship projects (GNO Force-to-Load, FWA forecast, FP&A automation) appear on BOTH lenses with different emphasis** — process for the decomposition, model design and operating-routine redesign; technical for building the pipelines and automated analytics. They are written up as evidence that the capability is **domain-portable across field operations, fixed wireless and finance**, deliberately so that the rubric does not read as a workforce-analytics specialism.
- **Forester is on both**: Lean Six Sigma methodology translated into software requirements (process), and 18+ sprints of multi-tenant SaaS with architecture contribution and agentic AI direction (technical).
- **Domain-agnostic on both lenses.** The process capability applies to anyone who has a process; the technical capability travels the same way. Unfamiliar industry never lowers a grade. Where a JD makes deep industry tenure a hard requirement, that is named as the **blocker** — their expectation is the obstacle, not the capability.

### 18.4 What is deliberately NOT in the rubric

**The coding-assessment constraint.** Level 1 catches only what a JD discloses (`ASSESSMENT_GATE_TERMS`, `CODING_TEST_TERMS`, and the `BLOCKED_POSTERS` employer list) — measured: **242 of 80,272 active postings, 0.3%**, and widening the coding rule beyond tier 3 would newly reject exactly one posting, a false positive. But the judge reading a JD has precisely the same information as the regex and cannot predict an undisclosed assessment either. The old cap was using "is this an analytics role" as a **proxy** for assessment risk, and that proxy is what killed the workforce-analytics roles the user actively wants. Assessment risk is discovered, not predicted: it belongs in `BLOCKED_POSTERS` as it is learned, and in the user's judgement at application time.

### 18.5 Schema and tooling

Schema **v7**, additive: `llm_labels.grade_process` and `.grade_technical` (NULL on pre-split rows, which reads correctly as "graded before the lenses existed"); `judge.overall()`; `_validate` still accepts pre-split result files so the 890 existing labels stay valid; `vw_lens_grades` exposes both grades plus a `lens_bucket` of `process` / `technical` / `both` / `neither`; `judge report --csv` gains `grade_process`, `grade_technical` and `favoured_lens`.

**Report plan (user request, not yet built): three lists — strong on process, strong on technical, and strong on BOTH.** The both-lenses list is the point: it is the candidate's least substitutable shape and was invisible under one axis. GM "Senior Process Transformation Business Intelligence" (*"builds Python/SQL data pipelines and LLM-powered business tools under a Process Transformation mandate"*) is the worked example.

### 18.6 Queue-ordering bug, recorded because it nearly caused a wrong decision

The first `--relabel` queue concatenated grades alphabetically, so it was sorted by old grade. Batches 1–12 were 100% old-`adjacent` rows and the running tally showed a 55% good-fit rate against an 18.5% corpus baseline — which reads exactly like a rubric gone permissive, and nearly triggered a rewrite of a rubric that was in fact correct. Caught by the migration matrix returning 208 old-`adjacent` and nothing else. `_spread()` now sorts on each posting's fractional position within its own grade, so every slice carries the corpus mix (§17.2), and `--relabel` is idempotent: it queues only postings whose newest label predates the current `rubric_version`. **Lesson for any future run: never read a mid-run tally as a corpus estimate without checking how the queue was ordered.**

### 18.7 Pilot results and the two decisions they forced (2026-09-16, n=99, balanced 25 per old grade)

**Model choice, measured not assumed.** The same 25 postings were graded by Sonnet and by Opus. Process lens: 80% exact, 96% within one grade. Technical lens: 64% exact, **100% within one grade**, with Opus systematically more generous (mean shift −0.28). Opus hedges toward `stretch`/`adjacent` where Sonnet says `wrong` — on a plant-floor Lean role, on a posting whose text is too thin to grade, and on an HR business-partner role with no analysis in it. Opus also produced 8 "both-lens" roles against Sonnet's 5, which inflates precisely the signal that matters most. **Sonnet is the right grader here and Opus is not worth the money.** (There is no reasoning-effort knob on subagents; the only lever is the model.)

**Lens independence confirmed.** On the 99-posting pilot the two lenses gave **different grades on 67 of 99 postings (67%)**. Anchoring — grading once and sliding the second grade to match — would have shown up as near-total agreement. It did not. Worked examples: Novartis "Executive Director, Strategic Projects Lead" `bullseye`/`wrong`; Salesforce "Lead, Workforce Intelligence" `wrong`/`bullseye`; GM "Senior Process Transformation BI Data Analyst" `wrong`/`bullseye` (correctly reading past the title to a build role).

**A decisiveness rule was added to `RUBRIC_PUBLIC` between the two pilots:** a lens with no relevant content is `wrong` on that lens, not `stretch`; `stretch` requires overlap the judge can name. Technical-lens `stretch` fell from 20% to 12%.

**Decision 1 — `both` requires at least one `bullseye`.** Counting bullseye+adjacent as "strong" put `adjacent`/`adjacent` in the both bucket: 9 of 25 were lukewarm-on-both rather than the rare role that genuinely demands both. Tightened in `vw_lens_grades`; the pilot's both count went 25 → **16**.

**Decision 2 — `grade` is the AVERAGE of the two lenses, not the better of them.** Taking the max inflated the corpus by construction (18 of 25 old `stretch` rows became good simply because two judgments replaced one). On the same 99 postings, max gave bullseye 36 / stretch 9; average gives bullseye 16 / stretch 28. Ties round toward the better grade so a role excellent on one lens and irrelevant on the other still surfaces.

**`grade` is now explicitly a compatibility column.** The user's actual decision — apply, and position the package as process, technical or both — is made from `grade_process` and `grade_technical` **individually**, because a role strong on one lens is worth pursuing on that lens's positioning. Any single number destroys that: `bullseye`/`wrong` and `adjacent`/`adjacent` both average to `adjacent` and mean completely different things.

**Known limitation, stated plainly: run-to-run variance on individual postings is real.** Oracle "Sr HR Business Partner" was graded `adjacent`/`wrong`, then `stretch`/`stretch`, then `bullseye`/`stretch` across three runs, and the last is probably a misgrade. Batch-level `stretch` usage ranged 1–5 across four batches of 25. Aggregates at n≈100 are stable; **single rows are not, and no report should be read as if they were.**

### 18.8 Next phase — a fit model per lens

The single TF-IDF fit model is the last place the one-axis design survives. With two label columns the right architecture is two models producing `fit_process` and `fit_technical`, which is also what the three report lists need in order to rank within each list. Until that exists, the single model trains on the averaged `grade` and is a compromise. Blend weights will need revisiting: content is at 0.90 after the 2026-09-15 sweep, but a two-lens content score may want its own.

**Built 2026-09-16.** Both models trained (`c80d6fb39bc7` process, `3fee819b2a64` technical); schema v8 adds `screens.fit_process` / `fit_technical`, `pipeline.screen` fills them, and a full rescreen put a lens prediction on all 60,391 active rows that have JD text. The scores are stored and reported ONLY -- `combine` does not read them, so `final_score` and `model_version` are unchanged and the blend question below is still open.

### 18.9 Acceptance

- [ ] The corpus is re-graded under two lenses (currently MIXED: 890 at a single-lens corrected rubric, 2,083 at the original — do not retrain or read a corpus-wide distribution until this is resolved).
- [ ] Grade distribution reported per lens, plus the `lens_bucket` crosstab; the `both` count is the headline.
- [ ] Fit model retrained on the new overall grades; report AUC, AUC-vs-`wrong`, and OOF fit by grade among SURVIVORS (the flat 0.77 / 0.64 / 0.65 / 0.63 is the number to beat).
- [x] The three report lists **built** 2026-09-16 (`finder.py lenses` -> `Lens_Lists_*.md`), awaiting user
      review: strong process, strong technical, and `both`. Ranked within each list, not merged. A `lens_source`
      column marks user / judge / model on every row. `both` still needs more than adjacent/adjacent, but an
      ungraded row earns it by clearing `lens_standout_p()` 0.80 rather than a predicted bullseye -- the models
      cannot reproduce that split (F1 0.64 / 0.61, precision ~0.5) and must not be asked to.
      Actionable at build time: both 200, process 540, technical 222.
- [ ] `pipeline.combine` weights revisited: content is at 0.90 after this session's sweep (AUC 0.537 → 0.589, P@50 0.84 → 0.90), but a two-lens content score may want its own blend.

## 19. Amendment — coverage recalibration and experiments (2026-09-16/17, Opus; RESULTS + OPEN DECISIONS, not yet a user decision)

This section records what was built and measured after §18, and the decisions it puts in front of the user. Nothing here changes a binding rule until the user confirms it; each proposal is marked. Full per-run record: **`docs/COVERAGE_EXPERIMENTS.md`**. Session state: `docs/STATUS.md` "NOW".

### 19.1 What changed in the codebase and environment

- **Lens models corrected** (`72167e5`): in-sample calibration numbers were replaced with out-of-fold ones; OOF mean probability falls monotonically bullseye → adjacent → stretch → wrong on both lenses (AUC 0.95 each).
- **Judged `wrong` labels feed the hard negatives** (`e8431f9`): `vw_hard_negatives` was empty; `refresh_hard_negatives` now draws from three sources (audit · judged_wrong top-300 by fit · fit_top 200).
- **The repo moved to the WSL ext4 disk** (`~/jobsearch`, `621fec4`). Heavy ML imports over the `/mnt/e` 9p mount failed under Windows memory pressure (bus error, ENOMEM on `open()`, one WSL crash). The E: copy is deleted. Venv, `db/` and gitignored files live natively. Caveat: the ext4 disk is a VHDX on a nearly full C:.
- **Evidence reads PostgreSQL** (`b1c9d3d`): new `postgres` source type (`psql --csv`, `path = "$RESUME_DB_URL"` from `.env`); bullets, duty statements and claim guards come from the `resume` database views instead of CSV exports. `evidence.ensure_current` re-embeds only changed units before cover/calibrate; a rebuild refuses to empty a listed source that is unreachable. The encoder now loads the manifest's `embed_model` (it silently used the default before).
- **Experiment switches** (`e5cbc39`, env, unset = production): `JOBSEARCH_REQ_CONTEXT=title`, `JOBSEARCH_RERANKER=<cross-encoder>`, `JOBSEARCH_RERANK_TOP=<n>`. `requirement_units` has no model column in its primary key, so a variant must run on its own DB copy.
- **Calibration reporting** (`e5cbc39`, `d8fe17a`, `1566e32`): AUC is now also reported vs the judge's `stretch` rows (806) and vs judge-confirmed `wrong` only (299); hard-negative fit uses out-of-fold scores for labeled rows. Tests: 131.

### 19.2 Results (coverage AUC; fit model shown for reference)

| Run | Change | vs hard neg (439) | vs judged wrong (299) | vs stretch (806) |
|---|---|---|---|---|
| baseline (R2/S2a/S2c) | bge-small, cosine bands | 0.559 | 0.582 | 0.575 |
| S1 | evidence = bullets + duties + SOAR only | 0.543 | — | — |
| S2 | requirement embedded as "<title>: <requirement>" | 0.607 | 0.637 | 0.610 |
| S3 | bge-base-en-v1.5 (768-d) | 0.566 | 0.621 | 0.628 |
| **S4a** | **MiniLM cross-encoder reranks top-5 evidence** | 0.604 | 0.677 | **0.669** |
| S4b | S4a + title context | 0.617 | 0.683 | 0.664 |
| fit model (held-out) | — | 0.706 | 0.995 | **0.955** |

### 19.3 Findings

1. **Coverage does not rank.** Best variant 0.669 vs the fit model's 0.955 on the confusable band; every blend lowers the fit model's stretch AUC. §16.6's 3b (turning on `CONTENT_BLEND` and the coverage gate) is not supported by the evidence.
2. **The reranker fixes coverage's failure mode.** Bi-encoders (small or base) match on topic, so data/AI *engineering* roles read as covered; the cross-encoder removes them (smoke test: "Staff AI Engineer: build ML pipelines" vs an article about AI assistants scores 0.000). Title context adds nothing on top of it and reintroduces title-word false positives. The evidence mix was not the cause (S1).
3. **The calibration target is biased.** `fit_top` negatives are selected for high fit, so they cap the fit model's AUC by construction, and after reranking several read as genuine fits. Judged `wrong` rows are mostly easy (fit median 0.23). `stretch` is the honest confusable set.
4. **Thresholds want continuous credit under the reranker** (COVER_PARTIAL pinned at the grid floor, 0.05, in both reranker runs).
5. **Cost** (4 CPU threads, i7-11370H): baseline embedding ~15 min for 2,735 postings; title context ~5×; bge-base ~2 h; reranking ~2 h 15 min (~40 pairs/s). The daily increment is a few hundred postings.

### 19.4 Proposals for the user (not binding until confirmed)

- **(a)** Retire 3b: coverage stays at weight 0 in `combine` permanently and ships as an **explainer** (gaps + matched evidence in the report) using bge-small + MiniLM reranker, no title context. Replaces §16.6's 3b gate and the §15.7 eyeball thresholds, which were written for coverage-as-ranker.
- **(b)** Recalibrate on graded rows only: replace `fit_top` with `stretch` + judged `wrong` (+ user labels) as the calibration target.
- **(c)** Next coverage variant if (a) is accepted: credit = reranker probability (no bands).
- **(d)** Parked user idea: per-requirement matches (with `bullet_id`) as **advisory, tie-breaking** metadata for the Keystone job application skill's bullet selection; the LLM still decides and story groups stay whole. Needs `bullet_id` in match refs, full per-requirement storage, and a `--jd-file` entry point. Vault note: `Professional/Areas/Job_Search/Tools/IDEA_Coverage_Bullet_Selection.md`.
- **(e)** A third grading lens for applied-AI work (enablement / adoption / governance / agentic delivery vs hands-on AI platform engineering), raised 2026-09-17; worked example Navy Federal *Principal AI Engineer (Agentic AI)*, judged wrong/wrong, user reads it as a partial fit. Needs a rubric lens block, `grade_ai` + view, a corpus regrade (~3,000 postings) and a third lens model.
- Still open from earlier: the level ceiling rule (STATUS "QUEUED"), `pipeline.combine` blend weights for the two lens scores (§18.9), storage (compact the VHDX; a second VHDX on E: for `db/` and Meridian's Postgres).

### 19.5 Acceptance record

- [x] `coverage --calibrate` completes on the native venv (R1 onward) and reports thresholds, AUCs, medians and the paired gap (1.7 points, under §15.7's 5-point bar).
- [x] Evidence reads the official PostgreSQL record; unit ids identical to the CSV path (2,918 units, 113 guards).
- [x] Every experiment recorded with calibration version, thresholds, AUCs on all negative sets, top false positives and a reading.
- [ ] §15.7 eyeball (Henry Schein R134977 ≥ 80 etc.) — **not run**; superseded if proposal (a) is accepted.
- [ ] User decisions (a)–(e).

## 20. Amendment — repairs, and a level rule (2026-09-17, Fable; §20.1 DONE, §20.2 PROPOSED, needs the user's confirmation)

### 20.1 Repairs (commit `3763d6e`, tests 131 → 157)
The audit (vault `Tools/Jobsearch_Audit_20260917.md`) found and this commit fixed: all three fit models unloadable after the E: move (absolute `models.path`); CV folds not grouped by JD text (identical reposts in train and test); the requirement splitter dropping short skill bullets; remote detection reading 600 characters and trusting a generic ATS flag; "team of N" hard-rejecting as N direct reports; travel regex missing 100%; Workday clamp unchecked once a scope was stored; dead detail fetches eating the budget; Eightfold count=0 close-passing; tracker fuzzy match unbounded in time; inline HTML tags splitting sentences. **Every AUC quoted before this commit was measured with the leak and the splitter defect; the numbers below the retrain supersede them.** Known limits that remain: an ATS `onsite` flag with a specific city and a JD that never says remote stays on-site (Microsoft stores its remote flag in metadata the adapter does not keep); "US Off-Site" is one employer's label and is not a remote term.

### 20.2 Level fit — a deterministic rule with its own column (PROPOSED)
**Why.** Reviewing the surfaced rows, the user's `wrong` calls were mostly about level and scope, not function; grading them `wrong` would teach the model that work he does well is work he cannot do. Level is written in the JD, so it gets a rule, not a model, and human `level_fit` labels in `report_feedback` are the rule's test set.

**Values, in priority order for the report:** `in_range` (senior IC, or manager of a small team, assume small team if team size is not specified) → `stretch_up` (manager of managers or Director-type promotion) → `out_of_reach` (Senior Director, VP, AVP, Head of, Chief; org-building scope; P&L ownership; team above `MAX_DIRECT_REPORTS`) → `too_low` (junior / early-career signals, required years under the floor in `profile_local`, or band top under the pay floor, however, if pay band is within range and required years is low, keep it in_range). `unknown` when nothing is stated.

**Signals (title and Required block only):** title tier; "N years managerial / people leadership"; org-building phrases ("build and lead a … organization", "global teams", "spans of control", "executive"); P&L; team size (from the fixed `_TEAM_SIZE` / `_REPORTS` split); the written minimums are scored separately from scope, so "Master's + 10 yrs" met with an org-building mandate is `out_of_reach` for scope, not for years. Two or more scope hits → `out_of_reach`; one → `stretch_up`; none with a senior title → `in_range`.

**Schema:** `screens.level_fit VARCHAR` (v10, additive, filled by the rule at screen time; `rescreen-all` after). `report_feedback` table per the vault spec `Report_Feedback_Table_Spec.md` plus `level_fit`, `grade_before_split`, `needs_confirm`, `split_reason`; loaded from the vault CSV with `assessor` and `confirmed_by_user` as given. A view `vw_level_agreement` = rule vs confirmed human `level_fit`.

**Report:** `Jobs_Found` and the lens lists sort `in_range` first, then `stretch_up`; `out_of_reach` and `too_low` are listed in a collapsed tail, never in the top blocks. Score is untouched until agreement is measured.

**Acceptance:** agreement with the confirmed human rows reported (target ≥ 80% exact on `in_range` / `out_of_reach`, disagreements listed); the 232 feedback rows re-exported with the rule's answer beside the human column so the user grades only disagreements.

**Acceptance checklist (2026-09-17):**
- [x] `level_fit_rule` built in `rules.py`, wired into `screen_row`; never touches `verdict` / `rule_score` (Agent A).
- [x] Schema v10 (`screens.level_fit`, `report_feedback`, `vw_level_agreement`) and the write path in `pipeline.screen` (Agent B).
- [x] `backend/finder/feedback.py` (`load_csv`, `rule_level_fit`, `agreement`, `export`) and `finder.py feedback` (Agent B).
- [x] `report.write_jobs_found` / `write_lens_lists` order `in_range` → `stretch_up` → `unknown`/NULL → score; `out_of_reach` / `too_low` routed to a collapsed tail, never a block or the top table; a `Level` column added (Agent D).
- [x] Agreement run on the live DB (2026-09-17): 20/22 exact, 12/13 on `in_range`/`out_of_reach` (92%, target ≥ 80%); the two disagreements are listed in `docs/STATUS.md`.
- [x] 232 feedback rows re-exported with the rule's answer, the judge's three grades and `needs_you` (73 flagged) — vault `Tools/Report_Feedback_20260917_rule.csv`.
- [x] Full suite green (197).
- [ ] `rescreen-all` fills `level_fit` on the whole corpus (per §20.4's ordering, after the fresh ingest).

### 20.3 Remote signals that are evidence only when present (user, 2026-09-17)
Boards tag remote roles inconsistently. Two more positive-only signals join `REMOTE_TERMS`: the location segment
`US Off-Site` (an employer label meaning remote) and LinkedIn breadcrumb tags `#LI-Remote` (also `#BI-REMOTE`). When
`#LI-Hybrid` and `#LI-Remote` both appear, the posting is **remote** (the user's ruling). Absence of a tag says nothing:
a posting is never made non-remote by a missing tag. Add the CACI / Blue Yonder / TrendAI / breadcrumb shapes as tests.

**Acceptance checklist (2026-09-17):**
- [x] `REMOTE_TERMS` gains `us off-site`, `#li-remote`, `#bi-remote`; `#li-hybrid` + `#li-remote` together reads remote; a lone `#li-hybrid` adds nothing (Agent A).
- [x] Tests: CACI, Blue Yonder, TrendAI, and a bare breadcrumb-tag shape, in `tests/test_level_fit.py` (Agent A; not re-verified by Agent D).
- [ ] Live-DB spot check that `US Off-Site` and `#LI-Remote` postings flip to `is_remote` after the next `rescreen-all` (§20.4 order — not yet run).

### 20.4 Order of operations before the next rescreen (user, 2026-09-17)
1. Build §20.2 (level rule + `report_feedback` load) and §21 (AI lens) — the new dimensions are retrofitted first.
2. **Fresh ingestion** (`sweep_ats.py`): the three partitioned boards' ~760 unreachable reqs, the Workday clamp fix and
   the `detail_attempts` budget all land here. Non-US rows are already hard-rejected by rules and the country /
   partition scopes keep them out of the pull; scored foreign rows already in the corpus stay for their labels.
3. `rescreen-all`, then the coverage re-baseline on the fixed splitter.
4. Only then Phase 4: Gemma sees a capped daily subset (in_range rows clearing a lens standout bar), and the Keystone
   job application skill sees the top rows by Gemma's read plus the user's hand picks. Both caps live in
   `profile_local` next to the pay floor, not in code. **Starting values (user, 2026-09-17): Gemma cap 100 per day,
   application-skill cap 10.** Verified 2026-09-17 on the Gemini API pricing page (updated 2026-09-16): Gemma 4 is
   "free of charge" for input and output and its paid tier is "not available", so a billing-enabled key cannot be
   charged for Gemma; the rate-limit page lists no Gemma row, so the build sets `GEMINI_RPM` / `GEMINI_TPM` /
   `GEMINI_DAILY_CAP` from a live probe, and a 429 is a refusal, never a charge. Estimated ~15–30 s per screening
   call, so 100 rows is a 30–50 minute nightly run.

### 20.5 The golden source (`Report_Feedback_20260916.xlsx`) — help wanted (user, 2026-09-17)
The user has confirmed 41 of 232 rows and cannot read 200 JDs by hand. After the level rule exists: re-export the sheet
with the rule's `level_fit`, the judge's two lens grades and the AI lens grade beside the human columns, and a
`needs_you` flag on rows where any two of {rule, Opus review, judge, user} disagree by more than one step or where
confidence is low. The user grades only the flagged rows. Rows the user never grades stay unconfirmed and never train.
**Timing (user):** the user works the golden source after today's applications; do not run a Sonnet JD-read pass on the
unconfirmed rows ahead of the level rule and the AI lens — one grading pass, not two.

## 21. Amendment — a third lens for applied AI (2026-09-17, Fable; PROPOSED, design with the user before building)
**What it grades.** `grade_ai`: applied-AI capability as the user actually has it — agentic workflow design and delivery, context engineering (skills, memory, deterministic tools around a model), evals and regression discipline for knowledge work, human-in-the-loop grounding, model routing and token-cost judgment, adoption and teaching. The lens source is the coaching brief's decomposition (vault `Professional/Resources/AI_Experience_Coaching_Brief_20260916.md` §5.1–5.8), rewritten into rubric prose from the evidence sources, not paraphrased.
**What it does not grade as a fit:** building models (ML/NLP/RAG engineering), AI platform / infrastructure engineering, distributed services in Python, AI security threat modelling as a primary duty. Those stay `wrong` on this lens; the coding-assessment constraint stays outside the rubric (§18.4).
**Mechanics:** `llm_labels.grade_ai` (additive), a `vw_label_set_ai` view, `LENS_VIEWS` gains `ai`, `screens.fit_ai`, and the lens lists gain an `ai` bucket. `grade` (the overall) stays the average of process and technical; `ai` is reported beside them and never folded in.
**Confirmed by the user 2026-09-17:** the rubric boundary above is correct as written. Validation is a live loop, not
a one-shot regrade: sample postings with AI in the TITLE first, grade them, the user and Claude read the grades
together and adjust the rubric prose until both are satisfied it grades as well as the process and technical lenses
(§18.7's bar: lens independence measured, rationales name actual duties, no rubric pattern-matching). Only then the
AI-term subset, then the corpus decision.
**Order and cost:** build only after §20.2 lands and the retrain numbers are recorded. Regrade in two steps: first the postings whose JD hits an AI-term list (estimate before running; expected several hundred) plus a stratified control sample of 100, measure lens independence as in §18.7, then decide whether the corpus regrade (~3,000 postings, ~5M Sonnet tokens) is worth it.

**Acceptance checklist (2026-09-17):**
- [x] `RUBRIC_LENS_AI` (public) and `RUBRIC_PERSONAL_AI` (personal, gitignored) written from the coaching brief; `rubric_version()` changed (Agent C).
- [x] `judge.py` batch header, `_validate`, `load_results`, `to_csv`, `agreement()` carry `grade_ai`; back-compat for older batch files with no `grade_ai` (Agent C).
- [x] `AI_TITLE_RE` and `pools(only=["ai_title"])`; `features.LENSES` gains `"ai"` → `vw_label_set_ai` (Agent C).
- [x] Schema (`llm_labels.grade_ai`, `vw_label_set_ai`), `vw_lens_fit` / `vw_shortlist` gain `grade_ai`, `fit_ai`, `ai_strong`, `ai_standout`; `lens_bucket` / `lens_source` unchanged (Agent B).
- [x] `finder.py lenses` prints the applied-AI count beside the three bucket counts (Agent B).
- [x] Report: a fourth lens list, "Strong on APPLIED AI", reading `ai_strong`, never folded into the other three; a `Level` column added to all lens lists (Agent D).
- [x] First live-validation batch exported and graded (2026-09-17, 40 AI-titled postings, `db/batches_ai_pilot`, imported): bullseye 3 · adjacent 17 · stretch 3 · wrong 17; the boundary held. Two rubric questions for the user are in `docs/STATUS.md`.
- [x] Lens independence on the pilot: 28/40 (70%) differ from both other lenses (§18.7 bar 67%).
- [x] AI-term corpus estimate: 1,704 active postings with AI in the title (118 not rejected), 11,929 with an AI term in the JD.
- [ ] Rubric prose adjusted with the user; then the AI-term subset (+100 control), then the corpus decision, then `train --lens ai`.
