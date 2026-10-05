-- Table-level privileges for the agent roles (see roles.sql for the roles themselves).
--
-- Idempotent and exact: each run revokes and re-grants inside one transaction, so the result
-- is the same whatever was there before, and other sessions never see a half-applied state.
-- Run it after every migration (deploy/deploy.sh does), as the bootstrap superuser or as
-- agent_migrator (whose sessions act as agent_owner, the owner of every object):
--   docker compose exec -T postgres psql -U agent_migrator -d agent -f - < deploy/sql/grants.sql
--
-- New tables a migration creates get the default privileges below straight away
-- (agent_app: SELECT/INSERT/UPDATE, agent_retention: SELECT/UPDATE/DELETE, agent_web: nothing).
-- The per-table exceptions (events append-only, the viewer's column list) need this file re-run.

\set ON_ERROR_STOP on
BEGIN;

-- agent_app (ingest + worker): read and write app tables, never delete; events is append-only.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agent_app;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM agent_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO agent_app;
SELECT 'REVOKE UPDATE ON events FROM agent_app' WHERE to_regclass('public.events') IS NOT NULL \gexec
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO agent_app;

-- agent_web (citation viewer, progress page, /status): exactly what the pages render. Not pages
-- (the extracted text): a citation carries the sentences around its quote (context_before/after).
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agent_web;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM agent_web;
SELECT format('GRANT SELECT ON %I TO agent_web', t)
FROM unnest(ARRAY['citations', 'documents', 'matters', 'events']) AS t
WHERE to_regclass(format('public.%I', t)) IS NOT NULL \gexec
-- requests: only the progress-page columns, plus provider and reply_sent_at for /status's
-- aggregates (volume by regulator, reply time). No raw MIME hash, auth results, parsed body,
-- subject, thread or outbound ids. (`SELECT *` is therefore denied: name the columns.)
SELECT 'GRANT SELECT (id, track_token, state, progress, result, matter, doc_type, from_addr, '
       'reject_reason, received_at, updated_at, provider, reply_sent_at) ON requests TO agent_web'
WHERE to_regclass('public.requests') IS NOT NULL \gexec

-- agent_retention (purge): find, pseudonymise and delete past retention; log what it did.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agent_retention;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM agent_retention;
GRANT SELECT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agent_retention;
SELECT 'GRANT INSERT ON events TO agent_retention' WHERE to_regclass('public.events') IS NOT NULL \gexec
-- DSAR delete adds the requester to the suppression list in the same run as the purge.
SELECT 'GRANT INSERT ON suppression TO agent_retention' WHERE to_regclass('public.suppression') IS NOT NULL \gexec
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO agent_retention;

-- Nobody but the owner may truncate, reference or trigger; functions are not public by default.
REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM PUBLIC;

-- Default privileges for objects agent_owner creates from now on (i.e. every migration).
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public REVOKE ALL ON TABLES FROM agent_app, agent_web, agent_retention;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public REVOKE ALL ON SEQUENCES FROM agent_app, agent_web, agent_retention;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO agent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public GRANT USAGE ON SEQUENCES TO agent_app;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public GRANT SELECT, UPDATE, DELETE ON TABLES TO agent_retention;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner IN SCHEMA public GRANT USAGE ON SEQUENCES TO agent_retention;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_owner REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

COMMIT;
