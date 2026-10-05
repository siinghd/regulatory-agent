# Data Subject Request (DSAR) Procedure

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: P5.1, P5.2, P4.3, P8.1. Public commitment: [privacy notice](privacy-notice.md). Review this procedure 1 time each year.

[Operations, section 8](../guide/operations.md#8-data-subject-requests-dsar) gives the commands with their expected results.

## 1. Intake

- The channel is **privacy@hsingh.app**. Some privacy requests arrive at `agent@hsingh.app` or `security@hsingh.app`. Forward them to privacy@hsingh.app.
- A sender can also send an email with **DELETE MY DATA** to `agent@hsingh.app`. The agent processes this request automatically (section 4.3).
- Record each request in the DSAR register (`/home/deploy/privacy/dsar-register.md`, mode 600). Record these items:
  - the date of receipt and the address of the requester;
  - the type (access, correction, deletion or objection);
  - the due date (the date of receipt plus 30 days);
  - the status and the date of closure.
- Do not put copies of the data in the register.

## 2. Verify the identity

We know people only by their email address. For this reason, the proof of identity is control of that address. Accept 1 of these 2 proofs:

- The request comes **from** the address that it is about, with a DMARC-aligned SPF or DKIM pass. The agent applies this check automatically to a "DELETE MY DATA" email. The local MTA adds no `Authentication-Results` header. For an email to privacy@hsingh.app, do the check by hand, or use the next proof.
- We send a one-time code to that address, and the requester returns the code to us.

WARNING: Never act on a request for the address of a different person without this proof. Send data only to the verified address.

## 3. Find the data

1. Export all data that the system holds about the address.

   ```bash
   docker compose exec worker ragent dsar export <address> --out /data/tmp/dsar-export.json
   ```

   Expected result: `wrote /data/tmp/dsar-export.json: N requests, N raw emails, N events`. The file is `data/tmp/dsar-export.json` on the host, with mode 600.

The export contains these items:

- the request rows, found by the address or by its HMAC (`requests.from_h`), so it also finds pseudonymised rows;
- the raw emails that the system still keeps (30 days after the request settles);
- the metadata of the emails that the agent sent (the bodies are sealed, and the export does not contain them);
- the audit events, found by the HMAC of the address (`events.subject_h`).

These data are not in the export:

- **Mailbox:** messages from the address that are still in the IMAP folders of `agent@hsingh.app`. Ingest expunges them 7 days after the request settles.
- **Access logs:** you cannot search them by email address. Search them only if the requester gives an IP address and a time.
- **Backups:** do not restore a backup for a DSAR. The backups expire within 14 days (35 days off-site). Tell this to the requester.
- **Vendors:** OpenRouter routes only to zero-data-retention endpoints, so it keeps nothing. TypeSafe is not zero-data-retention on our plan. It can keep the email text for a period under its own terms. Tell this to the requester.

## 4. Respond

### 4.1 Access

1. Write a plain summary of the data.
2. Send the summary and the export to the verified address. Use a drop link if possible (encrypted, expiry after 7 days).
3. Delete the export file.

   ```bash
   shred -u data/tmp/dsar-export.json
   ```

WARNING: The export contains personal data and raw emails. Send it only to the verified data subject.

### 4.2 Correction

The only data that a requester can correct is the data that they sent. Usually, a note in the register is sufficient.

### 4.3 Deletion or objection

WARNING: This erases all data about the address and revokes its download links. You cannot undo it.

1. If the data subject asked for a copy, do section 4.1 first.
2. Erase the address.

   ```bash
   docker compose --profile ops run --rm retention dsar delete <address> --yes
   ```

   Expected result: a JSON object with the counts. The exit code is 0.

3. If the exit code is 1, read `links_not_revoked` in the output. Revoke those links again later with `ragent revoke <request id>`.
4. Delete the messages from the address in the mailbox by hand, if they are still there.

The command does these actions:

- It adds the HMAC of the address to the suppression list (`reason = 'dsar_delete'`, `erase = true`). The gate then drops later mail from the address before it does any other work.
- It revokes the drop links of the address.
- It deletes the request rows (citations and outbox rows are deleted with them) and the raw emails.
- Each daily purge erases again the data of each address with `erase` set.

A "DELETE MY DATA" email has the same result. The agent adds the address to the suppression list and sends 1 confirmation. The next daily purge erases the data after the confirmation is sent.

The audit events about the person stay for their retention of 400 days. They contain only the pseudonym. Tell this to the requester in the reply.

### 4.4 Exceptions and closure

- Explain each exception in the reply, for example a legal hold or an active abuse investigation about the address. The code has no hold list. For a hold, refer to the [retention policy, section 4](data-retention.md#4-exceptions).
- Close the register entry with the date and the actions that you did.
- The deadline is 30 days. You can extend it 1 time by 30 days, if you send the reason to the requester.
