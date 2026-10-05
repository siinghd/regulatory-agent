-- Least-privilege Postgres roles for the regulatory agent (SOC 2 CC6.1, CC6.3).
--
--   agent_owner      NOLOGIN. Owns every table and sequence; nobody logs in as it.
--   agent_migrator   LOGIN, member of agent_owner. Its sessions start as agent_owner
--                    (ALTER ROLE ... SET role), so whatever a migration creates is owned by
--                    agent_owner and picks up the default privileges in grants.sql.
--   agent_app        ingest + worker: SELECT/INSERT/UPDATE on app tables, events append-only.
--   agent_web        viewer + progress page: read-only, only what the pages render.
--   agent_retention  purge job: SELECT/UPDATE/DELETE to pseudonymise and delete past retention.
--   agent_backup     pg_dump: pg_read_all_data and nothing else.
--   agent            the bootstrap superuser: break-glass only (pg_hba rejects it over TCP,
--                    so it is reachable only through `docker compose exec postgres`).
--
-- Idempotent: creates what is missing and re-asserts every attribute on every run. Run as the
-- bootstrap superuser, normally through deploy/db-cutover.sh, which passes each password as a
-- psql variable holding a SCRAM-SHA-256 verifier, so the plaintext never reaches the server,
-- its logs (log_statement=ddl) or the process list:
--   psql -v migrator_password=... -v app_password=... -v web_password=...
--        -v retention_password=... -v backup_password=... -d agent -f roles.sql -f grants.sql

\set ON_ERROR_STOP on
\if :{?migrator_password} \else \warn 'roles.sql: -v migrator_password=<SCRAM verifier> is required' \q \endif
\if :{?app_password} \else \warn 'roles.sql: -v app_password=<SCRAM verifier> is required' \q \endif
\if :{?web_password} \else \warn 'roles.sql: -v web_password=<SCRAM verifier> is required' \q \endif
\if :{?retention_password} \else \warn 'roles.sql: -v retention_password=<SCRAM verifier> is required' \q \endif
\if :{?backup_password} \else \warn 'roles.sql: -v backup_password=<SCRAM verifier> is required' \q \endif

DO $$ BEGIN
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    RAISE EXCEPTION 'roles.sql must run as the bootstrap superuser (connected as %)', current_user;
  END IF;
END $$;

BEGIN;

-- 1. Roles: create if missing, then force every attribute.
SELECT format('CREATE ROLE %I', r)
FROM unnest(ARRAY['agent_owner', 'agent_migrator', 'agent_app', 'agent_web',
                  'agent_retention', 'agent_backup']) AS r
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) \gexec

ALTER ROLE agent_owner     NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                           INHERIT PASSWORD NULL;
ALTER ROLE agent_migrator  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                           INHERIT CONNECTION LIMIT 5 PASSWORD :'migrator_password';
ALTER ROLE agent_app       LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                           INHERIT CONNECTION LIMIT 60 PASSWORD :'app_password';
ALTER ROLE agent_web       LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                           INHERIT CONNECTION LIMIT 20 PASSWORD :'web_password';
ALTER ROLE agent_retention LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                           INHERIT CONNECTION LIMIT 3 PASSWORD :'retention_password';
ALTER ROLE agent_backup    LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                           INHERIT CONNECTION LIMIT 3 PASSWORD :'backup_password';

-- The migrator acts as the owner; nothing else may.
GRANT agent_owner TO agent_migrator;
ALTER ROLE agent_migrator SET role = 'agent_owner';
GRANT pg_read_all_data TO agent_backup;

-- Custom settings that migrations bind into SECURITY DEFINER functions (`CREATE FUNCTION ...
-- SET agent.events_maintenance = 'on'`, migration 005: purge_events). Postgres lets only a
-- superuser or a role granted SET on the parameter do that.
GRANT SET ON PARAMETER agent.events_maintenance TO agent_owner;

-- 2. Database and schema: only these roles connect, only the owner creates objects, and no
-- role gets TEMP (REVOKE ALL from PUBLIC removes it).
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"DBNAME"
  TO agent_migrator, agent_app, agent_web, agent_retention, agent_backup;
SELECT 'REVOKE CONNECT ON DATABASE postgres FROM PUBLIC'
WHERE EXISTS (SELECT FROM pg_database WHERE datname = 'postgres') \gexec
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO agent_owner;
GRANT USAGE ON SCHEMA public TO agent_app, agent_web, agent_retention, agent_backup;

-- 3. Existing objects (created by the superuser before this cutover) move to agent_owner.
-- Indexes and column-owned sequences follow their table. Extension members are left alone.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.oid::regclass AS obj,
           CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                          WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END AS kind
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
      AND c.relowner <> 'agent_owner'::regrole
      AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                      AND d.objid = c.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO agent_owner', r.kind, r.obj);
    RAISE NOTICE 'owner: % -> agent_owner', r.obj;
  END LOOP;
  FOR r IN  -- standalone sequences (serial/identity sequences moved with their table above)
    SELECT c.oid::regclass AS obj
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'S' AND c.relowner <> 'agent_owner'::regrole
      AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_class'::regclass
                      AND d.objid = c.oid AND d.deptype IN ('a', 'i', 'e'))
  LOOP
    EXECUTE format('ALTER SEQUENCE %s OWNER TO agent_owner', r.obj);
    RAISE NOTICE 'owner: % -> agent_owner', r.obj;
  END LOOP;
  FOR r IN
    SELECT p.oid::regprocedure AS obj,
           CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END AS kind
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public' AND p.proowner <> 'agent_owner'::regrole
      AND NOT EXISTS (SELECT FROM pg_depend d WHERE d.classid = 'pg_proc'::regclass
                      AND d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO agent_owner', r.kind, r.obj);
    RAISE NOTICE 'owner: % -> agent_owner', r.obj;
  END LOOP;
END $$;

COMMIT;

-- 4. Table and default privileges live in grants.sql (re-run after every migration);
-- db-cutover.sh feeds it to psql straight after this file.
