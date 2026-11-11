-- Re-grade study (measurement system analysis of the human gold labels; vault Articles BACKLOG item 6, A2).
-- The user re-grades a stratified sample of his own blind gold rows, blind to his earlier call, to measure his
-- repeatability (test-retest agreement) on the Required-fit call. Kept APART from report_feedback and
-- jobs.decision_queue on purpose: a re-grade must never replace a gold label, so nothing here is read by
-- apply-queue, judge2, Jev or the reports. scripts/regrade_study.py draws, presents and scores the study.
CREATE TABLE IF NOT EXISTS jobs.regrade_study (
  study_id          text        NOT NULL,             -- e.g. 'msa1'
  item_no           int         NOT NULL,             -- presentation order, 1..n (shuffled at draw time)
  posting_id        text        NOT NULL,
  description_hash  text,                             -- the text the original grade was made on
  employer          text,
  title             text,
  description_text  text,                             -- copied at draw time so grading never needs the DuckDB
  original_fit      text        NOT NULL CHECK (original_fit IN ('meets', 'fails')),   -- hidden while grading
  original_at       timestamptz NOT NULL,
  regrade_fit       text        CHECK (regrade_fit IN ('meets', 'fails')),
  regraded_at       timestamptz,
  changes           int         NOT NULL DEFAULT 0,   -- times an answer was changed with Back (misclicks)
  note              text,
  drawn_at          timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (study_id, item_no),
  UNIQUE (study_id, posting_id)
);
