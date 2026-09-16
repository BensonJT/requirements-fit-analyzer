"""DuckDB job store — one normalized `postings` table, upsert + lifecycle tracking,
and the filtering views / parameterized table macros.

Lifecycle (DESIGN_ats_registry.md §13a):
- A req is upserted every run it appears in: first_seen_at set once, last_seen_at
  bumped, status forced back to 'active'. Nothing is ever re-ingested as a new row.
- A req is closed (status='closed', closed_at stamped) the first run it is missing
  from a board that was pulled cleanly and completely. A failed or truncated pull
  never closes anything.
- Nothing is ever deleted: description_text accumulates as a corpus.

Performance: the DB file lives on the external drive behind WSL's 9p mount, where
each autocommitted statement costs ~40 ms. Every board's writes therefore go through
a staging table inside ONE transaction (measured 10x faster), not row-by-row.
"""
import hashlib
import os
import shutil
from datetime import datetime

import duckdb

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "db", "jobsearch.duckdb"
)

SCHEMA_VERSION = 8  # v8 (2026-09-16): screens.fit_process / fit_technical;
                    # v7 (2026-09-16): llm_labels two-lens grades; v6 (2026-09-15): training_exclusions; v5 (2026-09-15): llm_labels; v3 (2026-09-15): finder tables; v4 (2026-09-16): coverage tables; no postings changes

