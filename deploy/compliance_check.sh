#!/usr/bin/env bash
# Technical control checks for the regulatory agent host (SOC 2 evidence). Read-only: it inspects
# files, containers, Postgres, Redis and host settings and changes nothing.
# Prints one PASS/WARN/FAIL line per control; exits 1 if any FAIL.
#
#   deploy/compliance_check.sh [--report-dir DIR]     also writes DIR/compliance-<UTC date>.txt
#
# Weekly via regagent-compliance.timer; evidence index: docs/soc2/control-matrix.md.
# Root-only facts (sshd effective config, ufw, private cert files) are read with `sudo -n`;
# without it those lines are WARN "cannot verify", never a silent PASS.
set -uo pipefail
. "$(dirname "$0")/lib/common.sh"
cd "$REPO" || exit 1

report_dir=""
[ "${1:-}" = --report-dir ] && report_dir=${2:?--report-dir needs a directory}
if [ -n "$report_dir" ]; then
  mkdir -p "$report_dir" && chmod 700 "$report_dir"
  exec > >(tee "$report_dir/compliance-$(date -u +%Y-%m-%d).txt") 2>&1
fi

fails=0 warns=0
pass() { echo "PASS $*"; }
warn() { echo "WARN $*"; warns=$((warns + 1)); }
fail() { echo "FAIL $*"; fails=$((fails + 1)); }
check() { local desc=$1; shift; if "$@" >/dev/null 2>&1; then pass "$desc"; else fail "$desc"; fi; }
root() { sudo -n "$@" 2>/dev/null; }

echo "# compliance check $(date -u +%FT%TZ) host=$(hostname) repo=$REPO sha=$(git rev-parse --short HEAD 2>/dev/null)"

# ---------------------------------------------------------------- secrets at rest
mode=$(stat -c '%a %U' .env 2>/dev/null)
[ "$mode" = "600 $(id -un)" ] && pass ".env is mode 600, owned by $(id -un)" || fail ".env mode/owner is '$mode' (want 600 $(id -un))"
for f in deploy/env/{ingest,worker,web,migrate,db-grants}.env; do
  if [ ! -f "$f" ]; then fail "$f missing (deploy/split-env.sh)"; continue; fi
  [ "$(stat -c %a "$f")" = 600 ] && pass "$f is mode 600" || fail "$f is mode $(stat -c %a "$f") (want 600)"
done
if [ -f deploy/env/worker.env ]; then
  [ -z "$(find .env -newer deploy/env/worker.env)" ] && pass "per-service env files are newer than .env" \
    || fail ".env changed after deploy/split-env.sh last ran"
fi
loose=$(find . -path ./.venv -prune -o -path ./data -prune -o \( -name '.env*' -o -name '*.env' \) -perm /044 -print 2>/dev/null | grep -v '\.example$')
[ -z "$loose" ] && pass "no group/world-readable env files in the repo" || fail "readable env files: $loose"
tracked=$(git ls-files | grep -E '(^|/)\.env$|\.env$|\.env\.bak|\.dump$|\.age$' || true)
[ -z "$tracked" ] && pass "no env files, dumps or backups tracked by git" || fail "tracked secret-bearing files: $tracked"

