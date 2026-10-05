# Abuse protection

This document is written in ASD-STE100 Simplified Technical English.

This document gives each layer of abuse protection, its default value and the result when a sender is over the limit. Each layer is a parameter in `agent/config.py`, except the Postfix limits.

## 1. Keys and privacy

The limits count senders, domains and client IPs. Redis never stores these values in clear text. Each key is an HMAC of the normalised value (`agent/limits.py`).

| Value | Normalisation |
|---|---|
| Sender address | Lower case, without the `+tag`, and without dots in a Gmail local part. `googlemail.com` becomes `gmail.com`. The domain becomes its A-label. |
| Domain | The organizational domain from the Public Suffix List |
| Client IP | The IP in the `Received` header of our own MTA. An IPv6 address counts as its /64 network. |

For example, `Alice+1@Gmail.com`, `a.l.i.c.e+2@gmail.com` and `alice@googlemail.com` are 1 sender.

## 2. Layers

The layers are in the order that a message meets them.

| # | Layer | Where | Keyed on | Default | Result when over the limit |
|---|---|---|---|---|---|
| 1 | Postfix client limits | Host MTA | Client IP | 30 connections, 30 messages and 100 recipients each 60 s | Postfix refuses the client for a time |
| 2 | Inbound ceiling | Ingest, first | All mail | 120 messages each minute | The message stays unread. The next sweep (within 60 s) reads it. |
| 3 | Inbound size | Ingest | Message | 5,000,000 bytes | Only the headers are read |
| 4 | Pre-authentication, each IP | Ingest, before it stores the body and before DNS | Client IP | 30 each hour | Headers only. Rejected `preauth_rate_limited`. Marked read. No job, no reply. |
| 5 | Pre-authentication, each domain | Ingest | Organizational domain of the claimed From | 60 each hour | The same as layer 4 |
| 6 | Suppression list | Gate, first | Address or domain (HMAC) | DSAR and operator blocks | Rejected. No reply. |
| 7 | Automated mail | Gate | Headers | Always | Rejected. No reply. |
| 8 | Sender authentication | Gate | DMARC-aligned SPF or DKIM | Necessary | Rejected. No reply. |
| 9 | Each sender | Gate, after authentication | Normalised sender | 6 each hour, 20 each day | Rejected. At most 1 "slow down" reply each hour (each day for the daily cap), and at most 3 each day. |
| 10 | Each domain | Gate | Organizational domain | 30 each hour, 100 each day | The same as layer 9 |
| 11 | Global | Gate | All senders | 300 each hour, 1000 each day | Rejected. No reply. |
| 12 | Each thread | Gate | Thread and sender | 5 requests | Rejected. No reply. |
| 13 | In flight | Before the fetch | Normalised sender | 2 requests in `fetching` or `packaging` | The request waits (no attempt used) until its 2 h deadline, then 1 apology |
| 14 | LLM budget | Gate and summaries | UTC day | USD 2.00 | Triage uses only the rules. No new summaries. Documents and cached summaries still go out. 1 ERROR log line each day. |
| 15 | Portal visits | Each matter lookup, list or download batch | Provider and UTC day | UARB 400, OEB 2000, FERC 2000 | The request waits. The worker examines the budget again every 10 min. 1 "delayed" email for each request. 1 apology at the deadline. |
| 16 | Bytes for each sender | Package | Normalised sender and UTC day | 1,500,000,000 bytes | The agent sends what fits and tells what it left out |
| 17 | Each request | Package | Request | 10 documents, 600,000,000 bytes, 200,000,000 bytes for each file | The same as layer 16 |
| 18 | Viewer | Each route except `/health` and `/health/deep` | Client IP (IPv6: /64) | Refer to section 3 | HTTP 429 with `Retry-After` and `Cache-Control: no-store` |
| 19 | Disk guard | Ingest and downloads | Free space under `data/` | 5,000,000,000 bytes | Mail stays unread. Downloads stop with a retryable error. |

Layers 4, 5, 9, 10 and 11 count the hits in the last hour or in the last 24 h. Each window is a Redis sorted set. The member of a request is its request id, and the member of a message is its SHA-256 or its IMAP uid. For this reason, a retry never counts 2 times.

## 3. Viewer limits

The viewer uses a token bucket for each client IP and each route class. A bucket holds its full limit and refills evenly over its window.

| Route class | Default |
|---|---|
| `/r/{token}.json` | 60 each minute |
| `/r/{token}` | 30 each minute |
| `/files/*` | 30 each minute and 200 each hour |
| `/c/*` | 60 each minute |
| All other routes | 120 each minute |

If Redis does not answer within 0.25 s, the viewer serves pages without limits for 5 s. It writes a warning, at most 1 each minute.

## 4. What the requester sees

| Situation | Email |
|---|---|
| Over a pre-authentication limit, global limit or thread cap | None |
| Over the sender or domain limit | At most 1 "slow down" reply each hour, and at most 3 each day |
| Over the in-flight cap | None. The progress page tells the requester to wait for the earlier requests. |
| A portal budget is spent | 1 "delayed" email. The progress page shows the delay. |
| The LLM budget is spent | The reply has no new summary |
| Over the byte allowance | The reply lists the documents that the agent left out |

## 5. Metrics and audit

- Each decision increments `limiter_decisions{limiter, decision}`. The decision is `allowed`, `limited` or `deferred`.
- The gauges `budget_used{budget}` and `budget_limit{budget}` show the daily budgets.
- `budget_exhausted{budget}` increments 1 time for each budget and day.
- Each rejection or wait writes a `rate_limited` event. The event has only the key type, never the address.

## 6. Load test

`scripts/load_limits.py` sends bursts against the integration harness: the real pipeline, ingest and web app on the compose Postgres and Redis. The portal, SMTP, DNS and the LLM are fakes. The script sends no email. It exits with code 1 if a burst gets through more than its limit allows.

The README gives this output (default limits):

| Burst | Sent | Allowed | Limited | Notes |
|---|---|---|---|---|
| 50 emails from 1 sender (+tags, dots, googlemail, 50 IPs) | 50 | 6 | 44 | 1 "slow down" reply |
| 200 emails from 1 client IP and 200 domains (1 IMAP sweep) | 200 | 30 | 90 | 80 stayed unread for the next sweep |
| 5 requests at the same time from 1 sender | 5 | 3 | 2 | At most 2 in flight |
| 1000 progress polls from 1 IP | 1000 | 62 | 938 | 60, plus the refill over 2.3 s |

## 7. Procedure: change a limit

CAUTION: A higher limit increases the load on the regulator portals and the model spend. Do not increase `PORTAL_DAILY_VISITS` without a reason.

1. Open `.env` on the host.
2. Add or change the environment variable, for example `RATE_PER_SENDER_HOUR=10`.
3. Write the per-service env files.

   ```bash
   deploy/split-env.sh
   ```

4. Restart the service that reads the parameter (`ingest` for `PREAUTH_*` and `INBOUND_*`, `web` for `WEB_RATE_*`, else `worker`).

   ```bash
   docker compose up -d --no-deps worker
   ```

5. Examine the start log of the service.

   ```bash
   docker compose logs --tail 50 worker
   ```

   Expected result: the service starts without errors.

NOTE: The daily budgets read their limit at each call. A new budget limit applies when the service restarts with the new environment.

To throttle a flood during an incident, use the containment table in `docs/policies/incident-response.md`.
