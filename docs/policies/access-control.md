# Access Control Policy

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05. SOC 2: CC6.1–CC6.8.

## 1. Rules

1. Give each identity, human or service, only the access that its job needs. Write the access in this document or in the file that this document names.
2. Humans authenticate with keys or MFA, never with a password alone. Services authenticate with generated 256-bit secrets in `.env`, divided for each service.
3. Do not use shared accounts. The only exception is the break-glass credentials in section 4. Record each use of them.
4. Remove access on the same day that it is no longer necessary. Review all access each quarter (section 5).

## 2. Human access

| Access | Who | How | Status |
|---|---|---|---|
| SSH to the host as `deploy` | Operator | Ed25519 key. `PasswordAuthentication no`, `KbdInteractiveAuthentication no`, `PermitRootLogin without-password`, `MaxAuthTries 3`. The weekly compliance check examines these settings. | Implemented |
| Root on the host | Operator | `sudo` from `deploy` | Partial: `deploy` has sudo without a password, so a compromised `deploy` key gives root access. Substitute controls: SSH with keys only, ufw, and the key is only on the encrypted laptop of the operator. Next step: a sudo password or a separate admin user. |
| Docker | Operator (`deploy` is in the `docker` group) | Docker access is equal to root access | Accepted, with the same substitute controls |
| Grafana admin (`regagent-admin`) | Operator | Only on 127.0.0.1:3310 through an SSH tunnel. The password is in `deploy/observability/.env` (mode 600). Caddy answers 404 for the login, admin and API write paths on `/grafana/`. | Implemented |
| Vendor consoles (Hetzner, Cloudflare, Azure, OpenRouter, TypeSafe, GitHub, R2) | Operator | Account and MFA | Open: at the next access review, confirm MFA for each vendor and keep a screenshot |
| Production data on laptops | Nobody | Not permitted ([acceptable use](acceptable-use.md)). Backups stay encrypted. | Implemented |

## 3. Service identities (least privilege)

| Identity | Used by | Can | Cannot | Defined in |
|---|---|---|---|---|
| Postgres `agent_app` | ingest, worker | SELECT, INSERT and UPDATE on app tables. INSERT and SELECT on `events`. | DELETE, TRUNCATE, DDL, UPDATE on `events`, TEMP, other databases | `deploy/sql/roles.sql`, `deploy/sql/grants.sql` |
| Postgres `agent_web` | web | SELECT on `citations`, `documents`, `matters` and `events`. SELECT on the progress and `/status` columns of `requests`. | All other access (no raw MIME hash, authentication results, parsed body, subject or outbox) | Same |
| Postgres `agent_migrator` | migrate, db-grants | Acts as `agent_owner`: DDL on the app schema | Create roles or databases, read server files | Same |
| Postgres `agent_retention` | retention (`ragent purge`, `ragent dsar delete`) | SELECT, UPDATE and DELETE. INSERT on `events` and `suppression`. | INSERT other data, TRUNCATE, DDL | Same |
| Postgres `agent_backup` | `deploy/backup.sh` | `pg_read_all_data` | Any write | Same |
| Postgres `agent_monitor` | postgres-exporter (compose profile `db-exporters`) | `pg_monitor` only: statistics views and sizes | Table data, writes | `deploy/sql/monitor_role.sql` (applied by hand after the cutover) |
| Postgres `agent` (superuser) | Break-glass only | All | Connect over TCP (`deploy/postgres/pg_hba.conf` refuses it) | `docker-compose.yml` |
| Redis `agent` | ingest, worker, web (rate limiter, `/health/deep`) | The commands that arq, the rate limits, the budgets, the locks and the breakers use | KEYS, SCAN, FLUSHALL, FLUSHDB, CONFIG, DEBUG, ACL, MONITOR, SAVE, REPLICAOF and other administration commands | `deploy/redis/users.acl.template` |
| Redis `admin` | Break-glass only | All | Not applicable | Same |
| Redis `default` | Nobody | Nothing (disabled: unauthenticated clients get NOAUTH) | Authenticate | Same |
| Mailbox `agent@hsingh.app` | ingest (IMAP), worker (SMTP) | Read, flag and expunge its own mailbox, and submit mail | Other mailboxes | Dovecot and Postfix |
| OpenRouter API key | worker | Call models | Account administration | `.env`, then `deploy/env/worker.env` |
| TypeSafe API key | worker | Call Jev | Account administration | `.env`, then `deploy/env/worker.env` |
| Containers | All app services | uid 1000, read-only root file system, no capabilities, `no-new-privileges`, memory, CPU and PID limits. The web container mounts only `data/blobs`, read-only. | Write to the image, get more privileges | `docker-compose.yml` |
| Egress tunnel key | `uarb-egress-tunnel.service` | SSH to the Azure egress VM for SOCKS | Interactive use (not necessary) | `~deploy/.ssh`, Azure VM |

