#!/usr/bin/env bash
# Deploy the current commit: build candidate -> test it -> scan it -> promote -> migrate + grants
# -> restart services one at a time, each waiting for its healthcheck -> smoke /health -> record.
# Any failure after promotion rolls back to the previous image. Every run, good or bad, appends
# a line to deploy/deploys.log and (when Postgres is reachable) a `deploy` event to the audit log.
#
#   deploy/deploy.sh --change <ref>         normal change: a merged PR/commit reference
#   deploy/deploy.sh --change emergency:<why> [--allow-dirty] [--skip-scan]
#                                            emergency change (docs/policies/change-management.md):
#                                            allowed from a dirty tree, must get a retrospective PR
#   deploy/deploy.sh --rollback --change <ref>
#                                            put regulatory-agent:previous back
#
# Env: DEPLOY_WAIT_SERVICES (default "worker ingest web"; drop ingest until its heartbeat ships),
#      DEPLOY_PUBLIC_URL (default https://uarb.hsingh.app; empty to skip the public smoke check),
#      and for rehearsals against a throwaway compose project (COMPOSE_PROJECT_NAME/COMPOSE_FILE/
#      COMPOSE_ENV_FILES): DEPLOY_IMAGE_REPO (default regulatory-agent; set AGENT_IMAGE to match),
#      DEPLOY_ENV_DIR (default deploy/env), DEPLOY_LOG (default deploy/deploys.log; keep it out of
#      Git: a tracked log would make every next deploy see a dirty tree), DEPLOY_LOCK.
set -euo pipefail
. "$(dirname "$0")/lib/common.sh"
cd "$REPO"

change="" allow_dirty=0 skip_scan=0 rollback=0
while [ $# -gt 0 ]; do
  case $1 in
    --change) change=${2:-}; shift 2 ;;
    --allow-dirty) allow_dirty=1; shift ;;
    --skip-scan) skip_scan=1; shift ;;
    --rollback) rollback=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[ -n "$change" ] || die "--change <PR/commit ref | emergency:reason> is required (change management)"
if [[ $change != emergency:* ]] && { [ "$allow_dirty" = 1 ] || [ "$skip_scan" = 1 ]; }; then
  die "--allow-dirty/--skip-scan are for emergency changes only (--change emergency:<why>)"
fi

exec 9>"${DEPLOY_LOCK:-/tmp/regulatory-agent-deploy.lock}"
flock -n 9 || die "another deploy is running"

WAIT_SERVICES=${DEPLOY_WAIT_SERVICES:-worker ingest web}
PUBLIC_URL=${DEPLOY_PUBLIC_URL-https://uarb.hsingh.app}
IMG=${DEPLOY_IMAGE_REPO:-regulatory-agent}
ENV_DIR=${DEPLOY_ENV_DIR:-$REPO/deploy/env}
LOG=${DEPLOY_LOG:-$REPO/deploy/deploys.log}
TRIVY_IMAGE=aquasec/trivy:0.75.0@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa
who="${SUDO_USER:-$USER}"
who_email=$(git config user.email 2>/dev/null || true)
sha=$(git rev-parse HEAD)
dirty=false
[ -z "$(git status --porcelain --untracked-files=no)" ] || dirty=true
started=$(date -u +%FT%TZ)
result=failed
stage=preflight

record() {
  local image_id; image_id=$(docker image inspect "$IMG:latest" --format '{{.Id}}' 2>/dev/null || echo none)
  printf '%s deploy result=%s stage=%s sha=%s dirty=%s by=%s%s change=%q image=%s started=%s\n' \
    "$(date -u +%FT%TZ)" "$result" "$stage" "$sha" "$dirty" "$who" "${who_email:+ <$who_email>}" \
    "$change" "$image_id" "$started" >> "$LOG"
  # Audit event, as the app role over the socket; values travel as psql variables, not SQL text.
  printf "INSERT INTO events (request_id, kind, data) VALUES (NULL, 'deploy', jsonb_build_object(
    'result', :'result', 'stage', :'stage', 'sha', :'sha', 'dirty', :'dirty'::boolean, 'by', :'who',
    'change', :'change', 'image', :'image', 'started', :'started'));\n" \
  | docker compose exec -T postgres psql -X -q -U agent_app -d agent -v ON_ERROR_STOP=1 \
      -v result="$result" -v stage="$stage" -v sha="$sha" -v dirty="$dirty" -v who="$who" \
      -v change="$change" -v image="$image_id" -v started="$started" >/dev/null 2>&1 \
    || echo "warning: could not write the deploy event to Postgres (deploys.log has the record)" >&2
}
trap 'record' EXIT

