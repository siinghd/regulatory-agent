# System description: Regulatory Document Agent

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (@siinghd). Version 1.1. Last reviewed 2026-10-05.

This description uses the structure of the AICPA description criteria (DC 200). It is the start point for a SOC 2 readiness assessment. It describes the system on 2026-10-05.

NOTE: No auditor has examined the system. No statement in this document is the opinion of an auditor. The [control matrix](control-matrix.md) gives the controls and their status.

WARNING: This description and the control matrix describe the code and the configuration in this repository. On 2026-10-05, the live host ran an older version of the code. The [control matrix](control-matrix.md) lists the differences and the steps to remove them.

## DC1. Services provided

The Regulatory Document Agent answers email requests for public filings of utility regulators. A user sends an email to `agent@hsingh.app` with a matter number and a document category. An example is "the Other Documents for M12205".

The agent does these steps:

1. It authenticates the sender.
2. It gets the filings from the public portal of the regulator: Nova Scotia UARB, the Ontario Energy Board (OEB) or the US Federal Energy Regulatory Commission (FERC).
3. It puts up to 10 documents in a ZIP file with a manifest.
4. It writes a summary. Each claim in the summary links to the exact quoted passage.
5. It replies in the same thread: first an acknowledgement with a link to a live progress page, then the documents.

The documents go out as an end-to-end encrypted download link, or as an attachment. The viewer at `uarb.hsingh.app` shows the cited passages and the progress of each request. The viewer also serves a public status page (`/status`), the privacy notice (`/privacy`) and read-only Grafana dashboards (`/grafana/`).

## DC2. Principal service commitments and system requirements

The privacy notice, the README and `SECURITY.md` give these commitments to users:

1. The agent replies only to the authenticated sender. Unauthenticated or spoofed mail gets no reply.
2. Each authenticated request gets exactly 1 useful reply. A useful reply is the documents, a question, a "not found" answer or an apology.
3. The delivered files are the requested files. The agent compares each file with the requested id, the size limit and the file type, and records its SHA-256. The agent never sends rows that the regulator marks as confidential.
4. Each summary claim has a quoted passage from a delivered document.
5. The agent uses personal data only to answer, to secure and to operate the service. It keeps personal data for the periods of the retention schedule. It shares personal data only with the listed processors. It never sells personal data, and it does not use personal data to train models.
6. The operator acknowledges each security report within 3 business days.

These commitments give these system requirements:

- DMARC-aligned sender authentication, calculated by the agent from the header of its own MTA.
- Least-privilege access for each process.
- Encryption in transit.
- Encrypted backups with a nightly restore test.
- An RTO of 4 h and an RPO of 24 h.
- Fix times for vulnerabilities: 7 days for critical, 30 days for high.
- Retention by an automatic purge (`ragent purge`).
- Change management with automatic gates.

## DC3. Components of the system

### Infrastructure

- 1 Hetzner Cloud VM in Helsinki (`ubuntu-16gb-hel1-1`, Ubuntu 24.04 LTS, aarch64). Other projects of the operator use the same VM. The firewall is ufw. SSH accepts only keys.
- The Docker Compose project `regulatory-agent`:
  - Postgres 16 and Redis 7, published only on 127.0.0.1.
  - The app containers `ingest`, `worker` and `web`.
  - The 1-time containers `migrate` and `db-grants`, and the `retention` container (profile `ops`) for `ragent purge` and DSAR deletes.
  - The observability stack: Prometheus, Alertmanager, Grafana, the node exporter and the blackbox exporter, all on 127.0.0.1.
- The app containers run as uid 1000. They have read-only root file systems, no capabilities, `no-new-privileges`, and CPU, memory and PID limits. They use the host network mode, because the worker must reach the egress tunnel on the host loopback.
- Caddy is the TLS origin for the viewer, with Cloudflare in front. Postfix and Dovecot on the same host serve `mail.hsingh.app`. A self-hosted encrypted file drop runs at `drop.hsingh.app`.
- An Azure VM in Canada is the SOCKS egress for the UARB portal, because the portal answers only IP addresses in North America. A systemd unit (`uarb-egress-tunnel.service`) keeps an SSH tunnel to it. There is 1 egress only. No fallback proxy exists. An egress pool is a proposed upgrade (ADR-022 in the [decisions log](../guide/decisions-log.md)).

