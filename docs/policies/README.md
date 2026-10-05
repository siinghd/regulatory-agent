# Security and privacy policies

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

These policies apply to the Regulatory Document Agent. The scope is the email agent at `agent@hsingh.app`, the viewer at `uarb.hsingh.app`, the host and the services that the agent uses. Each policy tells what the operator does now and where the evidence is. Each policy also tells which controls are open.

NOTE: The agent is an MVP for evaluation. It has no service level agreement (SLA). When a policy and the code do not agree, the code is the current state. [Limitations and disclaimers](../guide/limitations-and-disclaimers.md) gives the known gaps.

| Policy | Contents |
|---|---|
| [Information Security Policy](information-security.md) | Objectives, roles, principles, risk assessment and the policy set |
| [Access Control Policy](access-control.md) | Identities, least privilege, break-glass access and access reviews |
| [Change Management Policy](change-management.md) | Normal, standard and emergency changes, and the substitute controls for 1 operator |
| [Vulnerability and Patch Management Policy](vulnerability-management.md) | Sources, remediation times for each severity, and exceptions |
| [Logging and Monitoring Policy](logging-monitoring.md) | Log sources, data that the system never logs, monitors and alerts |
| [Incident Response Plan](incident-response.md) | Severities, phases, notification and the [runbooks](../runbooks/) |
| [Business Continuity and Disaster Recovery](business-continuity.md) | RTO 4 h and RPO 24 h, degraded modes, and the rebuild procedure |
| [Backup Policy](backup.md) | Scope, encryption, restore tests and retention |
| [Data Classification](data-classification.md) | Classes, the rules for each class, and the data inventory |
| [Data Retention and Disposal](data-retention.md) | The retention schedule and how the system applies it |
| [Encryption and Key Management](encryption.md) | Encryption in transit and at rest, the key inventory and key rotation |
| [Vendor Management](vendor-management.md) and [Vendor Register](vendor-register.md) | Due diligence and subprocessors |
| [Privacy Notice](privacy-notice.md) and [DSAR Procedure](dsar-procedure.md) | The notice for the public, and the procedure for data subject requests |
| [AI and LLM Use](ai-use.md) | Models in the product and in development |
| [Acceptable Use](acceptable-use.md) | Rules for operators and for users of the service |
| [Asset Inventory](asset-inventory.md) | Hosts, services, data stores, domains and accounts |

SOC 2 references: [control matrix](../soc2/control-matrix.md) and [system description](../soc2/system-description.md).

## Governance

- **Owner and approver:** the system operator (GitHub `@siinghd`). Today, the operator has all roles: security officer, administrator, developer and incident commander. This is the largest organisational gap. It affects separation of duties, independent review and access reviews. Each policy names the substitute control for the gap. SOC 2 calls this a "compensating control".
- **Review:** The operator reviews each policy at least 1 time each year. The operator also reviews a policy when the system changes in a way that the policy describes. Examples are a new vendor, a new data store, or a new regulator with a different data flow. A review is a pull request (PR) that updates the "Last reviewed" date. Thus, the PR history is the review log.
- **Exceptions:** An exception is a written entry with the reason, the substitute control, the person who accepted it and an expiry date. The entry is in the PR that needs it, or in `.trivyignore` for scanner findings. The expiry is not more than 90 days after the entry.
- **Status words in these documents:** *Implemented* (in place, with evidence), *Partial* (in place, but not complete) and *Open* (not done, with the owner and the next step).
