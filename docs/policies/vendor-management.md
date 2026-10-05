# Vendor Management Policy

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: CC9.2, CC3.2, P6.1, C1.1. Register: [vendor-register.md](vendor-register.md). Review this policy 1 time each year.

## 1. Scope

This policy applies to each third party that hosts, carries or processes the system or its data, or that can get access to them. It also applies to the suppliers of software that runs in the system (images and packages). The regulator portals are data *sources*, not processors. The register lists them only to be complete.

## 2. Tiers

| Tier | Definition | Due diligence before use | Review |
|---|---|---|---|
| 1 | The vendor processes Confidential data or hosts the system | A security report (SOC 2 Type II or ISO 27001). A DPA with a list of subprocessors and a transfer mechanism. The data location, the breach notification terms and the retention terms. MFA on our account. | Each year, and after each incident or change of terms |
| 2 | The vendor sees only Public data, ciphertext or metadata | A security page or a certification. MFA on our account. | Each year |
| 3 | Software supply chain (registries, base images, packages) | Pins by digest or hash, vulnerability scans, and provenance where it is available | Continuously, through CI |

## 3. Process

1. Before you add a vendor, decide its tier from the data that it will see.
2. Do the due diligence for that tier.
3. Add a row to the register in the same PR that adds the vendor.
4. If the vendor gets personal data, update the [privacy notice](privacy-notice.md).
5. Send each vendor only the data that it needs. Examples:
   - The models get the email text that is necessary to classify a request. They never get headers, attachments or the data of other requesters.
   - R2 will get only backups that age encrypted.
6. Do the annual review together with the access review in Q1:
   - Read the security report and the terms again.
   - Examine the breach reports and the changes of subprocessors.
   - Confirm MFA.
   - Update the "Reviewed" date of the register.
7. When you stop the use of a vendor:
   - Revoke the keys and accounts.
   - Ask the vendor to delete our data, and get a written confirmation.
   - Remove the row from the register. The PR history keeps the old row.

## 4. Complementary controls

Some controls are the responsibility of a vendor. Examples are the physical security of Hetzner and the TLS termination of Cloudflare. The register lists these controls. The [system description](../soc2/system-description.md) shows them as complementary subservice organisation controls.
