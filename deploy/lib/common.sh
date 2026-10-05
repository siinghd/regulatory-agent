# Shared helpers for deploy/*.sh. Source it; it does not run anything by itself.
# shellcheck shell=bash

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ENV_FILE=${ENV_FILE:-$REPO/.env}
# Same image as the server (docker-compose.yml): used as a throwaway psql client over TCP.
PG_CLIENT_IMAGE=${PG_CLIENT_IMAGE:-postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea}
REDIS_CLIENT_IMAGE=${REDIS_CLIENT_IMAGE:-redis:7-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499}

envtool() { python3 "$REPO/deploy/lib/envtool.py" "$@"; }
envget() { envtool get "$ENV_FILE" "$1" 2>/dev/null || true; }
die() { echo "error: $*" >&2; exit 1; }
say() { printf '==> %s\n' "$*"; }

# The running Postgres container (overridable so the scripts can target a throwaway one).
pg_container() {
  if [ -n "${PG_CONTAINER:-}" ]; then echo "$PG_CONTAINER"; return; fi
  local id; id=$(docker compose --project-directory "$REPO" ps -q postgres 2>/dev/null)
  [ -n "$id" ] || die "postgres container is not running (set PG_CONTAINER to target another)"
  echo "$id"
}

# psql over the container's Unix socket as the bootstrap superuser (break-glass path).
psql_super() {
  docker exec -i "$(pg_container)" psql -X -q -v ON_ERROR_STOP=1 \
    -U "${PG_SUPERUSER:-agent}" -d "${PG_DB:-agent}" "$@"
}

# psql over TCP as the role in the DSN stored under .env key $1. The DSN travels in an
# --env-file on a pipe, so the password is never in any process's argv.
psql_dsn() {
  local key=$1; shift
  local dsn; dsn=$(envget "$key")
  [ -n "$dsn" ] || die "$key is not set in $ENV_FILE"
  docker run --rm -i --network host --env-file <(printf '%s' "$dsn" | envtool libpq-env) \
    -v "$REPO/deploy/sql:/sql:ro" "$PG_CLIENT_IMAGE" psql -X -v ON_ERROR_STOP=1 "$@"
}

# Copy $ENV_FILE to deploy/env/backups/<UTC stamp>-<label>.env (mode 600; `*.env` is gitignored
# and deploy/ is outside the image build context). An ENV_FILE elsewhere (tests) is backed up
# next to itself. Prints the backup path.
backup_env() {
  local dir="$REPO/deploy/env/backups" dest
  [ "$(realpath "$ENV_FILE")" = "$REPO/.env" ] || dir="$(dirname "$ENV_FILE")/env-backups"
  mkdir -p "$dir" && chmod 700 "$dir"
  dest="$dir/$(date -u +%Y%m%dT%H%M%SZ)-${1:-manual}.env"
  cp "$ENV_FILE" "$dest" && chmod 600 "$dest"
  echo "$dest"
}
