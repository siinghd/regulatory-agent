# Operations

This document is written in ASD-STE100 Simplified Technical English.

This document gives the procedures for the operator: deploy, cutover, backup, restore, purge, DSAR, kill switch, health checks and metrics. The incident runbooks are in `docs/runbooks/`. The policies are in `docs/policies/`.

NOTE: The agent is an MVP for evaluation. It has no service level agreement. 1 operator does all roles.

## 1. Before you start

- Connect to the host with SSH as the user `deploy`.
- Do all procedures from the repository root: `/home/deploy/senpilot-agent`.
- Run `ragent` commands inside a container. The worker container has the correct database role for most commands.

| Command form | Use |
|---|---|
| `docker compose exec worker ragent <command>` | Operator commands that read data or write without DELETE |
| `docker compose --profile ops run --rm retention <command>` | Commands that delete data (`purge`, `dsar delete`) |
| `make help` | A list of the Makefile targets |

WARNING: `.env` and `deploy/env/*.env` contain all secrets. Do not copy them to another host, a chat or a ticket. Keep their mode at 600.

## 2. Scheduled tasks

| Task | Schedule (UTC) | Where |
|---|---|---|
| Sweeper | Every 5 min | Worker (arq cron) |
| Metrics refresh | Every 1 min | Worker |
| Audit export | 00:30 | Worker |
| Backup | 02:40 | `regagent-backup.timer` |
| Purge in the worker | 03:15. It runs only if the database role of the worker can delete. After the least-privilege cutover, `agent_app` cannot delete, so it does nothing. | Worker |
| Retention purge | 03:30 | `regagent-retention.timer` |
| Reconciliation | 06:00 | Worker |
| Compliance check | Monday 06:30 | `regagent-compliance.timer` |

To see the host timers, do this step:

1. List the timers.

   ```bash
   systemctl list-timers 'regagent-*'
   ```

   Expected result: each installed timer shows its next run.

NOTE: On 2026-10-05, the live host ran an older version of the code, and only `regagent-backup.timer` and `regagent-backup-metrics.timer` were installed. The cutover runbook (`docs/runbooks/cutover-least-privilege.md`) installs `regagent-retention.timer` and `regagent-compliance.timer`.

## 3. Deploy

`deploy/deploy.sh` builds a candidate image, tests it, scans it, promotes it, migrates the database, restarts the services 1 at a time and records the result.

### 3.1 Normal deploy

CAUTION: A deploy restarts `worker`, `ingest` and `web`. Jobs in progress stop and continue after the restart. Mail waits in IMAP. Do not deploy while you are in an incident, except as an emergency change.

1. Commit all changes. The script refuses a tree with uncommitted changes.
2. Make sure that `.env` is not newer than the per-service env files.

   ```bash
   deploy/split-env.sh
   ```

3. Start the deploy with the reference of the change (a pull request or a commit).

   ```bash
   make deploy CHANGE="<PR or commit reference>"
   ```

   Expected result: the script writes `deployed <sha> (change: ...)`.

4. Read the last line of the deploy log.

   ```bash
   tail -1 deploy/deploys.log
   ```

   Expected result: the line contains `result=ok`.

5. Send a request from a known sender and watch it finish.

   ```bash
   docker compose logs -f worker
   ```

The script does these stages, in this order:

1. Build the release image and the test image.
2. Run the unit and adversarial tests in the test image, without network and with a read-only file system.
3. Scan the image with Trivy.
4. Promote the image. The old image becomes `:previous`.
5. Run `migrate` and `db-grants`.
6. Restart the services 1 at a time, with health checks.
7. Do a smoke test of `/health` on the loopback and on `https://uarb.hsingh.app`.

A failure after the promotion restores `:previous` automatically.

### 3.2 Return to the previous image

1. Deploy the previous image again.

   ```bash
   deploy/deploy.sh --rollback --change "<reason>"
   ```

   Expected result: the deploy log has a line with `result=rolled-back`.

### 3.3 Emergency deploy

WARNING: An emergency deploy can skip the image scan. Use it only to stop harm. Write a retrospective pull request after it.

1. Start the deploy with the `emergency:` prefix.

   ```bash
   deploy/deploy.sh --change "emergency:<why>" --allow-dirty
   ```

2. Add `--skip-scan` only if the scan itself blocks the fix.

## 4. Cutover to least-privilege roles and the Redis ACL

This is a 1-time change. The full runbook is `docs/runbooks/cutover-least-privilege.md`. The steps here are a summary.

CAUTION: The cutover stops the app for approximately 15 min. Mail waits in IMAP during this time.

1. Make a fresh backup.

   ```bash
   deploy/backup.sh && tail -1 ~/backups/regulatory-agent/backup.log
   ```

2. Build and test the candidate image.

   ```bash
   make build test-image integration
   ```

