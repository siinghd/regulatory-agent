# Incident Response Plan

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05. SOC 2: CC7.3–CC7.5, CC2.3, P6.

Review this plan each year and after each SEV1 or SEV2 incident.

## 1. Definition of an incident

An incident is an event that causes harm, or can cause harm, to the confidentiality, integrity or availability of the service or of its data. Examples:

- a reply to the wrong person;
- a leaked credential, or a compromised host or account;
- a reply to spoofed mail;
- wrong documents in a reply;
- an outage that is longer than the targets;
- a breach at a vendor that affects our data;
- a security report that is correct.

## 2. Severity

| Severity | Examples | Response |
|---|---|---|
| SEV1 | Personal data or documents sent to the wrong recipient. A compromised host or credential with evidence of use. The agent answers spoofed mail at scale. | Start immediately. Continue until the harm stops. |
| SEV2 | An outage of more than 1 h, or the RTO is at risk. A security control does not operate (authentication checks off, ACL or roles bypassed). A leaked credential without evidence of use. Wrong documents found before the agent sent them. | On the same day |
| SEV3 | 1 failed request, a degraded mode (no summaries), a near miss | On the next business day |

## 3. Roles

The 1 operator is the incident commander, the investigator and the communicator. External contacts:

- vendor support: Hetzner, Cloudflare, OpenRouter, TypeSafe, Azure and GitHub (the [vendor register](vendor-register.md) lists the consoles);
- legal and privacy counsel (**Open**: name a counsel before the first customer contract).

If the operator is not available, the service stays in its safe failure modes. Unauthenticated mail never gets a reply, and failures end with apologies. A delegate can rebuild the service with the escrowed recovery material ([business continuity](business-continuity.md)).

## 4. Phases

1. **Detect and record.** Start an incident note immediately, with UTC times: what you saw, who saw it, and when. Sources are healthchecks, alerts, the weekly compliance report, logs, a report to `security@hsingh.app`, or a vendor notice.
2. **Triage.** Give the incident a severity. Decide if the incident possibly involves personal data. If yes, start section 6 now.
3. **Contain.** Use the smallest control that stops the harm (section 4.1).
4. **Keep the evidence** before you change more. Section 4.2 gives the procedure.
5. **Remove the cause and recover.** Correct the cause through the change process. You can use an emergency change. Restore from a backup if necessary. Run `deploy/compliance_check.sh` and `deploy/verify-db-roles.sh` before you declare the recovery.
6. **Notify** (section 6).
7. **Review.** For SEV1 and SEV2, write the review within 5 business days. Section 4.3 gives the contents.

### 4.1 Controls to contain an incident

| Control | Effect |
|---|---|
| `docker compose exec worker ragent pause --reason "<why>"` | Kill switch: all requests park and the outbox holds all mail. `ragent resume` stops it ([Operations](../guide/operations.md), section 9.1). |
| `docker compose exec worker ragent block <address or @domain> --reason "<why>"` | The gate drops all mail from that sender or domain without a reply |
| `docker compose exec worker ragent revoke <request id>` | Deletes the download link of a request |
| `docker compose stop ingest` | The agent takes no new mail. The mail waits in IMAP. |
| `docker compose stop worker` | The agent fetches, summarises and sends nothing |
| `SENDER_AUTH_MODE=allowlist` (and `SENDER_ALLOWLIST`), then `deploy/split-env.sh` and a restart of the worker | The agent serves only named senders |
| Lower `RATE_*` limits | Fewer authenticated requests in a flood |
| Lower `PREAUTH_*` and `INBOUND_PER_MINUTE` (ingest) | Fewer messages in a flood, before the system stores or examines them |
| Lower `LLM_DAILY_BUDGET_USD`, `PORTAL_DAILY_VISITS` and `BYTES_PER_SENDER_DAY` | Less spend, less load on a regulator, and less data sent out |
| Set `GATE_CLASSIFIER=llm` and `CITATION_CHECK=llm` | No more data goes to TypeSafe ([LLM provider incident runbook](../runbooks/llm-provider-incident.md)) |
| Lower `WEB_RATE_*` (web) | Fewer scrapes of the viewer |
| Revoke or rotate the credential ([runbook](../runbooks/credential-leak.md)) | The leaked key no longer gives access |
| Cloudflare: pause the `uarb.hsingh.app` route, or enable "Under attack" mode | The viewer is offline, or each visitor gets a challenge |
| `ufw deny`, or Postfix client restrictions | Blocks a source |

### 4.2 Procedure: keep the evidence

1. Make the incident directory `/home/deploy/incidents/<id>/` with mode 700.
2. Write the container logs to the incident directory.

   ```bash
   docker compose logs --no-color > /home/deploy/incidents/<id>/logs.txt
   ```

3. Make an encrypted database dump with `deploy/backup.sh`.
4. Copy the related `events` rows, the Redis `ACL LOG`, the Caddy and mail logs and `deploys.log`.
5. Keep the directory for 400 days.

### 4.3 Contents of the review

- the timeline;
- the root cause;
- the signal that found the incident, and a signal that can find it sooner;
- the actions, each with an owner and a date;
- the necessary changes to policies and runbooks.

Keep the review with the incident note. Record each action as an issue.

## 5. Runbooks

- [Credential leak](../runbooks/credential-leak.md)
- [Spoofing wave](../runbooks/spoofing-wave.md)
- [Portal serves wrong documents](../runbooks/portal-wrong-documents.md)
- [LLM provider incident](../runbooks/llm-provider-incident.md)
- [Disk full](../runbooks/disk-full.md)
- 1 runbook for each alert. Each alert has a `runbook_url`, and [deploy/observability/README.md](../../deploy/observability/README.md#alerts) lists the alerts. These runbooks cover:
  - failed requests, a stuck queue, worker metrics down and ingest stalled;
  - an open breaker and budgets;
  - web down, mail endpoints down and the egress tunnel down;
  - TLS expiry, Postgres down, Redis down and an old backup;
  - the latency SLO and the observability stack itself.

## 6. Notification

- **Affected requesters:** Send an email if the data or the documents of a requester were exposed. Also send an email if a requester received wrong documents. Send it from the agent mailbox, only to the authenticated address. Tell what occurred, what it means for the requester, and what the operator did. Send it when the facts are confirmed, and not later than 72 hours after the confirmation.
- **Regulators:** Assess a breach of personal information against the laws of the persons in it. Write the assessment and the reasons into the incident note, also when the result is "not notifiable".
  - Canada (PIPEDA): when there is a real risk of significant harm, report to the Office of the Privacy Commissioner. Also notify the persons "as soon as feasible". Keep a breach record for 24 months.
  - EU senders (GDPR): notify the supervisory authority within 72 hours of awareness, unless a risk is unlikely.
  - Other persons: apply their laws as necessary.
- **Customers with contracts:** As their contract specifies (**Open**: none yet).
- **Vendors:** If the incident involves their service or our credentials on it.

## 7. Tests

- Do a tabletop exercise 2 times each year, with 1 runbook each time. The first exercise is the credential leak runbook, in 2027-Q1.
- Do a real restore drill each quarter ([business continuity](business-continuity.md)).
- Record the results in the review log.
