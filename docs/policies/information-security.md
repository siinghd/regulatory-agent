# Information Security Policy

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

## 1. Scope

This policy applies to the Regulatory Document Agent and to all items that it needs to operate:

- the email interface `agent@hsingh.app` (IMAP in, SMTP out, through Postfix and Dovecot on the host);
- the processes `ingest`, `worker`, `web`, `migrate` and `retention`, and their data stores (Postgres, Redis, `data/raw`, `data/blobs`) on 1 Hetzner VM (`ubuntu-16gb-hel1-1`, Helsinki);
- the viewer `uarb.hsingh.app` (Cloudflare, then Caddy, then `web` on 127.0.0.1:8710), with the public `/status` page and the public read-only Grafana at `/grafana/`;
- the observability stack (Prometheus, Alertmanager, Grafana and the exporters) on the same host;
- the encrypted file drop for delivery (`drop.hsingh.app`, on the same host);
- the Canadian egress VM (Azure) that carries the UARB portal traffic through an SSH SOCKS tunnel;
- the vendors that process the data ([vendor register](vendor-register.md)), the source repository and CI;
- the workstation and the accounts that the operator uses to administer these items.

## 2. Security objectives

The objectives are in priority order. They agree with the goals in [DESIGN.md](../../DESIGN.md).

1. **Do not send the wrong data to the wrong person.** Replies go only to the authenticated sender (DMARC-aligned SPF or DKIM pass). The provider compares each document with the requested id before the agent sends it. The agent never sends confidential regulator rows.
2. **Protect the data that senders give.** Email addresses and message content are Confidential ([classification](data-classification.md)). The system keeps them only for the time in the [retention schedule](data-retention.md). Only the listed vendors receive them.
3. **Keep the content of the replies correct.** Each summary claim quotes the document that it cites. Code removes figures that the sources do not contain. The audit trail of each request is append-only for the application.
4. **Keep the service available within the stated targets** (RTO 4 h, RPO 24 h, [business continuity](business-continuity.md)). Do not fail silently. Each authenticated request ends with exactly 1 useful reply.

## 3. Roles and responsibilities

| Role | Held by | Responsibilities |
|---|---|---|
| Security officer | Operator | These policies, the risk register, exceptions and vendor reviews |
| System administrator | Operator | Host, containers, databases, secrets, backups and access |
| Developer | Operator (and AI code assistants under the [AI use policy](ai-use.md)) | Code, tests and dependency updates |
| Incident commander | Operator | [Incident response](incident-response.md) |
| Independent reviewer | **Open**: nobody yet | Second approval of high-risk changes and of access reviews |
| Users | All persons who send email to the agent | [Acceptable use](acceptable-use.md) of the service |

1 person has all roles. Thus, there is no separation of duties. The substitute controls are automatic. Thus, they do not depend on the attention of the operator on a given day:

- CI gates on each change;
- deploys only through `deploy/deploy.sh`, which records who deployed what, and when;
- a weekly `deploy/compliance_check.sh` that finds drift;
- append-only audit events;
- backups that are encrypted to a key that the host cannot read.

