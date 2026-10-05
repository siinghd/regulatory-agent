# Runbook: spoofing wave

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentPreauthRejectionSpike` | `warning` | For 15 min, the pre-authentication rejections are more than 3 times the 7-day hourly average. The count is for the last 1 h, and it is at least 10. |
| `RegagentUnauthenticatedSpike` | `warning` | For 15 min, the sender authentication verdicts other than `pass` are more than 3 times the 7-day hourly average. The count is for the last 1 h, and it is at least 10. |

Ingest counts the pre-authentication rejections (`limiter_decisions_total`, port 9711). The worker counts the authentication verdicts (`auth_verdicts_total`, port 9710).

Other signals: a surge of inbound mail from addresses that the senders do not control. A sudden increase of `rejected` requests with the reason `unauthenticated:*`. The goal of such a wave is to make the agent send mail to third parties (reflection or backscatter).

Incident severity: SEV3 while the gate holds. SEV1 if a reply went to a spoofed address. Parent document: [Incident response](../policies/incident-response.md).

## 1. Make sure that the gate holds

The agent answers only senders with a DMARC-aligned SPF or DKIM pass. The agent calculates this from the `Received` header of our own MTA.

1. Open a database session as the superuser.

   ```bash
   docker compose exec -T postgres psql -U agent -d agent
   ```

2. Count the reject reasons of the last 24 h.

   ```sql
   SELECT reject_reason, count(*) FROM requests
   WHERE received_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC;
   ```

3. Look for outbound mail for a request that was not authenticated.

   ```sql
   SELECT r.id, r.from_addr, r.reject_reason, e.kind, e.at
   FROM requests r JOIN events e ON e.request_id = r.id
   WHERE e.kind LIKE 'sent:%' AND (r.auth->>'aligned_via') IS NULL
     AND r.received_at > now() - interval '7 days';
   ```

   Expected result: 0 rows.

4. Make sure that the MTA does not send bounces to forged senders (backscatter). Examine the mail queue.

   ```bash
   postqueue -p
   ```

5. Count the bounces of the last 24 h.

   ```bash
   journalctl -u postfix --since -24h | grep -c 'status=bounced'
   ```

## 2. Contain the wave

Do the steps in this sequence. Each step has a larger effect than the step before it.

1. Decrease the rate limits in `.env`. The gate uses `RATE_PER_DOMAIN_HOUR` and `RATE_GLOBAL_HOUR`. Ingest uses `PREAUTH_PER_IP_HOUR` and `PREAUTH_PER_DOMAIN_HOUR`.
2. Write the env files for each service again.

   ```bash
   deploy/split-env.sh
   ```

3. Start the worker and ingest with the new environment.

   ```bash
   docker compose up -d worker ingest
   ```

4. Block an address or a domain that the wave uses. The gate then drops its mail without a reply.

   ```bash
   docker compose exec worker ragent block @<domain> --reason "<why>"
   ```

5. If the wave continues, use the allowlist mode. Set `SENDER_AUTH_MODE=allowlist` and `SENDER_ALLOWLIST=["user@example.org","@example.com"]` with the known users or domains. Then do steps 2 and 3 again.
6. If the wave continues, stop the intake. Mail waits in IMAP. No mail is lost.

   ```bash
   docker compose stop ingest
   ```

7. If the wave continues, refuse the sources at the MTA. Use a Postfix `check_client_access` map in `smtpd_client_restrictions` with the bad IPs or networks. You can also use `ufw deny from <net> to any port 25`.

## 3. If a reply went to a spoofed address

1. Find each affected address. Join the `events` rows (`sent:ack`, `sent:reply`, `sent:notice`) to `requests`.
2. Send nothing more to these addresses. The content was public regulator documents and an acknowledgement, but the recipient did not ask for it.
3. Record the incident. Assess the risk of abuse complaints and of blocklists.
4. Find the check that let the message through, and why. The `auth` JSON of the request row has `verdict`, `spf`, `dkim_domains`, `aligned_via` and `client_ip`. The agent took `client_ip` from the `Received` header of our MTA.
5. Correct the cause through an emergency change.
6. Add the message to the adversarial corpus (`tests/adversarial/emails/`).
7. Keep the allowlist mode until you deploy the correction.

## 4. After the incident

1. Examine the reputation of our domain. Read the DMARC aggregate reports for `hsingh.app`. Examine the public blocklists for the IP of the host.
2. Make sure that our own SPF, DKIM and DMARC records did not change.
3. Remove the allowlist mode and the lower rate limits through a normal change.
