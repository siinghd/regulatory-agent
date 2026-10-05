# Runbook: credential leak

No alert links here. Start this runbook when a secret is in a location where it must not be. Examples are a commit, a log, a paste, a screenshot, a secret that gitleaks or Trivy found, or a notice from a vendor. Also start it when a person with access leaves.

Incident severity: SEV2. SEV1 if there is evidence that a person used the credential. Parent document: [Incident response](../policies/incident-response.md).

## 1. Contain first, then investigate

Rotate the credential before you do other work. A rotation never needs the old value.

| Credential | How to rotate | How to look for misuse |
|---|---|---|
| `OPENROUTER_API_KEY` | In the OpenRouter dashboard, revoke the key. Make a new key with a credit limit. Put the new key in `.env`. | Examine the OpenRouter activity and usage for unknown calls or spend. |
| `TYPESAFE_API_KEY` | In the TypeSafe account, revoke the key. Make a new key. Put the new key in `.env`. | Examine the TypeSafe usage for calls that the audit trail (`llm.call` events) does not show. |
| `AGENT_MAIL_PASSWORD` | Change the mailbox password in Dovecot (`doveadm pw`, then the passdb). Put the new password in `.env`. | Run `journalctl -u postfix -u dovecot --since -7d`. Look for logins and submissions from unknown IPs, and for mail that is not in the `events` table. |
| Postgres role DSNs: `DATABASE_URL`, `WEB_DATABASE_URL`, `MIGRATION_DATABASE_URL`, `RETENTION_DATABASE_URL`, `BACKUP_DATABASE_URL` | Delete the line from `.env`. Run `deploy/db-cutover.sh`. The script makes a new password for each missing DSN and applies the role again. | Run `docker compose logs postgres \| grep 'connection authorized: user=<role>'`. Look for unexpected hosts. |
| `POSTGRES_PASSWORD` (the superuser) | Run `deploy/db-cutover.sh --rotate-superuser`. | `pg_hba.conf` refuses the superuser over TCP, so remote use is not possible. Examine the access to the host. |
| `REDIS_PASSWORD`, `REDIS_ADMIN_PASSWORD` | Delete the lines from `.env`. Run `deploy/redis-cutover.sh`. | Examine `ACL LOG` (as `admin`) and look for unexpected keys. |
| `AUDIT_HMAC_KEY` | Make a new value with `python3 deploy/lib/envtool.py gen`. Read the WARNING below this table first. | Not applicable. The key makes pseudonyms. It gives no access. |
| `DATA_ENCRYPTION_KEY` | Do not replace it without more steps. The sealed values (outbox bodies, drop links, delete tokens) then become unreadable. A rotation needs a re-seal step and key versions in `agent/crypto.py` (Open). Until then, let the affected drop links expire and treat queued outbox rows as lost. | Find who had read access to `.env` or `data/keys/`. |
| `age` backup recipient (public key) | The public key is not secret. The private key is offline. If the private key leaks, make a new key pair. Set `BACKUP_AGE_RECIPIENT`. Encrypt the old backups again or delete them. | Find who had the offline copy. |
| SSH key of `deploy` | Remove the key from `~/.ssh/authorized_keys` on the host and on the egress VM. Add the new key. | Run `journalctl -u ssh --since -30d \| grep Accepted`. |
| Cloudflare origin key (`/etc/caddy/certs/origin.key`) | In Cloudflare, go to SSL/TLS, then Origin Server. Revoke the certificate and make a new certificate. Replace the files. Run `systemctl reload caddy`. | Examine the Cloudflare audit log. |
| Cloudflare, Hetzner, Azure, GitHub or R2 account credentials | Change the password. Revoke the sessions and the tokens. Make sure that MFA is on. | Examine the audit log of each vendor. |
| Drop delete tokens or links | Delete the upload in drop. The token is in the sealed delivery record. You can also run `ragent revoke <request_id>`. | Examine the drop access logs. |

WARNING: A new `AUDIT_HMAC_KEY` changes all pseudonyms. The suppression list keeps blocked and erased addresses only as HMACs. After the rotation, the gate does not recognise these addresses, and it processes their mail again. Audit lookups by address (`ragent audit --email`) also do not find older events.

After a rotation of `AUDIT_HMAC_KEY`, block the known addresses again with `ragent block`. Record that the erased addresses are no longer on the suppression list.

CAUTION: If `DATA_ENCRYPTION_KEY` is empty, the at-rest key comes from `AUDIT_HMAC_KEY`. A rotation of `AUDIT_HMAC_KEY` then also makes the sealed values unreadable. Make sure that `DATA_ENCRYPTION_KEY` has a value before you rotate `AUDIT_HMAC_KEY`.

After each change to `.env`, do these steps:

1. Write the env files for each service again.

   ```bash
   deploy/split-env.sh
   ```

2. Start the services again. Compose makes new containers only for the services with a changed environment.

   ```bash
   docker compose up -d
   ```

3. Prove the database roles.

   ```bash
   deploy/verify-db-roles.sh
   ```

   Expected result: `ALL ROLE CHECKS PASSED`.

4. Prove the Redis ACL.

   ```bash
   make validate-redis
   ```

   Expected result: `ALL REDIS ACL CHECKS PASSED`.

5. Run the technical control checks.

   ```bash
   deploy/compliance_check.sh
   ```

   Expected result: no `FAIL` line.

## 2. If the secret is in Git

1. Rotate the secret first (section 1). A change to the history does not remove a secret that is already pushed.
2. Remove the secret from the history only if the repository is private and has no forks. Use `git filter-repo --replace-text`.
3. Force-push the changed history.
4. Ask GitHub support to delete the cached views.
5. Add a gitleaks allowlist entry only for a test fixture. Never add an entry for a real secret.

## 3. Close the incident

1. Record these facts in the incident note: what leaked, where, for how long and who had access to it. Also record the rotation time and the result of the misuse check.
2. If the credential gives access to personal data, do the notification assessment of the incident plan. This applies to the database roles, the mailbox and `DATA_ENCRYPTION_KEY`.
3. Find why the secret was in that location. Correct the cause. Examples are a log line, a script that put the secret in `argv`, or a missing `.gitignore` rule.
