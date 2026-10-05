-- SOC 2 application controls: an append-only, attributable audit trail; pseudonymous subject
-- ids; retention bookkeeping; the suppression list. Idempotent: safe to run on every deploy.

-- ---------------------------------------------------------------- requests and documents

-- HMAC-SHA256 of the sender address (AUDIT_HMAC_KEY), set at ingest. Audit events, DSAR lookups
-- and retention find a person's records by it, also after the address itself is gone.
ALTER TABLE requests ADD COLUMN IF NOT EXISTS from_h text;
ALTER TABLE requests ADD COLUMN IF NOT EXISTS pseudonymised_at timestamptz;
CREATE INDEX IF NOT EXISTS requests_from_h_idx ON requests (from_h);
CREATE INDEX IF NOT EXISTS requests_received_idx ON requests (received_at);

-- When a request last used a stored file: blobs and page text are disposed of after
-- blob_retention_days without use.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS last_used_at timestamptz;
UPDATE documents SET last_used_at = coalesce(downloaded_at, listed_at) WHERE last_used_at IS NULL;
CREATE INDEX IF NOT EXISTS documents_last_used_idx ON documents (last_used_at) WHERE sha256 IS NOT NULL;

-- ---------------------------------------------------------------- suppression list

-- Senders the gate drops before doing anything else: a data subject who asked for deletion or
-- objected (DSAR), or an address/domain an operator blocked. Only HMACs are kept.
CREATE TABLE IF NOT EXISTS suppression (
    value_h     text PRIMARY KEY,                    -- HMAC of the address, or of '@domain'
    kind        text NOT NULL CHECK (kind IN ('address', 'domain')),
    reason      text NOT NULL,                       -- dsar_delete | dsar_email | blocked
    erase       boolean NOT NULL DEFAULT false,      -- also erase what we hold (re-applied by purge)
    note        text,
    created_by  text NOT NULL DEFAULT current_user,
    created_at  timestamptz NOT NULL DEFAULT now(),
    erased_at   timestamptz
);

-- ---------------------------------------------------------------- events: the audit trail

ALTER TABLE events ADD COLUMN IF NOT EXISTS actor text;          -- database role (trigger-set)
ALTER TABLE events ADD COLUMN IF NOT EXISTS component text;      -- ingest | worker | web | cli | cron
ALTER TABLE events ADD COLUMN IF NOT EXISTS app_version text;
ALTER TABLE events ADD COLUMN IF NOT EXISTS subject_h text;      -- HMAC of the requester, never the address
CREATE INDEX IF NOT EXISTS events_subject_idx ON events (subject_h, id) WHERE subject_h IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_at_idx ON events (at);

-- The audit trail outlives the request it describes: no foreign key, so deleting a request
-- (retention, DSAR) neither cascades into nor is blocked by its events.
DO $$
DECLARE c record;
BEGIN
  FOR c IN SELECT conname FROM pg_constraint WHERE conrelid = 'events'::regclass AND contype = 'f' LOOP
    EXECUTE format('ALTER TABLE events DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

-- Who wrote an event is not the writer's to claim: actor is always the session's role, and an
-- event about a request is tied to that request's subject.
CREATE OR REPLACE FUNCTION events_stamp() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.actor := current_user;
  IF NEW.subject_h IS NULL AND NEW.request_id IS NOT NULL THEN
    NEW.subject_h := (SELECT from_h FROM requests WHERE id = NEW.request_id);
  END IF;
  NEW.component := coalesce(NEW.component, nullif(current_setting('agent.component', true), ''));
  NEW.app_version := coalesce(NEW.app_version, nullif(current_setting('agent.app_version', true), ''));
  RETURN NEW;
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'events'::regclass AND tgname = 'events_stamp') THEN
    CREATE TRIGGER events_stamp BEFORE INSERT ON events FOR EACH ROW EXECUTE FUNCTION events_stamp();
  END IF;
END $$;

-- Append-only: UPDATE and DELETE are refused for every role (the grants already deny them to the
-- app roles; this also binds agent_retention and anything with a table-wide grant). The only
-- way rows leave is purge_events() below, which runs as the table owner with the maintenance
-- flag set for the duration of the call.
CREATE OR REPLACE FUNCTION events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF current_setting('agent.events_maintenance', true) = 'on'
     AND current_user = (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = TG_RELID) THEN
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
  END IF;
  RAISE EXCEPTION 'events is append-only: % refused', TG_OP USING ERRCODE = 'insufficient_privilege';
END $$;
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'events'::regclass AND tgname = 'events_append_only') THEN
    CREATE TRIGGER events_append_only BEFORE UPDATE OR DELETE ON events
      FOR EACH ROW EXECUTE FUNCTION events_append_only();
  END IF;
END $$;

-- Disposal past the audit retention period (never less than 400 days), in bounded batches.
CREATE OR REPLACE FUNCTION purge_events(older_than interval, max_rows integer DEFAULT 5000)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, public
SET agent.events_maintenance = 'on'
AS $$
DECLARE n bigint;
BEGIN
  IF older_than < interval '400 days' THEN
    RAISE EXCEPTION 'audit events are kept at least 400 days (asked for %)', older_than;
  END IF;
  DELETE FROM public.events WHERE id IN (
    SELECT id FROM public.events WHERE at < now() - older_than ORDER BY id LIMIT greatest(max_rows, 0));
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;
REVOKE ALL ON FUNCTION purge_events(interval, integer) FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_retention') THEN
    GRANT EXECUTE ON FUNCTION purge_events(interval, integer) TO agent_retention;
  END IF;
END $$;

-- Events written before this migration carried the sender's raw address in `received`; keep
-- the event, drop the address (the request row still has it until it is pseudonymised).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM events WHERE kind = 'received' AND data ? 'from') THEN
    SET LOCAL agent.events_maintenance = 'on';
    UPDATE events SET data = data - 'from' WHERE kind = 'received' AND data ? 'from';
    SET LOCAL agent.events_maintenance = 'off';
  END IF;
END $$;
