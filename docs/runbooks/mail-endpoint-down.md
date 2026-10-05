# Runbook: mail endpoint down

This document is written in ASD-STE100 Simplified Technical English.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentMailEndpointDown` | `page` | For 5 min, the probe `mail-imaps` (mail.hsingh.app:993, TLS and the first IMAP line) or `mail-submission` (mail.hsingh.app:587, EHLO and STARTTLS) fails. |

Incident severity: SEV2. Ingest reads mail through IMAP (Dovecot). Replies go out through submission (Postfix).

## 1. Confirm the fault from the host

1. Examine the mail services.

   ```bash
   systemctl status dovecot postfix@- --no-pager
   ```

2. Examine the IMAP endpoint.

   ```bash
   openssl s_client -connect mail.hsingh.app:993 -quiet </dev/null | head -2
   ```

   Expected result: a line that starts with `* OK`.

3. Examine the submission endpoint.

   ```bash
   openssl s_client -connect mail.hsingh.app:587 -starttls smtp -quiet </dev/null | head -2
   ```

4. Read the mail logs.

   ```bash
   sudo journalctl -u dovecot -u postfix@- --since -30min | tail -50
   ```

## 2. Repair

| Cause | Repair |
|---|---|
| A service stopped | Run `sudo systemctl restart dovecot` or `sudo systemctl restart postfix`. |
| The TLS handshake fails | The certificate expired or the service cannot read it. Refer to [TLS certificate expiry](tls-expiry.md). |
| The disk is full (Postfix queue, Dovecot index) | Refer to [Disk full](disk-full.md). |

## 3. After the repair

Inbound mail waits on the server. Ingest reads it when IMAP operates again.

The outbox tries again to send the replies that did not go out. A temporary SMTP error gives `deferred`, and the worker tries again later. A permanent SMTP error gives `undeliverable`, and the request ends `failed`.

1. Open the "Delivery & mail" dashboard.
2. Look for `undeliverable` in "Outbound email by kind and outcome".
