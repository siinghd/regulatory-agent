# Data Classification & Handling Policy

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: C1.1, CC6.1, P3–P4. Review this policy 1 time each year.

## 1. Classes

| Class | Definition | Examples |
|---|---|---|
| **Public** | Data that is public, or that we publish | Regulator filings and their metadata. Matter titles and counts. The cited summaries and quotes, and the citation pages of the viewer. Aggregate metrics on the public dashboards. |
| **Internal** | Data that we do not publish, but that causes no damage if it leaks | Source code, configuration without secrets, policies, application logs (they do not contain message content or secrets) |
| **Confidential** | Personal data or customer data | Sender addresses and names. Email subjects and bodies, raw MIME and attachments. Request rows and their audit events. Progress tokens and drop links (they give access). The outbox, IP addresses in access logs, and backups. |
| **Restricted** | Data that gives access to Confidential data or to the system | `.env` and all its contents, and `deploy/env/*.env`. Database and Redis passwords, API keys and the mailbox password. `DATA_ENCRYPTION_KEY` and `AUDIT_HMAC_KEY`. The age private key, TLS and SSH private keys, and decrypted database dumps. |

The portal marks some regulator rows as confidential. We do not hold that data. The agent lists those rows, but it never downloads or sends them.

## 2. Rules for each class

| | Public | Internal | Confidential | Restricted |
|---|---|---|---|---|
| Storage | Any location | The repository and the host | Only the data stores on the host (Postgres, `data/raw`, the sealed outbox). Backups are encrypted with age. | `.env` (mode 600) and the env file of each service (mode 600) on the host, and an offline escrow copy. Never in Git, images, logs, tickets or chat. |
| In transit | Any | TLS | TLS (Cloudflare, SMTP and IMAP with STARTTLS or TLS, HTTPS to vendors). Loopback inside the host. | Pipes and env files. Never in command-line arguments. |
| To a model | Yes | Yes | Only the email text that is necessary to classify a request. It goes to TypeSafe (not zero-data-retention) and to the zero-data-retention endpoints of OpenRouter ([AI use](ai-use.md)). | Never |
| In logs | Yes | Yes | Never the message content. The audit trail identifies a sender only by a pseudonym. | Never |
| Disclosure | Any person | Contractors under an NDA | Only the data subject (replies go only to the authenticated sender) and the vendors in the [register](vendor-register.md) | Nobody. If a secret is exposed, rotate it. |
| Retention | As necessary | As necessary | [Retention schedule](data-retention.md) | Until rotation |
| Disposal | Not applicable | Delete | The retention purge (`ragent purge`) or a recorded deletion. Backups expire. | Rotate, then delete |
| Laptops | Yes | Yes | No. Exception: encrypted backups in transit during a drill. | Only in the password manager |

## 3. Inventory

| Data | Class | Location | Who or what can read it | Retention |
|---|---|---|---|---|
| Raw inbound MIME | Confidential | `data/raw/<sha>` | ingest and worker (file system). Not web. | 30 days after the request settles (rejected mail: 7 days) |
| Request rows (`from_addr`, subject, parsed request, authentication result) | Confidential | Postgres `requests` | `agent_app`. `agent_web` reads only the progress columns (the page masks the address). | Pseudonymised at 90 days, deleted at 400 days |
| Audit events | Confidential (pseudonymous) | Postgres `events` | `agent_app` can insert and read. `agent_web` reads them for the timeline. | 400 days |
| Outbox (rendered replies) | Confidential | Postgres `outbound.body`, sealed with AES-256-GCM | worker | The purge clears the body with the raw MIME (30 days) |
| Delivery records (drop link, delete token) | Confidential | `requests.delivery`, sealed | worker | The purge removes them at 90 days. The links expire after 7 days. |
| Regulator documents, page text, summaries, citations | Public | `data/blobs`, `pages`, `summaries`, `citations` | All persons, through the viewer | Files and page text: 395 days after the last use |
| Viewer access logs (IP address, path) | Confidential | Caddy log and Cloudflare | Operator | 30 days (`roll_keep_for 720h`) |
| Metrics | Public (aggregate only) | Prometheus volume | All persons, through the public dashboards | 30 days or 5 GB |
| Secrets | Restricted | `.env`, `deploy/env/`, offline escrow | Each service gets only its own keys ([access control](access-control.md)) | Until rotation |
| Backups | Confidential (encrypted) | Backup directory, off-site copy | The holder of the offline key | 14 days local, 35 days off-site |
