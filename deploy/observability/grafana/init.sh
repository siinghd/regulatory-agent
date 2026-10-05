#!/bin/sh
# One-shot (compose service grafana-init): names org 1 "Regulatory Document Agent", the org
# anonymous viewers land in (GF_AUTH_ANONYMOUS_ORG_NAME). Grafana has no file provisioning for
# org names. Idempotent. The admin password reaches curl on stdin, never in argv.
set -eu
: "${GRAFANA_URL:?}" "${GF_SECURITY_ADMIN_USER:?}" "${GF_SECURITY_ADMIN_PASSWORD:?GF_SECURITY_ADMIN_PASSWORD missing}"
NAME="Regulatory Document Agent"

api() {
  printf 'user = "%s:%s"\n' "$GF_SECURITY_ADMIN_USER" "$GF_SECURITY_ADMIN_PASSWORD" \
    | curl -fsS -K - -H 'Content-Type: application/json' "$@"
}

current=$(api "$GRAFANA_URL/api/orgs/1" | sed -n 's/.*"name":"\([^"]*\)".*/\1/p')
if [ "$current" = "$NAME" ]; then
  echo "grafana-init: org 1 is already \"$NAME\""
else
  api -X PUT "$GRAFANA_URL/api/orgs/1" -d "{\"name\":\"$NAME\"}" >/dev/null
  echo "grafana-init: org 1 renamed from \"$current\" to \"$NAME\""
fi
