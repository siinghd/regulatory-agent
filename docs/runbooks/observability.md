# Runbook: observability stack

This document is written in ASD-STE100 Simplified Technical English.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentWatchdog` | `none` | Always fires. This is the design. |
| `RegagentMonitoringTargetDown` | `warning` | For 10 min, Prometheus cannot scrape the node exporter, the blackbox exporter or Alertmanager. |

Incident severity: SEV3. The risk is that the operator does not see a fault. The agent continues to operate. [deploy/observability/README.md](../../deploy/observability/README.md) and [Observability](../guide/observability.md) describe the stack.

WARNING: Alertmanager sends all alerts to the receiver `blackhole` until the owner sets `ALERT_RECEIVER` (`email` or `webhook`) in `deploy/observability/.env`. The alert rules are configured, but no alert reaches a person until then.

| Component | Address | Access |
|---|---|---|
| Prometheus | 127.0.0.1:9090 | Host only |
| Alertmanager | 127.0.0.1:9093 | Host only |
| Grafana | 127.0.0.1:3310 | Public read-only at https://uarb.hsingh.app/grafana/ (anonymous Viewer). The admin account operates only on 127.0.0.1:3310, through an SSH tunnel. |
| Worker metrics | 127.0.0.1:9710 | Host only |
| Ingest metrics | 127.0.0.1:9711 | Host only |
| Web metrics | `GET /metrics` on 127.0.0.1:8710 | Loopback peers without proxy headers (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`) only. Caddy answers 404 on the public site. |

## 1. Find what is down

1. Show the containers, the targets, the probes and the alerts that fire.

   ```bash
   make metrics-status
   ```

2. Read the logs of the stack.

   ```bash
   docker compose logs --tail 50 prometheus alertmanager grafana node-exporter blackbox-exporter
   ```

## 2. Repair

| Cause | Repair |
|---|---|
| A container stopped | Run `make metrics-up`. It starts only the observability services, never the app. |
| A configuration change caused the fault | Run `make metrics-check`. It names the bad file. Correct the file. Then reload Prometheus with `docker compose kill -s HUP prometheus`, or restart the container. |
| Alertmanager refuses its configuration after a change to `deploy/observability/.env` | Run `docker compose logs alertmanager`. The entrypoint names the bad `ALERT_*` value. Correct `deploy/observability/.env`. Then run `docker compose up -d alertmanager`. |

## 3. Watchdog

`RegagentWatchdog` must go to an external dead man's switch. Set `ALERT_WATCHDOG_RECEIVER=watchdog` and `ALERT_WATCHDOG_URL` in `deploy/observability/.env`. Alertmanager then sends the heartbeat to that URL each 5 min. When the heartbeat stops, the external service tells the operator. This is how the operator sees a stopped Prometheus, Alertmanager or host.

Until the owner sets these values, the watchdog goes to `blackhole`.

To test the alert path after you set a receiver, do this step:

1. Send a test alert.

   ```bash
   docker compose exec alertmanager amtool alert add RegagentTest severity=warning --annotation=summary=test --alertmanager.url=http://127.0.0.1:9093
   ```

   Expected result: the receiver gets the test alert.
