-- Schema `jobs`: snapshot of the pipeline's fit population (design section 7).
-- Replaced wholesale by scripts/pg_snapshot.py; never written by the tracker library.
CREATE SCHEMA IF NOT EXISTS jobs;

CREATE TABLE IF NOT EXISTS jobs.fit_snapshot (
  posting_id       text PRIMARY KEY,
  employer         text,
  platform         text,
  req_id           text,
  title            text,
  url              text,
  location_primary text,
  workplace_type   text,
  work_site        text,
  pay_min          int,
  pay_max          int,
  pay_interval     text,
  posted_at        date,
  posting_end_at   date,
  first_seen_at    timestamp,
  last_seen_at     timestamp,
  closed_at        timestamp,
  status           text,
  screen_verdict   text,
  screen_score     int,
  description_text text,
  -- scoring columns from vw_lens_fit, where present
  final_score      int,
  band             text,
  tier             int,
  verdict          text,
  fit_process      double precision,
  fit_technical    double precision,
  fit_ai           double precision,
  fit_required     double precision,
  fit_bullseye     double precision,
  level_fit        text,
  grade            text,
  grade_process    text,
  grade_technical  text,
  grade_ai         text,
  required_fit     text,
  rank_score       double precision,
  rank_why         text,
  lens_best        double precision,
  latest_decision  text,
  in_tracker       boolean,
  snapshot_at      timestamp
);

CREATE TABLE IF NOT EXISTS jobs.decisions (
  posting_id text,
  decision   text,
  reason     text,
  source     text,
  source_ref text,
  decided_at timestamp
);
CREATE INDEX IF NOT EXISTS decisions_posting_idx ON jobs.decisions (posting_id);

CREATE TABLE IF NOT EXISTS jobs.snapshot_run (
  run_at    timestamptz PRIMARY KEY DEFAULT now(),
  rows      int,
  decisions int,
  seconds   numeric,
  source    text
);
