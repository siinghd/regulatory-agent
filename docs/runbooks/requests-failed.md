# Runbook: requests failed

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentRequestsFailed` | `warning` | At least 1 request reached the final state `failed` in the last 1 h. |

A `failed` request means that the retries stopped. An authenticated requester got 1 apology in place of the documents.

Incident severity: SEV3. SEV2 if all requests fail. Parent document: [Incident response](../policies/incident-response.md).

The retry policy is in `agent/config.py`. A request has at most 8 attempts and a deadline of 2 h after receipt. The backoff starts at 30 s and stops at 900 s. A random factor from 0.5 to 1.0 scales each delay. [Reliability](../guide/reliability.md) gives the formulas.

## 1. Find the requests and the cause

1. Find the failed requests in the worker log. Each `request.dead_letter` line has the `request_id` and the error.

   ```bash
   docker compose logs --no-log-prefix --since 2h worker | grep 'request.dead_letter'
   ```

2. Show the errors of the same period.

   ```bash
   docker compose logs --no-log-prefix --since 2h worker | grep -E '"level": "(error|critical)"' | tail -50
   ```

3. Show the audit trail of 1 request: the state changes, the retries and the errors.

   ```bash
   docker compose exec worker ragent audit <request_id>
   ```

4. Open the "Overview" dashboard. "Requests by outcome" shows the trend. "Retries by cause" shows the causes.

## 2. Usual causes

| Cause | Runbook |
|---|---|
| A regulator portal changed or is down. `RegagentBreakerOpen` fires. | [Circuit breaker open](breaker-open.md), [Portal sends wrong documents](portal-wrong-documents.md) |
| Model vendor errors, or a daily budget is used | [LLM provider incident](llm-provider-incident.md), [Budget](budget.md) |
| The mail server refuses outbound mail | [Mail endpoint down](mail-endpoint-down.md) |
| The egress tunnel is down (UARB only) | [Egress tunnel down](egress-tunnel-down.md) |

## 3. After the repair

The requester can send the request again after you correct the cause.

If many requests fail in sequence, set the kill switch while you correct the cause. The kill switch parks the requests and holds the outbound mail.

1. Set the kill switch.

   ```bash
   docker compose exec worker ragent pause --reason "<why>"
   ```

2. Correct the cause.
3. Release the kill switch.

   ```bash
   docker compose exec worker ragent resume
   ```

   Expected result: `resumed`. Parked requests continue within approximately 2 min.

[Operations, section 9.1](../guide/operations.md#91-stop-all-processing-kill-switch) describes the kill switch.
