#!/usr/bin/env bash
# Split .env into deploy/env/<service>.env (mode 600) so each container gets only the secrets it
# needs (mapping: deploy/env/services.toml). Re-run after every .env change; compose reads
# the generated files, never .env itself (except for ${...} interpolation of the redis/postgres
# passwords). Keys no service claims are reported and copied nowhere.
#
#   deploy/split-env.sh [ENV_FILE] [OUTDIR]
set -euo pipefail
. "$(dirname "$0")/lib/common.sh"
src=${1:-$ENV_FILE}
out=${2:-$REPO/deploy/env}
[ -f "$src" ] || die "$src not found"
[ "$(stat -c %a "$src")" = 600 ] || die "$src must be mode 600 (is $(stat -c %a "$src"))"
umask 077
envtool split "$src" "$out"
