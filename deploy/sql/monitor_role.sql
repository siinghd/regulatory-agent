-- Read-only monitoring login for postgres_exporter (docker-compose.yml service postgres-exporter,
-- profile db-exporters; deploy/observability/README.md "After the cutover").
--
--   agent_monitor    LOGIN, member of pg_monitor only: pg_stat_* views, pg_settings, database
--                    and table sizes, locks, replication state. No table data (unlike
--                    agent_backup's pg_read_all_data), no writes, read-only transactions.
--
-- Not part of roles.sql on purpose: apply it at (or after) the least-privilege cutover, as the
-- bootstrap superuser, with the password as a SCRAM-SHA-256 verifier (the same convention as
-- roles.sql, so the plaintext never reaches the server, its logs or the process list):
--   psql -v monitor_password='SCRAM-SHA-256$4096:...' -d agent -f monitor_role.sql
-- Then add agent_monitor to the `host agent ...` line of deploy/postgres/pg_hba.conf and reload
-- Postgres (SELECT pg_reload_conf()); until then pg_hba rejects it. Idempotent.

\set ON_ERROR_STOP on
\if :{?monitor_password} \else \warn 'monitor_role.sql: -v monitor_password=<SCRAM verifier> is required' \q \endif

DO $$ BEGIN
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    RAISE EXCEPTION 'monitor_role.sql must run as the bootstrap superuser (connected as %)', current_user;
  END IF;
END $$;

BEGIN;

SELECT 'CREATE ROLE agent_monitor'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_monitor') \gexec

ALTER ROLE agent_monitor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS
                         INHERIT CONNECTION LIMIT 3 PASSWORD :'monitor_password';
ALTER ROLE agent_monitor SET default_transaction_read_only = on;
ALTER ROLE agent_monitor SET statement_timeout = '10s';

GRANT pg_monitor TO agent_monitor;
GRANT CONNECT ON DATABASE :"DBNAME" TO agent_monitor;

COMMIT;
