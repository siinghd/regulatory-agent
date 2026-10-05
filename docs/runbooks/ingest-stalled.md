# Runbook: ingest stalled

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentIngestStalled` | `page` | For 5 min, `ingest_heartbeat_age_seconds` is more than 600 s. |

Incident severity: SEV2. The IMAP loop of ingest does not write `ingest:heartbeat` in Redis. The agent does not read new email.

Ingest writes the heartbeat at least each 60 s while its IMAP connection operates. The worker reads the heartbeat age each 1 min and exports it as `ingest_heartbeat_age_seconds`. For this reason, this alert needs a worker that operates. Refer to [Worker metrics down](worker-metrics-down.md).

NOTE: Mail is not lost while ingest is stalled. The messages stay unread on the IMAP server. Ingest reads them when it operates again.

## 1. Confirm the fault

1. Examine the container.

   ```bash
   docker compose ps ingest
   ```

2. Read the heartbeat age.

   ```bash
   docker compose exec ingest ragent healthcheck ingest
   ```

   Expected result for a healthy ingest: `healthy: heartbeat Ns ago (limit 600s)`.

3. Read the ingest log.

   ```bash
   docker compose logs --since 30m ingest | tail -100
   ```

## 2. Causes and repairs

| Cause | Signal | Repair |
|---|---|---|
| IMAP is not available, or the login fails | `RegagentMailEndpointDown`. Authentication errors in the log. | Refer to [Mail endpoint down](mail-endpoint-down.md). An authentication error means that the mailbox password changed (`AGENT_MAIL_PASSWORD`). |
| Redis is not available | Ingest cannot write the heartbeat. | Refer to [Redis down or memory high](redis-down.md). |
| The process does not move | No new log lines, no errors | Restart ingest (step 1 below). |

1. If the process does not move, restart ingest.

   ```bash
   docker compose restart ingest
   ```

2. Read the heartbeat age again (section 1, step 2).

NOTE: A full disk does not stop the heartbeat. Below `DISK_MIN_FREE_BYTES`, ingest leaves new mail unread and writes `ingest.disk_low` log lines. Refer to [Disk full](disk-full.md).
