# Runbook: queue stuck

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentQueueStuck` | `page` | For 10 min, more than 20 jobs wait in the arq queue (`queue_depth`). |

Incident severity: SEV2. The worker does not process requests. Parent document: [Incident response](../policies/incident-response.md).

## 1. Make sure that the worker operates

1. Examine the container.

   ```bash
   docker compose ps worker
   ```

   Expected result: `healthy`.

2. Read the worker log.

   ```bash
   docker compose logs --since 30m worker | tail -100
   ```

3. Run the worker healthcheck.

   ```bash
   docker compose exec worker ragent healthcheck worker
   ```

4. Find the state of the kill switch. Read the field `paused` of the deep health check.

   ```bash
   curl -s 127.0.0.1:8710/health/deep
   ```

5. If `paused` is `true` and the cause is gone, release the kill switch.

   ```bash
   docker compose exec worker ragent resume
   ```

   Expected result: `resumed`.

6. If the worker is unhealthy or does not move, restart it. Jobs in progress return to the queue. The pipeline continues from the state in Postgres.

   ```bash
   docker compose restart worker
   ```

## 2. The worker is healthy, but the queue increases

| Cause | Signal | Runbook |
|---|---|---|
| A slow dependency | An open breaker, or long stage times on the "Overview" dashboard ("Time spent in each stage") | [Circuit breaker open](breaker-open.md) |
| A flood of inbound mail | The "Abuse & rate limits" dashboard | [Spoofing wave](spoofing-wave.md), if the senders are not authenticated |
| Capacity | The limiters (`inflight`, each sender, global) make work wait. This is the design. The panel "Limited or deferred, by limiter" shows the waits. | No action, unless the waits continue |
