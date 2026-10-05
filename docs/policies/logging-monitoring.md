# Logging and Monitoring Policy

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05. SOC 2: CC7.2, CC7.3, CC4.1, A1.1.

[Observability](../guide/observability.md) gives the technical details and the procedures for the logs, the metrics, the alerts and the dashboards.

## 1. Log sources

| Source | Content | Where | Retention |
|---|---|---|---|
| Application (ingest, worker, web, migrate, retention) | Structured JSON (structlog): request id, state changes, retries, provider errors, times. No secrets, no message bodies. | stdout, then the Docker `json-file` driver: 5 files of 20 MB for each container | Approximately 30 days at the current volume (limited by size) |
| Audit trail | Each state change, outbound email, retry, deploy, purge and admin command, for each request | Postgres `events`. Append-only for the app roles (no UPDATE, no DELETE). | 400 days |
| Deploy log | Each deploy attempt: time, result, stage, SHA, user, change reference, image | `deploy/deploys.log` and `deploy` events | 400 days (as the audit trail) |
| Postgres server | Connections (`log_connections`), all DDL and role changes (`log_statement=ddl`), lock waits. Passwords occur only as SCRAM verifiers. | Docker `json-file` | Approximately 30 days |
| Redis | ACL denials (`ACL LOG`, 256 entries) and slow commands | In memory. The weekly check reads them. | Rolling |
| Caddy (viewer and `/grafana/`) | Access log, JSON: IP, path (with progress tokens), status, time | `/var/log/caddy/uarb-access.log` | 30 days (`roll_keep_for 720h` in `deploy/caddy/uarb.caddy` and in the live Caddy configuration; setting `access_log_retention_days`) |
| Mail (Postfix, Dovecot) | Delivery, authentication, rejections | journald and syslog | Host default (journald is limited by size) |
| SSH, sudo | Authentication and use of privileges | journald (`auth`) | Host default |
| Backups | 1 line for each run, with the sizes and the result of the restore check | `/home/deploy/backups/regulatory-agent/backup.log` | 400 days |
| Compliance checks | PASS, WARN or FAIL for each control, each week | `/home/deploy/compliance/compliance-<date>.txt` | 400 days |
| Metrics (Prometheus) | Aggregate counters, gauges, histograms and probe results. No personal data and no request content (`deploy/observability/METRICS_CONTRACT.md`). | Docker volume `promdata` | 30 days or 5 GB |

## 2. Data that the system never logs

The system never logs these data:

- secrets (passwords, API keys, DSNs, drop link keys and delete tokens). The code hides them from `repr`.
- email bodies and attachments;
- document text;
- full sender addresses on the progress page.

Audit events identify the requester only by `subject_h`. This is an HMAC-SHA256 of the address with the key `AUDIT_HMAC_KEY`. Migration `005_audit_retention_privacy.sql` removes the address from events that the system wrote before that migration.

## 3. Integrity and time

- No application role can change or delete audit events. The roles do not have the grants, and `deploy/verify-db-roles.sh` proves it.
- A trigger refuses UPDATE and DELETE on `events` for each role (migration 005). Only the break-glass superuser can bypass it, and Postgres logs each DDL statement that it runs.
- Rows leave `events` only through `purge_events()`. This function refuses rows that are younger than 400 days, and only the retention role can call it.
- Each event records the database role that wrote it (`actor`). A trigger sets this value, not the writer.
- Logs and events use UTC from the host clock (systemd-timesyncd).
- Log files are only on the host (**Open**: send the streams for the audit to a store outside the host. At least the deploy log, the authentication log and the Postgres DDL log are necessary. Then a compromised host cannot erase them.)

## 4. Monitors and alerts

| Signal | Mechanism | Status |
|---|---|---|
| Process health | Docker healthchecks with `ragent healthcheck web\|worker\|ingest`. Web: `GET /health` answers with `db: true`. Worker: the arq health key exists. Ingest: the heartbeat key in Redis is younger than 600 s. | Implemented |
| Control drift, certificates, backups, disk, ACL denials | `deploy/compliance_check.sh`, weekly timer `regagent-compliance.timer` | Partial: the timer was not installed on the host on 2026-10-05 |
| Backup age | The compliance check fails if the last backup with a restore check is older than 26 h. The alert `RegagentBackupStale` uses a metric that `regagent-backup-metrics.timer` writes each 5 min. | Implemented |
| Request outcomes | Each authenticated request ends with a reply or an apology. A failure is a `failed` row with its errors. | Implemented |
| Metrics, probes, alert rules | Prometheus collects the metrics of the worker (127.0.0.1:9710), ingest (127.0.0.1:9711), web (`GET /metrics`, loopback only) and the host. Black-box probes examine the viewer and drop `/health`, IMAP and submission, the egress tunnel, Postgres, Redis and the TLS expiry dates. Rules cover failures, the queue, breakers, budgets, abuse spikes, availability, disk, backups, certificates and the reply latency SLO. Each alert has 1 runbook (`deploy/observability/README.md`). | Implemented |
| Dashboards | Grafana, public and read-only at https://uarb.hsingh.app/grafana/ (anonymous Viewer, aggregate metrics only). The admin account works only on 127.0.0.1:3310. | Implemented |
| Public status page | `/status` with aggregate numbers only (`agent/web/status.py`) | Implemented |
| Alerts to a person | Alertmanager and the alert rules are configured. Alertmanager sends each alert to `blackhole` until the operator sets `ALERT_RECEIVER` (email with a dedicated sender, or a webhook). The operator must also set the watchdog receiver for a dead man's switch. The next step is a mailbox canary: a test email each 6 h that must get its acknowledgement. | **Open** (Operator): no alert reaches a person |

NOTE: The deployed containers can be older than the code. Then a metrics endpoint or a page in this table is possibly not available on the live system. `make metrics-status` shows the current scrape targets.

## 5. Review

- Each week, examine the compliance report, the failed requests and the Redis `ACL LOG`.
- Each week, examine the error lines of the last 7 days:

  ```bash
  docker compose logs --since 168h | grep -E '"level": "(error|critical)"'
  ```

- After each deploy, examine the smoke result and the first minutes of the worker logs.
- If you cannot explain an event, start an incident ([incident response](incident-response.md)).
