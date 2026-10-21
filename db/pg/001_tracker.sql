-- Schema `tracker`: applications and their history (design DESIGN_tracker_db.md section 3).
-- Idempotent: safe to run on every migrate.
CREATE SCHEMA IF NOT EXISTS tracker;

CREATE TABLE IF NOT EXISTS tracker.status (
  status     text PRIMARY KEY,
  is_active  boolean NOT NULL,
  sort_order int NOT NULL          -- render order inside Active / Closed
);
INSERT INTO tracker.status (status, is_active, sort_order) VALUES
  ('offer', true, 1), ('interview', true, 2), ('screen', true, 3), ('applied', true, 4), ('built', true, 5),
  ('accepted', false, 10), ('declined', false, 11), ('rejected', false, 12), ('assumed-rejected', false, 13),
  ('withdrawn', false, 14), ('passed', false, 15), ('closed-unsent', false, 16)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS tracker.application (
  app_id          serial PRIMARY KEY,
  posting_id      text UNIQUE,                    -- 20-hex DuckDB id; NULL = not in our DB
  employer_job_id text,                           -- the employer's own req / job id
  company         text NOT NULL,
  role            text NOT NULL,                  -- the title, without location / pay / req noise
  location        text,
  pay_text        text,                           -- as posted
  source          text NOT NULL DEFAULT 'manual'
                  CHECK (source IN ('pipeline','linkedin','recruiter','referral','board','manual','migrated')),
  folder          text UNIQUE,                    -- vault-relative application folder
  status          text NOT NULL REFERENCES tracker.status,
  status_date     date NOT NULL,
  date_applied    date,
  closes_on       date,
  next_action     text,
  follow_up_due   date,
  close_reason    text,
  notes           text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (posting_id IS NULL OR posting_id ~ '^[0-9a-f]{20}$')
);
CREATE INDEX IF NOT EXISTS application_company_req_idx
  ON tracker.application (lower(company), employer_job_id);

CREATE TABLE IF NOT EXISTS tracker.event (        -- append-only history; never UPDATE or DELETE
  event_id    bigserial PRIMARY KEY,
  app_id      int NOT NULL REFERENCES tracker.application ON DELETE CASCADE,
  at          timestamptz NOT NULL DEFAULT now(),
  kind        text NOT NULL CHECK (kind IN ('created','status','note','contact','field','migrated')),
  status      text REFERENCES tracker.status,     -- set when kind = 'status' (and 'created')
  status_date date,
  field       text,                                -- set when kind = 'field'
  old_value   text,
  new_value   text,
  note        text,
  actor       text NOT NULL                        -- 'user' | 'claude-code' | 'claude-desktop' | 'migration' | 'pipeline'
);
CREATE INDEX IF NOT EXISTS event_app_at_idx ON tracker.event (app_id, at);

CREATE TABLE IF NOT EXISTS tracker.contact (
  contact_id serial PRIMARY KEY,
  name       text NOT NULL,
  title      text,
  company    text,
  email      text,
  phone      text,
  linkedin   text,
  notes      text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tracker.app_contact (
  app_id     int NOT NULL REFERENCES tracker.application ON DELETE CASCADE,
  contact_id int NOT NULL REFERENCES tracker.contact ON DELETE CASCADE,
  role       text NOT NULL DEFAULT 'recruiter'
             CHECK (role IN ('recruiter','hiring_manager','panelist','referral','staffing','other')),
  PRIMARY KEY (app_id, contact_id, role)
);

CREATE TABLE IF NOT EXISTS tracker.network (       -- consultant networks; bids get their own table later
  network_id    serial PRIMARY KEY,
  platform      text NOT NULL UNIQUE,
  date_accepted date,
  status        text NOT NULL,
  profile_url   text,
  notes         text
);

CREATE TABLE IF NOT EXISTS tracker.application_bullet (   -- phase 2; created empty now
  app_id        int NOT NULL REFERENCES tracker.application ON DELETE CASCADE,
  bullet_id     int NOT NULL,                      -- resume.master_bullets.id (no FK in phase 1)
  block         text,
  position      int NOT NULL,
  page          int,
  tailored_text text,
  sent_text     text,
  edited        boolean,
  recorded_at   timestamptz,
  PRIMARY KEY (app_id, bullet_id)
);

CREATE TABLE IF NOT EXISTS tracker.render_log (    -- guards the generated view against silent overwrite
  rendered_at timestamptz NOT NULL DEFAULT now(),
  path        text NOT NULL,
  sha256      text NOT NULL
);

-- a.* is expanded at creation; adding application columns later means DROP + CREATE of this view
CREATE OR REPLACE VIEW tracker.vw_applications AS
  SELECT a.*, s.is_active, s.sort_order,
         (SELECT string_agg(c.name || ' (' || ac.role || ')', '; ' ORDER BY c.name)
            FROM tracker.app_contact ac JOIN tracker.contact c USING (contact_id)
           WHERE ac.app_id = a.app_id) AS contacts
    FROM tracker.application a JOIN tracker.status s USING (status);
