-- Proves the role model by doing, as the connected role, what that role's process really does,
-- and confirming that what it must never do is refused with insufficient_privilege (42501).
--
-- Run once per role, connected AS that role (deploy/verify-db-roles.sh does this over TCP with
-- each DSN, so passwords and pg_hba are exercised too). Everything happens inside one
-- transaction that is rolled back: safe against the live database.
--
-- Prints PASS/FAIL lines; exits non-zero (ON_ERROR_STOP) if any check failed.

\set ON_ERROR_STOP on
\set QUIET on
BEGIN;
-- never queue behind live traffic: fail fast instead of blocking the app
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

DO $verify$
DECLARE
  c record;
  allowed boolean;
  sqlstate_seen text;
  failures int := 0;
  checks int := 0;
  -- rows: (role, kind, statement, expected)
  --   kind 'run'    -> execute; expected true = must succeed, false = must fail with 42501
  --   kind 'assert' -> SELECT <expr>; must return true
BEGIN
  IF (SELECT rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb FROM pg_roles
      WHERE rolname = session_user) THEN
    RAISE WARNING 'FAIL % has SUPERUSER/BYPASSRLS/CREATEROLE/CREATEDB', session_user;
    failures := failures + 1;
  ELSE
    RAISE NOTICE 'PASS % is not superuser, cannot create roles or databases, does not bypass RLS', session_user;
  END IF;

  FOR c IN SELECT * FROM (VALUES
    -- ---------------------------------------------------------------- agent_app (ingest + worker)
    ('agent_app', 'run', $$INSERT INTO requests (message_id, track_token, raw_sha256, from_addr, subject)
                           VALUES ('<verify-grants@invalid>', 'verifygrants0000', repeat('0', 64), 'verify@example.invalid', 'grant check')$$, true),
    ('agent_app', 'run', $$UPDATE requests SET state = 'accepted', progress = '{"step": "x"}', updated_at = now()
                           WHERE message_id = '<verify-grants@invalid>'$$, true),
    ('agent_app', 'run', $$UPDATE requests SET attempts = attempts + 1 WHERE message_id = '<verify-grants@invalid>' RETURNING attempts$$, true),
    ('agent_app', 'run', $$INSERT INTO events (request_id, kind, data)
                           SELECT id, 'verify', '{}' FROM requests WHERE message_id = '<verify-grants@invalid>'$$, true),
    ('agent_app', 'run', $$SELECT kind, data, at FROM events ORDER BY id DESC LIMIT 5$$, true),
    ('agent_app', 'run', $$INSERT INTO matters (provider, matter, info, fetched_at) VALUES ('verify', 'V1', '{}', now())
                           ON CONFLICT (provider, matter) DO UPDATE SET info = EXCLUDED.info, fetched_at = EXCLUDED.fetched_at$$, true),
    ('agent_app', 'run', $$INSERT INTO documents (provider, matter, doc_type, external_id, title)
                           VALUES ('verify', 'V1', 'Other Documents', 'verify-1', 't') ON CONFLICT DO NOTHING$$, true),
    ('agent_app', 'run', $$UPDATE documents SET page_count = 1 WHERE external_id = 'verify-1'$$, true),
    ('agent_app', 'run', $$INSERT INTO pages (sha256, page, text) VALUES (repeat('f', 64), 1, 'x') ON CONFLICT DO NOTHING$$, true),
    ('agent_app', 'run', $$INSERT INTO summaries (key, summary, claims) VALUES ('verify', 's', '[]') ON CONFLICT (key) DO NOTHING$$, true),
    ('agent_app', 'run', $$INSERT INTO citations (id, request_id, document_id, page, quote, char_start, char_end, claim)
                           SELECT 'verifygrants', r.id, d.id, 1, 'q', 0, 1, 'c'
                           FROM requests r, documents d WHERE r.message_id = '<verify-grants@invalid>' AND d.external_id = 'verify-1'$$, true),
    ('agent_app', 'run', $$SELECT pg_advisory_xact_lock(7342999)$$, true),
    ('agent_app', 'run', $$UPDATE events SET kind = kind$$, false),
    ('agent_app', 'run', $$DELETE FROM events$$, false),
    ('agent_app', 'run', $$DELETE FROM requests WHERE message_id = '<verify-grants@invalid>'$$, false),
    ('agent_app', 'run', $$TRUNCATE requests$$, false),
    ('agent_app', 'run', $$CREATE TABLE verify_grants_ddl (id int)$$, false),
    ('agent_app', 'run', $$CREATE TEMP TABLE verify_grants_tmp (id int)$$, false),
    ('agent_app', 'run', $$ALTER TABLE requests ADD COLUMN verify_grants int$$, false),
    ('agent_app', 'run', $$CREATE INDEX IF NOT EXISTS requests_state_idx ON requests (state, updated_at)$$, false),
    ('agent_app', 'run', $$DROP TABLE events$$, false),
    ('agent_app', 'run', $$SET ROLE agent_owner$$, false),
    ('agent_app', 'run', $$SELECT pg_read_file('/etc/passwd')$$, false),
    ('agent_app', 'run', $$COPY (SELECT 1) TO PROGRAM 'id'$$, false),
    ('agent_app', 'run', $$ALTER ROLE agent_app CREATEROLE$$, false),

    -- ---------------------------------------------------------------- agent_web (viewer, progress page, /status)
    ('agent_web', 'run', $$SELECT c.id, c.claim, c.quote, c.page, c.context_before, c.context_after, d.id, d.provider,
                                  d.matter, d.doc_type, d.external_id, d.title, d.filed_on, d.page_count, d.filename,
                                  m.info->>'title', m.info->>'portal_url'
                           FROM citations c JOIN documents d ON d.id = c.document_id
                           LEFT JOIN matters m ON m.provider = d.provider AND m.matter = d.matter LIMIT 1$$, true),
    ('agent_web', 'run', $$SELECT external_id, sha256, filename FROM documents LIMIT 1$$, true),
    ('agent_web', 'run', $$SELECT id, matter, doc_type, from_addr, state, progress, result, reject_reason, received_at, updated_at
                           FROM requests WHERE track_token = 'nonexistent-token'$$, true),
    ('agent_web', 'run', $$SELECT kind, data, at FROM events WHERE request_id IS NULL ORDER BY id LIMIT 1$$, true),
    -- /status (agent/web/status.py): aggregates over these columns only
    ('agent_web', 'run', $$SELECT state, provider, count(*), count(reply_sent_at),
                                  percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM reply_sent_at - received_at)::float8)
                           FROM requests WHERE received_at >= now() - interval '7 days' GROUP BY state, provider$$, true),
    ('agent_web', 'run', $$SELECT count(*) FILTER (WHERE data->>'escalated' = 'true') FROM events
                           WHERE kind = 'llm.call' AND at >= now() - interval '7 days'$$, true),
    ('agent_web', 'run', $$SELECT message_id, thread_root FROM requests LIMIT 1$$, false),
    ('agent_web', 'run', $$SELECT attempts, sender_h, from_h FROM requests LIMIT 1$$, false),
    ('agent_web', 'run', $$SELECT * FROM requests LIMIT 1$$, false),
    ('agent_web', 'run', $$SELECT auth FROM requests LIMIT 1$$, false),
    ('agent_web', 'run', $$SELECT subject, parsed FROM requests LIMIT 1$$, false),
    ('agent_web', 'run', $$SELECT raw_sha256 FROM requests LIMIT 1$$, false),
    ('agent_web', 'run', $$SELECT text FROM pages LIMIT 1$$, false),
    ('agent_web', 'run', $$SELECT claims FROM summaries LIMIT 1$$, false),
    ('agent_web', 'run', $$INSERT INTO events (kind) VALUES ('verify')$$, false),
    ('agent_web', 'run', $$UPDATE requests SET state = state$$, false),
    ('agent_web', 'run', $$DELETE FROM citations$$, false),
    ('agent_web', 'run', $$CREATE TABLE verify_grants_ddl (id int)$$, false),

    -- ---------------------------------------------------------------- agent_retention (purge)
    -- `AND false`: privilege checks run, but no live row is touched
    ('agent_retention', 'run', $$UPDATE requests SET from_addr = from_addr, subject = '' WHERE received_at < now() - interval '90 days' AND false$$, true),
    ('agent_retention', 'run', $$DELETE FROM requests WHERE received_at < now() - interval '400 days' AND false$$, true),
    ('agent_retention', 'run', $$DELETE FROM events WHERE at < now() - interval '400 days' AND false$$, true),
    ('agent_retention', 'run', $$UPDATE documents SET sha256 = sha256 WHERE false$$, true),
    ('agent_retention', 'run', $$DELETE FROM pages WHERE false$$, true),
    ('agent_retention', 'run', $$INSERT INTO events (kind, data) VALUES ('retention.verify', '{}')$$, true),
    ('agent_retention', 'run', $$INSERT INTO requests (message_id, track_token, raw_sha256, from_addr)
                                 VALUES ('<retention@invalid>', 'retentionverify0', 'x', 'x@invalid')$$, false),
    ('agent_retention', 'run', $$TRUNCATE events$$, false),
    ('agent_retention', 'run', $$CREATE TABLE verify_grants_ddl (id int)$$, false),
    ('agent_retention', 'run', $$DROP TABLE summaries$$, false),

    -- ---------------------------------------------------------------- agent_backup (pg_dump)
    ('agent_backup', 'run', $$SELECT count(*) FROM requests$$, true),
    ('agent_backup', 'run', $$SELECT count(*) FROM pages$$, true),
    ('agent_backup', 'run', $$SELECT last_value FROM events_id_seq$$, true),
    ('agent_backup', 'run', $$INSERT INTO events (kind) VALUES ('verify')$$, false),
    ('agent_backup', 'run', $$UPDATE requests SET state = state$$, false),
    ('agent_backup', 'run', $$DELETE FROM summaries$$, false),
    ('agent_backup', 'run', $$CREATE TABLE verify_grants_ddl (id int)$$, false),

    -- ---------------------------------------------------------------- agent_migrator (acts as agent_owner)
    ('agent_migrator', 'assert', $$current_user = 'agent_owner'$$, true),
    ('agent_migrator', 'run', $$CREATE TABLE verify_grants_ddl (id bigserial PRIMARY KEY, note text)$$, true),
    ('agent_migrator', 'assert', $$(SELECT relowner = 'agent_owner'::regrole FROM pg_class WHERE oid = 'verify_grants_ddl'::regclass)$$, true),
    ('agent_migrator', 'assert', $$has_table_privilege('agent_app', 'verify_grants_ddl', 'SELECT') AND has_table_privilege('agent_app', 'verify_grants_ddl', 'INSERT')
                                    AND has_table_privilege('agent_app', 'verify_grants_ddl', 'UPDATE')$$, true),
    ('agent_migrator', 'assert', $$NOT has_table_privilege('agent_app', 'verify_grants_ddl', 'DELETE')$$, true),
    ('agent_migrator', 'assert', $$has_sequence_privilege('agent_app', 'verify_grants_ddl_id_seq', 'USAGE')$$, true),
    ('agent_migrator', 'assert', $$NOT has_table_privilege('agent_web', 'verify_grants_ddl', 'SELECT')$$, true),
    ('agent_migrator', 'assert', $$has_table_privilege('agent_retention', 'verify_grants_ddl', 'DELETE')$$, true),
    ('agent_migrator', 'run', $$ALTER TABLE verify_grants_ddl ADD COLUMN IF NOT EXISTS extra int$$, true),
    ('agent_migrator', 'run', $$CREATE INDEX IF NOT EXISTS verify_grants_idx ON verify_grants_ddl (extra)$$, true),
    ('agent_migrator', 'run', $$DROP TABLE verify_grants_ddl$$, true),
    -- existing tables are owned by the role the migrator acts as, so ALTER on them works
    -- (proved without taking a lock on a live table)
    ('agent_migrator', 'assert', $$(SELECT bool_and(relowner = current_user::regrole) FROM pg_class
                                    WHERE oid IN ('requests'::regclass, 'events'::regclass, 'citations'::regclass))$$, true),
    ('agent_migrator', 'run', $$CREATE ROLE verify_grants_role$$, false),
    ('agent_migrator', 'run', $$SELECT pg_read_file('/etc/passwd')$$, false),
    ('agent_migrator', 'run', $$ALTER ROLE agent_app SUPERUSER$$, false),

    -- ---------------------------------------------------------------- audit trail (migration 005 onwards)
    -- events is append-only by trigger too; rows leave only through purge_events() (owner,
    -- SECURITY DEFINER), which only the retention role may call.
    ('agent_retention', 'assert', $$to_regprocedure('purge_events(interval,integer)') IS NULL
                                    OR has_function_privilege('agent_retention', 'purge_events(interval,integer)', 'EXECUTE')$$, true),
    ('agent_app', 'assert', $$to_regprocedure('purge_events(interval,integer)') IS NULL
                              OR NOT has_function_privilege('agent_app', 'purge_events(interval,integer)', 'EXECUTE')$$, true),
    ('*', 'assert', $$to_regprocedure('events_append_only()') IS NULL
                      OR EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'events'::regclass
                                 AND tgname = 'events_append_only' AND tgenabled <> 'D')$$, true),

    -- ---------------------------------------------------------------- everyone: the role model itself
    ('*', 'assert', $$NOT EXISTS (SELECT FROM pg_roles WHERE rolname LIKE 'agent\_%' AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb))$$, true),
    ('*', 'assert', $$NOT (SELECT rolcanlogin FROM pg_roles WHERE rolname = 'agent_owner')$$, true),
    ('*', 'assert', $$NOT EXISTS (SELECT FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                                  WHERE n.nspname = 'public' AND c.relkind IN ('r','p','S','v','m')
                                    AND c.relowner <> 'agent_owner'::regrole)$$, true),
    ('*', 'assert', $$NOT EXISTS (SELECT FROM pg_auth_members m WHERE m.roleid = 'agent_owner'::regrole
                                    AND m.member <> 'agent_migrator'::regrole)$$, true)
  ) AS t(role, kind, stmt, expected)
  WHERE role IN (session_user::text, '*')
  LOOP
    checks := checks + 1;
    sqlstate_seen := NULL;
    IF c.kind = 'assert' THEN
      EXECUTE 'SELECT ' || c.stmt INTO allowed;
      IF allowed THEN
        RAISE NOTICE 'PASS % assert: %', session_user, regexp_replace(c.stmt, '\s+', ' ', 'g');
      ELSE
        failures := failures + 1;
        RAISE WARNING 'FAIL % assert: %', session_user, regexp_replace(c.stmt, '\s+', ' ', 'g');
      END IF;
      CONTINUE;
    END IF;
    BEGIN
      EXECUTE c.stmt;
      allowed := true;
    EXCEPTION WHEN insufficient_privilege THEN
      allowed := false;
      sqlstate_seen := SQLERRM;
    END;
    IF allowed = c.expected THEN
      RAISE NOTICE 'PASS % % %', session_user, CASE WHEN c.expected THEN 'allowed:' ELSE 'denied: ' END,
        left(regexp_replace(c.stmt, '\s+', ' ', 'g'), 110);
    ELSE
      failures := failures + 1;
      RAISE WARNING 'FAIL % expected %, got %: % %', session_user,
        CASE WHEN c.expected THEN 'allowed' ELSE 'denied' END,
        CASE WHEN allowed THEN 'allowed' ELSE 'denied' END,
        left(regexp_replace(c.stmt, '\s+', ' ', 'g'), 110), coalesce('(' || sqlstate_seen || ')', '');
    END IF;
  END LOOP;

  IF checks < 5 THEN
    RAISE EXCEPTION 'no checks defined for role %', session_user;
  END IF;
  IF failures > 0 THEN
    RAISE EXCEPTION '% of % grant checks failed for %', failures, checks, session_user;
  END IF;
  RAISE NOTICE 'OK % : % checks passed', session_user, checks;
END
$verify$;

ROLLBACK;
