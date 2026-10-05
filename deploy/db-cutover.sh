#!/usr/bin/env bash
# One-step move from "every process connects as the bootstrap superuser" to least-privilege
# roles (deploy/sql/roles.sql). Idempotent: re-running keeps the passwords already in .env.
#
#   deploy/db-cutover.sh [--rotate-superuser]
#
# What it does, in order:
#   1. backs up .env to deploy/env/backups/<UTC stamp>-db-cutover.env (mode 600, gitignored)
#   2. writes one DSN per role into .env (new 256-bit passwords, or the existing ones):
#        DATABASE_URL            agent_app        ingest + worker
#        WEB_DATABASE_URL        agent_web        viewer
#        MIGRATION_DATABASE_URL  agent_migrator   the one-shot migrate service
#        RETENTION_DATABASE_URL  agent_retention  purge job
#        BACKUP_DATABASE_URL     agent_backup     (backup.sh uses the socket; kept for tooling)
#      POSTGRES_PASSWORD stays as the break-glass superuser credential (rotated with
#      --rotate-superuser: do that once, since the old value sat in every app container's env)
#   3. applies roles.sql + grants.sql as the superuser over the container's socket, with each
#      password sent as a SCRAM verifier: no plaintext reaches the server, its logs or argv
#   4. verifies: no agent_* role is superuser, every object belongs to agent_owner, and each
#      role logs in over TCP and passes deploy/sql/verify_grants.sql
#
# Stop ingest/worker/web first (they hold superuser connections and would keep them).
# Overrides for testing against a throwaway server: ENV_FILE, PG_CONTAINER, PG_ADDR, PG_DB.
set -euo pipefail
. "$(dirname "$0")/lib/common.sh"

rotate=0
for arg in "$@"; do
  case $arg in
    --rotate-superuser) rotate=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found"
umask 077
PG_DB=${PG_DB:-agent}
PG_ADDR=${PG_ADDR:-127.0.0.1:5442}
container=$(pg_container)
psql_super -At -c 'SELECT 1' >/dev/null || die "cannot reach Postgres as the superuser via the socket"

backup=$(backup_env db-cutover)
say "backed up $ENV_FILE to $backup"

# Reuse the password in an existing DSN only when that DSN already belongs to the right role
# (the pre-cutover DATABASE_URL belongs to the superuser and is replaced, never reused).
password_for() {  # <env key> <role>
  local dsn; dsn=$(envget "$1")
  if [[ $dsn =~ ^postgres(ql)?://$2: ]]; then printf '%s' "$dsn" | envtool dsn-password
  else envtool gen; fi
}
declare -A key_for=([agent_app]=DATABASE_URL [agent_web]=WEB_DATABASE_URL
  [agent_migrator]=MIGRATION_DATABASE_URL [agent_retention]=RETENTION_DATABASE_URL
  [agent_backup]=BACKUP_DATABASE_URL)
declare -A pw
for role in "${!key_for[@]}"; do pw[$role]=$(password_for "${key_for[$role]}" "$role"); done

# 2. .env first, so a generated password is never lost if a later step fails (re-run to resume).
{
  for role in "${!key_for[@]}"; do
    printf '%s=postgresql://%s:%s@%s/%s\n' "${key_for[$role]}" "$role" "${pw[$role]}" "$PG_ADDR" "$PG_DB"
  done
  printf 'BACKUP_PG_USER=agent_backup\n'
} | envtool set "$ENV_FILE"
say "wrote DATABASE_URL, WEB_/MIGRATION_/RETENTION_/BACKUP_DATABASE_URL and BACKUP_PG_USER to $ENV_FILE"

# 3. roles + grants. \set lines first, then the two files, all on stdin (nothing in argv).
verifier() { printf '%s' "$1" | envtool scram; }
out=$({
  printf "\\\\set migrator_password '%s'\n" "$(verifier "${pw[agent_migrator]}")"
  printf "\\\\set app_password '%s'\n" "$(verifier "${pw[agent_app]}")"
  printf "\\\\set web_password '%s'\n" "$(verifier "${pw[agent_web]}")"
  printf "\\\\set retention_password '%s'\n" "$(verifier "${pw[agent_retention]}")"
  printf "\\\\set backup_password '%s'\n" "$(verifier "${pw[agent_backup]}")"
  cat "$REPO/deploy/sql/roles.sql" "$REPO/deploy/sql/grants.sql"
} | psql_super 2>&1) || { printf '%s\n' "$out" >&2; die "roles.sql/grants.sql failed (re-run after fixing)"; }
[ -z "$out" ] || printf '%s\n' "$out" | sed 's/^/  /'
say "applied deploy/sql/roles.sql and deploy/sql/grants.sql in container ${container}"

if [ "$rotate" = 1 ]; then
  su_pw=$(envtool gen)
  printf 'POSTGRES_PASSWORD=%s\n' "$su_pw" | envtool set "$ENV_FILE"
  printf "\\\\set su_password '%s'\nALTER ROLE :\"PG_SUPERUSER\" PASSWORD :'su_password';\n" \
    "$(verifier "$su_pw")" | psql_super -v PG_SUPERUSER="${PG_SUPERUSER:-agent}"
  say "rotated the bootstrap superuser password (POSTGRES_PASSWORD in $ENV_FILE)"
fi

# 4. verify
say "roles"
psql_super -c "SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls, rolcanlogin,
                      rolconnlimit, array_to_string(rolconfig, ',') AS config,
                      (SELECT string_agg(b.rolname, ',') FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid
                        WHERE m.member = r.oid) AS member_of
               FROM pg_roles r WHERE rolname LIKE 'agent%' ORDER BY rolname"
bad=$(psql_super -At -c "SELECT string_agg(rolname, ' ') FROM pg_roles
                         WHERE rolname LIKE 'agent\_%' AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)")
[ -z "$bad" ] || die "privileged agent_* roles: $bad"
echo "PASS SELECT rolsuper: no agent_* role is superuser/createrole/createdb/bypassrls"
stray=$(psql_super -At -c "SELECT string_agg(c.relname || ':' || pg_get_userbyid(c.relowner), ' ')
                           FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                           WHERE n.nspname = 'public' AND c.relkind IN ('r','p','S','v','m')
                             AND c.relowner <> 'agent_owner'::regrole")
[ -z "$stray" ] || die "objects not owned by agent_owner: $stray"
echo "PASS every table and sequence in public is owned by agent_owner"

if [ "$(psql_super -At -c "SELECT to_regclass('public.requests') IS NOT NULL")" = t ]; then
  ENV_FILE=$ENV_FILE "$REPO/deploy/verify-db-roles.sh" \
    || die "role verification failed (fix and re-run; .env backup: $backup)"
else
  echo "NOTE empty schema (fresh install): run the migrate + db-grants services, then deploy/verify-db-roles.sh"
fi

cat <<EOF

Done. Next:
  deploy/split-env.sh                      # per-service env files from the new .env
  docker compose up -d postgres            # recreate with deploy/postgres/pg_hba.conf (superuser off TCP)
  deploy/verify-db-roles.sh                # now also proves the superuser is refused over TCP
Rollback: cp $backup $ENV_FILE && deploy/split-env.sh (the roles can stay; unused until the app uses them).
EOF