Each process receives only its own secrets. `deploy/split-env.sh` writes `deploy/env/<service>.env` from `.env`, as `deploy/env/services.toml` specifies. A key that no service claims goes to no service, and the script reports it. The web process has no mail, model or drop credentials. Its only secrets are its read-only database role and the Redis password for its rate limiter.

Evidence:

- `deploy/verify-db-roles.sh` connects as each Postgres role and runs `deploy/sql/verify_grants.sql`. The script has 98 checks of permitted and refused operations, and the checks include the append-only audit trail.
- `deploy/validate_redis_acl.py` runs each arq, limit and breaker code path as the Redis `agent` user. It also tries 33 forbidden commands, unauthenticated access and a wrong password (51 checks).

## 4. Break-glass access

| Credential | Where | Use |
|---|---|---|
| Postgres superuser | `POSTGRES_PASSWORD` in `.env`. Only through `docker compose exec postgres psql -U agent`. | Restores, role changes, incidents |
| Redis admin | `REDIS_ADMIN_PASSWORD` in `.env`. Only through `docker compose exec -e REDISCLI_AUTH=<password> redis redis-cli --user admin`. | Review of `ACL LOG`, examination during an incident |
| Root on the host | sudo | Host maintenance |

Record each break-glass use outside a planned change as an incident note: what, why and when. If the credential was possibly exposed, rotate it after the use. Postgres logs each connection and each DDL statement, so superuser sessions are in the database log.

## 5. New access, removal of access and reviews

- **New person:** The person reads the policies. The person gets a personal SSH key on the host and personal vendor accounts with MFA, never the accounts of the operator. The person gets access to production data only if the role needs it.
- **Person who leaves:** On the same day, remove the SSH key, the vendor accounts and the GitHub access. Rotate each shared break-glass secret that the person possibly saw ([credential-leak runbook](../runbooks/credential-leak.md)).
- **Quarterly access review** (first Monday of each quarter): Examine `authorized_keys` on the host and on the egress VM, sudoers and the `docker` group. Also examine the vendor account members and their MFA status, and the GitHub collaborators and deploy keys. Examine the Postgres roles and the Redis users from the output of `deploy/compliance_check.sh`.
- **Record of the review:** Write a dated note in `docs/soc2/evidence/access-reviews/` (Open: the first review is due on 2027-01-04). With 1 operator, this is a self-review. When the system has an independent reviewer, that person signs the note.

## 6. Access of end users to the service

- The email interface acts only for senders who pass DMARC-aligned SPF or DKIM. `SENDER_AUTH_MODE=allowlist` limits the service to named senders.
- Limits apply at each layer ([Abuse protection](../guide/abuse-protection.md) gives the default values):
  - Before authentication: limits for each client IP and for each claimed sender domain. Over a limit, ingest keeps only the headers and sends no reply. A global inbound ceiling holds mail for a later sweep and does not drop it.
  - After authentication: hourly and daily limits for each normalised sender, each organizational domain and all senders. The normalised form ignores case, `+tag` and Gmail dots. A sender gets at most 3 "slow down" replies each day.
  - In flight: at most 2 requests for each sender at the same time.
  - Daily budgets: LLM spend, visits to each regulator portal, and bytes delivered to each sender.
- Rate-limit keys are HMACs. Redis never holds an address.
- The viewer limits the rate of each route for each client IP, except the health checks. It uses token buckets in Redis and answers 429 with `Retry-After`. If Redis is not available, the viewer continues to serve pages without limits.
- The viewer has no login by design, and it serves public regulator records. Pages for a request use capability tokens that are not possible to guess (16-character progress tokens, short citation ids). The pages mask the sender address and show no data about other requests.
