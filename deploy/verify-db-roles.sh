#!/usr/bin/env bash
# Log in as each agent role over TCP (real password, real pg_hba) and run
# deploy/sql/verify_grants.sql: typical queries must work, forbidden ones must be refused.
# Read-only in effect: every check runs in a transaction that is rolled back.
#
#   deploy/verify-db-roles.sh            all roles found in .env
#   deploy/verify-db-roles.sh DATABASE_URL WEB_DATABASE_URL
set -euo pipefail
. "$(dirname "$0")/lib/common.sh"

keys=("$@")
[ ${#keys[@]} -gt 0 ] || keys=(DATABASE_URL WEB_DATABASE_URL MIGRATION_DATABASE_URL RETENTION_DATABASE_URL BACKUP_DATABASE_URL)

fail=0
for key in "${keys[@]}"; do
  if [ -z "$(envget "$key")" ]; then
    echo "FAIL $key is not set in $ENV_FILE"; fail=1; continue
  fi
  say "$key"
  if ! psql_dsn "$key" -q -f /sql/verify_grants.sql 2>&1 | sed -E 's/^(psql:[^ ]+ )?(NOTICE|WARNING|ERROR): +/  /'; then
    fail=1
  fi
done

# The superuser must not be reachable over TCP (pg_hba `reject`), even with the right password.
# Only meaningful once the container runs with deploy/postgres/pg_hba.conf (docker compose up -d
# postgres after the cutover); before that it is reported, not failed.
su_pw=$(envget POSTGRES_PASSWORD)
addr=$(envget DATABASE_URL | sed -E 's#^[a-z]+://[^@]*@([^/]+)/.*#\1#')
hba=$(psql_super -At -c 'SHOW hba_file' 2>/dev/null || true)
if [ "$hba" != /etc/postgresql/pg_hba.conf ]; then
  echo "SKIP superuser-over-TCP check: hba_file is '${hba:-?}', not deploy/postgres/pg_hba.conf yet (recreate postgres)"
elif [ -n "$su_pw" ] && [ -n "$addr" ]; then
  if docker run --rm --network host --env-file <(printf 'PGHOST=%s\nPGPORT=%s\nPGUSER=%s\nPGPASSWORD=%s\nPGDATABASE=%s\n' \
       "${addr%:*}" "${addr##*:}" "${PG_SUPERUSER:-agent}" "$su_pw" "${PG_DB:-agent}") \
       "$PG_CLIENT_IMAGE" psql -X -At -c 'SELECT 1' >/dev/null 2>&1; then
    echo "FAIL bootstrap superuser can log in over TCP at $addr (pg_hba.conf not active yet?)"; fail=1
  else
    echo "PASS bootstrap superuser is refused over TCP at $addr"
  fi
fi

[ "$fail" = 0 ] && echo "ALL ROLE CHECKS PASSED" || { echo "ROLE CHECKS FAILED"; exit 1; }
