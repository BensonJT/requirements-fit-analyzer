-- A human Required-block call on a 'grade' (grades-only) queued row (feedback applet). apply-queue writes it, with
-- the overall `grade`, to the DuckDB report_feedback row through `feedback.record_mark`, so a blind one joins the
-- gold set (vw_report_feedback_blind, judge2.EVAL_SET_SQL). meets | fails only: a human Required call has two
-- values (blind_sheet.REQUIRED_FIT_VALUES); the gold bars score nothing else. Build / pass rows leave it NULL:
-- their Required call comes from the decision itself (`unmet`, a 'requirement' pass), as in `finder.py mark`.
ALTER TABLE jobs.decision_queue ADD COLUMN IF NOT EXISTS required_fit text;
ALTER TABLE jobs.decision_queue DROP CONSTRAINT IF EXISTS decision_queue_required_fit_check;
ALTER TABLE jobs.decision_queue ADD CONSTRAINT decision_queue_required_fit_check
  CHECK (required_fit IS NULL OR required_fit IN ('meets', 'fails'));
