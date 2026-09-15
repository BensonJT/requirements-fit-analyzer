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

SCHEMA_VERSION = 2

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
    con.execute(VIEWS)
    return con


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
