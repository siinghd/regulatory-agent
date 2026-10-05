# Observability

This document is written in ASD-STE100 Simplified Technical English.

This document describes the logs, the audit trail, the metrics, the dashboards and the alerts of the agent.

NOTE: The observability stack (Prometheus, Alertmanager, Grafana and exporters) and the public `/status` page (`agent/web/status.py`) were new on 2026-10-05. The configuration is in `docker-compose.yml`, `deploy/observability/` and `deploy/caddy/uarb.caddy`. Run `make metrics-status` to see what runs on the host now.

## 1. Signals

| Signal | Where | Retention |
|---|---|---|
| Application logs | Docker `json-file`, 1 JSON object for each line | 20 MB × 5 files for each container |
| Audit trail | Postgres `events`, and daily JSONL files in `data/audit/` | 400 days |
| Metrics | Worker on `127.0.0.1:9710/metrics`, ingest on `127.0.0.1:9711/metrics`, web at `/metrics` (loopback only). Prometheus scrapes them. | Prometheus: 30 days or 5 GB |
| Health endpoints | `/health`, `/health/deep`, `ragent healthcheck` | Not stored |
| Public status page | `https://uarb.hsingh.app/status` | Not stored. The web process calculates it again after 60 s. |
| Reconciliation | `ragent reconcile`, and a daily `reconcile` event | 400 days (event) |
| Compliance check | `/home/deploy/compliance/compliance-<date>.txt` | Operator decision |
| Deploy log | `deploy/deploys.log`, and a `deploy` event | 400 days (event) |
| Caddy access log | `/var/log/caddy/uarb-access.log` (JSON) | 720 h |

## 2. Logs

The app writes structured JSON logs (structlog). Each line has `event`, `level` and `timestamp`. Lines of a request job also have `request_id`, `job_try` and `attempt`.

A line with `"alert": true` needs the attention of the operator. Examples:

| Event | Description |
|---|---|
| `request.dead_letter` | A request ended `failed` |
| `outbound.undeliverable` | The outbox stopped the delivery of an email |
| `breaker.open` | A circuit breaker opened |
| `budget.exhausted` | A daily budget is spent (1 time for each budget and day) |
| `disk.low`, `ingest.disk_low`, `disk.full` | The data disk is nearly full |
| `admin.paused` | The kill switch is on |
| `dsar.requested` | A sender emailed "DELETE MY DATA" |
| `request.park_exhausted` | A parked job reached the arq try limit. The sweeper continues it. |

At each start, the worker writes `worker.models` (which model does which job) and `typesafe.data_policy` (what goes to TypeSafe).

Logs never contain an email address in clear text in an event. Secrets are never logged.

### 2.1 Procedure: find alert lines

1. Show the alert lines of the worker for the last 24 h.

   ```bash
   docker compose logs --no-log-prefix --since 24h worker | grep '"alert": true'
   ```

   Expected result: no lines, or lines that you can explain.

2. Do the same for `ingest`.

   ```bash
   docker compose logs --no-log-prefix --since 24h ingest | grep '"alert": true'
   ```

## 3. Audit trail

The `events` table records each state change, each email sent, each model call, each limit decision, each admin command and each deploy.

- Each row has `actor` (the database role, set by a trigger), `component`, `app_version` and `subject_h` (an HMAC of the address).
- The table is append-only for all roles.
- Each day, the worker exports the events of the day before to `data/audit/YYYY-MM-DD.jsonl`.
- The first line of each export has the SHA-256 of the export before it.

`ragent audit <request id>` shows the timeline of 1 request. `ragent audit --verify` examines the hash chain ([Operations](operations.md)).

## 4. Application metrics

`agent/metrics.py` defines the metrics. Each process exports the events that it counts:

- the worker on `127.0.0.1:9710` (`METRICS_PORT`);
- ingest on `127.0.0.1:9711` (`METRICS_INGEST_PORT`);
- web at `GET /metrics` on `127.0.0.1:8710`, only for a loopback client without the headers `X-Forwarded-For`, `X-Real-IP` and `Forwarded`. Caddy answers 404 for `/metrics` on the public site.

`deploy/observability/METRICS_CONTRACT.md` gives each name, label, bucket and privacy rule. Labels never contain an address, a domain, an IP address, a matter number, a title or a request id.

