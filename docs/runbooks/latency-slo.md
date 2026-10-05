# Runbook: reply latency SLO

This document is written in ASD-STE100 Simplified Technical English.

The objective: 95% of requests get their reply within 180 s after the email arrives. The metric is `request_e2e_seconds`. This is an evaluation target for the MVP. It is not a contractual SLA.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentLatencySLOFastBurn` | `page` | For 2 min, more than 72% of the replies of the last 1 h were slow. Also, more than 72% of the replies of the last 5 min were slow. At least 3 replies in 1 h. |
| `RegagentLatencySLOSlowBurn` | `warning` | For 15 min, more than 30% of the replies of the last 6 h were slow. Also, more than 30% of the replies of the last 30 min were slow. At least 5 replies in 6 h. |
| `RegagentLatencySLOBreached` | `warning` | For 5 min, more than 5% of the replies of the last 1 h were slow. At least 5 replies in 1 h. |

A slow reply takes more than 180 s. The error budget is 5%. The fast burn rate is 14.4 times the budget, and the slow burn rate is 6 times the budget.

Incident severity: SEV3. SEV2 for a fast burn.

## 1. Find where the time goes

1. Open the "Overview" dashboard. The panel "Time spent in each stage" shows the slow state (`stage_duration_seconds` by `stage`).
2. Open the "Regulator portals" dashboard. Examine the fetch latency for each provider.
3. Open the "Models" dashboard. Examine the call latency for each model.

## 2. Usual causes

| Cause | Runbook |
|---|---|
| A slow portal, or a portal that limits our rate. UARB through the tunnel is the most frequent. | [Circuit breaker open](breaker-open.md), [Egress tunnel down](egress-tunnel-down.md) |
| Model latency or model retries | [LLM provider incident](llm-provider-incident.md) |
| A queue backlog, or limiter waits on the "Abuse & rate limits" dashboard | [Queue stuck](queue-stuck.md) |
| Ingest reads new mail late, because IMAP IDLE does not wake | [Ingest stalled](ingest-stalled.md) |