# ---------------------------------------------------------------- data directory
for d in data data/raw data/blobs; do
  [ -d "$d" ] || continue
  m=$(stat -c %a "$d")
  [ $((8#$m & 8#007)) -eq 0 ] && pass "$d is not accessible to other users (mode $m)" \
    || fail "$d is mode $m: other users can read it (fix: chmod -R o-rwx data)"
done
if [ -d data/raw ]; then
  n=$(find data/raw -perm /004 -type f 2>/dev/null | head -1000 | wc -l)
  [ "$n" -eq 0 ] && pass "no world-readable raw MIME files" || fail "$n world-readable raw MIME files under data/raw"
fi

# ---------------------------------------------------------------- Postgres
if pg=$(docker compose ps -q postgres 2>/dev/null) && [ -n "$pg" ]; then
  q() { docker exec -i "$pg" psql -X -At -v ON_ERROR_STOP=1 -U agent -d agent -c "$1" 2>/dev/null; }
  roles=$(q "SELECT count(*) FROM pg_roles WHERE rolname IN ('agent_owner','agent_migrator','agent_app','agent_web','agent_retention','agent_backup')")
  if [ "$roles" != 6 ]; then
    fail "Postgres: least-privilege roles missing ($roles/6; deploy/db-cutover.sh)"
  else
    bad=$(q "SELECT string_agg(rolname, ' ') FROM pg_roles WHERE rolname LIKE 'agent\_%'
             AND (rolsuper OR rolbypassrls OR rolcreaterole OR rolcreatedb)")
    [ -z "$bad" ] && pass "Postgres: 6 least-privilege roles, none superuser/createrole/createdb/bypassrls" \
      || fail "Postgres: privileged agent roles: $bad"
    if stray=$(q "SELECT coalesce(string_agg(c.relname, ' '), '') FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE n.nspname = 'public' AND c.relkind IN ('r','p','S') AND c.relowner <> 'agent_owner'::regrole"); then
      [ -z "$stray" ] && pass "Postgres: every table/sequence owned by agent_owner" || fail "Postgres: not owned by agent_owner: $stray"
    else
      fail "Postgres: ownership query failed"
    fi
    [ "$(q "SELECT has_table_privilege('agent_app', 'events', 'UPDATE') OR has_table_privilege('agent_app', 'events', 'DELETE')")" = f ] \
      && pass "Postgres: events is append-only for agent_app" || fail "Postgres: agent_app can UPDATE/DELETE events (or the check failed)"
  fi
  for pair in ingest:agent_app worker:agent_app web:agent_web migrate:agent_migrator; do
    svc=${pair%%:*} want=${pair##*:}
    got=$(envtool get "deploy/env/$svc.env" DATABASE_URL 2>/dev/null | sed -E 's#^[a-z]+://([^:@]*).*#\1#')
    [ "$got" = "$want" ] && pass "$svc connects as $want" || fail "$svc connects as '${got:-?}' (want $want)"
  done
  hba=$(q "SHOW hba_file")
  [ "$hba" = /etc/postgresql/pg_hba.conf ] && pass "Postgres: repo pg_hba.conf active (superuser refused over TCP)" \
    || fail "Postgres: hba_file is '$hba' (deploy/postgres/pg_hba.conf not active)"
else
  fail "Postgres container not running"
fi

# ---------------------------------------------------------------- Redis
if rd=$(docker compose ps -q redis 2>/dev/null) && [ -n "$rd" ]; then
  anon=$(docker exec "$rd" redis-cli ping 2>&1)
  [[ $anon == *NOAUTH* ]] && pass "Redis: unauthenticated clients refused (default user off)" || fail "Redis: unauthenticated PING answered '$anon'"
  admin_pw=$(envget REDIS_ADMIN_PASSWORD)
  if [ -n "$admin_pw" ]; then
    rc() { docker exec -e REDISCLI_AUTH="$admin_pw" "$rd" redis-cli --user admin --no-auth-warning "$@" 2>&1; }
    denied=0
    for c in "flushall" "flushdb" "keys *" "config get maxmemory" "debug sleep 0" "acl list" "shutdown"; do
      # shellcheck disable=SC2086
      out=$(rc acl dryrun agent $c); [[ $out == OK ]] || denied=$((denied + 1))
    done
    [ "$denied" = 7 ] && pass "Redis: agent user denied FLUSHALL/FLUSHDB/KEYS/CONFIG/DEBUG/ACL/SHUTDOWN" \
      || fail "Redis: agent user allowed $((7 - denied)) of 7 forbidden commands"
    [ "$(rc config get appendonly | tail -1)" = yes ] && [ "$(rc config get maxmemory-policy | tail -1)" = noeviction ] \
      && pass "Redis: AOF on, maxmemory-policy noeviction" || fail "Redis: persistence/eviction config drifted"
    n=$(rc --json acl log 100 | python3 -c "import json,sys; print(sum(1 for e in json.load(sys.stdin) if e.get('username') == 'agent'))" 2>/dev/null || echo "?")
    [ "$n" = 0 ] && pass "Redis: no ACL denials for the agent user" \
      || warn "Redis: $n ACL denials for the agent user (\`ACL LOG\`): a code path needs a command the ACL lacks?"
  else
    fail "Redis: REDIS_ADMIN_PASSWORD not in .env (deploy/redis-cutover.sh)"
  fi
else
  fail "Redis container not running"
fi

# ---------------------------------------------------------------- containers
for svc in ingest worker web; do
  id=$(docker compose ps -q "$svc" 2>/dev/null)
  if [ -z "$id" ]; then fail "$svc is not running"; continue; fi
  f=$(docker inspect "$id" --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.Privileged}} {{.Config.User}} {{json .HostConfig.CapDrop}} {{json .HostConfig.SecurityOpt}}')
  [[ $f == "true false 1000:1000 [\"ALL\"] "*no-new-privileges* ]] && pass "$svc: read-only, unprivileged, uid 1000, no capabilities, no-new-privileges" \
    || fail "$svc hardening drifted: $f"
  h=$(docker inspect "$id" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}')
  [ "$h" = healthy ] && pass "$svc healthcheck: healthy" || fail "$svc healthcheck: $h"
done
for p in 5442 6392 8710 9710 9711; do  # postgres, redis, web, worker metrics, ingest metrics
  l=$(ss -ltnH "sport = :$p" 2>/dev/null | awk '{print $4}' | sort -u | tr '\n' ' ')
  if [ -z "$l" ]; then [ "$p" = 9710 ] || [ "$p" = 9711 ] || warn "nothing listening on :$p"
  elif echo "$l" | grep -qvE '^(127\.0\.0\.1|\[::1\]):'; then fail ":$p listens beyond loopback: $l"
  else pass ":$p is bound to loopback only ($l)"; fi
done

# ---------------------------------------------------------------- configuration drift
cfg=(docker-compose.yml Dockerfile deploy .github requirements.lock requirements-dev.lock requirements-build.lock pyproject.toml)
dirty=$(git status --porcelain --untracked-files=no -- "${cfg[@]}" 2>/dev/null)
[ -z "$dirty" ] && pass "deploy configuration matches the committed repo" || fail "uncommitted changes to deploy configuration: $(echo "$dirty" | wc -l) files"
extra=$(git status --porcelain --untracked-files=all -- "${cfg[@]}" 2>/dev/null | grep '^??' | grep -vE ' deploy/deploys\.log$' || true)
[ -z "$extra" ] || warn "untracked files in deploy configuration paths: $(echo "$extra" | awk '{print $2}' | tr '\n' ' ')"
# What `up -d` would change, without changing it: an in-sync service is reported as Running,
# a drifted one as a recreate (old container id prefixed to its name).
drift=$(docker compose up --dry-run -d --no-deps postgres redis ingest worker web 2>&1 \
  | grep -oE '[0-9a-f]{12}_[a-z0-9-]+-[a-z-]+-[0-9]+|[a-z0-9-]+-[a-z-]+-[0-9]+ +(Recreate|Creat)[a-z]*' \
  | sed -E 's/^[0-9a-f]{12}_//; s/ +.*//' | sort -u | tr '\n' ' ')
[ -z "$drift" ] && pass "running containers match docker-compose.yml + .env (compose dry-run: nothing to recreate)" \
  || fail "containers differ from docker-compose.yml/.env (would be recreated): $drift"
latest=$(docker image inspect regulatory-agent:latest --format '{{.Id}}' 2>/dev/null)
stale=""
for svc in ingest worker web; do
  id=$(docker compose ps -q "$svc" 2>/dev/null); [ -n "$id" ] || continue
  [ "$(docker inspect "$id" --format '{{.Image}}')" = "$latest" ] || stale="$stale $svc"
done
[ -z "$stale" ] && pass "app containers run regulatory-agent:latest" || fail "running an image other than :latest:$stale"
rev=$(docker image inspect regulatory-agent:latest --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)
last=$(grep ' result=ok ' deploy/deploys.log 2>/dev/null | tail -1 | sed -E 's/.* sha=([0-9a-f]+).*/\1/')
[ -n "$rev" ] && [ "${rev%-dirty}" = "$last" ] && pass "running image revision $rev matches the last recorded deploy" \
  || warn "image revision '${rev:-none}' vs last recorded deploy '${last:-none}' (deploy outside deploy/deploy.sh?)"

# ---------------------------------------------------------------- host
if sshd=$(root sshd -T); then
  get() { echo "$sshd" | awk -v k="$1" '$1 == k {print $2; exit}'; }
  [ "$(get passwordauthentication)" = no ] && pass "sshd: PasswordAuthentication no" || fail "sshd: PasswordAuthentication $(get passwordauthentication)"
  [ "$(get kbdinteractiveauthentication)" = no ] && pass "sshd: KbdInteractiveAuthentication no" || fail "sshd: KbdInteractiveAuthentication $(get kbdinteractiveauthentication)"
  [[ $(get permitrootlogin) =~ ^(no|prohibit-password|without-password)$ ]] && pass "sshd: PermitRootLogin $(get permitrootlogin)" || fail "sshd: PermitRootLogin $(get permitrootlogin)"
  [ "$(get permitemptypasswords)" = no ] && pass "sshd: PermitEmptyPasswords no" || fail "sshd: PermitEmptyPasswords $(get permitemptypasswords)"
  [ "$(get maxauthtries)" -le 6 ] 2>/dev/null && pass "sshd: MaxAuthTries $(get maxauthtries)" || warn "sshd: MaxAuthTries $(get maxauthtries)"
else
  warn "sshd: cannot read the effective config (needs sudo -n sshd -T)"
fi
if ufw=$(root ufw status); then
  [[ $ufw == *"Status: active"* ]] && pass "ufw is active" || fail "ufw is not active"
else
  warn "ufw: cannot verify (needs sudo -n ufw status)"
fi
systemctl is-active --quiet unattended-upgrades && pass "unattended-upgrades is active" || warn "unattended-upgrades is not active"
for u in regagent-backup.timer regagent-compliance.timer uarb-egress-tunnel.service; do
  systemctl is-active --quiet "$u" && pass "$u is active" || fail "$u is not active"
done
if sudo -n -l 2>/dev/null | grep -q 'NOPASSWD: ALL'; then
  warn "$(id -un) has passwordless sudo for ALL (compensating control: SSH key-only + docs/policies/access-control.md)"
fi

# ---------------------------------------------------------------- certificates
for cert in /etc/ssl/mail/fullchain.pem /etc/caddy/certs/origin.crt; do
  pem=$(cat "$cert" 2>/dev/null || root cat "$cert")
  if [ -z "$pem" ]; then warn "$cert: cannot read"; continue; fi
  end=$(printf '%s' "$pem" | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
  days=$(( ( $(date -d "$end" +%s) - $(date +%s) ) / 86400 ))
  if [ "$days" -lt 14 ]; then fail "$cert expires in $days days ($end)"
  elif [ "$days" -lt 30 ]; then warn "$cert expires in $days days ($end)"
  else pass "$cert valid for $days more days"; fi
done

# ---------------------------------------------------------------- backups
bdir=$(envget BACKUP_DIR); bdir=${bdir:-/home/deploy/backups/regulatory-agent}
lastok=$(grep ' ok ' "$bdir/backup.log" 2>/dev/null | tail -1 | cut -d' ' -f1)
if [ -z "$lastok" ]; then fail "backup: no successful run in $bdir/backup.log"
else
  age_h=$(( ( $(date +%s) - $(date -d "$lastok" +%s) ) / 3600 ))
  [ "$age_h" -le 26 ] && pass "backup: last restore-tested backup ${age_h}h ago ($lastok)" || fail "backup: last good backup ${age_h}h ago ($lastok)"
fi
plain=$(find "$bdir" -maxdepth 1 -type f ! -name '*.age' ! -name 'backup.log' 2>/dev/null | head -3)
[ -z "$plain" ] && pass "backup: only age-encrypted files at rest" || fail "backup: unencrypted files in $bdir: $plain"
[ -n "$(envget BACKUP_REMOTE)" ] && pass "backup: off-site copy configured (BACKUP_REMOTE)" || warn "backup: no off-site copy (BACKUP_REMOTE unset): RPO depends on this disk"

# ---------------------------------------------------------------- capacity
for path in "$REPO/data" /var/lib/docker; do
  read -r pcent avail < <(df --output=pcent,avail -BG "$path" 2>/dev/null | tail -1 | tr -d '%G')
  if [ -z "${pcent:-}" ]; then warn "disk: cannot stat $path"; continue; fi
  if [ "$pcent" -ge 90 ] || [ "$avail" -lt 5 ]; then fail "disk: $path ${pcent}% used, ${avail} GB free"
  elif [ "$pcent" -ge 80 ]; then warn "disk: $path ${pcent}% used, ${avail} GB free"
  else pass "disk: $path ${pcent}% used, ${avail} GB free"; fi
done
[ -d data/tmp ] && { t=$(du -sm data/tmp 2>/dev/null | cut -f1); [ "${t:-0}" -lt 2048 ] && pass "data/tmp scratch is ${t} MB" || warn "data/tmp scratch is ${t} MB (crash leftovers?)"; }

echo "# summary: $fails FAIL, $warns WARN"
[ "$fails" -eq 0 ]
