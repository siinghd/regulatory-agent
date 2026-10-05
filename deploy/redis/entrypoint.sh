#!/bin/sh
# Renders the Redis ACL file from users.acl.template with SHA-256 hashes of REDIS_PASSWORD and
# REDIS_ADMIN_PASSWORD (from .env via compose), then hands over to the image's own entrypoint.
# The ACL file only ever holds hashes, and lives on a tmpfs (/run/redis).
set -eu
: "${REDIS_PASSWORD:?REDIS_PASSWORD must be set (.env)}"
: "${REDIS_ADMIN_PASSWORD:?REDIS_ADMIN_PASSWORD must be set (.env)}"

sha256hex() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
umask 077
mkdir -p /run/redis
grep -v '^#' /usr/local/etc/redis/users.acl.template \
  | sed -e "s/__AGENT_PASSWORD_SHA256__/$(sha256hex "$REDIS_PASSWORD")/" \
        -e "s/__ADMIN_PASSWORD_SHA256__/$(sha256hex "$REDIS_ADMIN_PASSWORD")/" \
  > /run/redis/users.acl
chown -R redis:redis /run/redis
unset REDIS_ADMIN_PASSWORD  # the server never needs the plaintext; the healthcheck uses REDIS_PASSWORD

[ "${1:-}" = redis-server ] && shift  # the image's default CMD, if compose passes it through
exec docker-entrypoint.sh redis-server /usr/local/etc/redis/redis.conf "$@"
