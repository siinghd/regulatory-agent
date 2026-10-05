#!/usr/bin/env bash
# Nightly backup: Postgres dump (restore-tested before it is kept), raw MIME and blobs,
# encrypted to an offline age public key so this host cannot read its own backups.
# Optional off-site copy via rclone (BACKUP_REMOTE). Retention: 14 daily dumps locally.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
: "${BACKUP_AGE_RECIPIENT:?set BACKUP_AGE_RECIPIENT in .env}"

DIR=${BACKUP_DIR:-/home/deploy/backups/regulatory-agent}
mkdir -p "$DIR" && chmod 700 "$DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
plain=$(mktemp "$DIR/.pg-XXXXXX.dump")
trap 'rm -f "$plain"' EXIT

dc() { docker compose exec -T postgres "$@"; }
# The dump runs as agent_backup (pg_read_all_data only) once deploy/db-cutover.sh has set
# BACKUP_PG_USER; the restore check below still needs the superuser (CREATE DATABASE).
dc pg_dump -U "${BACKUP_PG_USER:-agent}" -Fc agent > "$plain"

# Restore test on a throwaway database: a backup that doesn't restore isn't a backup.
dc psql -U agent -qc "DROP DATABASE IF EXISTS agent_restorecheck" -c "CREATE DATABASE agent_restorecheck"
dc pg_restore -U agent -d agent_restorecheck --no-owner < "$plain"
live=$(dc psql -U agent -d agent -tAc "SELECT count(*) FROM requests")
restored=$(dc psql -U agent -d agent_restorecheck -tAc "SELECT count(*) FROM requests")
dc psql -U agent -qc "DROP DATABASE agent_restorecheck"
[ "$restored" -ge 1 ] && [ "$((live - restored))" -le 50 ] || { echo "restore check failed: live=$live restored=$restored" >&2; exit 1; }

age -r "$BACKUP_AGE_RECIPIENT" -o "$DIR/pg-$stamp.dump.age" "$plain"
tar -C data -cf - raw blobs audit 2>/dev/null | age -r "$BACKUP_AGE_RECIPIENT" -o "$DIR/files-$stamp.tar.age"
chmod 600 "$DIR"/*.age

if [ -n "${BACKUP_REMOTE:-}" ]; then
  rclone copy "$DIR" "$BACKUP_REMOTE" --include "*-$stamp.*" --s3-no-check-bucket
fi
find "$DIR" -name '*.age' -mtime +14 -delete
echo "$(date -u +%FT%TZ) ok pg=$(stat -c %s "$DIR/pg-$stamp.dump.age")B files=$(stat -c %s "$DIR/files-$stamp.tar.age")B requests=$restored remote=${BACKUP_REMOTE:-none}" >> "$DIR/backup.log"