The first employee or contractor with access becomes the independent reviewer ([change management](change-management.md#5-substitute-controls-for-1-operator)).

## 4. Principles

- **Least privilege for each component.** Each process has its own database role (`deploy/sql/roles.sql`), its own Redis user (`deploy/redis/users.acl.template`) and its own env file with only its secrets (`deploy/env/services.toml`). Containers are read-only, have no capabilities and do not run as root.
- **Only Caddy and the MTA listen on public interfaces.** Postgres, Redis, the web process, the metrics endpoints and the observability stack bind to 127.0.0.1. ufw is active. SSH accepts only keys.
- **Secrets are only in `.env`** (mode 600, not in Git). `deploy/split-env.sh` gives each service only its own secrets. The system never writes secrets to logs or to command lines. Deploy scripts give them through pipes or env files.
- **Untrusted input is data.** Email bodies, portal HTML, PDFs and LLM output are untrusted. Code parses them with care, fences them in prompts and validates them against schemas and closed sets.
- **Fail closed and visibly.** Unauthenticated mail gets no reply. `deploy/split-env.sh` copies an unknown env key to no service and reports it. At its memory limit, Redis refuses writes and does not remove queue entries (`noeviction`).
- **Reproducible system.** The operator can rebuild the system from the repository, the escrowed secrets and the backups.

## 5. Risk assessment

The operator does the risk assessment at least 1 time each year and after each significant change. This table is the record, and the PR history keeps each assessment. Likelihood (L) and impact (I) are 1 (low) to 3 (high).

| # | Risk | L | I | Treatment | Status |
|---|---|---|---|---|---|
| R1 | The agent replies to spoofed senders (reflection or spam) | 2 | 3 | DMARC alignment from the `Received` header of our own MTA. Pre-authentication limits. Sender, domain and global rate limits. Limited "slow down" replies. Loop detection. | Implemented |
| R2 | Prompt injection changes the output | 2 | 3 | The recipient never comes from content. Fenced prompts, schemas and closed sets. Code finds each quote in the page text. | Implemented |
| R3 | The portal serves a wrong file under the correct name | 2 | 3 | The provider compares each file with the requested id. A download lock. [Runbook](../runbooks/portal-wrong-documents.md). | Implemented |
| R4 | A credential leaks (repository, logs, host) | 1 | 3 | gitleaks in CI, secrets on pipes, 1 env file for each service, [runbook](../runbooks/credential-leak.md) | Implemented |
| R5 | Loss of the host (1 VM) | 1 | 3 | Nightly encrypted backups with a restore check. An off-site copy. | Partial: the off-site copy is not configured |
| R6 | An attacker gets control of the host through the app (a browser exploit in portal content) | 1 | 3 | Patched Chromium. Read-only containers without capabilities. Separate database and Redis roles. gVisor is installed. | Partial: gVisor needs the end of the host network mode |
| R7 | A model vendor keeps or leaks email content | 2 | 2 | Zero-data-retention routing on OpenRouter. Minimum content. Vendor review. TypeSafe is not zero-data-retention on our plan (owner decision 2026-10-05, disclosed in the [privacy notice](privacy-notice.md)). `GATE_CLASSIFIER=llm` and `CITATION_CHECK=llm` stop all traffic to TypeSafe. | Partial: DPAs and the endpoint allowlist are open |
| R8 | The 1 operator is not available, or makes a mistake that nobody reviews | 2 | 2 | Substitute controls (section 3), runbooks, escrowed secrets | Partial: no second person |
| R9 | A full disk stops ingest | 2 | 2 | Free-space guard (`disk_min_free_bytes`, 5,000,000,000 bytes). Retention purge (`ragent purge`). Disk alerts. Weekly check. | Partial: `regagent-retention.timer` is in `deploy/`, but it was not installed on the host on 2026-10-05 |
| R10 | A dependency or an image is not patched | 2 | 2 | Dependabot, pip-audit, Trivy gates, weekly OS update layer | Implemented |

## 6. Policy set, compliance and enforcement

The policies in [README.md](README.md) are part of this policy. CI examines the technical controls on each change. `deploy/compliance_check.sh` examines them each week and writes its evidence to `/home/deploy/compliance/`. A person who gets access in the future must read and accept these policies before the operator gives the access. A breach of a policy causes the removal of access. Exceptions obey the [governance rules](README.md#governance).

NOTE: On 2026-10-05, `regagent-compliance.timer` was not installed on the host, and `/home/deploy/compliance/` did not exist. Until the operator installs the timer, do this step each week:

1. Run the compliance check and keep the report.

   ```bash
   deploy/compliance_check.sh --report-dir /home/deploy/compliance
   ```

   Expected result: 1 PASS, WARN or FAIL line for each control, and the file `/home/deploy/compliance/compliance-<date>.txt`.
