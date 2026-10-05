#!/usr/bin/env bash
# Prepare .env for the ACL'd Redis (deploy/redis/): generates REDIS_PASSWORD (the `agent` user)
# and REDIS_ADMIN_PASSWORD (break-glass `admin`), and points REDIS_URL at the agent user.
# Idempotent: existing passwords are kept. Takes effect when the redis service is recreated
# (`docker compose up -d redis`), which renders the ACL from these values.
#
#   deploy/redis-cutover.sh            then: deploy/split-env.sh && docker compose up -d redis
set -euo pipefail
. "$(dirname "$0")/lib/common.sh"
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found"
umask 077

backup=$(backup_env redis-cutover)

agent_pw=$(envget REDIS_PASSWORD); [ -n "$agent_pw" ] || agent_pw=$(envtool gen)
admin_pw=$(envget REDIS_ADMIN_PASSWORD); [ -n "$admin_pw" ] || admin_pw=$(envtool gen)

# Keep host, port and db index from the current URL; replace any credentials.
url=$(envget REDIS_URL); url=${url:-redis://127.0.0.1:6392/0}
hostpart=$(printf '%s' "$url" | sed -E 's#^rediss?://([^@/]*@)?([^/]+)(/.*)?$#\2#')
dbpart=$(printf '%s' "$url" | sed -E 's#^rediss?://([^@/]*@)?([^/]+)(/.*)?$#\3#')
{
  printf 'REDIS_PASSWORD=%s\n' "$agent_pw"
  printf 'REDIS_ADMIN_PASSWORD=%s\n' "$admin_pw"
  printf 'REDIS_URL=redis://agent:%s@%s%s\n' "$agent_pw" "$hostpart" "${dbpart:-/0}"
} | envtool set "$ENV_FILE"
say "REDIS_PASSWORD, REDIS_ADMIN_PASSWORD and REDIS_URL (user agent @ $hostpart${dbpart:-/0}) set in $ENV_FILE (backup: $backup)"