3. Stop the app processes.

   ```bash
   docker compose stop ingest worker web
   ```

4. Create the database roles and write their passwords into `.env`.

   ```bash
   deploy/db-cutover.sh
   ```

5. Create the Redis passwords.

   ```bash
   deploy/redis-cutover.sh
   ```

6. Write the per-service env files.

   ```bash
   deploy/split-env.sh
   ```

7. Start Postgres and Redis with the hardened configuration.

   ```bash
   docker compose up -d --wait postgres redis
   ```

8. Prove the database roles.

   ```bash
   deploy/verify-db-roles.sh
   ```

   Expected result: all role checks pass, and Postgres refuses the superuser over TCP.

9. Prove the Redis ACL.

   ```bash
   make validate-redis
   ```

10. Deploy the app (section 3.1).
11. Put a copy of the new `.env` in the password manager.

## 5. Backup

`deploy/backup.sh` runs each night at 02:40 UTC. It dumps Postgres, restores the dump into a scratch database, compares the request counts and encrypts the files with age. The host has only the public age key.

### 5.1 Examine the last backup

1. Read the last line of the backup log.

   ```bash
   tail -1 /home/deploy/backups/regulatory-agent/backup.log
   ```

   Expected result: a time from the last 26 h, then `ok`.

### 5.2 Make a backup now

1. Run the backup script.

   ```bash
   deploy/backup.sh
   ```

   Expected result: a new `ok` line in `backup.log`, and new `pg-<stamp>.dump.age` and `files-<stamp>.tar.age` files.

The local copies stay for 14 days. The off-site copy (`BACKUP_REMOTE`) is not configured yet.

## 6. Restore

WARNING: A restore replaces the live database. Make a backup of the current state first, if the database still runs.

WARNING: The decrypted dump contains personal data. Restore only on the server. Delete the plaintext file immediately after the restore.

CAUTION: Create the roles before you restore the data. Otherwise the owners and grants do not restore correctly.

1. Copy the encrypted backup files and the offline age key to the server.
2. Start the data stores.

   ```bash
   docker compose up -d postgres redis
   ```

3. Create the roles.

   ```bash
   deploy/db-cutover.sh
   ```

4. Decrypt the database dump.

   ```bash
   age -d -i <offline key> pg-<stamp>.dump.age > /tmp/pg.dump
   ```

5. Restore the dump.

   ```bash
   docker compose exec -T postgres pg_restore -U agent -d agent --exit-on-error < /tmp/pg.dump
   ```

6. Decrypt and unpack the files.

   ```bash
   age -d -i <offline key> files-<stamp>.tar.age | tar -C data -xf -
   ```

7. Remove the access of other users and delete the plaintext dump.

   ```bash
   chmod -R o-rwx data && shred -u /tmp/pg.dump
   ```

8. Remove the age private key from the server.
9. Deploy the app as an emergency change.

   ```bash
   deploy/deploy.sh --change "emergency:restore"
   ```

10. Prove the roles and the ACL.

    ```bash
    deploy/verify-db-roles.sh && make validate-redis
    ```

11. Send a test request from a known sender. Watch it finish.

The full rebuild procedure for a lost host is in `docs/policies/business-continuity.md`.

## 7. Retention purge

The command `ragent purge` (`agent/retention.py`) applies the retention schedule. After the least-privilege cutover, the timer `regagent-retention.timer` runs it each day at 03:30 UTC. Before the cutover, the worker connects as the owner role, so the worker job at 03:15 UTC runs it.

| Data | Age | Action |
|---|---|---|
| Raw mail of settled requests | 30 days | Deleted |
| Raw mail of rejected requests | 7 days | Deleted |
| Request rows | 90 days | Pseudonymised |
| Request rows | 400 days | Deleted |
| Audit events | 400 days | Deleted |
| Files and page text | 395 days without use | Deleted |

### 7.1 Purge by hand

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

## 8. Data subject requests (DSAR)

The procedure for identity proof and answer times is `docs/policies/dsar-procedure.md`.

A sender can also email "DELETE MY DATA" from the authenticated address. The agent then adds the address to the suppression list, sends 1 confirmation and does no more work on mail from that address. The daily purge erases the data.

### 8.1 Export the data of an address

WARNING: The export contains personal data and raw emails. Send it only to the authenticated data subject. Delete the file after delivery.

1. Write the export to a file in the data directory.

   ```bash
   docker compose exec worker ragent dsar export <address> --out /data/tmp/dsar-export.json
   ```

   Expected result: `wrote /data/tmp/dsar-export.json: N requests, N raw emails, N events`. The file is `data/tmp/dsar-export.json` on the host, with mode 600.

2. Deliver the file to the data subject.
3. Delete the file.

   ```bash
   shred -u data/tmp/dsar-export.json
   ```

