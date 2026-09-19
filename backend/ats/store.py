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
import re
import shutil
import unicodedata
from datetime import datetime

import duckdb

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "db", "jobsearch.duckdb"
)

SCHEMA_VERSION = 13  # v13 (2026-09-19): screens.fit_required -- the Required-block ranking model's probability
                     #                  (features.train(lens="required")). NOT a lens: ranking signal only,
                     #                  never read by pipeline.content_fit / combine;
                     # v12 (2026-09-18): llm_labels.required_fit / required_unmet (the judge's Required-block
                     #                  call, a SECOND question from the three lens grades -- see the column
                     #                  comments below);
                     # v11 (2026-09-18): description_hash normalized before hashing (whitespace/NBSP/zero-width/
                     #                  case/NFKC no longer orphan llm_labels / report_feedback / coverage /
                     #                  requirement_units / embeddings on a trivial re-fetch) — see
                     #                  normalize_for_hash and _migrate_v11_hash_normalization;
                     # v10 (2026-09-17): screens.level_fit / fit_ai, llm_labels.grade_ai, report_feedback table
                     #                  (sprint plan 20.2 / 21 — level rule + applied-AI lens);
                     # v9 (2026-09-17): postings.detail_attempts;
                     # v8 (2026-09-16): screens.fit_process / fit_technical;
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
    fit_ai          DOUBLE,                    -- per-lens fit (21): applied-AI delivery / enablement
    fit_required    DOUBLE,                    -- Required-block ranking model (features.train(lens="required"));
                                                -- NOT a lens -- never read by pipeline.content_fit / combine,
                                                -- ranking only. NULL until the model exists / a rescreen fills it.
    embed_sim       DOUBLE,                    -- Phase 3 (raw cosine)
    llm_score       INTEGER,                   -- Phase 4
    final_score     INTEGER NOT NULL,          -- 0-100
    band            VARCHAR NOT NULL,          -- very_strong | strong | partial | weak | none
    reasons         JSON,
    flags           JSON,
    top_terms       JSON,                      -- [["lean six sigma", 0.41], ...]
    llm_notes       VARCHAR,
    level_fit       VARCHAR,                   -- too_low | in_range | stretch_up | out_of_reach | unknown (20.2)
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
    grade_ai VARCHAR,                        -- applied-AI delivery / enablement lens (21); reported beside the
                                              -- other two, never folded into `grade`
    lane VARCHAR, confidence VARCHAR, blocker VARCHAR, rationale VARCHAR,
    required_fit VARCHAR,                    -- meets | arguable | fails; a SECOND, SEPARATE question from the
                                              -- three lens grades above -- those say what KIND of work the
                                              -- posting is and train the classifier, this says whether the
                                              -- candidate clears the posting's own Required block. NULL = judged
                                              -- before the field existed.
    required_unmet VARCHAR,                  -- the unmet qualification lines, ' ; '-joined, or empty
    batch VARCHAR,
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

-- The golden source (sprint plan 20.2 / 20.5): the user's own reads of a JD, loaded from a vault CSV rather
-- than typed here. One row per (posting, JD text, who assessed it) -- a `user`/`human-override` row always
-- outranks a review row for the SAME text (vw_report_feedback_latest), and a row whose description_hash no
-- longer matches the posting's current text is simply absent from that view: the JD changed under it, so the
-- label is retired, not deleted (report_feedback itself keeps every row forever for the export/audit trail).
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

-- The user's own read (or override) of a posting always wins over a review row for the SAME text; otherwise
-- the row confirmed by the user wins; otherwise the newest assessment. A row whose description_hash no longer
-- matches the posting's CURRENT text is simply absent here -- the JD changed under it and the label expired.
CREATE OR REPLACE VIEW vw_report_feedback_latest AS
    SELECT f.* FROM report_feedback f JOIN postings p ON p.posting_id = f.posting_id
      AND coalesce(p.description_hash, '') = f.description_hash
    QUALIFY row_number() OVER (PARTITION BY f.posting_id
        ORDER BY (f.assessor IN ('user', 'human-override')) DESC, f.confirmed_by_user DESC, f.assessed_at DESC) = 1;

