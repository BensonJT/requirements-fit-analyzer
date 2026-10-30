-- jobs.fit_snapshot: bridge-track fields (2026-10-30, user request: the bridge roles report). NULL on fit rows.
-- pay_low / pay_high / pay_unit: the structured pay, else the first pay statement in the JD (bridge.pay_from_text).
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS track text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS bridge_place text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS employment_type text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS voice text;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS pay_low numeric;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS pay_high numeric;
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS pay_unit text;
