# Decisions log

This log has 1 short record for each design decision, in the form of an architecture decision record (ADR). The dates are UTC. They come from the commit times and the file times in the repository. [DESIGN.md](../../DESIGN.md) gives the decisions as 1 table.

Status values: **Accepted** (in the code), **Accepted, done** (a change that is complete), **Accepted, not done** (agreed, work open), **Proposed** (not agreed or not started).

---

### ADR-001: Deterministic browser automation for UARB

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The UARB portal is a FileMaker WebDirect application. It has no API. The workflow is the same for each request.
- **Decision:** Use Playwright with fixed code. No model controls the browser.
- **Alternatives:** An agentic browser or computer use.
- **Consequences:** The scraper is fast and testable. Text on a page cannot steer it. A layout change on the portal needs a code change.

### ADR-002: Enter key for the matter search, and "not found" only after a second session

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** A click on Search immediately after the typed number searched for an empty value. The portal showed "No Records Found" for real matters (2 of 2 runs).
- **Decision:** Press Enter to commit and submit. Believe "not found" only if it occurs again in a fresh session.
- **Alternatives:** A fixed wait before the click.
- **Consequences:** No false "not found" in the tests. A real "not found" takes 1 more portal session.

### ADR-003: Compare each UARB download with the requested id, and serialise the download step

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** GO GET IT acts on the active record. The portal shares prepared files across guest sessions from 1 client IP. Session A got the files of sessions B and C under the correct names.
- **Decision:** Compare the served file name with the requested id. Discard a wrong file and retry. Hold a Redis lock for each egress IP from the click until the correct file starts.
- **Alternatives:** Trust the portal. Use only 1 session.
- **Consequences:** A wrong file never ships. The lock holds only for approximately 0.7 s to 1.8 s for each file, so transfers stay parallel.

### ADR-004: Providers behind a small interface, with categories that each provider defines

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** Each regulator groups its documents in a different way: 5 tabs (UARB), 9 categories from many document types (OEB), 8 categories from document classes (FERC).
- **Decision:** 1 provider for each regulator. Each provider declares its matter format and its categories.
- **Alternatives:** 1 scraper with fixed categories for all regulators.
- **Consequences:** OEB and FERC use the same pipeline as UARB. [Providers](providers.md) has the procedure to add a provider.

### ADR-005: JSON APIs over plain HTTP for OEB and FERC

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The OEB WebDrawer and the FERC eLibrary front end use JSON APIs.
- **Decision:** Use httpx with no redirects, a status-code classification and a canary search before "not found".
- **Alternatives:** A browser for all portals.
- **Consequences:** Faster requests and structured data. The FERC API has no public documentation and can change.

### ADR-006: Postgres state machine and an arq queue on Redis

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** Each request has a few steps, and each step must survive a crash.
- **Decision:** 1 row for each request with compare-and-set transitions and an event log. arq on Redis runs the jobs. The job id is the request id.
- **Alternatives:** Temporal, Celery.
- **Consequences:** 2 dependencies. Each step is idempotent, so a later move to Temporal is possible.

### ADR-007: Attempts in Postgres, a 2 h deadline, and parked tries

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The arq try counter starts again at 1 when the sweeper puts a job back on the queue.
- **Decision:** Count attempts in Postgres (at most 8). Stop at 2 h after receipt. A try that waits for a lock, a breaker or a limit refunds its attempt.
- **Alternatives:** The retry counter of the queue.
- **Consequences:** The count of attempts is correct after any crash. An authenticated request that cannot succeed gets 1 apology approximately 2 h after receipt.

### ADR-008: Outbox with fixed Message-IDs

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** A crash between the SMTP send and the database commit can send a second, different email.
- **Decision:** Render each email and store it in the same transaction as the state change. The Message-ID comes from the request id and the kind. Mark the reply as sent in the same transaction as the final state.
- **Alternatives:** Send from the job directly.
- **Consequences:** Delivery is "at least once" with the same Message-ID. SMTP 5xx ends the request `failed`. SMTP 552 sends the documents again as a link.

