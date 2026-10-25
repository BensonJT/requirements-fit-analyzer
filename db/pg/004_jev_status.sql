-- jobs.fit_snapshot: Jev's call on each row (2026-10-25, user request; backend/finder/jev_status.py).
-- jev_result: Demoted | Check Required | Kept | Not acted on | No review yet | Not judged.
-- jev_note: Jev's reason, or for a Not judged row the gate that kept it out of Jev's subset.
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS jev_result text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS jev_note text;
