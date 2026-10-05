#!/usr/bin/env bash
# Static checks for the observability config (make metrics-check): compose file, Prometheus config
# and rules (promtool check + unit tests), the rendered Alertmanager config (amtool), blackbox
# modules, and that the dashboards match their generator. Needs Docker; changes nothing.
set -euo pipefail
. "$(dirname "$0")/../lib/common.sh"
cd "$REPO"
OBS=deploy/observability
img() { docker compose config --images "$1" 2>/dev/null | head -1; }
PROM=$(img prometheus) AM=$(img alertmanager) BB=$(img blackbox-exporter)

say "compose file"
docker compose config -q

say "prometheus config + rules"
docker run --rm --network none --entrypoint promtool \
  -v "$REPO/$OBS/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$REPO/$OBS/rules:/etc/prometheus/rules:ro" "$PROM" check config /etc/prometheus/prometheus.yml

say "rule unit tests"
docker run --rm --network none --entrypoint promtool -v "$REPO/$OBS/rules:/rules:ro" -w /rules/tests \
  "$PROM" test rules rules_test.yml

say "alertmanager config (rendered with the defaults: blackhole)"
docker run --rm --network none --entrypoint sh -v "$REPO/$OBS/alertmanager:/src:ro" "$AM" -c '
  sed -e "s#/etc/alertmanager/alertmanager.yml.tmpl#/src/alertmanager.yml.tmpl#" -e "s#^exec .*#exit 0#" \
    /src/entrypoint.sh > /tmp/render.sh && sh /tmp/render.sh && amtool check-config /tmp/alertmanager.yml'

say "blackbox modules"
docker run --rm --network none -v "$REPO/$OBS/blackbox/blackbox.yml:/config.yml:ro" "$BB" \
  --config.file=/config.yml --config.check

say "dashboards match build_dashboards.py"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cp -r "$OBS/grafana/." "$tmp/"
python3 "$tmp/build_dashboards.py" >/dev/null
diff -r "$OBS/grafana/dashboards" "$tmp/dashboards" && echo "dashboards up to date"