restart_services() {
  stage=services
  for svc in $WAIT_SERVICES; do
    say "restarting $svc"
    docker compose up -d --no-deps --wait --wait-timeout 240 "$svc"
  done
  for svc in ingest worker web; do  # services not waited on still get restarted
    [[ " $WAIT_SERVICES " == *" $svc "* ]] || docker compose up -d --no-deps "$svc"
  done
}

smoke() {
  stage=smoke
  local port; port=$(envtool get "$ENV_DIR/web.env" WEB_PORT 2>/dev/null || echo 8710)
  curl -fsS --max-time 5 "http://127.0.0.1:$port/health" | grep -q '"db":true' \
    || die "smoke: http://127.0.0.1:$port/health is not ok with db:true"
  if [ -n "$PUBLIC_URL" ]; then
    curl -fsS --max-time 10 "$PUBLIC_URL/health" | grep -q '"db":true' || die "smoke: $PUBLIC_URL/health failed"
  fi
  say "smoke ok"
}

# ---------------------------------------------------------------- preflight
for f in "$ENV_FILE" "$ENV_DIR"/{ingest,worker,web,migrate,db-grants}.env; do
  [ -f "$f" ] || die "$f missing (run deploy/split-env.sh)"
  [ "$(stat -c %a "$f")" = 600 ] || die "$f must be mode 600"
done
[ -z "$(find "$ENV_FILE" -newer "$ENV_DIR/worker.env")" ] || die "$ENV_FILE is newer than the per-service env files: run deploy/split-env.sh"
if [ "$dirty" = true ] && [ "$allow_dirty" = 0 ]; then
  die "working tree has uncommitted changes; commit them (or --change emergency:<why> --allow-dirty)"
fi

if [ "$rollback" = 1 ]; then
  stage=rollback
  docker image inspect "$IMG:previous" >/dev/null 2>&1 || die "no $IMG:previous image"
  docker tag "$IMG:previous" "$IMG:latest"
  sha=$(docker image inspect "$IMG:latest" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
  restart_services
  smoke
  result=rolled-back
  exit 0
fi

# ---------------------------------------------------------------- build
stage=build
version=$sha; [ "$dirty" = false ] || version="$sha-dirty"
build_args=(--build-arg "GIT_SHA=$version" --build-arg "BUILD_DATE=$(date -u +%FT%TZ)"
            --build-arg "OS_UPDATES_EPOCH=$(date -u +%G-W%V)")
say "building $IMG:candidate ($version)"
docker build -q "${build_args[@]}" --target release -t "$IMG:candidate" . >/dev/null
docker build -q "${build_args[@]}" --target test -t "$IMG:candidate-test" . >/dev/null

# ---------------------------------------------------------------- test
stage="test"
say "unit + adversarial tests in the candidate (no network, read-only, no capabilities)"
docker run --rm --network none --read-only --tmpfs /tmp:size=256m --cap-drop ALL \
  --security-opt no-new-privileges:true -e HOME=/tmp "$IMG:candidate-test" -q -p no:cacheprovider

# ---------------------------------------------------------------- scan
if [ "$skip_scan" = 0 ]; then
  stage=scan
  say "trivy: HIGH/CRITICAL with a fix available fail the deploy"
  mkdir -p "$HOME/.cache/trivy"
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$HOME/.cache/trivy:/root/.cache/trivy" \
    -v "$REPO/.trivyignore:/.trivyignore:ro" "$TRIVY_IMAGE" image --quiet --exit-code 1 --scanners vuln \
    --severity HIGH,CRITICAL --ignore-unfixed --ignorefile /.trivyignore "$IMG:candidate"
fi

# ---------------------------------------------------------------- promote (rollback point)
stage=promote
if docker image inspect "$IMG:latest" >/dev/null 2>&1; then
  docker tag "$IMG:latest" "$IMG:previous"
fi
docker tag "$IMG:candidate" "$IMG:latest"
rollback_on_error() {
  local failed_stage=$stage
  echo "deploy failed at stage $failed_stage: rolling back to $IMG:previous" >&2
  if docker image inspect "$IMG:previous" >/dev/null 2>&1; then
    docker tag "$IMG:previous" "$IMG:latest"
    for svc in ingest worker web; do docker compose up -d --no-deps "$svc" || true; done
    result=rolled-back
  fi
  stage=$failed_stage
}
trap 'rollback_on_error; record' EXIT

# ---------------------------------------------------------------- data stores, migrations, grants
stage=migrate
docker compose up -d --wait --wait-timeout 120 postgres redis
docker compose run --rm --no-deps migrate
docker compose run --rm --no-deps db-grants

restart_services
smoke
result=ok
trap 'record' EXIT
say "deployed $version (change: $change)"
