# Limitations and disclaimers

This document is written in ASD-STE100 Simplified Technical English.

Read this document before you use the agent, its numbers or its documents for a decision.

## 1. Status of the system

WARNING: The Regulatory Document Agent is an MVP for evaluation. It is not a production service. It has no service level agreement (SLA), no support hours and no guaranteed availability.

- 1 operator builds and runs the system. No second person reviews the changes.
- The system runs on 1 VM. Postgres, Redis, the mail server and the UARB egress tunnel are single points of failure.
- The measured numbers in this guide come from single runs on this host. They are not benchmarks.
- On 2026-10-05, a review made the policies, the runbooks and the SOC 2 documents agree with the code. If a document and the code do not agree, the code is the current state.

## 2. Content of the replies

WARNING: Summaries are machine-written. A model writes each summary and each key point. Code makes sure that each quote is on its cited page, but a claim can still say more or less than its quote. Examine the cited source before you use a summary for a decision. A summary is not legal advice.

- In the output eval, 92% to 96% of the kept claims were "supported" and 0 were "not supported". 4% to 8% were "partially supported". [Quality and evals](quality-and-evals.md) gives the method.
- The agent delivers scanned PDFs without a text layer, but does not summarise or cite them. OCR is not implemented.
- The agent delivers spreadsheets, recordings, TXT files and old DOC files, but does not summarise or cite them.
- For a long document, the summary reads only the first 400 pages.
- The reply tells on how many of the documents the summary is based.

## 3. What the agent can fetch

- At most 10 documents for each request, and at most 600,000,000 bytes.
- Only whole categories. A request for 1 specific document (for example "exhibit H-1") gets a question.
- Only rows that the portal marks "Public". Rows with an unknown label are never sent (fail closed). For this reason, the agent possibly does not send some public files.
- FERC: only the primary file of each document (the first PDF, else the first DOCX). Hydro project dockets (for example `P-2114`) are not supported.
- 3 regulators only: Nova Scotia UARB, the Ontario Energy Board and the US FERC.

## 4. Regulator portals

CAUTION: Regulator portals can change without notice. A change can stop a provider until a developer changes the code. The agent then retries and sends an apology. It does not send wrong data, but it can send no data.

- The FERC eLibrary API is an internal API without public documentation.
- The UARB portal answers only IP addresses in North America.
- The UARB portal shares its prepared download state across sessions from 1 client IP. The agent compares each file with the requested id and serialises the download step.

## 5. Single egress for UARB

CAUTION: All UARB traffic uses 1 SSH SOCKS tunnel to 1 Azure VM in Canada. If the tunnel or the VM stops, all UARB requests fail after their retries.

- 1 lock for the egress IP serialises the UARB download step across all workers.
- No fallback egress exists. The code has no fallback proxy and no fallback proxy parameter (ADR-023 in the [decisions log](decisions-log.md)).
- The upgrade path is an egress pool: a second Canadian VM, or a residential proxy with sticky sessions, each with its own lock. This is not implemented (ADR-022).

## 6. Email authentication

- A sender must pass DMARC alignment with SPF or DKIM. Mail from a domain without SPF or DKIM gets no reply.
- ARC is not evaluated. The agent treats mail through a mailing list that breaks both SPF and DKIM as unauthenticated. That mail gets no reply.
- The agent replies only to the authenticated sender. It never replies to a `Reply-To` address or to an address in the body.

## 7. Privacy and model vendors

WARNING: TypeSafe is not zero-data-retention on our plan. The agent sends the email subject and body (triage) and public document excerpts (citation checks) to TypeSafe. The owner accepted this for the MVP, and the privacy notice discloses it. Set `GATE_CLASSIFIER=llm` and `CITATION_CHECK=llm` to send TypeSafe nothing.

- OpenRouter calls go only to zero-data-retention endpoints.
- Data processing agreements (DPAs) with the vendors are not signed.
- Download links and progress links are capability tokens. A person who has the link can use it until it expires.

## 8. Security and compliance

NOTE: The technical SOC 2 controls are implemented. Examples are least-privilege roles, the Redis ACL, hardened containers and the audit trail. Others are retention, DSAR commands, backups, CI scans and the kill switch. The organisational controls are open.

Open organisational items:

- no auditor and no SOC 2 examination;
- no second reviewer for changes, access reviews and the risk register;
- no signed DPAs with Hetzner, Cloudflare, OpenRouter or TypeSafe;
- no background checks and no records of security courses (there is no staff).

Open technical items:

- The host is shared with other services of the operator. This is a SOC 2 gap.
- The disk is not encrypted at rest.
- The off-site backup copy is not configured. A loss of the host also loses the local backups.
- The app containers use the host network mode, so gVisor cannot isolate the worker.
- The alert receiver is `blackhole` by default. No alert reaches a person until the operator sets a receiver.
- The observability stack and the `/status` page were new on 2026-10-05. They have little history.

## 9. Evals

- The gate result of 100% is in-sample. The developer tuned the prompt, the rules and the thresholds on the same 190 emails. The held-out result is the honest estimate: DeepSeek 97.5% exact and 100% operator-correct on 40 emails. Jev alone, on its first held-out run: 92.5% exact, 95.0% operator-correct and 2 wrong fetches.
- The held-out set has only 40 emails. 1 email changes the result by 2.5 points.
- The output eval changed its judge model from Opus (baseline) to Sonnet (later runs). The before and after numbers are not strictly comparable.
- Each output eval case had 1 generator run. Run-to-run variance is not measured.
- The judge is 1 model. It is a reference, not the ground truth.
