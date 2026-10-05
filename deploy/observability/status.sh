#!/usr/bin/env bash
# make metrics-status: containers, Prometheus targets and probes, firing alerts, where to look.
set -uo pipefail
. "$(dirname "$0")/../lib/common.sh"
cd "$REPO" || exit 1

docker compose ps -a prometheus alertmanager grafana grafana-init node-exporter blackbox-exporter \
  --format 'table {{.Name}}\t{{.Status}}'
echo
get() { curl -fsS --max-time 5 "$1" 2>/dev/null || echo null; }
TARGETS=$(get http://127.0.0.1:9090/api/v1/targets) \
PROBES=$(get 'http://127.0.0.1:9090/api/v1/query?query=probe_success') \
ALERTS=$(get http://127.0.0.1:9093/api/v2/alerts) \
python3 - <<'EOF'
import json, os

targets, probes, alerts = (json.loads(os.environ[k]) for k in ("TARGETS", "PROBES", "ALERTS"))
if targets is None:
    print("Prometheus is not answering on 127.0.0.1:9090")
else:
    ts = targets["data"]["activeTargets"]
    print(f"Prometheus targets: {sum(t['health'] == 'up' for t in ts)}/{len(ts)} up")
    for t in sorted(ts, key=lambda t: t["labels"]["job"]):
        if t["health"] != "up":
            print(f"  down: {t['labels']['job']} {t['scrapeUrl']} ({t['lastError'][:90]})")
if probes is not None:
    r = sorted(probes["data"]["result"], key=lambda x: x["metric"].get("probe", ""))
    print("Probes: " + "  ".join(f"{x['metric'].get('probe')}={'UP' if x['value'][1] == '1' else 'DOWN'}" for x in r))
if alerts is None:
    print("Alertmanager is not answering on 127.0.0.1:9093")
else:
    a = [x for x in alerts if x["status"]["state"] == "active" and x["labels"]["alertname"] != "RegagentWatchdog"]
    print(f"Alerts firing: {len(a)}")
    for x in a:
        print(f"  {x['labels']['alertname']} ({x['labels'].get('severity')}) -> {','.join(r['name'] for r in x['receivers'])}")
EOF
cat <<'EOF'

Grafana (public, read-only):  https://uarb.hsingh.app/grafana/
Grafana admin (local only):   ssh -L 3300:127.0.0.1:3310 <host>, then http://localhost:3300/grafana/login
                              (user regagent-admin; password: GF_SECURITY_ADMIN_PASSWORD in deploy/observability/.env)
Prometheus / Alertmanager:    127.0.0.1:9090 / 127.0.0.1:9093 on the host (tunnel them the same way)
EOF
