# Business Continuity & Disaster Recovery Plan

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: A1.2, A1.3, CC9.1, CC7.5. Review this plan 1 time each year and after each real recovery.

## 1. Targets

| | Target | How the system meets it | Status |
|---|---|---|---|
| **RTO** (the service answers email again) | 4 h | Rebuild from the repository, the escrowed secrets and the latest backup (section 5). Drills measure the time. | Partial: nobody has timed a full rebuild from start to end |
| **RPO** (data lost) | 24 h | A nightly backup at 02:40 UTC, with a restore check. The IMAP mailbox also keeps inbound mail until ingest processes it. | Partial: the backups are on the same host until the operator sets `BACKUP_REMOTE`. A loss of the host also loses the backups. |

A loss of 24 h of data means the request rows, audit events and raw MIME of the last day. The regulator documents are public, and the agent can download them again. Mail that ingest did not process stays in the IMAP mailbox. The sweeper puts each request whose state is in Postgres back on the queue.

## 2. Dependencies and single points of failure

| Component | Failure | Effect | Mitigation or degraded mode |
|---|---|---|---|
| Hetzner VM (all components) | Loss of the host | Full outage | Rebuild on a new VM (section 5). The images and the code come from the repository. |
| Postgres volume | Corruption | Outage | Restore the last dump with `pg_restore`, then run `deploy/db-cutover.sh` |
| Redis | Loss of data | The queue is lost | The sweeper queues the requests that are not settled again from Postgres within 15 min |
| Mail (Postfix and Dovecot on the same host) | Down | No inbound mail and no replies | Mail waits in the queues of the sender MTAs for some days. Start the mail server again. |
| Azure egress VM (Canada) | Down | UARB only: the fetches fail | The agent retries as "portal unavailable", then sends 1 apology. OEB and FERC continue. No second egress exists. This is a single point of failure, and `/status` shows it. The egress pool is a proposed upgrade (ADR-022 in the [decisions log](../guide/decisions-log.md)). |
| TypeSafe | Down | No Jev triage and no Jev citation checks | The LLM does the triage and the citation checks |
| OpenRouter and the models | Down | No LLM | Triage uses Jev and the rules. The agent sends the documents without a summary. |
| drop (`drop.hsingh.app`) | Down | No encrypted links | ZIPs up to 7,000,000 bytes go as attachments. Larger ZIPs wait for drop. |
| Cloudflare | Down | The viewer is not available | Email replies continue. Citation links fail until Cloudflare is available again. |
| Regulator portals | Down or changed | That provider fails | Retries, then 1 apology. Canary and drift detection are a next step ([DESIGN.md section 6](../../DESIGN.md#6-path-to-scale)). |
| The operator | Not available | No changes and no recovery | The safe failure modes continue. With the recovery kit (section 4), a delegate can rebuild the system. |

## 3. Backups

Refer to the [Backup Policy](backup.md). In summary:

- Each night, `pg_dump` runs as `agent_backup`. The backup also contains `data/raw`, `data/blobs` and `data/audit`.
- age encrypts the backup to an offline key.
- Each run does a restore check.
- The host keeps 14 days of backups.
- The off-site copy with rclone to Cloudflare R2 is **Open**: set `BACKUP_REMOTE`.

## 4. Recovery kit (kept off the host)

- The repository at the deployed commit. `deploy/deploys.log` and the image label `org.opencontainers.image.revision` give the commit.
- `.env` (all secrets). The operator keeps a copy in a password manager after each change. **Open**: confirm that the escrow copy agrees with `.env` after the cutover.
- The age **private** key for the backups (offline: a password manager and a paper copy).
- The SSH key for the egress VM.
- Access to the Cloudflare, Hetzner, Azure, OpenRouter, TypeSafe and GitHub accounts, with MFA.
- This document.

## 5. Rebuild procedure (host lost)

The times are targets for an operator who has practised the procedure. [Operations, section 6](../guide/operations.md#6-restore) gives the restore steps with their expected results.

1. Make a new VM (30 min).
   - Use Hetzner Cloud with Ubuntu 24.04. The images are multi-arch, so arm64 and amd64 both work.
   - Use at least 4 vCPU, 8 GB of memory and 80 GB of disk.
   - Create the user `deploy` (uid 1000) and install the SSH key.
   - Configure sshd for keys only.
   - Configure the firewall: `ufw allow 22,25,80,443,465,587,993/tcp && ufw enable`.
   - Install `unattended-upgrades`.
2. Install the platform (30 min).
   - Install Docker Engine and the compose plugin. gVisor `runsc` is optional.
   - Install Caddy with the Cloudflare origin certificate and `deploy/caddy/uarb.caddy`.
   - Install Postfix and Dovecot for `agent@hsingh.app`. **Open**: the mail server configuration is not in this repository. Write it down or move it into a repository.
   - Install the egress tunnel unit `deploy/uarb-egress-tunnel.service`.
3. Install the code and the secrets (10 min).
   - Clone the repository with `git clone`.
   - Select the deployed commit with `git checkout <commit>`.
   - Restore `.env` with mode 600.
   - Run `deploy/split-env.sh`.
4. Restore the data stores (30 min to 60 min).

   CAUTION: Create the roles before you restore the data. Otherwise the owners and grants do not restore correctly.

   WARNING: The decrypted dump contains personal data. Delete the plaintext file immediately after the restore.

   ```bash
   docker compose up -d postgres redis
   deploy/db-cutover.sh                          # roles before data, so owners/grants restore
   age -d -i <offline key> pg-<stamp>.dump.age > /tmp/pg.dump
   docker compose exec -T postgres pg_restore -U agent -d agent --exit-on-error < /tmp/pg.dump
   age -d -i <offline key> files-<stamp>.tar.age | tar -C data -xf -
   chmod -R o-rwx data && shred -u /tmp/pg.dump
   ```

5. Start and verify the system (30 min).
   - Run `deploy/deploy.sh --change "emergency:dr-rebuild"`.
   - Run `deploy/verify-db-roles.sh`, `make validate-redis` and `deploy/compliance_check.sh`.
   - Send a test request from a known sender. Watch it until it is complete.
6. Change the DNS (15 min).
   - In Cloudflare, point `uarb.hsingh.app` and `mail.hsingh.app` to the new host.
   - If the IP address changed, also change the MX and SPF records.
   - Get a new mail certificate.

## 6. Tests

- **Each night:** `deploy/backup.sh` restores the dump into a scratch database and compares the request counts. If the check fails, the backup fails.
- **Each quarter:** do a restore drill. Decrypt the latest backup on a temporary Postgres server, run `deploy/db-cutover.sh` on it, restore, then run `deploy/verify-db-roles.sh`. Record the duration and the problems.
- The first drill was on 2026-10-04, with a dump from the `agent_backup` role. It restored 24 requests with the correct owners and grants, and it passed all 91 role checks.
- **Each year:** do a full rebuild on a new VM with the procedure in section 5. Compare the time with the RTO.
