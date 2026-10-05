# SOC 2 control matrix

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (@siinghd). Version 1.1. Last reviewed 2026-10-05.

This matrix maps each 2017 Trust Services Criterion to the control in the system, its evidence and the open items. It uses the 2022 points of focus. The [system description](system-description.md) gives the scope and the boundaries.

Status values:

- **I**: implemented, with evidence.
- **P**: partial.
- **O**: open.

"Operator" is the 1 person who runs the system (@siinghd). "2nd" marks a gap that needs a second person or an external party.

The status letters describe the controls in the code and in the configuration of this repository. The "Gap / owner" column and the summary also give the steps that are necessary on the live host.

WARNING: On 2026-10-05, the live host ran an older version of the code than this repository. A control in this matrix operates on the live host only after the operator deploys the current code and does the [least-privilege cutover](../runbooks/cutover-least-privilege.md).

On 2026-10-05, the live host had these differences:

- The database had only migrations 001 to 003. It had no `outbound` table and no `suppression` table.
- The services connected to Postgres as the bootstrap superuser `agent`.
- The Redis `default` user had no password.
- `deploy/deploys.log` did not exist.

NOTE: No auditor has examined the system for SOC 2. **O (2nd):** Engage an auditor for a readiness assessment. Then get a Type I report, and then a Type II report over a period of 3 to 6 months.

## Common criteria

