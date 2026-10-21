-- Read-only role for the MCP free-SQL tool. No password here: it is set by hand
-- (ALTER ROLE claude_ro PASSWORD ...) and kept in ~/.pgpass, never in the repo.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claude_ro') THEN
    CREATE ROLE claude_ro LOGIN;
  END IF;
END $$;

DO $$
DECLARE s text;
BEGIN
  FOREACH s IN ARRAY ARRAY['tracker', 'jobs', 'resume'] LOOP
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = s) THEN
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO claude_ro', s);
      EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO claude_ro', s);
      EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT SELECT ON TABLES TO claude_ro', s);
    END IF;
  END LOOP;
END $$;
