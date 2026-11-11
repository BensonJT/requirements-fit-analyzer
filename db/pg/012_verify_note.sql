-- jobs.fit_snapshot.verify_note (2026-11-11, the user: tracker #570). A SuccessFactors career site can keep
-- listing a requisition that is closed behind it; backend/ats/listing_age.py flags listings old enough to be one,
-- and the focus report prints the note so the user clicks Apply before building. Idempotent.
ALTER TABLE jobs.fit_snapshot ADD COLUMN IF NOT EXISTS verify_note text;
