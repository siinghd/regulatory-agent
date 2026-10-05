# Metrics contract: what the app exports

The dashboards (`grafana/build_dashboards.py`) and alert rules (`rules/*.yml`) are written against
the exact names and labels below. Change a name or label here first, then in both places.
Everything in "To add" is already wired into panels and rules: they show "No data" / stay silent
until the app exports the metric.

## 1. Privacy rules (apply to every metric; enforced by a unit test)

The dashboards are **public** (anonymous, read-only, https://uarb.hsingh.app/grafana/), and any
anonymous viewer can query every series in Prometheus. So metrics are aggregate counts and
durations only.

**Allowed label names (complete list):**

| Label | Values |
|---|---|
| `provider` | provider `name` (`uarb`, `oeb`, `ferc`, ...) or `none` |
| `outcome` | per-metric enum below |
| `stage` | request state names (`agent/store.py`) |
| `model` | a model id from `LLM_MODELS` or the Jev model name (configuration, bounded) |
| `classifier` | `rules`, `jev`, `llm` |
| `escalated` | `true`, `false` |
| `verdict` | `pass`, `fail`, `none` (`AuthVerdict`) |
| `key_type` | `sender`, `domain`, `global`, `preauth`, `inbound`, `inflight`, `llm`, `portal`, `bytes`, `web` |
| `kind` | per-metric enum below |
| `state` | `done`, `failed`, `rejected`, `clarify` |
| `reason` | a fixed enum defined in code (never an error message) |
| `cause` | a fixed enum defined in code (never an error message) |

Already shipped in `agent/metrics.py` and also allowed, because their values are fixed sets:
`final_state` (states), `dependency` (provider names, `drop`, `smtp`, `openrouter:<model id>`),
`limiter` (limiter names incl. `portal_budget:<provider>`, `web_<route class>`), `decision`
(`allowed`, `limited`, `deferred`, `unavailable`), `budget` (`llm_usd`, `portal:<provider>`).
`le` and `quantile` are added by the client library.

**Never a label value:** email addresses or local parts, sender domains, subjects, matter
numbers, document titles or file names, IP addresses or networks, request ids / UUIDs / message
ids / tokens / drop keys, URLs, free-text error or exception messages.

**Fix needed in shipped code:** `retries_total{cause}` takes `cause` from the error text
(`error.split(":", 1)[0][:40]` in `agent/metrics.py::_cause`). It must map to a fixed enum (the
dependency name, or an exception class from a known list, else `other`).

**Unit test (instrumentation worker):** collect `prometheus_client.REGISTRY` after exercising the
code paths and assert (a) every label name is in the allowlist above, (b) every label value of the
enum labels is in its declared set, (c) no label value matches an email, IPv4/IPv6 or UUID pattern.

## 2. Endpoints (all on 127.0.0.1, never public)

| Process | Address | Prometheus job | Status |
|---|---|---|---|
| worker | `127.0.0.1:9710/metrics` (`METRICS_PORT`) | `regagent-worker` | exists in code; **not listening on the host today** (alert `RegagentWorkerMetricsDown` fires) |
| web | `127.0.0.1:8710/metrics` | `regagent-web` | **to add**. Caddy answers 404 for `/metrics` on the public site; the app should also refuse it unless the peer is loopback and no `X-Forwarded-For`/`X-Real-IP` is present |
| ingest | `127.0.0.1:9711/metrics` (e.g. `INGEST_METRICS_PORT`) | `regagent-ingest` | **to add**: pre-auth limiter decisions and auth verdicts are counted in ingest, which exposes nothing today |

Counters live per process; each process exports what it counts, and queries `sum()` across jobs.
Count each event in exactly one process.

## 3. Already exported (`agent/metrics.py`, worker)

`prometheus_client` appends `_total` to counters: `Counter("requests")` is exported as `requests_total`.

| Exported name | Type | Labels | Used by |
|---|---|---|---|
| `requests_total` | counter | `final_state` | Overview, Home, `RegagentRequestsFailed` |
| `retries_total` | counter | `cause` (see fix above) | Overview |
| `llm_cost_usd_total` | counter | none | Models |
| `stage_duration_seconds` | histogram | `stage` | Overview |
| `queue_depth` | gauge | none | Overview, Home, `RegagentQueueStuck` |
| `breaker_open` | gauge | `dependency` | Overview, Portals, Models, Delivery, `RegagentBreakerOpen` |
| `limiter_decisions_total` | counter | `limiter`, `decision` | Abuse, `RegagentPreauthRejectionSpike` (needs the ingest endpoint) |
| `budget_used`, `budget_limit` | gauge | `budget` | Overview, Portals, Models, `RegagentBudgetHigh` |
| `budget_exhausted_total` | counter | `budget` | `RegagentBudgetExhausted` |

## 4. To add

Names below are the **exported** names. Buckets are required where given (rules depend on them).

| Exported name | Type | Labels (values) | Process | When / value |
|---|---|---|---|---|
| `request_e2e_seconds` | histogram | `outcome` (`done`, `failed`, `clarify`) | worker | Once per request, when its first reply (documents, apology or clarification) is accepted by SMTP. Value: SMTP acceptance time minus the time the email was received (ingest's received timestamp). Buckets **must include 180**: `5, 10, 20, 30, 60, 90, 120, 180, 240, 300, 600, 1800, 3600`. Used by the SLO rules (`rules/slo.yml`), Overview, Home. |
| `ingest_heartbeat_age_seconds` | gauge | none | worker | Set by the minute `refresh` cron from Redis `ingest:heartbeat` (`agent.health.ingest_heartbeat_age`); `+Inf` when the key is missing. `RegagentIngestStalled` (> 600 s). |
| `provider_requests_total` | counter | `provider`, `state` | worker | With `requests_total`, at the final state, once per provider the request targeted (`none` if it never got one). Overview "Requests by regulator". |
| `provider_fetch_seconds` | histogram | `provider`, `outcome` (`ok`, `not_found`, `error`, `timeout`, `blocked`, `deferred`) | worker | Once per portal fetch operation (search or document download). Buckets: `0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600`. Regulator portals. |
| `provider_visits_total` | counter | `provider` | worker | Each visit counted against the portal budget (`Budgets.count_portal_visit`). Regulator portals. |
| `model_calls_total` | counter | `kind` (`jev`, `llm`), `model`, `outcome` (`ok`, `error`, `timeout`, `refused`, `budget`) | worker | Once per call to TypeSafe Jev or OpenRouter, including retries. Models. |
| `model_call_seconds` | histogram | `kind`, `model` | worker | Duration of each call. Buckets: `0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120`. Models. |
| `gate_decisions_total` | counter | `classifier` (`rules`, `jev`, `llm`), `escalated` (`true`, `false`), `outcome` (`accept`, `reject`, `clarify`) | worker | Once per gate decision; `classifier` is the one that decided, `escalated` whether a cheaper one passed it on. Models "escalation rate". |
| `outbound_messages_total` | counter | `kind` (`ack`, `reply`, `notice`: the kinds in `agent/mail/outbound.py`), `outcome` (`sent`, `undeliverable`, `deferred`, `suppressed`) | worker | Once per outbound email attempt result. Delivery & mail. |
| `deliveries_total` | counter | `kind` (`attachment`, `drop`), `outcome` (`ok`, `error`) | worker | Once per document delivery. Delivery & mail "drop vs attachment". |
| `auth_verdicts_total` | counter | `verdict` (`pass`, `fail`, `none`) | ingest | Once per inbound message after the sender-authentication check. Delivery & mail, `RegagentUnauthenticatedSpike`. |
| `web_rate_limited_total` | counter | `kind` (route class: `progress`, `progress_json`, `files`, `citation`, `default`) | web | Each 429 answered by `agent/web/ratelimit.py`. Abuse "Web 429s". |

The ingest endpoint also makes the existing `limiter_decisions_total{limiter=~"preauth_.*|inbound_minute"}`
visible (they are counted in ingest today but never exported).

## 5. Recording rules the dashboards use (Prometheus side, nothing for the app to do)

`regagent:request_e2e_slow:ratio_rate{5m,30m,1h,6h}`, `regagent:request_e2e:{p50,p95}_1h`,
`regagent:request_e2e:count{1h,6h}`, `regagent:preauth_limited:{increase1h,avg_hourly_7d}`,
`regagent:auth_not_pass:{increase1h,avg_hourly_7d}`, `regagent:limiter_decisions:rate5m_by_key_type`,
`regagent:filesystem_avail:ratio`. Host-side: `regagent_backup_*` (textfile collector, `backup_metrics.sh`).