### ADR-009: Circuit breakers in Redis for each dependency

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** When a portal or a vendor is down, all retries fail and use attempts.
- **Decision:** 1 breaker for each dependency in Redis: 5 failures open it for 60 s, then up to 600 s. Only availability failures count. A request parks while the breaker is open.
- **Alternatives:** No breakers. Breakers in each process.
- **Consequences:** All workers share 1 view. A long outage does not use attempts. If Redis is down, the breakers let calls through.

### ADR-010: Content-addressed file store and a summary cache keyed by document versions

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The same PDF can occur in many matters. Repeat requests are common.
- **Decision:** Store files by SHA-256. Key the summary cache by the summary version, the matter and the SHA-256 values of the files.
- **Alternatives:** 1 folder for each request. A summary cache keyed by the matter.
- **Consequences:** A repeat request takes approximately 3 s and makes no model call. A changed document gives a new summary.

### ADR-011: End-to-end encrypted drop links

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** A ZIP can be larger than the mail size limit of 10,240,000 bytes.
- **Decision:** Upload the ZIP to drop in AES-256-GCM chunks. Put the key only in the URL fragment. Attach the ZIP only if drop fails and the ZIP is not larger than 7,000,000 bytes.
- **Alternatives:** Always attach. A plain download server.
- **Consequences:** The file server never sees the plaintext. Links expire after 7 days or 25 downloads.

### ADR-012: The agent calculates SPF, DKIM and DMARC alignment itself

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The local MTA accepts mail without these checks and adds no Authentication-Results header.
- **Decision:** Use the client IP in the `Received` header of our own MTA. Accept only a DMARC-aligned SPF or DKIM pass. DKIM must sign From and Subject, must not use `l=`, and must not be older than 3 days.
- **Alternatives:** Trust the From header. Trust an upstream header.
- **Consequences:** Spoofed mail gets no reply. Mail from domains without SPF or DKIM cannot use the agent.

### ADR-013: Summaries must quote the page

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** A model can write claims that the documents do not support.
- **Decision:** Each claim has a document, a page and a quote. Code finds the quote in the extracted text and removes claims and sentences with figures that the sources do not contain.
- **Alternatives:** A free-form summary.
- **Consequences:** Each key point links to its passage. The agent cannot cite scanned PDFs.

### ADR-014: Least-privilege database roles, Redis ACL and JSON jobs

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** At first, all processes connected as the Postgres superuser, and Redis had no password.
- **Decision:** 1 role for each process. Postgres refuses the superuser over TCP. Redis has an ACL with the default user off. Jobs are JSON, not pickle.
- **Alternatives:** 1 shared superuser connection.
- **Consequences:** A compromised process gets less access. Each new Redis command in the code needs an ACL change.

### ADR-015: Append-only audit trail with HMAC subject ids and a hash chain

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The audit trail must prove what the system did, but it must not keep addresses longer than necessary.
- **Decision:** A trigger refuses UPDATE and DELETE on `events`. Events name people only by an HMAC. Daily exports form a SHA-256 hash chain.
- **Alternatives:** Plain application logs.
- **Consequences:** Events stay valid after pseudonymisation. A change to an exported day is detectable.

### ADR-016: Layers of abuse protection with HMAC keys

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** A public email address attracts floods, and each request costs portal visits and model spend.
- **Decision:** Limits before authentication, after authentication, in flight, and as daily budgets. Keys are HMACs of normalised values.
- **Alternatives:** 1 global rate limit.
- **Consequences:** [Abuse protection](abuse-protection.md) lists each layer. A burst from 1 sender or 1 IP gets through only up to its limit.

### ADR-017: TypeSafe Jev for triage, with escalation to the LLM

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The LLM gate had a p95 latency of 2.90 s. Its output is JSON that code must validate.
- **Decision:** Rules first. Then 1 Jev request with typed questions. If a confidence gate fires or Jev does not answer, the LLM decides.
- **Alternatives:** The LLM gate alone. Jev alone with a question to the sender when it is not sure.
- **Consequences:** p95 0.35 s in-sample. 0 wrong fetches in-sample and held-out. Email text goes to TypeSafe (ADR-020).

### ADR-018: Pin the Jev model version

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The alias `jev-latest` changes when TypeSafe releases a new version. The evals tuned the thresholds on `jev-1.13.0`.
- **Decision:** Set `typesafe_model` to `jev-1.13.0`.
- **Alternatives:** Follow the alias.
- **Consequences:** A new Jev version is a change that needs the gate eval and the citation check eval again.

