-- Per-lens human grades on a queued call (feedback applet): apply-queue writes each filled one to the DuckDB
-- human_lens_grades table through gold_ingest's lens-sheet path (format F4), the same table blind lens
-- sheets load into. The overall `grade` column stays for `mark --grade` parity; the applet leaves it empty.
ALTER TABLE jobs.decision_queue ADD COLUMN IF NOT EXISTS grade_process   text;
ALTER TABLE jobs.decision_queue ADD COLUMN IF NOT EXISTS grade_technical text;
ALTER TABLE jobs.decision_queue ADD COLUMN IF NOT EXISTS grade_ai        text;