| Metric | Type | Labels | Description |
|---|---|---|---|
| `requests_total` | Counter | `final_state` | Requests that reached a final state |
| `retries_total` | Counter | `cause` | Failed attempts that the queue retried, by dependency or error type |
| `llm_cost_usd_total` | Counter | None | LLM spend that OpenRouter reported |
| `stage_duration_seconds` | Histogram | `stage` | Time in each state before the next state |
| `queue_depth` | Gauge | None | Jobs in the arq queue |
| `breaker_open` | Gauge | `dependency` | 1 while the breaker of the dependency is open |
| `limiter_decisions_total` | Counter | `limiter`, `decision` | Each limit decision: `allowed`, `limited`, `deferred` or `unavailable` |
| `budget_used` | Gauge | `budget` | Use of each daily budget today |
| `budget_limit` | Gauge | `budget` | The limit of each daily budget |
| `budget_exhausted_total` | Counter | `budget` | Budgets that were spent, 1 time for each budget and day |
| `request_e2e_seconds` | Histogram | `outcome` | Time from the receipt of the email to the SMTP acceptance of its reply |
| `ingest_heartbeat_age_seconds` | Gauge | None | Seconds since the last ingest heartbeat in Redis |
| `provider_requests_total` | Counter | `provider`, `state` | Requests that reached a final state, by regulator |
| `provider_fetch_seconds` | Histogram | `provider`, `outcome` | Each call to a portal: a matter lookup, a listing or 1 file |
| `provider_visits_total` | Counter | `provider` | Portal visits counted against the daily budget |
| `model_calls_total` | Counter | `kind`, `model`, `outcome` | Calls to TypeSafe Jev and to the OpenRouter models |
| `model_call_seconds` | Histogram | `kind`, `model` | Duration of each model call |
| `gate_decisions_total` | Counter | `classifier`, `escalated`, `outcome` | Gate decisions, by the classifier whose answer the gate used |
| `auth_verdicts_total` | Counter | `verdict` | Sender authentication results: `pass`, `fail` or `none`. The worker counts them. |
| `outbound_messages_total` | Counter | `kind`, `outcome` | Results of outbound email attempts |
| `deliveries_total` | Counter | `kind`, `outcome` | Document deliveries as a drop link or an attachment |
| `citations_total` | Counter | `outcome` | Summary claims: `kept`, `dropped` or `support_failed` |
| `web_rate_limited_total` | Counter | `kind` | Web requests that got HTTP 429, by route class |

A worker job refreshes `queue_depth`, `breaker_open`, the budget gauges and `ingest_heartbeat_age_seconds` every minute. Only the worker exports `queue_depth` and `ingest_heartbeat_age_seconds`.

NOTE: Counters are in the memory of each process. A restart sets them to 0. Prometheus functions such as `increase()` handle this.

## 5. Observability stack

| Service | Listen address | Purpose |
|---|---|---|
| Prometheus | `127.0.0.1:9090` | Scrapes the targets and evaluates the rules. Retention 30 days or 5 GB. |
| Alertmanager | `127.0.0.1:9093` | Groups and routes alerts |
| Grafana | `127.0.0.1:3310`, public read-only at `https://uarb.hsingh.app/grafana/` | Dashboards |
| node-exporter | `127.0.0.1:9100` | Host metrics |
| blackbox-exporter | `127.0.0.1:9117` | HTTP, SMTP, IMAP, TCP and Redis probes |
| postgres-exporter, redis-exporter | Profile `db-exporters`, off | Need the monitor logins. Apply `deploy/sql/monitor_role.sql` and the Redis `monitor` user at or after the least-privilege cutover (`deploy/observability/README.md`). |

The services start with `docker compose up -d`. They do not depend on the app services, so an app deploy does not touch them.

### 5.1 Scrape targets and probes

| Target | What it tells |
|---|---|
| Worker metrics | Section 4 |
| Web and ingest `/metrics` | The web rate limits, and the pre-authentication limits of ingest |
| `https://uarb.hsingh.app/health` and `http://127.0.0.1:8710/health` | The viewer through Cloudflare and Caddy, and directly |
| `https://drop.hsingh.app/health` | The file drop |
| `mail.hsingh.app:587` (STARTTLS), `mail.hsingh.app:993` (IMAPS) | Mail submission and IMAP |
| `127.0.0.1:1080` (TCP) | The SOCKS egress tunnel |
| `https://www.cloudflare.com/cdn-cgi/trace` through the tunnel, every 5 min | The tunnel carries traffic. A regulator portal is never the probe target, because each visit counts against the portal budget. |
| `127.0.0.1:5442` (TCP), `127.0.0.1:6392` (Redis `PING` gets `NOAUTH`) | Postgres is up. Redis is up, and its default user is still off. |
| Origin TLS certificate on `127.0.0.1:443` | The certificate that Caddy shows to Cloudflare |

