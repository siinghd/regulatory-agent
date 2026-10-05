# Runbook: backup stale

This document is written in ASD-STE100 Simplified Technical English.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentBackupStale` | `warning` | For 10 min, the newest restore-tested backup in `/home/deploy/backups/regulatory-agent/backup.log` is older than 26 h. |
| `RegagentBackupMetricsMissing` | `warning` | For 30 min, the backup metric is absent, or its textfile is older than 2 h. |

Incident severity: SEV3. The recovery point objective (RPO) increases for each hour without a backup. Policy: [Backup](../policies/backup.md).

The timer `regagent-backup.timer` starts `deploy/backup.sh` each day at 02:40 UTC. The script makes a Postgres dump, restores it into a test database and encrypts the dump and the files with `age`. After a good run, it writes 1 line with `ok` to `backup.log`.

## 1. Backup stale

1. Examine the timer and the service.

   ```bash
   systemctl status regagent-backup.timer regagent-backup.service --no-pager
   ```

2. Read the log of the last runs.

   ```bash
   journalctl -u regagent-backup.service --since -2d | tail -40
   ```

3. Read the last lines of the backup log.

   ```bash
   tail -3 /home/deploy/backups/regulatory-agent/backup.log
   ```

   Expected result: the last line starts with a time in the last 26 h and contains `ok`.

4. Correct the cause. Usual causes are a full disk, Postgres down, no `BACKUP_AGE_RECIPIENT` in `.env`, or a failed restore check.
5. Start a backup now.

   ```bash
   sudo systemctl start regagent-backup.service
   ```

6. Read the backup log again. Make sure that the last line contains `ok`.

## 2. Backup metric missing

The timer `regagent-backup-metrics.timer` runs `deploy/observability/backup_metrics.sh` each 5 min. The script writes `/var/lib/regagent-textfile/regagent_backup.prom`. The node exporter reads this file.

1. Examine the timer and the service.

   ```bash
   systemctl status regagent-backup-metrics.timer regagent-backup-metrics.service --no-pager
   ```

2. Read the metrics file.

   ```bash
   cat /var/lib/regagent-textfile/regagent_backup.prom
   ```

   Expected result: the file contains `regagent_backup_last_success_timestamp_seconds`.

3. If the timer is not installed, install the unit files.

   ```bash
   sudo install -m 644 deploy/regagent-backup-metrics.{service,timer} /etc/systemd/system/
   ```

4. Load the new unit files.

   ```bash
   sudo systemctl daemon-reload
   ```

5. Enable and start the timer.

   ```bash
   sudo systemctl enable --now regagent-backup-metrics.timer
   ```
