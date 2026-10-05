# Runbook: disk full

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentDiskSpaceLow` | `warning` | For 10 min, a file system has less than 20% free space. |
| `RegagentDiskSpaceCritical` | `page` | For 5 min, a file system has less than 10% free space. |

Other signals:

- `deploy/compliance_check.sh` writes `WARN` at 80% used, or `FAIL` at 90% used or less than 5 GB free.
- The guard of the agent (`DISK_MIN_FREE_BYTES`, 5,000,000,000 bytes) writes `ingest.disk_low` or `disk.low` log lines. Below the guard, ingest leaves new mail unread, and the worker does not download files.
- Postgres or Redis write errors.

Incident severity: SEV2 if ingest stopped or Postgres cannot write. SEV3 for all other cases. Parent document: [Incident response](../policies/incident-response.md).

NOTE: Other projects share this host. Examine all of the disk, not only the agent.

## 1. Find what uses the space

1. Show the free space.

   ```bash
   df -h / /var/lib/docker
   ```

2. Show the largest directories.

   ```bash
   sudo du -xh --max-depth=1 / 2>/dev/null | sort -h | tail -15
   ```

3. Show the size of the agent data and the local backups.

   ```bash
   du -sh ~/senpilot-agent/data/{raw,blobs,tmp} ~/backups/regulatory-agent
   ```

4. Show the Docker use: images, build cache, volumes and container logs.

   ```bash
   docker system df -v | head -40
   ```

5. Show the size of the journal.

   ```bash
   sudo journalctl --disk-usage
   ```

## 2. Remove data that is safe to remove

Do these steps in this sequence. Stop when there is sufficient free space.

1. Delete the agent scratch files that are older than 1 day. `data/tmp` holds ZIPs and browser profiles of jobs in progress. A crash left the older files.

   ```bash
   find data/tmp -mindepth 1 -mmin +1440 -delete
   ```

2. Delete the Docker build cache.

   ```bash
   docker builder prune -f
   ```

3. Delete the Docker images that have no tag. Keep `regulatory-agent:latest` and `regulatory-agent:previous` (rollback). You can delete old `:candidate*` tags.

   ```bash
   docker image prune -f
   ```

4. Delete the journal entries that are older than 30 days.

   ```bash
   sudo journalctl --vacuum-time=30d
   ```

5. Delete local backups that are older than 14 days. `deploy/backup.sh` usually does this. Never delete the 2 newest backups.

6. If an off-site copy exists (`BACKUP_REMOTE`), you can make the local retention shorter for a short time.

7. Run the retention purge. Refer to section 3.

WARNING: Never delete the Postgres volume, `data/raw` of requests in progress, `.env` or `deploy/env/`. Also never delete sockets under `/run`, or the directories of other projects without their owner.

## 3. Run the retention purge

The purge deletes raw mail, old request rows, old audit events and files that no request used for 395 days. It also deletes the database rows of these files. Never delete files in `data/blobs` by hand: the database then refers to files that do not exist.

The timer `regagent-retention.timer` runs the purge each day at 03:30 UTC. Before the least-privilege cutover, the worker also runs the purge each day at 03:15 UTC.

WARNING: The purge deletes data permanently. Do a dry run first.

1. Do a dry run.

   ```bash
   docker compose --profile ops run --rm retention purge --dry-run
   ```

   Expected result: a JSON object with the counts that the purge removes.

2. Examine the counts. Stop if a count is much larger than you expect.
3. Run the purge.

   ```bash
   docker compose --profile ops run --rm retention purge
   ```

   Expected result: a JSON object with the counts. A `purge` event is in the audit trail.

NOTE: The `retention` service uses `deploy/env/retention.env` and the role `agent_retention`. The cutover makes this file ([Cutover to least privilege](cutover-least-privilege.md)). Before the cutover, run `docker compose exec worker ragent purge --dry-run`, then `docker compose exec worker ragent purge`.

[Operations, section 7](../guide/operations.md#7-retention-purge) gives the retention schedule.

## 4. If Postgres or Redis cannot write

Postgres:

1. Make free space first (section 2).
2. Read the Postgres log. Look for `PANIC` or `could not write`.

   ```bash
   docker compose logs --tail 100 postgres
   ```

3. If Postgres does not recover without help, restart it.

   ```bash
   docker compose restart postgres
   ```

4. If the write-ahead log (WAL) uses the space, look for a replication slot or a long transaction in `pg_stat_activity`.

Redis:

Redis has `maxmemory 256mb` with `noeviction`. At the limit, writes fail with an error, for example a failed enqueue. Redis does not drop data silently. The usual cause is a key leak, for example keys of each request without a TTL. Refer to [Redis down or memory high](redis-down.md). If all other steps fail, the sweeper can make the queue again from the Postgres state.

## 5. After the incident

1. Make sure that the backlog decreases: the number of requests that are not in a final state must decrease. Ingest and downloads continue without help when the free space is above the guard.
2. Record the cause.
3. If the data increases normally, plan more capacity. An example is a Hetzner volume for `data/` and the backups. Do not delete data again and again.
