# Runbook: circuit breaker open

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentBreakerOpen` | `warning` | For 10 min, `breaker_open{dependency}` is 1 for a dependency. |

Incident severity: SEV3. Requests that need the dependency park and wait. A parked try does not use an attempt. At the 2 h deadline, the requester gets 1 apology.

The gauge `breaker_open` shows these dependencies: the regulator portals (`uarb`, `oeb`, `ferc`), `drop`, `smtp` and `openrouter:<model>`. The `typesafe` breaker also exists, but the gauge does not show it. For this reason, this alert does not fire for TypeSafe.

A breaker opens after 5 availability failures in sequence. It stays open for 60 s. Each failed probe doubles the interval, up to 600 s. [Reliability](../guide/reliability.md) gives the full rules.

## 1. Find if the dependency is down

1. Read the breaker lines in the worker log.

   ```bash
   docker compose logs --since 1h worker | grep -i breaker
   ```

   Expected result: `breaker.open` lines name the dependency and the error.

2. For a portal, open the "Regulator portals" dashboard. Examine the fetch outcomes and the latency for each provider.
3. For `uarb`, examine the egress tunnel first. Refer to [Egress tunnel down](egress-tunnel-down.md).
4. For `smtp` or `drop`, examine the "Delivery & mail" dashboard and its probes. Refer to [Mail endpoint down](mail-endpoint-down.md) and [Viewer or drop health](web-down.md).
5. For `openrouter:<model>`, refer to [LLM provider incident](llm-provider-incident.md).

## 2. Recovery

After the open interval, the breaker lets 1 call through as a probe. If the probe succeeds, the breaker closes. When the dependency is available again, no action is necessary.

CAUTION: Do not delete `breaker:*` keys in Redis to force traffic to a dependency that still fails. This sends all parked requests to the dependency at the same time.

1. Wait for the dependency to recover.
2. If a portal changed its pages, refer to [Portal sends wrong documents](portal-wrong-documents.md).
