# Backup Policy

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: A1.2, CC9.1, C1.2. Implementation: `deploy/backup.sh` and `deploy/regagent-backup.timer`. Review this policy 1 time each year.

[Operations, section 5](../guide/operations.md#5-backup) gives the procedures to examine a backup and to make a backup by hand.

## 1. What the backup contains

| Data | In the backup | Reason |
|---|---|---|
| Postgres database `agent` (requests, events, documents, citations, pages, summaries, matters, outbox) | Yes: `pg_dump -Fc` as `agent_backup` (read-only, `pg_read_all_data`) | It is the system of record |
| `data/raw` (raw inbound MIME) | Yes | It is necessary to process a request again within its retention of 30 days |
| `data/blobs` (downloaded regulator files, content-addressed) | Yes | The files are public, but a new download is slow and has rate limits |
| `data/audit` (daily audit exports with a hash chain) | Yes, when the folder exists | It is the evidence of what the system did |
| Redis | No | The data is temporary. The sweeper makes the queue again from Postgres. |
| Images | No | The build makes them again from the repository (pinned by digest and hash) |
| `.env` and keys | No, never with the data | The operator keeps them offline in a different location ([BCP section 4](business-continuity.md#4-recovery-kit-kept-off-the-host)). A backup that contains its own keys gives no protection. |
| `data/tmp` and logs | No | `data/tmp` is scratch space. Logs have their own retention. |

## 2. Schedule, encryption and integrity

- **Schedule:** each night at 02:40 UTC, from a systemd timer. The timer has `Persistent=true`, so a missed run starts at the next boot.
- **Encryption:** age encrypts the backup to `BACKUP_AGE_RECIPIENT`. The private key of this public key is offline. The host can write backups but it cannot read them. For this reason, an attacker on the host cannot read old backups. The files have mode 600 in a directory with mode 700.
- **Restore check on each run:** the script restores the plaintext dump into the temporary database `agent_restorecheck`. It then compares the request count with the live database. The restored count must be 1 or more, and at most 50 less than the live count. If the check fails, the run fails and the script keeps no file. The script encrypts the dump only after a good check, and then it deletes the plaintext.
- **Log:** each good run writes 1 line in `backup.log` with the sizes, the request count and the remote target.
- **Weekly check:** `deploy/compliance_check.sh` fails if the last good backup is older than 26 h. It also fails if an unencrypted file is in the backup directory.

## 3. Retention and location

| Copy | Location | Retention | Status |
|---|---|---|---|
| Local | `/home/deploy/backups/regulatory-agent/` on the host | 14 days | Implemented |
| Off-site | Cloudflare R2 through rclone (`BACKUP_REMOTE`), in a bucket with object lock or a lifecycle rule | 35 days (`offsite_backup_retention_days`) | **Open**: not configured. Until the operator configures it, the RPO is not met for a loss of the host. |

Data that the [retention schedule](data-retention.md) deletes stays in the backups until the backups expire. This is at most 14 days locally and at most 35 days off-site. After a restore, the next retention purge deletes the old data again. For this reason, no data that is past its retention returns to live use.

## 4. Access

Only the operator (as `deploy`) can read the backup directory. Only the holder of the offline age key can decrypt the backups. Do a restore only when it is necessary ([BCP section 5](business-continuity.md#5-rebuild-procedure-host-lost)) or in a drill. Never restore a backup onto a laptop.

## 5. Tests

- Each night: the automatic restore check (section 2).
- Each quarter: a restore drill on a temporary server, with the role model.
- Each year: a full rebuild.

Record the results in the BCP test log.