-- Ordinal rank for level_fit, low -> high; unknown/NULL has no rank (20.2's "one step" language needs a
-- distance, not just equality).
CREATE OR REPLACE MACRO level_fit_rank(v) AS
    CASE v WHEN 'too_low' THEN 0 WHEN 'in_range' THEN 1 WHEN 'stretch_up' THEN 2 WHEN 'out_of_reach' THEN 3 END;

-- Confirmed human level_fit vs. the rule's answer stored on the posting's latest screen. This view reads the
-- STORED screens.level_fit (post-rescreen); backend/finder/feedback.py's `agreement()` computes the rule LIVE
-- instead so the number does not wait on a rescreen -- see the sprint plan 20.2 note "this works before any
-- rescreen, which is the point".
CREATE OR REPLACE VIEW vw_level_agreement AS
    SELECT f.posting_id, p.employer, p.title, f.level_fit AS human_level_fit, s.level_fit AS rule_level_fit,
           f.level_fit = s.level_fit AS agree,
           abs(level_fit_rank(f.level_fit) - level_fit_rank(s.level_fit)) AS steps_apart
    FROM vw_report_feedback_latest f
    JOIN postings p USING (posting_id)
    LEFT JOIN vw_screen_latest s USING (posting_id)
    WHERE f.level_fit IS NOT NULL AND f.confirmed_by_user;