| # | Criterion (short) | Control | Evidence | St | Gap / owner |
|---|---|---|---|---|---|
| CC1.1 | Integrity and ethical values | Acceptable use policy for operators and for users. Commitments in the security policy. | `docs/policies/acceptable-use.md`, `information-security.md` | P | Signed acknowledgements when there is staff. A code of conduct (Operator). |
| CC1.2 | Board independence and oversight | None. The owner is a sole proprietor. | — | O (2nd) | An external advisor who examines the risk register and this matrix 2 times each year |
| CC1.3 | Structure, reporting lines, authority | Roles table. The operator holds all roles. | `information-security.md` section 3 | P | Separation of duties is not possible with 1 person. The compensating controls are in `change-management.md` section 5. |
| CC1.4 | Competence | The design, the tests and these controls show the technical and security practice of the operator. | `DESIGN.md`, `tests/`, CI | P (2nd) | Background checks and records of security courses for future staff (O) |
| CC1.5 | Accountability | Each deploy and each break-glass access has a record with the name of the person: `deploys.log`, `deploy` events, Postgres connection and DDL logs. | `deploy/deploys.log` (written by `deploy/deploy.sh`), `events`, Postgres log | I | |
| CC2.1 | Relevant, quality information | Structured logs, an append-only audit trail, compliance reports and an asset inventory | `logging-monitoring.md`, `/home/deploy/compliance/`, `asset-inventory.md` | I | |
| CC2.2 | Internal communication | Policies in the repository. The PR template contains the security checklist. | `docs/policies/`, `.github/pull_request_template.md` | I | |
| CC2.3 | External communication | Privacy notice at `/privacy`, `SECURITY.md`, `/.well-known/security.txt`, rules for incident notification | `privacy-notice.md`, `SECURITY.md`, `agent/web/app.py` (routes `/privacy` and `/.well-known/security.txt`), `incident-response.md` section 6 | P | Create `privacy@hsingh.app` (Operator). Deploy the current code. On 2026-10-05, the live `/privacy` and `/.well-known/security.txt` gave HTTP 404 (Operator). |
| CC3.1 | Objectives specified | Security objectives in priority order | `information-security.md` section 2, `DESIGN.md` section 1 | I | |
| CC3.2 | Risks identified and analysed | Risk register (likelihood × impact, treatment). Threat model. | `information-security.md` section 5, `DESIGN.md` section 4 | I | A new assessment each year (Operator) |
| CC3.3 | Fraud risk | Treatment of abuse risks: spoofed senders and reflection, prompt injection, misuse of credentials, model spend | `DESIGN.md` section 4, runbooks, OpenRouter credit limit, daily model budget (`llm_daily_budget_usd`) | P | A formal note about fraud risk in the register (Operator) |
| CC3.4 | Significant changes assessed | Risk section in the PR template. Rules for vendor changes and AI changes. | `.github/pull_request_template.md`, `change-management.md`, `ai-use.md` section 2 | I | |
| CC4.1 | Ongoing and separate evaluations | Weekly `deploy/compliance_check.sh`. CI on each change. Nightly restore check. | Timer `regagent-compliance.timer`, compliance reports, CI runs, `backup.log` | I | Install `regagent-compliance.timer` on the live host. On 2026-10-05, it was not installed. Push the repository to GitHub, so that CI runs (Operator). Independent evaluation (O, 2nd). |
| CC4.2 | Deficiencies communicated and corrected | Each FAIL line becomes an incident or a change. This matrix records each gap and its owner. | Compliance reports, PRs | P | An issue tracker for open items after the repository is on GitHub (Operator) |
| CC5.1 | Controls selected to decrease risk | The risk register maps each risk to its controls. | `information-security.md` section 5 | I | |
| CC5.2 | Technology general controls | Hardened containers, least-privilege roles, CI gates, a pinned supply chain | `docker-compose.yml`, `deploy/sql/`, `.github/workflows/ci.yml`, `Dockerfile` | I | |
| CC5.3 | Policies and procedures deployed | Policy set with owners, review intervals and exceptions | `docs/policies/README.md` | I | |
| CC6.1 | Logical access security | 1 Postgres role and 1 Redis ACL user for each process. Secrets for each service. All listeners bind to 127.0.0.1. SSH with keys only. ufw. | `deploy/sql/roles.sql`, `grants.sql`, `deploy/redis/users.acl.template`, `deploy/env/services.toml`, `pg_hba.conf`. Proofs: `deploy/verify-db-roles.sh`, `deploy/validate_redis_acl.py`, compliance report. | I | Do the least-privilege cutover on the live host (Operator). Disk encryption at rest (O, Operator). |
| CC6.2 | Users registered and authorised | Steps to give access. The code defines the service identities. | `access-control.md` sections 3 and 5 | P | Evidence of MFA on the vendor accounts (Operator) |
| CC6.3 | Role-based access, least privilege, removal | Role matrix. The app cannot DELETE or run DDL. `events` is append-only. The web role can read only some columns. Access is removed on the same day. | `access-control.md`, `deploy/sql/verify_grants.sql` (77 checks for specific roles, and 5 checks for each role) | P | Do the least-privilege cutover on the live host (Operator). `deploy` has passwordless sudo (O, Operator). The first quarterly access review is due on 2027-01-04. |
| CC6.4 | Physical access | Carved out: Hetzner (ISO 27001 data centres) | `vendor-register.md` | I (CSOC) | |
| CC6.5 | Disposal of assets and data | Retention purge (`ragent purge`, `agent/retention.py`), backup expiry, procedure to destroy the VM | `data-retention.md` section 3, `deploy/regagent-retention.timer` | I | Install `regagent-retention.timer` on the live host. On 2026-10-05, it was not installed (Operator). |
| CC6.6 | External threats at the boundary | Cloudflare edge. Caddy is the only entry point. ufw. CSP and HSTS. DMARC gate for email. Rate limits. | `deploy/caddy/uarb.caddy`, `DESIGN.md` section 4, compliance report (loopback ports) | I | Authenticated Origin Pulls (O, Operator) |
| CC6.7 | Data in transit protected | TLS for all traffic off the host. Client-side encryption for drop links. SSH tunnel for the UARB egress. | `encryption.md` section 2 | I | |
| CC6.8 | Malicious software prevented or detected | Read-only containers without capabilities. Pinned and scanned images. Dependencies checked by hash. No package installs at run time (pip is removed). gVisor is installed on the host, but the worker does not use it yet. | `Dockerfile`, CI Trivy and pip-audit, `docker-compose.yml` | P | gVisor for the worker, after the worker stops the use of the host network (Operator) |
| CC7.1 | Configuration changes and vulnerabilities detected | Drift check (compose dry run, configuration that is not committed, image revision). Trivy, pip-audit, Dependabot, unattended-upgrades. | `compliance_check.sh`, CI, `vulnerability-management.md` | I | |
| CC7.2 | Anomalies monitored | Health checks, logs, ACL denial log, review of failed requests. Prometheus metrics and black-box probes with alert rules, with 1 runbook for each rule. Grafana dashboards, public and read-only at `/grafana/`. | `logging-monitoring.md` section 4, `deploy/observability/`, `docs/runbooks/` | P | An alert receiver: Alertmanager sends all alerts to `blackhole` until the owner sets `ALERT_RECEIVER` and a dead man's switch URL. A copy of the logs off the host (O, Operator). |
| CC7.3 | Security events evaluated | Severity scheme and triage | `incident-response.md` sections 2 and 4 | I | |
| CC7.4 | Incidents responded to | Plan, kill switch and incident commands (`ragent pause`, `ragent block`, `ragent revoke`), runbooks, preservation of evidence | `incident-response.md`, `docs/runbooks/` | I | Tabletop exercise (O: 2027-Q1) |
| CC7.5 | Recovery from incidents | BCP rebuild steps, restore drills | `business-continuity.md` | P | Off-site backups (O, Operator) |
| CC8.1 | Change management | PR and CI gates. A scripted deploy with tests, a scan, a rollback and records. An emergency path. Compensating controls for 1 operator. | `change-management.md`, `.github/workflows/ci.yml`, `deploy/deploy.sh`, `deploys.log` | P | Branch protection (O). CI runs only after the repository is on GitHub (O). An independent approver (O, 2nd). |
| CC9.1 | Business disruption risk decreased | Degraded modes, backups, RTO and RPO | `business-continuity.md` | P | Off-site backups. A timed rebuild (Operator). |
| CC9.2 | Vendor and partner risk | Vendor tiers, due diligence, register, annual review | `vendor-management.md`, `vendor-register.md` | P | DPAs with Hetzner, Cloudflare, OpenRouter and TypeSafe (Operator) |

