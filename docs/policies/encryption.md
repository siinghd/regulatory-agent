# Encryption & Key Management Policy

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: CC6.1, CC6.7, C1.1. Review this policy 1 time each year.

## 1. Standards

Use only library primitives. Do not make your own cryptography. The approved primitives are:

- TLS 1.2 or higher (TLS 1.3 is preferred);
- AES-256-GCM, HKDF-SHA256 and HMAC-SHA256;
- SCRAM-SHA-256 for database passwords;
- Ed25519 for SSH;
- X25519 (age) for backups.

Make secrets with a CSPRNG at 256 bits (`python3 deploy/lib/envtool.py gen`).

## 2. In transit

| Path | Protection |
|---|---|
| Browser to `uarb.hsingh.app` | TLS at Cloudflare. HSTS (`max-age=31536000; includeSubDomains`). |
| Cloudflare to the origin (Caddy) | TLS. Caddy serves a Cloudflare Origin CA certificate (`/etc/caddy/certs/origin.crt`, snippet `origin_tls`) for the Cloudflare "Full (strict)" mode. **Open**: confirm that the zone uses "Full (strict)". Enable Authenticated Origin Pulls, so that the origin answers only Cloudflare. |
| Mail in and out (`mail.hsingh.app`) | SMTP with STARTTLS, IMAPS and submission with TLS. A Let's Encrypt certificate that renews automatically. |
| Worker to OpenRouter, TypeSafe, OEB, FERC and drop | HTTPS |
| Worker to the UARB portal | An SSH tunnel to the Canadian egress VM, then HTTPS to the portal |
| App to Postgres and Redis | Loopback on the same host (ports published on 127.0.0.1), without TLS. Accepted, because the traffic stays on the host. Authentication still uses SCRAM and the Redis ACL. |
| Delivery to requesters | drop links. The client encrypts the files in AES-256-GCM chunks. The key is only in the URL fragment, so the drop server never sees it. |

## 3. At rest

| Data | Protection | Status |
|---|---|---|
| Host disk (Postgres volume, `data/`) | None at the block level (Hetzner Cloud local disk, ext4) | **Open**: put `data/` and the Docker volumes on a Hetzner volume with LUKS encryption. Until then, the controls are the physical controls of the provider and data minimisation. |
| Outbox bodies, drop links and delete tokens | AES-256-GCM. HKDF-SHA256 derives the key from `DATA_ENCRYPTION_KEY` (`agent/crypto.py`). Associated data binds each value to its row. | Implemented |
| Backups | age (X25519) to an offline recipient. The host cannot decrypt them. | Implemented |
| Passwords that Postgres keeps | SCRAM-SHA-256 verifiers. The deploy scripts send verifiers, never plaintext. | Implemented |
| Passwords that Redis keeps | SHA-256 hashes in the ACL file (on tmpfs) | Implemented |
| Pseudonymous ids in the audit log | HMAC-SHA256 with `AUDIT_HMAC_KEY` | Partial: if `AUDIT_HMAC_KEY` is empty, the code derives the key from `data/keys/at-rest.key`. Set `AUDIT_HMAC_KEY` and keep an escrow copy. |

If `DATA_ENCRYPTION_KEY` is empty, `agent/crypto.py` uses `AUDIT_HMAC_KEY`. If both are empty, it makes a random key 1 time in `data/keys/at-rest.key` (mode 600).

## 4. Key inventory

| Key or secret | Purpose | Location | Custodian | Rotation |
|---|---|---|---|---|
| `POSTGRES_PASSWORD` | Break-glass superuser (socket only) | `.env` | Operator | Each year. On exposure: `db-cutover.sh --rotate-superuser`. |
| Postgres role passwords (5 DSNs) | Database access for each process | `.env`, then the env file of each service | Operator | Each year. On exposure: delete the DSN, then run `db-cutover.sh` again. |
| `REDIS_PASSWORD`, `REDIS_ADMIN_PASSWORD` | Redis users `agent` and `admin` | `.env` | Operator | Each year. On exposure: `redis-cutover.sh`. |
| `AGENT_MAIL_PASSWORD` | Mailbox (IMAP and SMTP) | `.env`, Dovecot passdb | Operator | Each year, and on exposure |
| `OPENROUTER_API_KEY` | LLM calls (the key has a credit limit) | `.env`, then the worker | Operator | Every 90 days, and on exposure |
| `TYPESAFE_API_KEY` | Jev calls for triage and citation checks | `.env`, then the worker | Operator | Every 90 days, and on exposure |
| `DATA_ENCRYPTION_KEY` | Seals the outbox and the delivery records | `.env`, then the worker. Fallback: `AUDIT_HMAC_KEY`, then `data/keys/at-rest.key`. | Operator, with an escrow copy | Only on exposure, with a new seal of the stored values. **Open**: key versions. The sealed format has a version byte, but no key id. |
| `AUDIT_HMAC_KEY` | Pseudonyms in the audit log and in Redis keys | `.env` | Operator, with an escrow copy | Only on exposure. After a rotation, old pseudonyms cannot be linked to new ones. |
| age key pair | Backup encryption | Public key: `.env`. Private key: offline only. | Operator | Every 2 years, and on exposure |
| Cloudflare Origin CA key | Origin TLS | `/etc/caddy/certs/origin.key` (root) | Operator | Before it expires (2035), and on exposure |
| Let's Encrypt key (mail) | Mail TLS | `/etc/letsencrypt` | certbot | Every 60 to 90 days, automatically |
| SSH keys (operator to host, host to egress VM) | Administration and the tunnel | `~/.ssh` | Operator | Every 2 years, and on exposure or departure |
| `GF_SECURITY_ADMIN_PASSWORD` | Grafana admin user (only on 127.0.0.1:3310) | `deploy/observability/.env` (mode 600) | Operator | Each year, and on exposure |
| `TEST_SENDER_PASSWORD` | Test mailbox for end-to-end tests | `.env` (host only) | Operator | Each year |

Rules:

- Each key has 1 purpose.
- Each secret is in `.env`, or in the system location that the table gives.
- Keep an offline escrow copy of each secret after each change.
- Give each secret only to the service that needs it (`deploy/env/services.toml`).

The weekly compliance check examines the file modes. It also makes sure that Git tracks no file with secrets. It examines the certificate expiry dates, and it fails when a certificate expires in less than 14 days.