## 6. Alerts

The rules are in `deploy/observability/rules/`. The files are the authoritative list. This table shows the groups on 2026-10-05.

| Group | Example alerts | Severity |
|---|---|---|
| Application | `RegagentRequestsFailed`, `RegagentQueueStuck`, `RegagentWorkerMetricsDown`, `RegagentIngestStalled`, `RegagentBreakerOpen` | `page` or `warning` |
| Budgets | `RegagentBudgetHigh` (above 80%), `RegagentBudgetExhausted` | `warning`, `page` |
| Availability | `RegagentWebHealthDown`, `RegagentMailEndpointDown`, `RegagentEgressTunnelDown`, `RegagentPostgresDown`, `RegagentRedisDown`, certificate expiry | Mostly `page` |
| Host | `RegagentDiskSpaceLow` (below 20%), `RegagentDiskSpaceCritical` (below 10%), `RegagentBackupStale` (older than 26 h), `RegagentWatchdog` | `warning`, `page`, `none` |
| Abuse | `RegagentPreauthRejectionSpike`, `RegagentUnauthenticatedSpike` (3 times the 7-day hourly average) | `warning` |
| SLO | Fast burn, slow burn and breach of the latency objective | `page`, `warning` |

The latency objective is: 95% of requests get their reply within 180 s of the email. The SLO rules use multi-window burn rates (14.4 over 1 h and 5 min, 6 over 6 h and 30 min). They use the histogram `request_e2e_seconds`.

### 6.1 Alert routing

Alertmanager renders its configuration from `deploy/observability/alertmanager/alertmanager.yml.tmpl` and `deploy/observability/.env`.

| Receiver | Effect |
|---|---|
| `blackhole` (default) | Nothing leaves the host |
| `email` | Alerts go to `ALERT_EMAIL_TO` through an SMTP server |
| `webhook` | Alerts go to `ALERT_WEBHOOK_URL` |

`page` alerts repeat every 1 h. Other alerts repeat every 4 h. `RegagentWatchdog` always fires. With a "dead man's switch" receiver, its absence shows that the alert path is broken.

CAUTION: With the default receiver `blackhole`, no alert reaches a person. Set `ALERT_RECEIVER` before you depend on alerts.

## 7. Dashboards

Grafana provisions these dashboards from `deploy/observability/grafana/dashboards/`: home, overview, portals, models, delivery, abuse and host. `build_dashboards.py` generates them.

The public Grafana is read-only. Anonymous users get the Viewer role. Caddy blocks the login, admin, account, Explore and API paths, and all requests that are not reads. The admin account works only on `127.0.0.1:3310` through an SSH tunnel. Caddy removes the client IP headers before Grafana, so Grafana stores no visitor IPs.

The home dashboard shows a disclaimer. It says that the dashboards are for the evaluation of an MVP. They show only aggregate metrics, without personal data or request contents. They are not a production SLA.

## 8. Public status page

The web process serves `/status` (`agent/web/status.py`). The page shows only aggregate numbers:

- the health of the components;
- the requests of the last 24 h and the last 7 days, by final state;
- the replies within the 180 s target, and the reply time at p50 and p95;
- the number of requests for each regulator;
- the kept and dropped citation claims of the last 7 days;
- the Jev calls and the escalations to the LLM of the last 7 days.

Of `requests`, the page reads only `state`, `provider`, `received_at` and `reply_sent_at`. It never reads an address, a subject, a matter number, an IP or a request id. The web process keeps the result for 60 s. If Postgres or Redis does not answer, the page tells which part is not available.

The page shows a disclaimer. It says that the page is for the evaluation of an MVP and has no personal data. It is not a service level agreement.

## 9. Procedures

### 9.1 Start the observability stack

1. Create the Grafana admin password file, if it does not exist.

   ```bash
   make metrics-env
   ```

2. Start the services and wait until they are healthy.

   ```bash
   make metrics-up
   ```

   Expected result: the command shows the containers, the targets, the probes and the URLs.

### 9.2 Examine the stack

1. Show the state of the containers, the targets, the probes and the alerts that fire.

   ```bash
   make metrics-status
   ```

   Expected result: all targets are up except the web and ingest `/metrics` targets. All probes show `UP`.

2. Do the static checks of the configuration and the rule unit tests.

   ```bash
   make metrics-check
   ```

   Expected result: the command ends without errors.

### 9.3 Stop the observability stack

1. Stop and remove the observability containers. The history in the named volumes stays.

   ```bash
   make metrics-down
   ```