## Availability

| # | Criterion | Control | Evidence | St | Gap / owner |
|---|---|---|---|---|---|
| A1.1 | Capacity managed | CPU, memory and PID limits for the containers. Redis `maxmemory` with `noeviction`. Disk guard (`DISK_MIN_FREE_BYTES`). Weekly disk check. Alerts for disk, memory, queue and budgets, and the Host dashboard. | `docker-compose.yml`, `redis.conf`, compliance report, `deploy/observability/rules/` | P | The shared disk was 87% full on 2026-10-05. Plan a dedicated volume (Operator). |
| A1.2 | Environmental protection, backup, recovery infrastructure | Hetzner data centre controls (carved out). Nightly encrypted backups with a restore test. AOF for Redis. | `backup.md`, `deploy/backup.sh` | P | Off-site copy (O) |
| A1.3 | Recovery plan tested | Nightly restore check. Quarterly drill. Annual rebuild. | `backup.log`, BCP section 6 (first drill on 2026-10-04) | P | No annual full rebuild yet |

## Confidentiality

| # | Criterion | Control | Evidence | St | Gap / owner |
|---|---|---|---|---|---|
| C1.1 | Confidential information identified and protected | Classification and inventory. Sealed outbox and links. Least-privilege access. The web process cannot read raw mail. | `data-classification.md`, `encryption.md` | I | Do the least-privilege cutover on the live host (Operator). |
| C1.2 | Confidential information disposed | Retention schedule. Purge role, purge command (`ragent purge`) and service unit. Backup expiry. Caddy keeps access logs for 30 days (`roll_keep_for 720h`). | `data-retention.md`, `agent/retention.py`, `deploy/caddy/uarb.caddy` | I | Install `regagent-retention.timer` on the live host (Operator) |

## Processing integrity

| # | Criterion | Control | Evidence | St | Gap / owner |
|---|---|---|---|---|---|
| PI1.1 | Processing specifications defined | What a request is, the categories of each provider, the contents of a reply | `README.md`, `DESIGN.md` sections 2 and 3, provider adapters | I | |
| PI1.2 | Inputs complete and accurate | Sender authentication. Rules first, then TypeSafe Jev with closed answer sets, then the LLM. Code validates the LLM output against a schema. The matter must occur in the email. The category must come from a closed set. | `agent/gate/`, `tests/adversarial/`, `ai-use.md`, `evals/gate/` | I | |
| PI1.3 | Processing complete and accurate | CAS state machine, idempotent steps, single-flight locks, an id check for each file, and a second check of "not found" in a fresh session | `DESIGN.md` sections 3 and 5, integration tests | I | |
| PI1.4 | Outputs complete and accurate | ZIP manifest with SHA-256. Each citation is located in the page text and checked for support. The reply states partial results. 1 reply for each request. | `agent/delivery/`, `agent/citations/` | I | |
| PI1.5 | Inputs and outputs stored completely and accurately | Content-addressed blobs. Append-only events. The outbox row is stored in the same transaction as the state change. | `agent/blobs.py`, `events`, `outbound` | I | |