### ADR-019: Jev for citation support checks

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The LLM check had a precision of 67% and a recall of 75% on 60 real claims, with a p95 of 2.48 s.
- **Decision:** 1 Jev request for each claim, all at the same time. Keep a claim only if P(supported) is 0.5 or more. If Jev fails, the LLM check decides.
- **Alternatives:** The LLM check alone.
- **Consequences:** Precision 100% and recall 88% on the same claims, p95 307 ms. Public document excerpts go to TypeSafe.

### ADR-020: Accept TypeSafe without zero data retention for the MVP

- **Date:** 2026-10-05. **Status:** Accepted.
- **Context:** TypeSafe offers zero data retention only on enterprise plans. Our plan is not an enterprise plan.
- **Decision:** The owner accepts this for the MVP. The privacy notice and the vendor register disclose it. Each worker start writes it to the log.
- **Alternatives:** `GATE_CLASSIFIER=llm` and `CITATION_CHECK=llm`, which send TypeSafe nothing.
- **Consequences:** Email subjects and bodies and public document excerpts go to a vendor that can keep them. A DPA and a review of the vendor are open.

### ADR-021: 1 Canadian egress for UARB through an SSH SOCKS tunnel

- **Date:** 2026-10-04. **Status:** Accepted.
- **Context:** The UARB portal answers only IP addresses in North America.
- **Decision:** A systemd unit opens an SSH SOCKS tunnel to an Azure VM in Canada. Only the UARB provider uses it.
- **Alternatives:** A residential proxy. A pool of egress IPs.
- **Consequences:** The tunnel is a single point of failure for UARB, and 1 lock serialises the UARB download step.

### ADR-022: Egress pool for UARB

- **Date:** 2026-10-05. **Status:** Proposed.
- **Context:** ADR-021 has a single point of failure and serialised downloads.
- **Decision:** Add a second Canadian VM or a residential proxy with sticky sessions. Give each egress IP its own download lock.
- **Alternatives:** Keep 1 egress.
- **Consequences:** Not implemented.

### ADR-023: Remove the unused `UARB_FALLBACK_PROXY` parameter

- **Date:** 2026-10-05. **Status:** Accepted, done.
- **Context:** `uarb_fallback_proxy` was in `agent/config.py`, but no code used it. Some policy text called it a fallback.
- **Decision:** Remove the parameter. Describe the egress pool (ADR-022) as the upgrade path.
- **Alternatives:** Implement the fallback.
- **Consequences:** `agent/config.py` has no fallback proxy parameter. The configuration and the documents do not suggest a fallback that does not exist.

### ADR-024: Prometheus, Alertmanager and Grafana with a public read-only view

- **Date:** 2026-10-05. **Status:** Accepted, not done.
- **Context:** The operator had no metrics history and no alerts.
- **Decision:** Run Prometheus, Alertmanager, Grafana and exporters on the loopback. Show Grafana read-only at `/grafana/` with a disclaimer. Add a public `/status` page with aggregate numbers only.
- **Alternatives:** A hosted monitor service.
- **Consequences:** [Observability](observability.md) describes the stack. The app exports all metrics that the rules use (`deploy/observability/METRICS_CONTRACT.md`). Alerts reach a person only after the operator sets a receiver.

### ADR-025: Split an email with more than 1 matter into requests

- **Date:** 2026-10-05. **Status:** Accepted, done.
- **Context:** A requester asked for 2 matters in 1 email. The agent fetched the first matter and asked the requester to send the second matter again.
- **Decision:** Make each further matter a request of its own, up to 3 matters in 1 email. Code pairs each matter with its category from the words next to the matter. The models do not pair them. Each split request counts against the rate limits.
- **Alternatives:** 1 request that fetches many matters into 1 ZIP and 1 reply. Ask the model to pair the matters and the categories.
- **Consequences:** The state machine, the retries, the single-flight locks and the outbox do not change, because each split request is a normal request. The requester gets 1 reply for each matter. When the pairing needs judgement, the agent does not guess: the reply names the matter.