### Software

The application is in Python 3.12, with FastAPI, arq, asyncpg, Playwright with Chromium, httpx and PyMuPDF. The build makes 1 image from the repository. The base images are pinned by digest, and the dependencies are locked by hash.

Each regulator has a provider adapter behind a small interface. The UARB adapter uses deterministic browser automation (Playwright). The OEB and FERC adapters use JSON APIs over HTTPS.

Triage uses rules first. If the rules cannot decide, TypeSafe Jev (`jev-1.13.0`, pinned) selects from closed sets. If Jev is not sure or does not answer, an LLM through OpenRouter decides. TypeSafe Jev also checks if each quote supports its claim. OpenRouter writes the summaries. OpenRouter calls go only to zero-data-retention endpoints, with the models `deepseek/deepseek-v4.1-flash` and then `qwen/qwen3.8-27b`.

NOTE: TypeSafe is not zero-data-retention on our plan. The agent sends it the email subject and body (triage) and public document excerpts (citation checks). The owner accepted this for the MVP on 2026-10-05. The privacy notice discloses it, and each worker start writes it to the log.

### People

1 operator (@siinghd) does all roles: development, operations, security and incident response. No other employee or contractor has access.

### Procedures

The policies in `docs/policies/` give the procedures. They cover these subjects:

- Access control, change management and vulnerability management.
- Logs and alerts, and incident response with runbooks.
- Business continuity and disaster recovery (BCP and DR), and backup.
- Data classification, retention and encryption.
- Vendor management, privacy, AI use, acceptable use and the asset inventory.

These scripts and commands do the procedures:

- `deploy/deploy.sh`, `deploy/db-cutover.sh`, `deploy/redis-cutover.sh` and `deploy/split-env.sh`.
- `deploy/backup.sh`, `deploy/compliance_check.sh`, `deploy/verify-db-roles.sh` and `deploy/validate_redis_acl.py`.
- `ragent purge`, `ragent dsar`, `ragent pause`, `ragent resume`, `ragent block`, `ragent revoke` and `ragent audit`.

### Data

| Data | Source | Store |
|---|---|---|
| Inbound email (address, name, subject, body, headers) | Requesters | `data/raw` (raw MIME), `requests` |
| Request state, audit trail, outbound mail | Generated | `requests`, `events`, `outbound` (sealed) |
| Suppressed senders (DSAR deletes and blocks) | Requesters, operator | `suppression` (HMAC of the address) |
| Regulator documents, page text, summaries, citations | Public portals, models | `data/blobs`, `pages`, `summaries`, `citations` |
| Metrics (aggregate counts and durations only) | Generated | Prometheus volume `promdata` (30 days or 5 GB) |
| Secrets | Operator | `.env` (mode 600), env files for each service (mode 600), offline escrow |

### Boundaries and data flow

```
requester ──SMTP──► Postfix/Dovecot ──IMAP──► ingest ──► Postgres (request row) + data/raw
                                                    └──► Redis queue ──► worker
worker ──► gate: SPF/DKIM/DMARC from our MTA's Received header; rate limits;
           rules → TypeSafe Jev → LLM (OpenRouter, ZDR) when Jev is not sure
       ──► provider adapters ──► UARB (Playwright, 1 Canadian SSH SOCKS egress)
                             ──► OEB (JSON API) / FERC eLibrary (JSON API)
       ──► blobs, ZIP, encrypted upload to drop
       ──► cited summary (OpenRouter, ZDR); citation support check (TypeSafe Jev)
       ──► reply through SMTP submission (to the authenticated sender only)
viewer:  browser ──TLS──► Cloudflare ──TLS──► Caddy ──► web (127.0.0.1:8710, read-only role)
                                                    └──► Grafana /grafana/ (127.0.0.1:3310, anonymous Viewer)
metrics: Prometheus (127.0.0.1:9090) ◄── worker 127.0.0.1:9710, ingest 127.0.0.1:9711,
                                        web /metrics (loopback only), exporters, probes
```

