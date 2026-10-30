-- jobs.fit_snapshot: report gates (2026-10-30, user request; backend/finder/report_gates.py). The objective
-- gates (location) filter the focus report; the judgment gates are shown as gem_gate, never filtered on.
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS us_primary boolean;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS location_ok boolean;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS location_note text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS lens_strong boolean;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS top_jobs text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS gem_gate text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS judge_required text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS judge2_required text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS jev_required text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS two_reviewer_fail boolean;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS screen_reasons text;
