# Runbook: Postgres down

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentPostgresDown` | `page` | For 2 min, the TCP connection to 127.0.0.1:5442 fails, or `pg_up` is 0. |

`pg_up` comes from `postgres_exporter`. This exporter runs only after the least-privilege cutover (profile `db-exporters`). Until then, only the TCP probe applies.

Incident severity: SEV1 or SEV2. The agent cannot ingest, process or show anything. Parent document: [Incident response](../policies/incident-response.md).

## 1. Diagnose

1. Examine the container.

   ```bash
   docker compose ps postgres
   ```

2. Read the Postgres log. Look for `PANIC`, `could not write` or `out of memory`.

   ```bash
   docker compose logs --tail 100 postgres
   ```

3. Examine the free disk space. A full disk stops Postgres.

   ```bash
   df -h /var/lib/docker
   ```

   If the disk is full, refer to [Disk full](disk-full.md).

## 2. Repair

1. If the container stopped or is unhealthy, start it and wait until it is healthy.

   ```bash
   docker compose up -d --wait postgres
   ```

   The app processes connect again without help. Until then, `/health` shows `"db": false`.

2. If the data is corrupt or the volume is lost, restore the newest backup. Refer to [Operations, section 6](../guide/operations.md#6-restore) and [Business continuity](../policies/business-continuity.md).

WARNING: Never run `docker volume rm` on the Postgres volume.

## 3. After the repair

1. Run the consistency checks.

   ```bash
   docker compose exec worker ragent reconcile
   ```

   Expected result: exit code 0 and `"anomalies": []`.

2. Watch the queue decrease on the "Overview" dashboard.
