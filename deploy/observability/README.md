# Observability: Prometheus, Alertmanager, Grafana

MVP monitoring for evaluating the Regulatory Document Agent. Aggregate metrics only; no personal
data or request contents (privacy rules: [METRICS_CONTRACT.md](METRICS_CONTRACT.md)). Not a
production SLA. SOC 2: CC7.2 (anomalies monitored), A1.1; policy:
[logging & monitoring](../../docs/policies/logging-monitoring.md) §4.

## What runs

Services in `docker-compose.yml`. They start with the stack (`docker compose up -d`) but have no
`depends_on` on the app, so app deploys never touch them. Host networking (their targets are on
the host's loopback); every listener is pinned to 127.0.0.1.

| Container | Listens | Purpose |
|---|---|---|
| `regagent-prometheus` | 127.0.0.1:9090 | Scrapes everything below every 30 s; 30 d / 5 GB retention (volume `promdata`); evaluates `rules/*.yml` |
| `regagent-alertmanager` | 127.0.0.1:9093 | Routes alerts; **black hole by default** (volume `alertmanagerdata`) |
| `regagent-grafana` | 127.0.0.1:3310 | Dashboards; public read-only at https://uarb.hsingh.app/grafana/ (volume `grafanadata`) |
| `regagent-grafana-init` | (one-shot) | Names org 1 "Regulatory Document Agent" (the anonymous org) |
| `regagent-node-exporter` | 127.0.0.1:9100 | Host CPU, memory, disk, network; textfile collector for backup freshness |
| `regagent-blackbox-exporter` | 127.0.0.1:9117 | Probes (below) |
| `regagent-postgres-exporter`, `regagent-redis-exporter` | 127.0.0.1:9187 / 9121 | After the cutover only (profile `db-exporters`) |

Ports 3300 and 9115 are taken on this host (codeshot, another project), hence 3310 and 9117.

**Scrape targets:** worker `127.0.0.1:9710` (alerted when down), web `127.0.0.1:8710/metrics` and
ingest `127.0.0.1:9711` (to be added by the app: scraped, tolerated down), the stack itself.

**Probes:** `uarb-health` (https://uarb.hsingh.app/health, body must report the DB up),
`web-local-health` (http://127.0.0.1:8710/health), `drop-health`, `mail-submission`
(mail.hsingh.app:587 EHLO + STARTTLS, every 2 min), `mail-imaps` (993 TLS + greeting, every 2 min),
`egress-tunnel` (TCP 127.0.0.1:1080), `egress-via-tunnel` (HTTPS through the SOCKS tunnel to a
neutral Cloudflare endpoint, never a regulator portal, every 3 min), `postgres` (TCP 5442),
`redis` (PING answered), `redis-auth` (PING refused with NOAUTH), `origin-cert` (Caddy's origin
certificate on 127.0.0.1:443). TLS expiry comes from every TLS probe.

**Backup freshness:** `backup_metrics.sh`, run every 5 minutes by `regagent-backup-metrics.timer`
(`deploy/regagent-backup-metrics.{service,timer}`, installed in `/etc/systemd/system/`), writes
`/var/lib/regagent-textfile/regagent_backup.prom` from the backup log.

## Day to day

```bash
make metrics-status       # containers, targets, probes, firing alerts, URLs
make metrics-up           # (re)start just these services, wait healthy, run grafana-init
make metrics-down         # stop and remove the containers; volumes (history) are kept
make metrics-check        # promtool config + rules + rule unit tests, amtool, blackbox, dashboards up to date
make metrics-dashboards   # regenerate grafana/dashboards/*.json from grafana/build_dashboards.py
```

After editing `prometheus/prometheus.yml` or `rules/*.yml`: `make metrics-check`, then
`docker compose kill -s HUP prometheus`. Blackbox: `docker compose kill -s HUP blackbox-exporter`.
Dashboards are files (provisioned read-only): edit `grafana/build_dashboards.py`, run
`make metrics-dashboards`; Grafana picks the change up within a minute.

## Grafana access

- **Public, read-only:** https://uarb.hsingh.app/grafana/ (anonymous Viewer; the home dashboard
  has the disclaimer and links). `deploy/caddy/uarb.caddy` (snippet `regagent_grafana`) answers 404
  for login, admin, account, user/org/datasource/service-account/plugin APIs, Explore, snapshots,
  public dashboards and every non-read request except `POST /grafana/api/ds/query` (the dashboards'
  queries). It strips `Authorization` and `Cookie` (the public path is always anonymous) and the
  client IP headers (Grafana's anonymous-device table only ever sees 127.0.0.1). Grafana itself:
  sign-up, Explore, snapshots, public dashboards, live, plugin admin, analytics, update checks,
  news, gravatar and its own SMTP are off; CSP on; `X-Frame-Options: deny`; version hidden.
  The "Sign in" link in the header leads to the 404 on the public path, by design.
- **Admin (local only):** `ssh -L 3300:127.0.0.1:3310 <host>`, then
  http://localhost:3300/grafana/login as `regagent-admin`, password `GF_SECURITY_ADMIN_PASSWORD`
  in `deploy/observability/.env` (mode 600, gitignored; `make metrics-env` generates it). The
  password is used when the Grafana volume is first created; to change it later:
  `docker compose exec grafana grafana cli admin reset-admin-password '<new>'` (and update `.env`).
- Anonymous viewers can send any PromQL query through `/api/ds/query`, so everything in
  Prometheus is effectively public: keep it aggregate (METRICS_CONTRACT.md §1). Query cost is
  bounded by `--query.timeout=30s`, `--query.max-samples`, `--query.max-concurrency`; this Caddy
  build has no rate-limit module.

## Alerts

Rules: `rules/app.yml`, `abuse.yml`, `availability.yml`, `host.yml`, `slo.yml`; unit tests in
`rules/tests/`. Every alert is named `Regagent*`, carries `severity` (`page` | `warning`; `none`
for the watchdog) and a `runbook_url` in `docs/runbooks/`; Prometheus adds `project=regagent`.

| Alert | Fires when | Severity |
|---|---|---|
| RegagentRequestsFailed | any request ended `failed` in the last hour | warning |
| RegagentQueueStuck | `queue_depth` > 20 for 10 min | page |
| RegagentWorkerMetricsDown | worker metrics endpoint down 5 min | page |
| RegagentIngestStalled | `ingest_heartbeat_age_seconds` > 600 for 5 min (once exported) | page |
| RegagentBreakerOpen | a breaker open > 10 min | warning |
| RegagentBudgetHigh / RegagentBudgetExhausted | a daily budget > 80 % / exhausted | warning / page |
| RegagentPreauthRejectionSpike / RegagentUnauthenticatedSpike | last hour > 3x the 7-day hourly average (and ≥ 10) | warning |
| RegagentWebHealthDown | viewer /health (public or local) failing 5 min | page |
| RegagentDropHealthDown | drop /health failing 5 min | warning |
| RegagentMailEndpointDown | IMAPS 993 or submission 587 failing 5 min | page |
| RegagentEgressTunnelDown / RegagentEgressViaTunnelFailing | tunnel port closed 5 min / no HTTPS through it 15 min | page / warning |
| RegagentTLSCertExpiringSoon / RegagentTLSCertExpiryCritical | any probed certificate < 14 d / < 3 d | warning / page |
| RegagentPostgresDown / RegagentRedisDown | data store unreachable 2 min | page |
| RegagentRedisUnauthenticatedAccess | Redis answers unauthenticated clients 10 min | warning |
| RegagentRedisMemoryHigh | > 80 % of maxmemory (redis_exporter) | warning |
| RegagentDiskSpaceLow / RegagentDiskSpaceCritical | a filesystem < 20 % / < 10 % free | warning / page |
| RegagentBackupStale / RegagentBackupMetricsMissing | last good backup > 26 h / freshness metric stale | warning |
| RegagentLatencySLOFastBurn / SlowBurn / Breached | reply latency SLO (95 % within 180 s) burn rates | page / warning / warning |
| RegagentMonitoringTargetDown | node/blackbox exporter or Alertmanager down 10 min | warning |
| RegagentWatchdog | always (dead man's switch heartbeat) | none |

### Turning alert delivery on

Today every alert goes to the `blackhole` receiver: nothing leaves the host. Delivery is
deliberately separate from the agent's own mailbox and SMTP account, so an alert about the agent's
mail still gets out. Edit `deploy/observability/.env` (template: `.env.example`), then
`docker compose up -d alertmanager` (it re-renders `alertmanager.yml.tmpl` at start; a bad value
stops it with a message in `docker compose logs alertmanager`).

- **Email via the host's Postfix** with a dedicated sender: create the `alerts@hsingh.app` mailbox
  first (it does not exist), then `ALERT_RECEIVER=email`, `ALERT_EMAIL_TO=<owner>`,
  `ALERT_SMTP_SMARTHOST=mail.hsingh.app:587`, `ALERT_SMTP_USERNAME=alerts@hsingh.app`,
  `ALERT_SMTP_PASSWORD=...`. This shares fate with the host's mail stack.
- **Email via an external SMTP relay**: same keys, the relay's host:port and credentials.
- **Phone push (ntfy) or any webhook**: `ALERT_RECEIVER=webhook`, `ALERT_WEBHOOK_URL=https://ntfy.sh/<long random topic>`
  (Alertmanager posts its JSON; ntfy shows it as the message body).
- **Dead man's switch** (recommended with either): `ALERT_WATCHDOG_RECEIVER=watchdog`,
  `ALERT_WATCHDOG_URL=<e.g. a healthchecks.io ping URL>`; the service alerts when the 5-minute
  heartbeat stops (dead host, Prometheus or Alertmanager).

Test once enabled:
`docker compose exec alertmanager amtool alert add RegagentTest severity=warning --annotation=summary=test --alertmanager.url=http://127.0.0.1:9093`.

## After the cutover: Postgres and Redis exporters

Until then Postgres and Redis are covered by the blackbox probes. Both exporters need read-only
monitoring logins that do not exist yet:

1. Postgres: generate a password, apply `deploy/sql/monitor_role.sql` as the bootstrap superuser
   with its SCRAM verifier (`printf %s "$pw" | python3 deploy/lib/envtool.py scram`), add
   `agent_monitor` to the `host agent ...` line in `deploy/postgres/pg_hba.conf`, reload Postgres
   (`SELECT pg_reload_conf()`).
2. Redis: in `deploy/redis/entrypoint.sh` substitute `__MONITOR_PASSWORD_SHA256__` from a new
   `REDIS_MONITOR_PASSWORD` (compose env for the redis service), uncomment the `monitor` line in
   `deploy/redis/users.acl.template`, restart Redis.
3. `deploy/observability/exporters.env` (mode 600): `DATA_SOURCE_PASS=<pg password>`,
   `REDIS_PASSWORD=<redis monitor password>`.
4. Uncomment the `regagent-postgres` and `regagent-redis` jobs in `prometheus/prometheus.yml`,
   remove `profiles: ["db-exporters"]` from both services (or run
   `docker compose --profile db-exporters up -d postgres-exporter redis-exporter`), reload Prometheus.

## Validation (2026-10-05)

`make metrics-check` passes (promtool config/rules, rule unit tests, amtool, blackbox, dashboards
current). All stack targets and probes up except the app endpoints not yet exported (worker
9710 not listening, web /metrics 404, ingest 9711) and `redis-auth` (Redis cutover not applied).
Public checks: `/grafana/` 200 anonymous with all 7 dashboards rendering (headless Chromium, no
console or CSP errors), `/grafana/login` 404, `/grafana/api/admin/settings` 404, app `/health` 200
and app routes unchanged.