## Privacy

| # | Criterion | Control | Evidence | St | Gap / owner |
|---|---|---|---|---|---|
| P1.1 | Notice of privacy practices | Plain-language notice. The web process serves it at `/privacy`. Each email from the agent has a link to it. | `docs/policies/privacy-notice.md`, `agent/web/templates/privacy.html`, `agent/mail/outbound.py` | I | Deploy the current code. On 2026-10-05, the live `/privacy` gave HTTP 404 (Operator). |
| P2.1 | Choice and consent | The sender starts the processing with an email. The notice explains the use of models. The agent honours an objection through the DSAR procedure and the `suppression` list. | Privacy notice, DSAR procedure, migration 005, `agent/admin.py` | I | Apply migration 005 on the live host (Operator) |
| P3.1 | Collection limited to the purpose | Only the email and its headers. The parser skips attachments. No trackers. | Privacy notice, `agent/mail/mime.py` | I | |
| P3.2 | Explicit consent for sensitive data | The system does not collect it, because a request needs only a matter number. The notice tells users not to send it. | Acceptable use, Part B | I | |
| P4.1 | Use limited to stated purposes | The agent uses data only to answer, to secure and to operate the service. It does not sell data. OpenRouter calls go only to zero-data-retention endpoints. TypeSafe is not zero-data-retention on our plan. The privacy notice discloses this. | Privacy notice, `ai-use.md` | I | |
| P4.2 | Retention | Schedule with mechanisms: `ragent purge` | `data-retention.md`, `agent/retention.py` | I | Install `regagent-retention.timer` on the live host (Operator) |
| P4.3 | Secure disposal | Purge, backup expiry, VM destruction | `data-retention.md` section 3 | I | Same as P4.2 |
| P5.1 | Access by data subjects | DSAR procedure with identity checks and queries | `dsar-procedure.md` | I | |
| P5.2 | Correction | Same procedure | `dsar-procedure.md` section 4 | I | |
| P6.1 | Disclosure to third parties | Only the listed processors, only for the stated purposes | `vendor-register.md` | I | |
| P6.2 | Record of disclosures | Register of processors. 1 `llm.call` event for each model call of a request, with the model, the provider, the data class, the zero-data-retention flag and the cost. | `vendor-register.md`, `events` (`llm.call`, `agent/audit.py`) | I | Deploy the current code on the live host (Operator) |
| P6.3 | Record of unauthorised disclosures | Incident notes, kept for 400 days | `incident-response.md` section 4 | I | |
| P6.4 | Third-party commitments | Vendor terms, zero-data-retention routing for OpenRouter | `vendor-register.md` | P | DPAs (O) |
| P6.5 | Third parties report breaches to us | Through the vendor terms | Vendor terms | P | Confirm the notification clauses in each DPA |
| P6.6 | Notify data subjects of breaches | Notification rules (PIPEDA, GDPR) | `incident-response.md` section 6 | I | |
| P6.7 | Accounting of disclosures to data subjects | The DSAR access answer lists the processors. | `dsar-procedure.md` section 4 | I | |
| P7.1 | Personal data accurate and complete | The data comes from the email of the data subject. The agent replies only to the authenticated address. | Gate, `DESIGN.md` section 4 | I | |
| P8.1 | Inquiries, complaints and disputes | Intake at `privacy@hsingh.app`, DSAR register, the notice explains escalation to the authorities | `dsar-procedure.md`, privacy notice | P | Create the mailbox or the alias (Operator) |

## Open items (summary)

Organisational items:

- Engage an auditor for a readiness assessment.
- Get an independent reviewer (a second person or an advisor) for changes, access reviews and the risk register.
- Sign DPAs with Hetzner, Cloudflare, OpenRouter and TypeSafe.
- Do background checks and keep records of security courses for future staff.
- Get signed policy acknowledgements.
- Get a contact for legal and privacy counsel.
- Push the repository to GitHub and set branch protection.

Technical items:

- Deploy the current code and apply the migrations on the live host. This adds `/privacy`, `/status` and `/.well-known/security.txt`.
- Do the least-privilege cutover for Postgres and Redis on the live host.
- Install `regagent-retention.timer` and `regagent-compliance.timer` on the live host.
- Configure the off-site backup copy.
- Encrypt the disk at rest.
- Set an alert receiver, and copy the logs off the host.
- Run the worker under gVisor.
- Remove passwordless sudo for `deploy`.