# Columns the adapters supply, in the order the staging table and upsert use them.
POSTING_COLUMNS = (
    "posting_id", "employer", "platform", "req_id", "title", "url",
    "location_primary", "locations", "country",
    "workplace_type", "employment_type", "job_family", "job_level",
    "pay_min", "pay_max", "pay_currency", "pay_interval", "pay_source",
    "posted_at", "posting_end_at",
    "description_text", "description_hash", "raw_json",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS postings (
    posting_id        VARCHAR PRIMARY KEY,   -- sha1(employer|platform|req_id), stable across runs
    employer          VARCHAR NOT NULL,
    platform          VARCHAR NOT NULL,
    req_id            VARCHAR NOT NULL,      -- the ATS's own identifier
    title             VARCHAR,
    url               VARCHAR,
    -- location, normalized across platforms
    location_primary  VARCHAR,               -- the ATS's primary location text
    locations         VARCHAR,               -- JSON array of every location name
    country           VARCHAR,               -- ISO-2 when the ATS says
    workplace_type    VARCHAR,               -- remote | hybrid | onsite | NULL
    employment_type   VARCHAR,               -- full_time | part_time | contract | intern | NULL
    job_family        VARCHAR,               -- department / job family / team, whatever the ATS calls it
    job_level         VARCHAR,               -- manager level / job level when exposed
    -- pay, normalized
    pay_min           INTEGER,
    pay_max           INTEGER,
    pay_currency      VARCHAR,
    pay_interval      VARCHAR,               -- year | hour
    pay_source        VARCHAR,               -- ats (structured field) | text (regex on the JD)
    -- dates from the ATS
    posted_at         DATE,                  -- when the ATS says it was posted
    posting_end_at    DATE,                  -- when the ATS says it closes, if it says
    -- the corpus
    description_text  VARCHAR,               -- plain text JD, kept forever
    description_hash  VARCHAR,
    description_fetched_at TIMESTAMP,        -- set when a detail call filled the JD
    raw_json          VARCHAR,               -- the platform's own list record, verbatim
    -- screening (filled later by screen.py)
    screen_verdict    VARCHAR,
    screen_score      INTEGER,
    screen_reasons    VARCHAR,
    screened_at       TIMESTAMP,
    -- lifecycle, owned by this module
    first_seen_at     TIMESTAMP NOT NULL,
    last_seen_at      TIMESTAMP NOT NULL,
    closed_at         TIMESTAMP,
    status            VARCHAR NOT NULL       -- active | closed
);

CREATE TABLE IF NOT EXISTS runs (
    run_id            VARCHAR PRIMARY KEY,
    started_at        TIMESTAMP NOT NULL,
    finished_at       TIMESTAMP,
    boards_attempted  INTEGER,
    boards_succeeded  INTEGER,
    boards_failed     INTEGER,
    total_jobs_seen   INTEGER,
    new_postings      INTEGER,
    reopened_postings INTEGER,
    closed_postings   INTEGER,
    details_fetched   INTEGER,
    elapsed_seconds   DOUBLE
);

CREATE TABLE IF NOT EXISTS board_runs (
    run_id            VARCHAR NOT NULL,
    employer          VARCHAR NOT NULL,
    platform          VARCHAR NOT NULL,
    ok                BOOLEAN NOT NULL,
    truncated         BOOLEAN DEFAULT FALSE, -- max_pages tripped: close-pass skipped
    job_count         INTEGER,
    new_count         INTEGER,
    closed_count      INTEGER,
    elapsed_seconds   DOUBLE,
    error             VARCHAR,
    ran_at            TIMESTAMP
);

-- ---- Finder (backend/finder/) ----
-- One row per posting per rules/model version; vw_screen_latest picks the newest.
CREATE TABLE IF NOT EXISTS screens (
    posting_id      VARCHAR NOT NULL,
    rules_version   VARCHAR NOT NULL,          -- finder.version.rules_version()
    model_version   VARCHAR NOT NULL DEFAULT 'none',
    screened_at     TIMESTAMP NOT NULL,
    verdict         VARCHAR NOT NULL,          -- candidate | review | reject
    tier            INTEGER,                   -- 1 precise | 2 broad | 3 data lane | NULL no function hit
    rule_score      INTEGER NOT NULL,          -- 0-100
    fit_prob        DOUBLE,                    -- Phase 2
    fit_process     DOUBLE,                    -- per-lens fit (18.8): process excellence / operating model
    fit_technical   DOUBLE,                    -- per-lens fit (18.8): data, analytics, quantitative
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

-- Mirror of the vault's Application_Tracker.md, rebuilt every run (the markdown stays the source of truth).
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

-- Jobs_Found files already read back for decisions (re-read only when mtime changes).
CREATE TABLE IF NOT EXISTS readback_log (file VARCHAR PRIMARY KEY, mtime DOUBLE NOT NULL, read_at TIMESTAMP NOT NULL);

-- Postings written as blocks into a Jobs_Found file; surfacing is not a decision.
CREATE TABLE IF NOT EXISTS surfaced (
    posting_id VARCHAR NOT NULL, file VARCHAR NOT NULL, surfaced_at TIMESTAMP NOT NULL,
    PRIMARY KEY (posting_id, file)
);

-- ---- Requirement coverage (sprint plan §15 / §16) ----
-- The user's evidence, embedded (text stays in this local file; see evidence.local.toml).
CREATE TABLE IF NOT EXISTS evidence_units (
    unit_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, kind VARCHAR NOT NULL, ref VARCHAR, text VARCHAR NOT NULL,
    weight DOUBLE NOT NULL, model VARCHAR NOT NULL, vector FLOAT[384] NOT NULL, content_hash VARCHAR,
    embedded_at TIMESTAMP NOT NULL
);
-- A JD's requirement units, embedded once per (JD hash, splitter); spec = specificity weight (§16.3).
CREATE TABLE IF NOT EXISTS requirement_units (
    posting_id VARCHAR NOT NULL, description_hash VARCHAR NOT NULL, splitter VARCHAR NOT NULL, ord INTEGER NOT NULL,
    unit_hash VARCHAR NOT NULL, text VARCHAR NOT NULL, section VARCHAR NOT NULL, grp VARCHAR NOT NULL,
    weight DOUBLE NOT NULL, klass VARCHAR NOT NULL, spec DOUBLE, model VARCHAR NOT NULL, vector FLOAT[384],
    embedded_at TIMESTAMP NOT NULL, PRIMARY KEY (posting_id, description_hash, splitter, ord)
);
CREATE TABLE IF NOT EXISTS coverage (
    posting_id VARCHAR NOT NULL, description_hash VARCHAR NOT NULL, evidence_version VARCHAR NOT NULL,
    model VARCHAR NOT NULL, calibration VARCHAR NOT NULL,
    coverage_required DOUBLE, coverage_role DOUBLE,            -- 0-100; NULL under 3 work units in that group
    n_required INTEGER, n_required_strong INTEGER, n_required_partial INTEGER,
    n_role INTEGER, n_role_strong INTEGER, n_role_partial INTEGER,
    gaps JSON, matches JSON, notes JSON, scored_at TIMESTAMP NOT NULL,
    PRIMARY KEY (posting_id, description_hash, evidence_version, model, calibration)
);
-- Postings that must never be trained on: unjudgeable text (marketing copy, general-application
-- placeholders) or a label the user has retired. Kept as rows so the reason survives a rebuild.
CREATE TABLE IF NOT EXISTS training_exclusions (
    posting_id VARCHAR PRIMARY KEY, reason VARCHAR NOT NULL, source VARCHAR NOT NULL, excluded_at TIMESTAMP NOT NULL
);

-- Graded function labels from an LLM judge (Phase 4 / the labeling run): one row per posting per rubric+scorer.
-- Two lenses (v7): the same JD is graded once for process / change-management work and once for technical /
-- analytical work, in ONE judging pass. `grade` stays the overall label -- the better of the two lenses -- so
-- every existing consumer (vw_label_set, the fit model, the reports) keeps working unchanged, while the pair
-- exposes what one axis could not: a role that is strong on BOTH is the candidate's least substitutable shape.
CREATE TABLE IF NOT EXISTS llm_labels (
    posting_id VARCHAR NOT NULL, description_hash VARCHAR NOT NULL, rubric_version VARCHAR NOT NULL,
    scorer VARCHAR NOT NULL,                 -- e.g. claude-sonnet-batch
    grade VARCHAR NOT NULL,                  -- bullseye | adjacent | stretch | wrong (overall = best lens)
    grade_process VARCHAR,                   -- process excellence / operating model / change management lens
    grade_technical VARCHAR,                 -- data, analytics, quantitative modelling and engineering lens
    lane VARCHAR, confidence VARCHAR, blocker VARCHAR, rationale VARCHAR, batch VARCHAR,
    judged_at TIMESTAMP NOT NULL,
    PRIMARY KEY (posting_id, description_hash, rubric_version, scorer)
);

-- What each board's own search API says it can filter by. Captured by `finder.py facets --discover` rather
-- than hardcoded: tenants publish their own value ids, and the facet parameter itself differs (Accenture
-- exposes locationMainGroup -> Country, Booz Allen exposes only a flat city list and 400s on locationCountry).
CREATE TABLE IF NOT EXISTS board_facets (
    employer VARCHAR NOT NULL, platform VARCHAR NOT NULL,
    facet_parameter VARCHAR NOT NULL,        -- e.g. locationMainGroup, jobFamilyGroup
    group_descriptor VARCHAR NOT NULL,       -- the nested group, e.g. 'Country' or 'City'; '' when flat
    value_id VARCHAR NOT NULL, descriptor VARCHAR NOT NULL, count INTEGER,
    discovered_at TIMESTAMP NOT NULL,
    PRIMARY KEY (employer, platform, facet_parameter, group_descriptor, value_id, descriptor)
);

-- The resolved enumeration strategy per board: how to pull it so the whole board is reachable and US-scoped.
-- `strategy` is plain | country | partition | truncate. A board is only close-passed when fully enumerated.
CREATE TABLE IF NOT EXISTS board_scope (
    employer VARCHAR NOT NULL, platform VARCHAR NOT NULL,
    strategy VARCHAR NOT NULL, facet_parameter VARCHAR, value_ids VARCHAR,   -- JSON array
    reported_total INTEGER, clamped BOOLEAN, note VARCHAR, verified_at TIMESTAMP NOT NULL,
    PRIMARY KEY (employer, platform)
);

-- Hard negatives for calibration that are not decisions: audit list + the fit model's confident mistakes (§16.1).
CREATE TABLE IF NOT EXISTS hard_negatives (
    posting_id VARCHAR NOT NULL, source VARCHAR NOT NULL, note VARCHAR, refreshed_at TIMESTAMP NOT NULL,
    PRIMARY KEY (posting_id, source)
);
"""

VIEWS = """
-- Everything currently visible on a board.
CREATE OR REPLACE VIEW vw_active AS
    SELECT * FROM postings WHERE status = 'active';

-- Active postings that are remote by flag OR by location text.
CREATE OR REPLACE VIEW vw_remote_active AS
    SELECT * FROM postings
    WHERE status = 'active'
      AND (workplace_type = 'remote'
           OR regexp_matches(coalesce(location_primary, '') || ' ' || coalesce(locations, ''), 'remote', 'i'));

-- Active postings in the DC / Northern Virginia / Maryland commute zone, or remote.
CREATE OR REPLACE VIEW vw_dmv_or_remote_active AS
    SELECT * FROM postings
    WHERE status = 'active'
      AND (workplace_type = 'remote'
           OR regexp_matches(coalesce(location_primary, '') || ' ' || coalesce(locations, ''),
                             'remote|virginia|\\bVA\\b|maryland|\\bMD\\b|washington,? d\\.?c|\\bDC\\b|reston|herndon|vienna|mclean|tysons|arlington|alexandria|fairfax|leesburg|ashburn|sterling|chantilly|winchester|frederick|bethesda|rockville|silver spring', 'i'));

-- Latest attempt per board, so a failing board is visible without reading logs.
CREATE OR REPLACE VIEW vw_board_health AS
    SELECT employer, platform, ok, truncated, job_count, new_count, closed_count,
           round(elapsed_seconds, 1) AS seconds, error, ran_at
    FROM board_runs
    QUALIFY row_number() OVER (PARTITION BY employer, platform ORDER BY ran_at DESC) = 1
    ORDER BY ok, employer;

-- How long postings stay up, per employer — the "closed_at - first_seen_at" corpus stat.
CREATE OR REPLACE VIEW vw_posting_lifetimes AS
    SELECT employer, platform, count(*) AS closed_postings,
           round(avg(date_diff('day', first_seen_at, closed_at)), 1) AS avg_days_visible
    FROM postings WHERE status = 'closed' AND closed_at IS NOT NULL
    GROUP BY 1, 2 ORDER BY 3 DESC;

-- ---- Parameterized table macros: SELECT * FROM new_postings(7), etc. ----

-- New to us in the last N days (first_seen_at), regardless of what the ATS says.
CREATE OR REPLACE MACRO new_postings(days) AS TABLE
    SELECT * FROM postings
    WHERE status = 'active' AND first_seen_at >= now() - to_days(days::INTEGER)
    ORDER BY first_seen_at DESC;

-- Posted by the ATS's own date in the last N days (falls back to first_seen_at).
CREATE OR REPLACE MACRO posted_within(days) AS TABLE
    SELECT * FROM postings
    WHERE status = 'active'
      AND coalesce(posted_at, first_seen_at::DATE) >= (current_date - days::INTEGER)
    ORDER BY coalesce(posted_at, first_seen_at::DATE) DESC;

-- Taken down in the last N days.
CREATE OR REPLACE MACRO taken_down(days) AS TABLE
    SELECT *, date_diff('day', first_seen_at, closed_at) AS days_visible FROM postings
    WHERE status = 'closed' AND closed_at >= now() - to_days(days::INTEGER)
    ORDER BY closed_at DESC;

-- Active postings whose title matches a regex (case-insensitive), e.g.
--   SELECT employer, title, location_primary FROM title_match('process (excellence|improvement)|operational excellence|six sigma');
CREATE OR REPLACE MACRO title_match(pattern) AS TABLE
    SELECT * FROM postings
    WHERE status = 'active' AND regexp_matches(title, pattern, 'i')
    ORDER BY employer, title;

-- Same, restricted to new-in-the-last-N-days: title_match_new(pattern, days).
CREATE OR REPLACE MACRO title_match_new(pattern, days) AS TABLE
    SELECT * FROM postings
    WHERE status = 'active' AND regexp_matches(title, pattern, 'i')
      AND first_seen_at >= now() - to_days(days::INTEGER)
    ORDER BY first_seen_at DESC;

-- Full-text (title + JD) regex over active postings that have a JD.
CREATE OR REPLACE MACRO text_match(pattern) AS TABLE
    SELECT * FROM postings
    WHERE status = 'active' AND description_text IS NOT NULL
      AND regexp_matches(coalesce(title, '') || '\n' || description_text, pattern, 'i')
    ORDER BY employer, title;

-- ---- Finder views ----
CREATE OR REPLACE VIEW vw_screen_latest AS
    SELECT * FROM screens QUALIFY row_number() OVER (PARTITION BY posting_id ORDER BY screened_at DESC) = 1;

CREATE OR REPLACE VIEW vw_decisions AS
    SELECT *, CASE WHEN decision != 'pass' THEN NULL
                   WHEN regexp_matches(lower(coalesce(reason, '')), '^(function|nuance|logistics|comp|other)\\b')
                   THEN regexp_extract(lower(reason), '^(function|nuance|logistics|comp|other)\\b', 1)
                   ELSE 'other' END AS reason_code
    FROM decisions QUALIFY row_number() OVER (PARTITION BY posting_id ORDER BY decided_at DESC) = 1;

-- Newest coverage per posting for its current JD text.
CREATE OR REPLACE VIEW vw_coverage_latest AS
    SELECT c.* FROM coverage c JOIN postings p ON p.posting_id = c.posting_id AND coalesce(p.description_hash, '') = c.description_hash
    QUALIFY row_number() OVER (PARTITION BY c.posting_id ORDER BY c.scored_at DESC) = 1;

-- The user's own adjudication always wins over a judge grade for the same posting, whenever it was written;
-- otherwise the newest grade for the posting's current text.
CREATE OR REPLACE VIEW vw_llm_labels_latest AS
    SELECT l.* FROM llm_labels l JOIN postings p ON p.posting_id = l.posting_id
      AND coalesce(p.description_hash, '') = l.description_hash
    QUALIFY row_number() OVER (PARTITION BY l.posting_id
                               ORDER BY (l.scorer = 'user-adjudicated') DESC, l.judged_at DESC) = 1;

-- Every judged posting with both lens grades and which lens favoured it. Feeds the three report lists the
-- user asked for: strong on process, strong on technical, and strong on BOTH (the least substitutable shape).
CREATE OR REPLACE VIEW vw_lens_grades AS
    SELECT l.posting_id, p.employer, p.title, p.url, l.grade, l.grade_process, l.grade_technical,
           l.blocker, l.rationale, s.final_score, s.band, s.verdict,
           l.grade_process IN ('bullseye', 'adjacent') AS process_strong,
           l.grade_technical IN ('bullseye', 'adjacent') AS technical_strong,
           -- `both` requires at least one bullseye. adjacent/adjacent is mediocre on both lenses, not the rare
           -- role that genuinely demands both, and letting it in fills the list the user most wants with
           -- lukewarm rows (9 of the 25 'both' rows on the 2026-09-16 pilot were adjacent/adjacent).
           CASE WHEN l.grade_process IS NULL THEN 'single-lens'
                WHEN l.grade_process IN ('bullseye','adjacent') AND l.grade_technical IN ('bullseye','adjacent')
                     AND 'bullseye' IN (l.grade_process, l.grade_technical) THEN 'both'
                WHEN l.grade_process IN ('bullseye','adjacent') THEN 'process'
                WHEN l.grade_technical IN ('bullseye','adjacent') THEN 'technical'
                ELSE 'neither' END AS lens_bucket
    FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
    LEFT JOIN vw_screen_latest s USING (posting_id);

-- Where the line falls between a lens model's probability and the judge's words. Macros rather than Python
-- constants so the views and the CLI read one source. Both were calibrated against the 2,953 graded rows
-- (2026-09-16), not chosen by eye.
--
-- `strong` at 0.70 is deliberately above the best-F1 point (0.58): these rows go in a list a human reads, so
-- precision is worth more than recall. Process P 0.97 / R 0.83, technical P 0.92 / R 0.83.
CREATE OR REPLACE MACRO lens_strong_p() AS 0.70;
-- There is NO model bullseye. Asked to reproduce the judge's bullseye/adjacent split the models manage F1
-- 0.64 / 0.61 at precision ~0.5 -- a coin flip -- because the two grades' probabilities overlap almost
-- completely (process means 0.886 vs 0.783). So an ungraded row earns the `both` bucket by clearing a HIGHER
-- probability bar on each lens, which the models are good at, instead of being handed a grade they cannot
-- predict. 0.80 is where strong-vs-not precision reaches 1.00 / 0.94.
CREATE OR REPLACE MACRO lens_standout_p() AS 0.80;

-- One row per active screened posting saying how each lens places it, and WHO placed it. The judge has
-- graded ~3k rows; the lens models cover the other 60k. Reading them from one view is what lets the three
-- report lists be the whole corpus instead of only the judged slice -- with `lens_source` on every row, so a
-- model guess is never mistaken for the user's own ruling.
CREATE OR REPLACE VIEW vw_lens_fit AS
    WITH base AS (
        SELECT p.posting_id, p.employer, p.title, p.url, p.location_primary, p.workplace_type,
               p.pay_min, p.pay_max, p.pay_interval, p.first_seen_at,
               date_diff('day', p.first_seen_at, now()) AS days_since_first_seen,
               s.final_score, s.band, s.tier, s.verdict, s.rule_score, s.fit_prob, s.fit_process, s.fit_technical,
               s.reasons, s.flags,
               g.grade, g.grade_process, g.grade_technical, g.blocker, g.scorer,
               d.posting_id IS NOT NULL AS decided,
               t.matched_posting_id IS NOT NULL AS in_tracker
        FROM postings p
        JOIN vw_screen_latest s USING (posting_id)
        LEFT JOIN vw_llm_labels_latest g USING (posting_id)
        LEFT JOIN vw_decisions d USING (posting_id)
        LEFT JOIN (SELECT DISTINCT matched_posting_id FROM tracker) t ON t.matched_posting_id = p.posting_id
        WHERE p.status = 'active'
    ), placed AS (
        SELECT *,
               -- Per lens: a grade wins over a prediction, and a row graded before the lenses existed falls
               -- back to the model for that lens rather than dropping out of the lists entirely.
               CASE WHEN grade_process IS NOT NULL THEN grade_process IN ('bullseye', 'adjacent')
                    ELSE fit_process >= lens_strong_p() END AS process_strong,
               CASE WHEN grade_technical IS NOT NULL THEN grade_technical IN ('bullseye', 'adjacent')
                    ELSE fit_technical >= lens_strong_p() END AS technical_strong,
               -- "Not merely adjacent": the judge says it with a bullseye, the model with a high probability.
               CASE WHEN grade_process IS NOT NULL THEN grade_process = 'bullseye'
                    ELSE fit_process >= lens_standout_p() END AS process_standout,
               CASE WHEN grade_technical IS NOT NULL THEN grade_technical = 'bullseye'
                    ELSE fit_technical >= lens_standout_p() END AS technical_standout,
               CASE WHEN scorer = 'user-adjudicated' THEN 'user'
                    WHEN grade_process IS NOT NULL AND grade_technical IS NOT NULL THEN 'judge'
                    WHEN grade_process IS NOT NULL OR grade_technical IS NOT NULL THEN 'judge+model'
                    ELSE 'model' END AS lens_source
        FROM base
    )
    SELECT *,
           -- `both` still demands more than adjacent on one lens: strong-but-lukewarm on each is a
           -- generalist, not the rare role that genuinely needs both (the rule vw_lens_grades established on
           -- the pilot, where 9 of 25 `both` rows were adjacent/adjacent).
           CASE WHEN process_strong AND technical_strong AND (process_standout OR technical_standout) THEN 'both'
                WHEN process_strong THEN 'process'
                WHEN technical_strong THEN 'technical'
                ELSE 'neither' END AS lens_bucket,
           -- A single per-row lens strength, used to break ties inside a list. A row with no JD scores
           -- NULL on both lenses and lands in `neither`, which is correct: nothing placed it.
           greatest(coalesce(fit_process, 0), coalesce(fit_technical, 0)) AS lens_max_p
    FROM placed;

-- Near misses coverage is calibrated against: 'pass --reason function' decisions plus the hard_negatives table.
CREATE OR REPLACE VIEW vw_hard_negatives AS
    SELECT posting_id, 'decision:function' AS source, reason AS note FROM vw_decisions
    WHERE decision = 'pass' AND reason_code = 'function'
    UNION
    SELECT posting_id, source, note FROM hard_negatives;

-- Active, not rejected, not decided, not already in the tracker; best first.
CREATE OR REPLACE VIEW vw_shortlist AS
    SELECT p.posting_id, p.employer, p.title, p.location_primary, p.workplace_type, p.employment_type,
           p.pay_min, p.pay_max, p.pay_interval, p.url, p.posted_at, p.first_seen_at,
           date_diff('day', p.first_seen_at, now()) AS days_since_first_seen,
           s.final_score, s.band, s.tier, s.verdict, s.rule_score, s.fit_prob,
           s.fit_process, s.fit_technical, s.embed_sim, s.llm_score,
           s.reasons, s.flags, s.top_terms, s.rules_version, s.model_version, s.screened_at,
           c.coverage_required, c.coverage_role
    FROM postings p
    JOIN vw_screen_latest s USING (posting_id)
    LEFT JOIN vw_coverage_latest c USING (posting_id)
    LEFT JOIN vw_decisions d USING (posting_id)
    LEFT JOIN (SELECT DISTINCT matched_posting_id FROM tracker) t ON t.matched_posting_id = p.posting_id
    WHERE p.status = 'active' AND s.verdict != 'reject' AND d.posting_id IS NULL AND t.matched_posting_id IS NULL
    ORDER BY s.final_score DESC, p.first_seen_at DESC;

CREATE OR REPLACE MACRO vw_scored_new(days) AS TABLE
    SELECT * FROM vw_shortlist WHERE days_since_first_seen <= days::INTEGER ORDER BY final_score DESC;

-- Every label the model may see, one row per source. `grade` is the judge's four-way grade where the row
-- came from the labeling run (sprint plan section 17) and NULL everywhere else; features.training_set picks
-- one row per job by SOURCE_PRIORITY and drops training_exclusions.
CREATE OR REPLACE VIEW vw_label_set AS
    SELECT label_id, source, posting_id, company, title, text, label, weight, NULL AS grade
    FROM label_docs WHERE text IS NOT NULL
    UNION ALL
    SELECT 'dec:' || d.posting_id, 'decision', p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN d.decision = 'build' THEN 1 ELSE 0 END, 1.0, NULL
    FROM vw_decisions d JOIN postings p USING (posting_id)
    WHERE d.decision IN ('build', 'pass') AND p.description_text IS NOT NULL
    UNION ALL
    -- Graded labels: bullseye / adjacent are positives (1.0 / 0.6), stretch / wrong the hard negatives the
    -- model never had (0.5 / 1.0). A user-adjudicated row is a separate, higher-priority source.
    SELECT 'llm:' || l.posting_id,
           CASE WHEN l.scorer = 'user-adjudicated' THEN 'user_adjudicated' ELSE 'llm_judge' END,
           p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN l.grade IN ('bullseye', 'adjacent') THEN 1 ELSE 0 END,
           CASE l.grade WHEN 'bullseye' THEN 1.0 WHEN 'adjacent' THEN 0.6 WHEN 'stretch' THEN 0.5 ELSE 1.0 END,
           l.grade
    FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
    WHERE length(p.description_text) >= 800;

-- Per-lens training sets (sprint plan 18.8). Identical to vw_label_set except that the graded rows carry the
-- lens grade instead of the averaged one, and only rows judged on that lens take part. The shared sources
-- (vault documents, decisions) stay in both: they describe the candidate, not a lens, and they anchor what
-- "his world" looks like while the lens grades do the discriminating.
CREATE OR REPLACE VIEW vw_label_set_process AS
    SELECT label_id, source, posting_id, company, title, text, label, weight, NULL AS grade
    FROM label_docs WHERE text IS NOT NULL
    UNION ALL
    SELECT 'dec:' || d.posting_id, 'decision', p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN d.decision = 'build' THEN 1 ELSE 0 END, 1.0, NULL
    FROM vw_decisions d JOIN postings p USING (posting_id)
    WHERE d.decision IN ('build', 'pass') AND p.description_text IS NOT NULL
    UNION ALL
    SELECT 'llm:' || l.posting_id,
           CASE WHEN l.scorer = 'user-adjudicated' THEN 'user_adjudicated' ELSE 'llm_judge' END,
           p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN l.grade_process IN ('bullseye', 'adjacent') THEN 1 ELSE 0 END,
           CASE l.grade_process WHEN 'bullseye' THEN 1.0 WHEN 'adjacent' THEN 0.6
                                WHEN 'stretch' THEN 0.5 ELSE 1.0 END,
           l.grade_process
    FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
    WHERE length(p.description_text) >= 800 AND l.grade_process IS NOT NULL;

CREATE OR REPLACE VIEW vw_label_set_technical AS
    SELECT label_id, source, posting_id, company, title, text, label, weight, NULL AS grade
    FROM label_docs WHERE text IS NOT NULL
    UNION ALL
    SELECT 'dec:' || d.posting_id, 'decision', p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN d.decision = 'build' THEN 1 ELSE 0 END, 1.0, NULL
    FROM vw_decisions d JOIN postings p USING (posting_id)
    WHERE d.decision IN ('build', 'pass') AND p.description_text IS NOT NULL
    UNION ALL
    SELECT 'llm:' || l.posting_id,
           CASE WHEN l.scorer = 'user-adjudicated' THEN 'user_adjudicated' ELSE 'llm_judge' END,
           p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN l.grade_technical IN ('bullseye', 'adjacent') THEN 1 ELSE 0 END,
           CASE l.grade_technical WHEN 'bullseye' THEN 1.0 WHEN 'adjacent' THEN 0.6
                                  WHEN 'stretch' THEN 0.5 ELSE 1.0 END,
           l.grade_technical
    FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
    WHERE length(p.description_text) >= 800 AND l.grade_technical IS NOT NULL;

-- Annualized pay band (hourly x 2000) for postings that carry one.
CREATE OR REPLACE VIEW vw_pay_annualized AS
    SELECT posting_id, employer, title, location_primary, status,
           CASE WHEN pay_interval = 'hour' THEN pay_min * 2000 ELSE pay_min END AS annual_min,
           CASE WHEN pay_interval = 'hour' THEN pay_max * 2000 ELSE pay_max END AS annual_max,
           pay_source
    FROM postings WHERE pay_min IS NOT NULL;
"""


def connect(db_path=None):
    db_path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    _migrate(db_path)
    con = duckdb.connect(db_path)
    con.execute(SCHEMA)
    if not con.execute("SELECT count(*) FROM schema_info").fetchone()[0]:
        con.execute("INSERT INTO schema_info VALUES (?)", [SCHEMA_VERSION])
    else:  # v2 -> v3 added tables only (CREATE IF NOT EXISTS above), so just record the version
        con.execute("UPDATE schema_info SET version = ? WHERE version < ?", [SCHEMA_VERSION, SCHEMA_VERSION])
    _add_missing_columns(con)
    con.execute(VIEWS)
    return con


def _add_missing_columns(con):
    """Additive column migrations. v7: the two lens grades on llm_labels (old rows keep NULL, which reads as
    'graded before the lenses existed'). v8: the two lens fit probabilities on screens (NULL = screened before
    the lens models existed; a rescreen fills them)."""
    for table, column, decl in (("llm_labels", "grade_process", "VARCHAR"),
                                ("llm_labels", "grade_technical", "VARCHAR"),
                                ("screens", "fit_process", "DOUBLE"),
                                ("screens", "fit_technical", "DOUBLE")):
        if column not in _columns(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _columns(con, table):
    return {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]).fetchall()}


def _migrate(db_path):
    """v1 (2026-09-14 first run) -> v2: rebuild `postings` with the normalized columns,
    keeping every row's first_seen_at/last_seen_at/status, and widen board_runs/runs.
    Takes a file copy first so the migration is reversible. No-op on a v2 file."""
    if not os.path.exists(db_path):
        return
    con = duckdb.connect(db_path)
    tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    if "postings" not in tables or "raw_json" in _columns(con, "postings"):
        con.close()
        return
    con.close()
    shutil.copy2(db_path, f"{db_path}.v1-backup-{datetime.now():%Y%m%d}")
    con = duckdb.connect(db_path)
    con.execute("BEGIN")
    con.execute("ALTER TABLE postings RENAME TO postings_v1")
    con.execute("ALTER TABLE board_runs RENAME TO board_runs_v1")
    con.execute("ALTER TABLE runs RENAME TO runs_v1")
    con.execute(SCHEMA)
    con.execute("""
        INSERT INTO postings (posting_id, employer, platform, req_id, title, url,
                              location_primary, workplace_type, description_text, description_hash,
                              screen_verdict, screen_score, screen_reasons,
                              first_seen_at, last_seen_at, closed_at, status)
        SELECT posting_id, employer, platform, req_id, title, url,
               CASE WHEN regexp_matches(coalesce(locations, ''), '^\\s*\\d+ Locations?\\s*$', 'i') THEN NULL ELSE locations END,
               CASE WHEN regexp_matches(coalesce(workplace_type, '') || ' ' || coalesce(locations, ''), 'remote', 'i') THEN 'remote'
                    WHEN regexp_matches(coalesce(workplace_type, ''), 'hybrid', 'i') THEN 'hybrid'
                    WHEN regexp_matches(coalesce(workplace_type, ''), 'on.?site', 'i') THEN 'onsite' END,
               description_text, description_hash, screen_verdict, screen_score, screen_reasons,
               first_seen_at, last_seen_at, closed_at, status
        FROM postings_v1
    """)
    con.execute("UPDATE postings SET description_fetched_at = first_seen_at WHERE description_text IS NOT NULL")
    # v1 registry mistakes: two employers were registered against another employer's
    # board, so their rows duplicate real postings held under the right name.
    con.execute("DELETE FROM postings WHERE employer = 'Optum (UnitedHealth Group)' AND platform = 'oracle_orc'")
    con.execute("DELETE FROM postings WHERE employer = 'Meridial by Invisible' AND platform = 'greenhouse'")
    con.execute("""INSERT INTO board_runs (run_id, employer, platform, ok, job_count, elapsed_seconds, error, ran_at)
                   SELECT b.run_id, b.employer, b.platform, b.ok, b.job_count, b.elapsed_seconds, b.error, r.started_at
                   FROM board_runs_v1 b LEFT JOIN runs_v1 r USING (run_id)""")
    con.execute("""INSERT INTO runs (run_id, started_at, finished_at, boards_attempted, boards_succeeded, boards_failed,
                                     total_jobs_seen, new_postings, closed_postings, elapsed_seconds)
                   SELECT run_id, started_at, finished_at, boards_attempted, boards_succeeded, boards_failed,
                          total_jobs_seen, new_postings, closed_postings, elapsed_seconds FROM runs_v1""")
    con.execute("DROP TABLE postings_v1")
    con.execute("DROP TABLE board_runs_v1")
    con.execute("DROP TABLE runs_v1")
    con.execute("DELETE FROM schema_info")
    con.execute("INSERT INTO schema_info VALUES (?)", [SCHEMA_VERSION])
    con.execute("COMMIT")
    con.close()


def posting_id(employer, platform, req_id):
    raw = f"{employer}|{platform}|{req_id}".lower()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def description_hash(text):
    if not text:
        return None
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _stage(con, employer, platform, jobs, now):
    """Loads one board's postings into a temp table (deduped on posting_id)."""
    con.execute("CREATE OR REPLACE TEMP TABLE stage AS SELECT * FROM postings WHERE 1 = 0")
    cols = ", ".join(POSTING_COLUMNS)
    marks = ", ".join("?" for _ in POSTING_COLUMNS)
    rows = []
    for j in jobs:
        if not j.get("req_id"):
            continue
        pid = posting_id(employer, platform, j["req_id"])
        rows.append([pid, employer, platform, str(j["req_id"]), j.get("title"), j.get("url"),
                     j.get("location_primary"), j.get("locations"), j.get("country"),
                     j.get("workplace_type"), j.get("employment_type"), j.get("job_family"), j.get("job_level"),
                     j.get("pay_min"), j.get("pay_max"), j.get("pay_currency"), j.get("pay_interval"), j.get("pay_source"),
                     j.get("posted_at"), j.get("posting_end_at"),
                     j.get("description_text"), description_hash(j.get("description_text")), j.get("raw_json")])
    if rows:
        con.executemany(
            f"INSERT INTO stage ({cols}, first_seen_at, last_seen_at, status) VALUES ({marks}, ?, ?, 'active')",
            [r + [now, now] for r in rows],
        )
        con.execute("""DELETE FROM stage WHERE rowid NOT IN (
                           SELECT min(rowid) FROM stage GROUP BY posting_id)""")
    return len(rows)


def record_board(con, employer, platform, jobs, now, truncated=False):
    """Upserts one board's pull and closes what went missing — in ONE transaction.

    Returns (seen, new, reopened, closed). `closed` is always 0 for a truncated pull.
    """
    con.execute("BEGIN")
    try:
        seen = _stage(con, employer, platform, jobs, now)
        new = con.execute("""SELECT count(*) FROM stage s
                             WHERE NOT EXISTS (SELECT 1 FROM postings p WHERE p.posting_id = s.posting_id)""").fetchone()[0]
        reopened = con.execute("""SELECT count(*) FROM stage s JOIN postings p USING (posting_id)
                                  WHERE p.status = 'closed'""").fetchone()[0]
        cols = ", ".join(POSTING_COLUMNS)
        con.execute(f"""
            INSERT INTO postings ({cols}, description_fetched_at, first_seen_at, last_seen_at, closed_at, status)
            SELECT {cols},
                   CASE WHEN description_text IS NOT NULL THEN first_seen_at END,
                   first_seen_at, last_seen_at, NULL, 'active'
            FROM stage
            ON CONFLICT (posting_id) DO UPDATE SET
                title            = excluded.title,
                url              = excluded.url,
                location_primary = coalesce(excluded.location_primary, postings.location_primary),
                locations        = coalesce(excluded.locations, postings.locations),
                country          = coalesce(excluded.country, postings.country),
                workplace_type   = coalesce(excluded.workplace_type, postings.workplace_type),
                employment_type  = coalesce(excluded.employment_type, postings.employment_type),
                job_family       = coalesce(excluded.job_family, postings.job_family),
                job_level        = coalesce(excluded.job_level, postings.job_level),
                pay_min          = coalesce(excluded.pay_min, postings.pay_min),
                pay_max          = coalesce(excluded.pay_max, postings.pay_max),
                pay_currency     = coalesce(excluded.pay_currency, postings.pay_currency),
                pay_interval     = coalesce(excluded.pay_interval, postings.pay_interval),
                pay_source       = coalesce(excluded.pay_source, postings.pay_source),
                posted_at        = coalesce(postings.posted_at, excluded.posted_at),
                posting_end_at   = coalesce(excluded.posting_end_at, postings.posting_end_at),
                description_text = coalesce(excluded.description_text, postings.description_text),
                -- a changed JD is a fresh fetch: without this the finder never re-screens text an ingest fix
                -- rewrote (the Lever `lists` bug rewrote 500 descriptions and nothing downstream noticed)
                description_fetched_at = CASE
                    WHEN excluded.description_text IS NOT NULL
                     AND excluded.description_text IS DISTINCT FROM postings.description_text
                    THEN timezone('UTC', now()) ELSE postings.description_fetched_at END,
                description_hash = coalesce(excluded.description_hash, postings.description_hash),
                raw_json         = coalesce(excluded.raw_json, postings.raw_json),
                last_seen_at     = excluded.last_seen_at,
                closed_at        = NULL,
                status           = 'active'
        """)
        closed = 0
        if not truncated:
            closed = con.execute("""
                UPDATE postings SET status = 'closed', closed_at = ?
                WHERE employer = ? AND platform = ? AND status = 'active'
                  AND posting_id NOT IN (SELECT posting_id FROM stage)
            """, [now, employer, platform]).fetchone()[0]
        con.execute("DROP TABLE stage")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return seen, new, reopened, closed


def log_board(con, run_id, employer, platform, ok, elapsed, now, job_count=None, new_count=None,
              closed_count=None, truncated=False, error=None):
    con.execute(
        "INSERT INTO board_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, employer, platform, ok, truncated, job_count, new_count, closed_count, elapsed,
         (error or "")[:300] or None, now],
    )


def log_run(con, run_id, started_at, finished_at, attempted, succeeded, failed,
            seen, new, reopened, closed, details, elapsed):
    con.execute(
        "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run_id, started_at, finished_at, attempted, succeeded, failed, seen, new, reopened, closed, details, elapsed],
    )


def detail_candidates(con, platforms, title_pattern=None, limit=300, employers=None, since=None):
    """Active postings still missing a JD, newest first. `title_pattern` (regex) is a
    BUDGETING device — it decides which postings get a detail request first, not
    which ones matter."""
    where = "status = 'active' AND description_text IS NULL AND platform IN (SELECT unnest(?::VARCHAR[]))"
    params = [list(platforms)]
    if since is not None:
        where += " AND first_seen_at >= ?"
        params.append(since)
    if employers is not None:
        where += " AND employer IN (SELECT unnest(?::VARCHAR[]))"
        params.append(list(employers))
    if title_pattern:
        where += " AND regexp_matches(coalesce(title, ''), ?, 'i')"
        params.append(title_pattern)
    params.append(limit)
    return con.execute(f"""
        SELECT posting_id, employer, platform, req_id, url, location_primary, locations,
               workplace_type, job_level, posted_at, posting_end_at
        FROM postings WHERE {where}
        ORDER BY coalesce(posted_at, first_seen_at::DATE) DESC, first_seen_at DESC
        LIMIT ?""", params).fetchall()


def apply_detail(con, pid, fields, now):
    fields = {k: v for k, v in fields.items() if v is not None}
    if fields.get("description_text"):
        fields["description_hash"] = description_hash(fields["description_text"])
    fields["description_fetched_at"] = now
    sets = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE postings SET {sets} WHERE posting_id = ?", list(fields.values()) + [pid])


def close_posting(con, pid, now):
    con.execute("UPDATE postings SET status = 'closed', closed_at = ?, description_fetched_at = ? "
                "WHERE posting_id = ? AND status = 'active'", [now, now, pid])
