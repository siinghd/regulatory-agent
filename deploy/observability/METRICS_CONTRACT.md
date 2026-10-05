# Metrics contract: what the app exports

The dashboards (`grafana/build_dashboards.py`) and the alert rules (`rules/*.yml`) use the exact
names and labels below. To change a name or a label, change it here first, then in
`agent/metrics.py`, the dashboards and the rules.

All metrics in section 3 are in `agent/metrics.py` and the code emits them. Section 4 lists label
values that the contract defines but the code does not emit today.

## 1. Privacy rules (for every metric; a test checks them)

The dashboards are **public** (anonymous, read-only, https://uarb.hsingh.app/grafana/). An
anonymous viewer can query every series in Prometheus. For this reason, metrics are aggregate
counts and durations only.

**Allowed label names (complete list):**

| Label | Values |
|---|---|
| `provider` | provider `name` (`uarb`, `oeb`, `ferc`, ...) or `none` |
| `outcome` | per-metric enum (section 3) |
| `stage` | request state names (`agent/store.py`) |
| `model` | a model id from `LLM_MODELS` or the Jev model name (configuration, bounded) |
| `classifier` | `rules`, `jev`, `llm` |
| `escalated` | `true`, `false` |
| `verdict` | `pass`, `fail`, `none` (`AuthVerdict`) |
| `key_type` | `sender`, `domain`, `global`, `preauth`, `inbound`, `inflight`, `llm`, `portal`, `bytes`, `web` |
| `kind` | per-metric enum (section 3) |
| `state` | `done`, `failed`, `rejected`, `clarify` |
| `reason` | a fixed enum in code (never an error message) |
| `cause` | a fixed enum in code (never an error message) |

These labels are also allowed, because their values are fixed sets:
`final_state` (final states), `dependency` (provider names, `drop`, `smtp`, `openrouter:<model id>`),
`limiter` (limiter names, with `portal_budget:<provider>` and `web_<route class>`), `decision`
(`allowed`, `limited`, `deferred`, `unavailable`), `budget` (`llm_usd`, `portal:<provider>`).
The client library adds `le` and `quantile`.

**Never a label value:** email addresses or local parts, sender domains, subjects, matter
numbers, document titles or file names, IP addresses or networks, request ids, UUIDs, message
ids, tokens, drop keys, URLs, free-text error or exception messages.

`retries_total{cause}` uses the fixed set `RETRY_CAUSES` in `agent/metrics.py`:
`portal_unavailable`, `scrape_error`, `timeout`, `llm_unavailable`, `smtp_temp`, `drop_unavailable`,
`db`, `redis`, `lock_contention`, `breaker_open`, `budget`, `disk_low`, `auth_temperror`, `internal`.
`agent.pipeline.retry_cause` maps each exception to 1 of them. A value outside the set becomes
`internal`.

**Test:** `tests/metrics_privacy.py` holds the rules (allowed label names, the enum values in
section 3, and patterns for email addresses, IP addresses, matter numbers and UUIDs).
`tests/unit/test_metrics.py` runs it after it exercises the instrumentation, and
`tests/conftest.py` runs it at the end of each test session over every recorded value.

## 2. Endpoints (all on 127.0.0.1, never public)

| Process | Address | Prometheus job | Notes |
|---|---|---|---|
| worker | `127.0.0.1:9710/metrics` (`METRICS_PORT`) | `regagent-worker` | All metrics. `queue_depth` and `ingest_heartbeat_age_seconds` come only from the worker. |
| ingest | `127.0.0.1:9711/metrics` (`METRICS_INGEST_PORT`) | `regagent-ingest` | Pre-auth limiter decisions (`preauth_ip`, `preauth_domain`, `inbound_minute`) and rows that ingest creates already rejected |
| web | `127.0.0.1:8710/metrics` | `regagent-web` | Web rate limits. The app answers only a loopback peer without `X-Forwarded-For`, `X-Real-IP` or `Forwarded`, else 404. Caddy also answers 404 for `/metrics` on the public site. |

A port value of 0 turns the endpoint off. If the port is taken, the process runs without metrics
and logs `metrics.unavailable`.

Counters live in each process. Each process exports what it counts, and queries use `sum()`
across jobs. Each event is counted in exactly 1 process.

NOTE: On 2026-10-05 the running worker and ingest containers were older than this code, and
nothing listened on 9710 or 9711. The table describes the code.

## 3. Exported metrics

`prometheus_client` adds `_total` to counters: `Counter("requests")` is exported as
`requests_total`.

| Exported name | Type | Labels (values) | Process | When / value | Used by |
|---|---|---|---|---|---|
| `requests_total` | counter | `final_state` | worker, ingest | 1 time for each request, at its final state | Overview, Home, `RegagentRequestsFailed` |
| `provider_requests_total` | counter | `provider`, `state` | worker, ingest | With `requests_total`, by the regulator of the request (`none` if it never got one) | Overview "Requests by regulator" |
| `retries_total` | counter | `cause` (section 1) | worker | Each failed attempt that the worker retries | Overview |
| `llm_cost_usd_total` | counter | none | worker | LLM spend in USD, as OpenRouter reports it | Models |
| `stage_duration_seconds` | histogram | `stage` | worker | Time in each state before the next state. Buckets: `0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 7200`. | Overview |
| `request_e2e_seconds` | histogram | `outcome` (`done`, `failed`, `clarify`) | worker | 1 time for each request, when SMTP accepts its reply. Value: SMTP acceptance time minus the receipt time of the email. Buckets: `5, 10, 20, 30, 60, 90, 120, 180, 240, 300, 600, 1200, 1800, 3600` (the SLO rules need `180`). | SLO rules (`RegagentLatencySLOFastBurn`, `RegagentLatencySLOSlowBurn`, `RegagentLatencySLOBreached`), Overview, Home |
| `queue_depth` | gauge | none | worker only | Jobs in the arq queue. The `refresh` cron sets it every minute. | Overview, Home, `RegagentQueueStuck` |
| `ingest_heartbeat_age_seconds` | gauge | none | worker only | Seconds since ingest wrote `ingest:heartbeat` in Redis; `+Inf` if the key is missing. The `refresh` cron sets it every minute. | `RegagentIngestStalled` (> 600 s) |
| `breaker_open` | gauge | `dependency` | worker | 1 while the breaker is open. The `refresh` cron sets it for each provider, `drop`, `smtp` and `openrouter:<model>`. The `typesafe` breaker exists, but the cron does not export it, so `RegagentBreakerOpen` cannot fire for TypeSafe. | Overview, Portals, Models, Delivery, `RegagentBreakerOpen` |
| `limiter_decisions_total` | counter | `limiter`, `decision` | worker, ingest, web | Each rate-limit or budget decision | Abuse, `RegagentPreauthRejectionSpike` |
| `budget_used`, `budget_limit` | gauge | `budget` | worker | Use and limit of each daily budget. The `refresh` cron sets them. | Overview, Portals, Models, `RegagentBudgetHigh`, `RegagentBudgetExhausted` |
| `budget_exhausted_total` | counter | `budget` | worker | 1 time for each budget and UTC day, when the budget runs out | `RegagentBudgetExhausted` |
| `provider_fetch_seconds` | histogram | `provider`, `outcome` (`ok`, `not_found`, `error`, `timeout`, `blocked`) | worker | Each call to a portal: a matter lookup, a listing or 1 downloaded file. Buckets: `0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600`. | Regulator portals |
| `provider_visits_total` | counter | `provider` | worker | Each visit counted against the daily portal budget | Regulator portals |
| `model_calls_total` | counter | `kind` (`jev`, `llm`), `model`, `outcome` (`ok`, `error`, `timeout`, `refused`) | worker | Jev: 1 time for each call (its short internal retries are inside it). LLM: 1 time for each attempt, for each model tried. | Models |
| `model_call_seconds` | histogram | `kind`, `model` | worker | Duration of each call. Buckets: `0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120`. | Models |
| `gate_decisions_total` | counter | `classifier` (`rules`, `jev`, `llm`), `escalated` (`true`, `false`), `outcome` (`accept`, `reject`, `clarify`) | worker | 1 time for each email that a classifier read. `classifier` is the one whose answer the gate used. `escalated` is `true` when Jev gave the email to the LLM. | Models "escalation rate" |
| `auth_verdicts_total` | counter | `verdict` (`pass`, `fail`, `none`) | worker | 1 time for each message, after the sender authentication check. The check runs in the worker (`agent/pipeline.py`), not in ingest. | Delivery and mail, `RegagentUnauthenticatedSpike` |
| `outbound_messages_total` | counter | `kind` (`ack`, `reply`, `notice`), `outcome` (`sent`, `undeliverable`, `deferred`, `suppressed`) | worker | 1 time for each result of an outbound email attempt | Delivery and mail |
| `deliveries_total` | counter | `kind` (`attachment`, `drop`), `outcome` (`ok`, `error`) | worker | 1 time for each document delivery | Delivery and mail "drop against attachment" |
| `citations_total` | counter | `outcome` (`kept`, `dropped`, `support_failed`) | worker | Claims that a new summary proposed: kept, dropped (grounding, figures, limits), or dropped by the support check | No panel or rule yet. The `/status` page reads the citation counts from Postgres, not from this metric. |
| `web_rate_limited_total` | counter | `kind` (`progress`, `progress_json`, `files`, `citation`, `default`) | web | Each 429 from `agent/web/ratelimit.py` | Abuse "Web 429s" |

The client library also exports `process_*` metrics. `agent/metrics.py` removes `python_info` and
`python_gc_*`, because their labels are not in the allowed list.

## 4. Values defined but not emitted

The contract and `tests/metrics_privacy.py` allow these values, but no code path emits them today.
A panel or a rule that filters on them shows no data.

| Metric | Label value | Note |
|---|---|---|
| `provider_fetch_seconds` | `outcome="deferred"` | `agent.metrics.fetch_outcome` returns only `ok`, `not_found`, `blocked`, `timeout` or `error`. A wait for the portal budget shows as `limiter_decisions_total{limiter="portal_budget:<provider>",decision="deferred"}`. |
| `model_calls_total` | `outcome="budget"` | When the LLM budget is spent, the agent makes no call, so nothing is counted here. The refusal shows as `limiter_decisions_total{limiter="llm_budget",decision="limited"}`. |

## 5. Recording rules that the dashboards use (Prometheus side, nothing for the app to do)

`regagent:request_e2e_slow:ratio_rate{5m,30m,1h,6h}`, `regagent:request_e2e:{p50,p95}_1h`,
`regagent:request_e2e:count{1h,6h}`, `regagent:preauth_limited:{increase1h,avg_hourly_7d}`,
`regagent:auth_not_pass:{increase1h,avg_hourly_7d}`, `regagent:limiter_decisions:rate5m_by_key_type`,
`regagent:filesystem_avail:ratio`. Host side: `regagent_backup_*` (textfile collector, `backup_metrics.sh`).
