# Runbook: Redis down or memory high

This document is written in ASD-STE100 Simplified Technical English.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentRedisDown` | `page` | For 2 min, 127.0.0.1:6392 does not answer a PING, or `redis_up` is 0. |
| `RegagentRedisMemoryHigh` | `warning` | For 10 min, Redis uses more than 80% of `maxmemory`. |

`redis_up` and the memory metrics come from `redis_exporter`. This exporter runs only after the least-privilege cutover (profile `db-exporters`). Until then, only the PING probe applies, and `RegagentRedisMemoryHigh` cannot fire.

Redis holds the arq queue, the limits, the locks and the breakers. Incident severity: SEV2 when Redis is down.

## 1. Redis down

1. Examine the container and read its log.

   ```bash
   docker compose ps redis && docker compose logs --tail 100 redis
   ```

2. Start Redis.

   ```bash
   docker compose up -d redis
   ```

   Redis loads the queue from its append-only file (AOF). The sweeper adds each request that is missing from the queue again, from the state in Postgres. The app processes connect again without help.

While Redis is down, the breakers let all calls through, and the web limits do not apply. The web process writes a warning.

## 2. Memory high

Redis has `maxmemory 256mb` with `noeviction`. At the limit, writes fail with an error, for example a failed enqueue. Redis does not drop queue entries.

1. Look for a key leak, for example keys without a TTL. Do this as the `admin` user.

   ```bash
   docker compose exec -e REDISCLI_AUTH=<REDIS_ADMIN_PASSWORD> redis redis-cli --user admin --bigkeys
   ```

   Before the cutover, Redis has no ACL. Then run `redis-cli --bigkeys` without `REDISCLI_AUTH` and without `--user admin`.

2. Correct the cause in the code through a normal change.

## 3. Unauthenticated access

The alert `RegagentRedisUnauthenticatedAccess` belongs to the ACL cutover. Redis answers an unauthenticated PING and does not refuse it with `NOAUTH`. Refer to [Cutover to least privilege](cutover-least-privilege.md).