In scope: all components in the diagram that run on the host or are in the repository.

Out of scope (carved out, refer to DC7): Hetzner, Cloudflare, Microsoft Azure, OpenRouter and the model providers behind it, TypeSafe, GitHub and Let's Encrypt.

## DC4. Identified system incidents

No incident was significant to the service commitments.

During development, before this description, the operator found 2 portal behaviours. Each of them can cause the agent to send wrong documents. The operator corrected both before they had an effect on a user:

- A click on Search immediately after the typed number searched for an empty value.
- The portal served the prepared file of another session under the requested name. Now the agent compares each file with the requested id, and a lock serialises the download step.

`README.md` and the [providers guide](../guide/providers.md) describe both. The [portal wrong documents runbook](../runbooks/portal-wrong-documents.md) covers the second.

## DC5. Applicable criteria and related controls

The applicable criteria are Security (common criteria CC1 to CC9), Availability (A1), Confidentiality (C1), Processing Integrity (PI1) and Privacy (P1 to P8). The [control matrix](control-matrix.md) gives the controls for each criterion.

## DC6. Complementary user entity controls (CUECs)

Users must do these things:

- Send requests from a domain that publishes SPF, DKIM and DMARC records. Keep the mailbox secure. The agent trusts that the authenticated sender is the requester.
- Do not forward progress links or download links to persons who must not have them. Links are capability tokens. Download links expire after 7 days or 25 downloads.
- Do not include confidential or personal information that the request does not need.
- Report suspected misuse to security@hsingh.app.

## DC7. Subservice organisations and complementary subservice organisation controls (CSOCs)

This description uses the carve-out method. The system relies on these controls at the subservice organisations:

| Subservice organisation | Controls relied on |
|---|---|
| Hetzner Online | Physical and environmental security of the data centre. Hypervisor isolation. Media sanitisation when a VM is deleted. Network availability. |
| Cloudflare | TLS termination and edge security for the viewer. DNS integrity. DDoS protection. Durability of R2 storage for encrypted backups, after the operator configures it. |
| Microsoft Azure | Isolation and availability of the egress VM |
| OpenRouter and the model providers behind it | Zero-data-retention routing. Access control to API traffic. Breach notification. |
| TypeSafe | Access control to API traffic. The data terms of our plan (not zero-data-retention). Breach notification. |
| GitHub | Integrity and access control of the repository and the CI runners. Isolation of Actions. |
| Let's Encrypt | Correct certificate issue |

## DC8. Criteria not applicable

No criterion is excluded. The privacy criteria apply in a reduced form:

- The only data subjects are the requesters, with the content of their emails.
- A requester gives consent by the request itself.
- The agent sends no advertisements, and it uses the data for no other purpose.

The subservice organisation (DC7) fully meets CC6.4 (physical access).

## DC9. Significant changes

- 2026-10: Least-privilege database roles, a Redis ACL, secrets for each service and hardened containers replace the single superuser connection and the shared environment. The operator added supply-chain pins and CI gates, and adopted this policy set. `deploy/deploys.log` records the cutover after the operator does it on the live host.
- 2026-10: The code added the FERC provider, and TypeSafe Jev for triage and citation checks. It also added the retention purge (`ragent purge`), the DSAR commands, the kill switch and the hash-chained audit export.
- 2026-10: The operator added Prometheus, Alertmanager and Grafana, with a public read-only view at `/grafana/`, and the public `/status` page.

Planned changes:

- Deploy the current code, and do the least-privilege cutover on the live host.
- Install `regagent-retention.timer` and `regagent-compliance.timer` on the live host.
- Configure the off-site backup copy.
- Encrypt the disk at rest.
- Set an alert receiver, so that alerts reach a person.
- Move the egress tunnel into a sidecar container. Then stop the host network mode, and run the worker under gVisor.