### 8.2 Delete the data of an address

WARNING: This erases all data about the address and revokes its download links. You cannot undo it.

1. Export the data first if the data subject asked for it (section 8.1).
2. Erase the address.

   ```bash
   docker compose --profile ops run --rm retention dsar delete <address> --yes
   ```

   Expected result: a JSON object with the counts. The exit code is 0.

3. If the exit code is 1, read `links_not_revoked` in the output. Revoke those links again later (section 9.3).

The address stays on the suppression list as an HMAC. The gate drops later mail from it without a reply.

## 9. Kill switch and incident commands

Each command writes an `admin.*` event with the name of the operator.

### 9.1 Stop all processing (kill switch)

The kill switch parks all requests and holds all outbound mail. Ingest continues to store mail. No request uses an attempt while it is paused.

1. Set the kill switch.

   ```bash
   docker compose exec worker ragent pause --reason "<why>"
   ```

   Expected result: the command writes "paused: requests park and no mail is sent until `ragent resume`".

2. After you correct the problem, release the kill switch.

   ```bash
   docker compose exec worker ragent resume
   ```

   Expected result: `resumed`. Parked requests continue within approximately 2 min.

For a full stop, use `docker compose stop worker` (the agent fetches and sends nothing) or `docker compose stop ingest` (the agent reads no new mail).

### 9.2 Block an address or a domain

1. Add the address or the domain to the suppression list.

   ```bash
   docker compose exec worker ragent block <address or @domain> --reason "<why>"
   ```

   Expected result: `blocked <value> (suppression ...)`. The gate drops all mail from it without a reply.

### 9.3 Revoke a download link

1. Delete the drop upload of the request.

   ```bash
   docker compose exec worker ragent revoke <request id>
   ```

   Expected result: `revoked`. If the result is `NOT revoked`, drop refused or did not answer. Try again later.

### 9.4 Show the audit trail

1. Show the events of 1 request.

   ```bash
   docker compose exec worker ragent audit <request id>
   ```

2. Show the events of 1 person, by the HMAC of the address.

   ```bash
   docker compose exec worker ragent audit --email <address>
   ```

3. Examine the hash chain of the exported audit files.

   ```bash
   docker compose exec worker ragent audit --verify
   ```

   Expected result: `chain intact (...)`.

Each audit view writes an `audit_viewed` event.

## 10. Health checks

| Check | Command | Healthy result |
|---|---|---|
| Container health | `docker compose ps` | `healthy` for `ingest`, `worker`, `web`, `postgres`, `redis` |
| Web | `curl -fsS http://127.0.0.1:8710/health` | `{"ok":true,"db":true}` |
| Public web | `curl -fsS https://uarb.hsingh.app/health` | `{"ok":true,"db":true}` |
| Worker | `docker compose exec worker ragent healthcheck worker` | `healthy: ...` |
| Ingest | `docker compose exec ingest ragent healthcheck ingest` | `healthy: heartbeat Ns ago (limit 600s)` |
| Deep health (loopback only) | `curl -fsS http://127.0.0.1:8710/health/deep` | `"ok": true`, a small queue, a recent backup |
| Egress tunnel | `systemctl status uarb-egress-tunnel` | `active (running)` |
| Reconciliation | `docker compose exec worker ragent reconcile --since 24h` | Exit code 0, `"anomalies": []` |
| Technical controls | `make compliance` | No `FAIL` line |

`/health/deep` gives these values:

- the database state and the age of the oldest open request;
- the queue depth, the ingest heartbeat age, the worker state and the kill switch state;
- the free disk space, the last backup and the app version.

### 10.1 Examine the egress tunnel

1. Examine the tunnel unit.

   ```bash
   systemctl status uarb-egress-tunnel
   ```

2. Send a test request through the tunnel to a neutral site, not to a regulator portal.

   ```bash
   curl -fsS --socks5-hostname 127.0.0.1:1080 https://www.cloudflare.com/cdn-cgi/trace | grep loc
   ```

   Expected result: `loc=CA`.

3. If the tunnel is down, restart it.

   ```bash
   sudo systemctl restart uarb-egress-tunnel
   ```

NOTE: Do not use a UARB matter as a tunnel test. Each portal visit counts against the daily UARB budget of 400 visits.

## 11. Metrics

The worker serves Prometheus metrics on `127.0.0.1:9710`, and ingest on `127.0.0.1:9711`. The web process serves them at `/metrics` on `127.0.0.1:8710`, only for loopback clients. [Observability](observability.md) describes the metrics, dashboards and alerts.

1. Read the main metrics.

   ```bash
   curl -fsS http://127.0.0.1:9710/metrics | grep -E '^(requests_total|retries_total|queue_depth|breaker_open|budget_used)'
   ```

   Expected result: request counters, the queue depth, the breaker states and the budgets of today.