-- Every judged posting with both lens grades and which lens favoured it. Feeds the three report lists the
-- user asked for: strong on process, strong on technical, and strong on BOTH (the least substitutable shape).
CREATE OR REPLACE VIEW vw_lens_grades AS
    SELECT l.posting_id, p.employer, p.title, p.url, l.grade, l.grade_process, l.grade_technical, l.grade_ai,
           l.blocker, l.rationale, s.final_score, s.band, s.verdict,
           l.grade_process IN ('bullseye', 'adjacent') AS process_strong,
           l.grade_technical IN ('bullseye', 'adjacent') AS technical_strong,
           -- Reported beside process/technical, never folded into `lens_bucket` (21: "ai is reported beside
           -- the others, never folded in") -- the overall positioning call still comes from process/technical.
           l.grade_ai IN ('bullseye', 'adjacent') AS ai_strong,
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

-- Apply/review/hidden tiers, computed from STORED fields only so the formula is tunable without re-judging.
-- Tiers were set against 61 blind gold rows on 2026-09-18: lens+meets matched the user's own adjacent/bullseye
-- call 9 of 14 (18 of 20 same-side on the held-out sheet); lens+arguable 3 of 20 (but holds one of his
-- bullseyes, so it is a review queue, not hidden); lens+fails 1 of 12; no-lens 1 of 15. All five lens+meets
-- misses are one pattern: a years-in-a-named-function line the judge read as generic. Level and location
-- gating stay with the screen (level_fit), not this view -- required_fit answers a different question (does
-- the candidate clear the posting's own Required block) than level_fit (does the seniority/comp band fit).
-- A user-adjudicated row carries no required_fit and needs none: his own grade IS the answer, so it tiers
-- on `grade` alone instead of dropping out of the view.
CREATE OR REPLACE VIEW vw_selection AS
    WITH base AS (
        SELECT l.posting_id, p.employer, p.title, p.url, p.status,
               l.grade_process, l.grade_technical, l.grade_ai,
               (l.grade_process IN ('bullseye', 'adjacent'))::INTEGER
             + (l.grade_technical IN ('bullseye', 'adjacent'))::INTEGER
             + (l.grade_ai IN ('bullseye', 'adjacent'))::INTEGER AS n_lenses_good,
               'bullseye' IN (l.grade_process, l.grade_technical, l.grade_ai) AS any_bullseye,
               l.required_fit, l.required_unmet, s.level_fit,
               l.scorer = 'user-adjudicated' AS adjudicated, l.grade
        FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
        LEFT JOIN vw_screen_latest s USING (posting_id)
        WHERE l.required_fit IS NOT NULL OR l.scorer = 'user-adjudicated'
    )
    SELECT * EXCLUDE (grade),
           CASE WHEN adjudicated THEN CASE WHEN grade IN ('bullseye', 'adjacent') THEN 'apply' ELSE 'hidden' END
                WHEN n_lenses_good >= 1 AND required_fit = 'meets' THEN 'apply'
                WHEN n_lenses_good >= 1 AND required_fit = 'arguable' THEN 'review'
                ELSE 'hidden' END AS tier
    FROM base;

-- Where the line falls between a lens model's probability and the judge's words. Macros rather than Python
-- constants so the views and the CLI read one source.
--
-- Calibrated OUT OF FOLD against the 2,953 graded rows (2026-09-16). The first calibration scored those rows
-- with a model trained on them and reported P 0.97 / R 0.83 at 0.70; out of fold the same threshold is
-- P 0.87 / R 0.61 (process) and P 0.79 / R 0.55 (technical). In-sample numbers on a model's own training set
-- are not evidence -- measure OOF or do not quote a number.
--
-- 0.70 is kept: it is above the best-F1 point because these rows go in a list a human reads, so precision is
-- worth more than recall, and the bucket counts barely move between 0.60 and 0.70 (both 208 -> 200) because
-- almost nothing in the surviving set sits near the boundary. What the correction changes is the confidence
-- attached to a `model` row, not which rows appear.
CREATE OR REPLACE MACRO lens_strong_p() AS 0.70;
-- The models rank the four grades cleanly -- OOF means 0.797 / 0.678 / 0.509 / 0.217 on process -- and
-- separate strong from not-strong at AUC 0.92. What they do NOT do well is tell bullseye from adjacent:
-- AUC 0.68 / 0.70, better than chance but weak, because those two distributions overlap heavily
-- (bullseye 0.797 +/- 0.18 vs adjacent 0.678 +/- 0.21). So an ungraded row is never handed a predicted
-- bullseye. It earns the `both` bucket by clearing this higher bar, which is a probability statement the
-- models can support, and the bar is understood to be soft.
CREATE OR REPLACE MACRO lens_standout_p() AS 0.80;

-- One number per lens whoever placed it, so the three can be compared and added: the judge's word where there
-- is one (a grade wins over a prediction, as everywhere in vw_lens_fit), the lens model's probability where
-- there is not. `adjacent` sits just above lens_strong_p() and `bullseye` above lens_standout_p() on purpose,
-- so a graded lens and a predicted lens cross the same lines.
CREATE OR REPLACE MACRO lens_value(grade, prob) AS
    CASE grade WHEN 'bullseye' THEN 1.0 WHEN 'adjacent' THEN 0.75 WHEN 'stretch' THEN 0.35 WHEN 'wrong' THEN 0.0
               ELSE coalesce(prob, 0.0) END;

-- Same "a grade wins over a prediction" pattern as lens_value, for the Required-block question: the judge's
-- own call where there is one (meets/arguable/fails answer whether the candidate clears the posting's Required
-- block), the ranking model's probability where there is not, and 0.5 -- unknown, neither rewarded nor zeroed
-- -- when neither exists. NOT a lens: this only ever multiplies lens_breadth (breadth_x_required below) and
-- orders the judge queue; it never enters lens_value, lens_best, lens_bucket or content_fit.
CREATE OR REPLACE MACRO required_value(required_fit, prob) AS
    CASE required_fit WHEN 'meets' THEN 1.0 WHEN 'arguable' THEN 0.5 WHEN 'fails' THEN 0.0
                       ELSE coalesce(prob, 0.5) END;

-- How much a level call discounts the ONE rank (rank_score below). The screen never rejects on level, so this
-- is where "a VP seat is not worth the read" is priced. Provisional v1 constants, like the rank itself.
CREATE OR REPLACE MACRO level_value(level_fit) AS
    CASE level_fit WHEN 'in_range' THEN 1.0 WHEN 'stretch_up' THEN 0.85 WHEN 'too_low' THEN 0.3
                   WHEN 'out_of_reach' THEN 0.2 ELSE 0.7 END;

-- One lens, in words, for rank_why: the judge's grade, or the model's probability marked as a guess; NULL when
-- the lens is not strong, so concat_ws drops it.
CREATE OR REPLACE MACRO lens_phrase(name, grade, prob) AS
    CASE WHEN grade IN ('bullseye', 'adjacent') THEN name || ' ' || grade
         WHEN grade IS NULL AND prob >= lens_strong_p() THEN name || ' ~' || printf('%.2f', prob) || ' (model)'
         ELSE NULL END;

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
               s.fit_ai, s.fit_required, s.level_fit,
               s.reasons, s.flags,
               g.grade, g.grade_process, g.grade_technical, g.grade_ai, g.blocker, g.scorer, g.required_fit, g.required_unmet,
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
               -- Applied-AI lens (21): same strong/standout pattern, but never folds into lens_bucket or
               -- lens_source below -- it is reported beside the other two, not merged with them.
               CASE WHEN grade_ai IS NOT NULL THEN grade_ai IN ('bullseye', 'adjacent')
                    ELSE fit_ai >= lens_strong_p() END AS ai_strong,
               CASE WHEN grade_ai IS NOT NULL THEN grade_ai = 'bullseye'
                    ELSE fit_ai >= lens_standout_p() END AS ai_standout,
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
           greatest(coalesce(fit_process, 0), coalesce(fit_technical, 0)) AS lens_max_p,
           -- The BEST lens decides whether a row is worth showing (pipeline.content_fit gates on the same idea);
           -- an applied-AI fit alone is enough.
           greatest(lens_value(grade_process, fit_process), lens_value(grade_technical, fit_technical),
                    lens_value(grade_ai, fit_ai)) AS lens_best,
           -- BREADTH: the three lenses ADDED (0-3), so a high number means two or three lenses scored well --
           -- the role that needs more than one of his skill sets. It is a SECOND field and only ever a
           -- tie-breaker among rows already worth showing, so it is multiplied by zero whenever the row is not:
           -- the best lens is not strong, the judge says he fails the Required block, or the screen rejected
           -- it (that is where "not remote and outside the commute area" and the pay floor live). The lens
           -- models share vocabulary, so the sum partly double-counts; it ranks, it does not measure.
           CASE WHEN verdict = 'reject' OR coalesce(required_fit, '') = 'fails'
                     OR greatest(lens_value(grade_process, fit_process), lens_value(grade_technical, fit_technical),
                                 lens_value(grade_ai, fit_ai)) < lens_strong_p() THEN 0.0
                ELSE lens_value(grade_process, fit_process) + lens_value(grade_technical, fit_technical)
                     + lens_value(grade_ai, fit_ai) END AS lens_breadth,
           -- The soft version of "multiply by zero if I don't meet the requirements": a judged row gets the
           -- hard 0 / 0.5 / 1 the judge assigned (required_value), an unjudged row gets the ranking model's
           -- probability. fit_required is NOT a lens and never touches lens_breadth itself -- only this
           -- product, which exists for ranking (the judge queue, ad hoc reads), never for content_fit.
           lens_breadth * required_value(required_fit, fit_required) AS breadth_x_required,
           -- THE rank: the one number a list is ordered by (0-100). Every component stays visible beside it;
           -- this is only their combination. Best lens carries it (0.8), breadth beyond the best lens adds to
           -- it (0.2 x the other two lenses' share). The best-lens weight must stay ABOVE 0.75: three adjacents
           -- score 0.75 whatever the split, one bullseye scores exactly this weight, and the user ruled that one
           -- bullseye outranks three adjacents (lukewarm on every lens is a generalist seat, not a fit), and then it is MULTIPLIED by whether he clears the Required
           -- block, by the level call, and by zero when the screen rejected the row (commute / remote / pay
           -- floor) or no lens is strong. Anything holding a bullseye comes first, more lenses beside it ranking higher
           -- (three bullseyes > bullseye + adjacent > one bullseye), then breadth among adjacent-only rows (three > two > one). PROVISIONAL v1 weights, chosen to reproduce the user's stated
           -- ordering; to be re-fit to his gold grades and build / pass decisions once there are enough.
           round(100.0 * (0.8 * lens_best + 0.2 * greatest(0.0, lens_breadth - lens_best) / 2.0)
                 * required_value(required_fit, fit_required) * level_value(level_fit)
                 * CASE WHEN verdict = 'reject' OR lens_best < lens_strong_p() THEN 0.0 ELSE 1.0 END, 1) AS rank_score,
           -- WHY it sits there, in words: the deciding facts, not the numbers again.
           concat_ws('; ',
               CASE WHEN process_strong AND technical_strong AND ai_strong THEN '★ three-lens' END,
               coalesce(nullif(concat_ws(' + ', lens_phrase('process', grade_process, fit_process),
                                                lens_phrase('technical', grade_technical, fit_technical),
                                                lens_phrase('AI', grade_ai, fit_ai)), ''), 'no strong lens'),
               CASE required_fit
                    WHEN 'meets' THEN 'Required: meets'
                    WHEN 'arguable' THEN 'Required arguable: ' || coalesce(nullif(left(required_unmet, 140), ''), 'see posting')
                    WHEN 'fails' THEN 'Required FAILS: ' || coalesce(nullif(left(required_unmet, 140), ''), 'see posting')
                    ELSE 'Required ~' || coalesce(printf('%.2f', fit_required), '?') || ' (model, not judged)' END,
               CASE WHEN coalesce(level_fit, 'unknown') != 'in_range' THEN 'level ' || coalesce(level_fit, 'unknown') END,
               CASE WHEN verdict = 'reject' THEN 'SCREEN REJECT: ' || coalesce(reasons ->> 0, 'see reasons') END
           ) AS rank_why
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
           s.fit_process, s.fit_technical, s.fit_ai, s.fit_required, s.level_fit, s.embed_sim, s.llm_score,
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

-- Applied-AI lens training set (21), same shape as vw_label_set_process / vw_label_set_technical, reading
-- grade_ai. features.LENS_VIEWS["ai"] points here (Agent C wires the lens name in; this view can exist and
-- be queried before that lands).
--
-- Unlike the other two lenses it takes NO positives from the shared sources (vault documents, decisions), only
-- their negatives. "They describe the candidate, not a lens" holds for process and technical work, which is
-- what he applied to; it is false here. Measured 2026-09-18: with them in, 365 of the 475 positives were his
-- application history, the top terms were lean / sigma / change management, and the worst false positives
-- were process jobs graded AI-`wrong` -- a process model with "ai" on top. Out: graded-positives AUC vs
-- `wrong` 0.878 -> 0.920, vs `stretch` 0.734 -> 0.775. That leaves ~110 positives, under LOW_DATA_MIN, so
-- the blend down-weights this model on its own until more AI grades exist -- which is the honest setting.
CREATE OR REPLACE VIEW vw_label_set_ai AS
    SELECT label_id, source, posting_id, company, title, text, label, weight, NULL AS grade
    FROM label_docs WHERE text IS NOT NULL AND label = 0
    UNION ALL
    SELECT 'llm:' || l.posting_id,
           CASE WHEN l.scorer = 'user-adjudicated' THEN 'user_adjudicated' ELSE 'llm_judge' END,
           p.posting_id, p.employer, p.title, p.description_text,
           CASE WHEN l.grade_ai IN ('bullseye', 'adjacent') THEN 1 ELSE 0 END,
           CASE l.grade_ai WHEN 'bullseye' THEN 1.0 WHEN 'adjacent' THEN 0.6
                           WHEN 'stretch' THEN 0.5 ELSE 1.0 END,
           l.grade_ai
    FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
    WHERE length(p.description_text) >= 800 AND l.grade_ai IS NOT NULL;

-- The Required-block ranking model's training set (features.LENS_VIEWS["required"]). NOT a lens -- it answers
-- a different question than process/technical/ai (does the candidate clear THIS posting's own Required block,
-- not what kind of work it is), so it is trained on ONLY the judge's required_fit call: no vault documents, no
-- decisions, no pseudo-negatives. Those shared sources describe the candidate's own history, which is exactly
-- "kind of work", already answered by the three lenses -- folding them in here would just re-teach that and
-- dilute the one signal this model exists to carry. label = 1 for `meets`, 0 for `arguable` and `fails`
-- (arguable is a real gap, not a fit); weight 1.0 for every row, matching what was measured (§ hard rule 2 --
-- do not invent a weighting). `grade` carries required_fit itself (meets/arguable/fails) rather than NULL, so
-- features.grade_report's held-out-fit-by-grade and AUC breakdown still print something meaningful for this
-- lens's training run.
CREATE OR REPLACE VIEW vw_label_set_required AS
    SELECT 'llm:' || l.posting_id AS label_id,
           CASE WHEN l.scorer = 'user-adjudicated' THEN 'user_adjudicated' ELSE 'llm_judge' END AS source,
           p.posting_id, p.employer AS company, p.title, p.description_text AS text,
           CASE WHEN l.required_fit = 'meets' THEN 1 ELSE 0 END AS label,
           1.0 AS weight,
           l.required_fit AS grade
    FROM vw_llm_labels_latest l JOIN postings p USING (posting_id)
    WHERE length(p.description_text) >= 800 AND l.required_fit IS NOT NULL;

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
    else:
        prev = con.execute("SELECT version FROM schema_info").fetchone()[0]
        if prev < 11 <= SCHEMA_VERSION:
            _migrate_v11_hash_normalization(con)
        # v2 -> v3 added tables only (CREATE IF NOT EXISTS above), so bumping the version is otherwise enough
        con.execute("UPDATE schema_info SET version = ? WHERE version < ?", [SCHEMA_VERSION, SCHEMA_VERSION])
    _add_missing_columns(con)
    con.execute(VIEWS)
    return con


def _add_missing_columns(con):
    """Additive column migrations. v7: the two lens grades on llm_labels (old rows keep NULL, which reads as
    'graded before the lenses existed'). v8: the two lens fit probabilities on screens (NULL = screened before
    the lens models existed; a rescreen fills them). v9: postings.detail_attempts (old rows default 0, i.e.
    'never tried'), so a posting whose fetch keeps coming back empty stops eating the detail budget forever.
    v10: screens.level_fit (NULL until the level rule runs / a rescreen fills it), screens.fit_ai and
    llm_labels.grade_ai (NULL until the applied-AI lens model / judge exist). v12: llm_labels.required_fit /
    required_unmet (NULL = judged before the Required-block question joined the rubric). v13: screens.fit_required
    (NULL until the Required-block ranking model exists / a rescreen fills it) -- NOT a lens, see the column
    comment on `screens` above."""
    for table, column, decl in (("llm_labels", "grade_process", "VARCHAR"),
                                ("llm_labels", "grade_technical", "VARCHAR"),
                                ("llm_labels", "grade_ai", "VARCHAR"),
                                ("llm_labels", "required_fit", "VARCHAR"),
                                ("llm_labels", "required_unmet", "VARCHAR"),
                                ("screens", "fit_process", "DOUBLE"),
                                ("screens", "fit_technical", "DOUBLE"),
                                ("screens", "fit_ai", "DOUBLE"),
                                ("screens", "fit_required", "DOUBLE"),
                                ("screens", "level_fit", "VARCHAR"),
                                ("postings", "detail_attempts", "INTEGER DEFAULT 0")):
        if column not in _columns(con, table):
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            if table == "postings" and column == "detail_attempts":
                con.execute("UPDATE postings SET detail_attempts = 0 WHERE detail_attempts IS NULL")


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


_HASH_WS_RE = re.compile("[\\s\u200b\u200c\u200d\ufeff]+")  # \\s already covers NBSP; zero-widths named by escape, never as literals


def normalize_for_hash(text: str) -> str:
    """NFKC + whitespace/zero-width collapse + case-fold, so two fetches of the same JD that differ only in
    incidental whitespace (measured: 10 of 12 retired gold labels differed from the prior text by 1-3 chars,
    similarity 1.000) hash identically. NFKC alone already maps NBSP to a plain space; the zero-width
    characters (ZWSP/ZWNJ/ZWJ/BOM) have no such mapping and are folded to a space explicitly."""
    return _HASH_WS_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip().lower()


def description_hash(text):
    if not text:
        return None
    return hashlib.sha1(normalize_for_hash(text).encode("utf-8")).hexdigest()[:16]


# Tables that store a description_hash column, and whether it is part of the primary key. Non-PK tables can
# be remapped with a plain UPDATE; PK tables need collision handling because two historical rows (fetched at
# different times, hashing differently under the OLD raw-text hash) can normalize to the SAME new hash.
# (extra_pk_cols, newest_ts_col) for the PK tables; the non-PK tables are handled directly in the migration.
_HASH_PK_TABLES = {
    "requirement_units": (("splitter", "ord"), "embedded_at"),
    "coverage": (("evidence_version", "model", "calibration"), "scored_at"),
    "llm_labels": (("rubric_version", "scorer"), "judged_at"),
    "report_feedback": (("assessor",), "assessed_at"),
}
_HASH_PLAIN_TABLES = ("embeddings",)  # description_hash present but NOT part of the primary key


def _remap_hash_pk_table(con, table: str, extra_pk_cols: tuple, ts_col: str) -> None:
    """Remaps `table`.description_hash via the `hash_map` temp table (posting_id, old_hash, new_hash), where
    description_hash is part of the primary key. On a collision -- a row already sits at the new hash for the
    same other PK columns -- the row with the newer `ts_col` survives and the other is deleted; ties favor the
    incoming (remapped) row. Safe to call twice: the second call finds hash_map empty of anything to do."""
    pk_cols = ", ".join(("posting_id", "description_hash") + extra_pk_cols)
    t_extra = ", ".join(f"t.{c}" for c in extra_pk_cols)
    u_extra = ", ".join(f"u.{c}" for c in extra_pk_cols)
    join_extra = " AND ".join(f"t.{c} = u.{c}" for c in extra_pk_cols)
    # 1. An existing row already at the new hash that LOSES to the incoming (old-hash) row: delete it.
    con.execute(f"""
        DELETE FROM {table} WHERE ({pk_cols}) IN (
            SELECT t.posting_id, t.description_hash, {t_extra}
            FROM {table} t
            JOIN hash_map m ON m.posting_id = t.posting_id AND m.new_hash = t.description_hash
            JOIN {table} u ON u.posting_id = m.posting_id AND u.description_hash = m.old_hash AND {join_extra}
            WHERE u.{ts_col} >= t.{ts_col}
        )""")
    # 2. The incoming (old-hash) row LOSES to an existing, newer row already at the new hash: delete it
    #    instead of remapping it (its data is superseded).
    con.execute(f"""
        DELETE FROM {table} WHERE ({pk_cols}) IN (
            SELECT u.posting_id, u.description_hash, {u_extra}
            FROM {table} u
            JOIN hash_map m ON m.posting_id = u.posting_id AND u.description_hash = m.old_hash
            JOIN {table} t ON t.posting_id = m.posting_id AND t.description_hash = m.new_hash AND {join_extra}
            WHERE t.{ts_col} > u.{ts_col}
        )""")
    # 3. Whatever is left at the old hash has no surviving collision: remap it.
    con.execute(f"""
        UPDATE {table} u SET description_hash = m.new_hash
        FROM hash_map m WHERE u.posting_id = m.posting_id AND u.description_hash = m.old_hash
    """)


def _migrate_v11_hash_normalization(con, log=lambda *a, **k: None) -> int:
    """v10 -> v11: rehash every posting's description under `normalize_for_hash` and remap every table that
    stores a description_hash (llm_labels, report_feedback, coverage, requirement_units, embeddings, postings)
    from the old raw-text hash to the new one. Idempotent -- a posting whose stored hash already equals its
    normalized hash contributes nothing to `hash_map`, so a second call is a no-op. Runs as ONE transaction.

    Deliberately does NOT touch description_fetched_at: the finder decides "needs (re)screen" by comparing
    screened_at to description_fetched_at (backend/finder/pipeline.RESCREEN_SQL), not by the hash, so this
    migration does not make the whole corpus look changed to the screening stage.
    """
    con.execute("BEGIN")
    try:
        con.execute("CREATE OR REPLACE TEMP TABLE hash_map "
                    "(posting_id VARCHAR, old_hash VARCHAR, new_hash VARCHAR)")
        reader = con.cursor()
        try:
            reader.execute("SELECT posting_id, description_hash, description_text FROM postings "
                           "WHERE description_text IS NOT NULL")
            while batch := reader.fetchmany(5000):
                rows = [(pid, old, description_hash(text)) for pid, old, text in batch]
                rows = [r for r in rows if r[1] and r[2] and r[1] != r[2]]
                if rows:
                    con.executemany("INSERT INTO hash_map VALUES (?, ?, ?)", rows)
        finally:
            reader.close()
        n_changed = con.execute("SELECT count(*) FROM hash_map").fetchone()[0]
        if n_changed:
            for table, (extra_pk_cols, ts_col) in _HASH_PK_TABLES.items():
                _remap_hash_pk_table(con, table, extra_pk_cols, ts_col)
            for table in _HASH_PLAIN_TABLES:
                con.execute(f"""UPDATE {table} e SET description_hash = m.new_hash FROM hash_map m
                               WHERE e.posting_id = m.posting_id AND e.description_hash = m.old_hash""")
            con.execute("""UPDATE postings p SET description_hash = m.new_hash FROM hash_map m
                          WHERE p.posting_id = m.posting_id AND p.description_hash = m.old_hash""")
        con.execute("DROP TABLE hash_map")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    log(f"v11 hash normalization: {n_changed} posting(s) rehashed")
    return n_changed


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


DETAIL_MAX_ATTEMPTS = 3  # a posting whose fetch keeps coming back empty/erroring stops being retried


def detail_candidates(con, platforms, title_pattern=None, limit=300, employers=None, since=None,
                       retry_exhausted=False):
    """Active postings still missing a JD, least-tried and newest first. `title_pattern` (regex) is a
    BUDGETING device — it decides which postings get a detail request first, not which ones matter.

    A posting whose detail fetch came back empty (or errored) is retried, but not forever: once
    `detail_attempts` reaches DETAIL_MAX_ATTEMPTS it drops out of the pool so it stops eating the
    budget every run. `retry_exhausted=True` lifts that floor for a deliberate re-check."""
    where = "status = 'active' AND description_text IS NULL AND platform IN (SELECT unnest(?::VARCHAR[]))"
    params = [list(platforms)]
    if not retry_exhausted:
        where += " AND detail_attempts < ?"
        params.append(DETAIL_MAX_ATTEMPTS)
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
        ORDER BY detail_attempts ASC, coalesce(posted_at, first_seen_at::DATE) DESC, first_seen_at DESC
        LIMIT ?""", params).fetchall()


def apply_detail(con, pid, fields, now):
    fields = {k: v for k, v in fields.items() if v is not None}
    if fields.get("description_text"):
        fields["description_hash"] = description_hash(fields["description_text"])
    fields["description_fetched_at"] = now
    sets = ", ".join(f"{k} = ?" for k in fields)
    con.execute(f"UPDATE postings SET {sets}, detail_attempts = detail_attempts + 1 WHERE posting_id = ?",
                list(fields.values()) + [pid])


def record_detail_error(con, pid, now):
    """Counts a failed detail fetch (network/parse error, not a 404) against the same
    `detail_attempts` budget as an empty result, so a persistently erroring posting also
    ages out of `detail_candidates` instead of being retried every run."""
    con.execute(
        "UPDATE postings SET detail_attempts = detail_attempts + 1, description_fetched_at = ? WHERE posting_id = ?",
        [now, pid])


def close_posting(con, pid, now):
    con.execute("UPDATE postings SET status = 'closed', closed_at = ?, description_fetched_at = ? "
                "WHERE posting_id = ? AND status = 'active'", [now, now, pid])
