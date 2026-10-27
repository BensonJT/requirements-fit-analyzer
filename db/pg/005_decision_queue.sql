-- Decision queue: build / pass / hold calls submitted while the pipeline DuckDB may be locked.
-- Written by the feedback applet (scripts/feedback_app.py) or anything else that can reach Postgres;
-- `finder.py apply-queue` replays pending rows through the same validation as `finder.py mark`
-- (launch start, just before the snapshot, and hourly from scripts/inbox_watch.sh when DuckDB is free).
-- Rows are kept as an audit trail: applied_at stamps a success, apply_error a refusal (fix and resubmit),
-- cancelled_at an Undo before it was applied.
CREATE TABLE IF NOT EXISTS jobs.decision_queue (
  queue_id     bigserial PRIMARY KEY,
  posting_id   text NOT NULL,
  decision     text NOT NULL CHECK (decision IN ('build', 'pass', 'hold')),
  reason       text,              -- for a pass, starts with a reason code ("logistics: ..."), as `mark --reason`
  unmet        text,              -- Required lines not met, ' || '-joined, as `mark --from-file`
  grade        text,              -- optional human lane grade
  basis        text NOT NULL DEFAULT 'seen' CHECK (basis IN ('blind', 'seen')),
  source       text NOT NULL DEFAULT 'applet',
  submitted_at timestamptz NOT NULL DEFAULT now(),
  applied_at   timestamptz,
  apply_error  text,
  cancelled_at timestamptz
);
CREATE INDEX IF NOT EXISTS decision_queue_pending_idx ON jobs.decision_queue (submitted_at)
  WHERE applied_at IS NULL AND cancelled_at IS NULL AND apply_error IS NULL;
