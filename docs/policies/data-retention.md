# Data Retention & Disposal Policy

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: C1.2, P4.2, P4.3. Review this policy 1 time each year.

## 1. Principle

Keep personal data only while it is necessary for these purposes:

- to answer the request and its follow-up messages;
- to examine abuse;
- to prove what the system did.

The agent keeps public regulator documents as a cache while they are useful.

## 2. Schedule

| Data | Kept for | Then | Mechanism | Status |
|---|---|---|---|---|
| Raw inbound MIME (`data/raw`) | 30 days after the request settles | Deleted. The purge also clears the rendered copies of the mail that the agent sent. | Retention purge (`raw_mime_retention_days`) | Implemented |
| Raw MIME of rejected mail (spoofed, automated, spam, over a rate limit) | 7 days | Deleted | Retention purge (`rejected_raw_retention_days`) | Implemented |
| Raw files and stored files that no row refers to (after a crash between the write and the insert) | 2 days | Deleted | Retention purge | Implemented |
| Messages in the IMAP mailbox of the agent | 7 days after the request settles | Expunged | Ingest expunges the messages and records `imap_expunged_at` | Implemented |
| Request rows (`requests`) | 90 days with identity | Pseudonymised. The sender becomes `h:` and an HMAC (`AUDIT_HMAC_KEY`). The purge clears the subject. It removes the client IP address, the text of an unsent reply, the drop link and the delete token. The Message-IDs become HMACs, and the progress token gets a new random value. | Retention purge (`request_pseudonymise_days`) | Implemented |
| Request rows, pseudonymised | 400 days in total | Deleted. Citations and outbox rows are deleted with them. Audit events stay, because they contain only HMACs. | Retention purge (`request_delete_days`), role `agent_retention` | Implemented |
| Audit events (`events`, pseudonymous `subject_h` only) | 400 days | Deleted only through `purge_events()`, which refuses events younger than 400 days | Retention purge (`audit_retention_days`) | Implemented |
| Downloaded documents (`data/blobs`) and their page text | 395 days after the last use (`blob_retention_days`) | Deleted. The document rows lose their link to the file, so a later request downloads the file again. | Retention purge | Implemented |
| Drop links (encrypted ZIPs) | 7 days, at most 25 downloads | They expire on the drop server | `DROP_EXPIRY_S`, `DROP_MAX_DOWNLOADS` | Implemented |
| Rate-limit and budget counters (Redis; senders, domains and client IP addresses only as HMACs) | Their window: 1 min to 24 h. Daily budgets: 48 h. | They expire in Redis | Key TTLs (`agent/limits.py`, `agent/web/ratelimit.py`) | Implemented |
| Application logs | Approximately 30 days at the current volume | Rotated | Docker `json-file`, 5 files of 20 MB for each container. The limit is a size, not a time. | Implemented |
| Audit logs (deploys, compliance reports, the backup log, incident notes) | 400 days | Deleted | The operator deletes old records by hand 1 time each year | Implemented |
| Viewer access logs (Caddy) | 30 days | Rotated | `roll_keep_for 720h` in `deploy/caddy/uarb.caddy` (setting `access_log_retention_days`) | Implemented |
| Metrics (Prometheus) | 30 days or 5 GB | Deleted | Prometheus storage retention | Implemented |
| Local backups | 14 days | Deleted | `deploy/backup.sh` (`find -mtime +14 -delete`) | Implemented |
| Off-site backups | 35 days | Deleted | Bucket lifecycle rule (`offsite_backup_retention_days`) | Open, together with the off-site copy |
| Data at OpenRouter model providers | Not kept | Not applicable | Zero-data-retention routing (`llm_zero_data_retention`) and the vendor terms | Partial: record the allowed endpoints for each model and examine them |
| Data at TypeSafe | The retention period of TypeSafe | Not applicable | Vendor terms. TypeSafe is not zero-data-retention on our plan (owner decision, 2026-10-05). | Open: get the retention period and a DPA ([vendor register](vendor-register.md)) |

### 2.1 Retention purge

The command `ragent purge` (`agent/retention.py`) applies the schedule. After the least-privilege cutover, the timer `deploy/regagent-retention.timer` runs it each day at 03:30 UTC through the compose service `retention` (profile `ops`).

- The purge runs as the role `agent_retention`. This role can SELECT, UPDATE and DELETE. It has no DDL. It can INSERT only into `events` (its audit events) and `suppression` (for `ragent dsar delete`).
- The purge works in batches with `FOR UPDATE SKIP LOCKED`, so it does not block live work.
- Before the other steps, the purge erases each address on the suppression list that has `erase` set (DSAR).
- Each run is idempotent. It ends with 1 `purge` audit event that contains only the counts.
- The purge does not touch a request that is not settled.
- The worker also has a purge job at 03:15 UTC. It runs only if the database role of the worker can delete. Before the cutover, the worker connects as the owner role, so this job runs the purge. After the cutover, `agent_app` cannot delete, so the job does nothing.

NOTE: On 2026-10-05, the live host ran an older version of the code and had not done the least-privilege cutover. `regagent-retention.timer` was not installed. The cutover runbook ([cutover-least-privilege.md](../runbooks/cutover-least-privilege.md)) installs it. Until then, run the purge by hand ([Operations, section 7.1](../guide/operations.md#71-purge-by-hand)).

[Operations, section 7](../guide/operations.md#7-retention-purge) gives the procedure for a dry run and a purge by hand.

## 3. Disposal

- **Database rows:** the purge deletes rows (or pseudonymises them with an UPDATE) in transactions. The `purge` event records only the counts. Autovacuum recovers the space.
- **Files:** the purge deletes a content-addressed file only when no live row refers to it.
- **Backups:** the backups expire on their own schedule (section 2). For this reason, deleted data leaves all copies within 14 days locally and 35 days off-site.
- **Secrets:** rotate the secret. Then delete the old value from `.env`, from the escrow copy and from each backup of `.env`.
- **Disks and hosts:** destroy a VM through Hetzner. The Hetzner ISO 27001 certification covers the disposal of data-centre media ([vendor register](vendor-register.md)). Before you destroy a host for a reason of your own, delete `data/`, the Postgres volume and the backup directory.

## 4. Exceptions

- **Legal hold or active incident:** the code has no hold list. To hold data, stop the timer with `sudo systemctl stop regagent-retention.timer`. Record the hold in the incident note. Start the timer again when the hold ends. **Open**: a hold for some requests only, while the purge continues for all other data.
- **Data subject requests** can delete data before the end of its schedule ([DSAR procedure](dsar-procedure.md)).
