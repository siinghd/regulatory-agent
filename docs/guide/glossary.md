# Glossary

This glossary gives the terms and abbreviations of this guide, in alphabetical order.

| Term | Definition |
|---|---|
| A-label | The ASCII form of an internationalised domain name (IDNA). The agent replies to the A-label of the domain that it authenticated. |
| Acknowledgement (ack) | The first email to the requester. It confirms the request and gives the progress link. |
| ADR | Architecture decision record. 1 short record of 1 design decision ([Decisions log](decisions-log.md)). |
| age | The file encryption tool that encrypts the backups to an offline public key |
| arq | The Python job queue on Redis that runs the worker jobs |
| Attempt | 1 try of a request job that counts against `max_attempts`. A parked try does not count. |
| Audit trail | The append-only `events` table and its daily exports |
| Blob | A file in the content-addressed store, `data/blobs/`, named by its SHA-256 |
| Breaker | A circuit breaker. It stops calls to a dependency that does not answer, for a time. |
| Budget | A daily limit (UTC day): LLM spend, portal visits for each provider, or bytes for each sender |
| Canary | A known matter that must return documents. A provider searches it before it reports "not found". |
| CAS | Compare-and-set. A state change that occurs only if the current state is 1 of the expected states. |
| Category | A group of documents as a regulator shows it, for example the UARB tab "Other Documents" |
| Citation | 1 claim of a summary with its document, page and quote. The viewer shows it at `/c/<id>`. |
| Claim | 1 key point of a summary. Each claim must quote its source page. |
| Clarify | The state of a request when the agent asked the requester a question |
| DKIM | DomainKeys Identified Mail. A signature over the headers and the body, with a key in DNS. |
| DMARC | Domain-based Message Authentication, Reporting and Conformance. The policy of a domain, and the rule that SPF or DKIM must align with the From domain. |
| DPA | Data processing agreement with a vendor |
| drop | The end-to-end encrypted file service at `drop.hsingh.app` |
| DSAR | Data subject access request: a request to export or delete the data about a person |
| Egress | The network path from the worker to a portal. UARB uses a SOCKS tunnel to Canada. |
| eLibrary | The document system of the US FERC |
| Escalation | The step when the Jev gate is not sure and the LLM triage decides |
| Exhibit | A UARB document in the hearing record, with a number such as `H-1` |
| Fail closed | When the agent is not sure, it does not send, does not download, or asks a question |
| FERC | US Federal Energy Regulatory Commission |
| Gate | The checks of the worker that decide if and how the agent answers an email |
| GO GET IT | The UARB portal button that prepares a file for download |
| Held-out set | Eval cases that were not used to tune the prompts, the rules or the thresholds |
| HMAC | A keyed hash. The agent stores people and IPs only as HMACs in Redis and in the audit trail. |
| IDLE | The IMAP command that waits for new mail without a poll |
| In-flight slot | 1 of the 2 places that a sender can have in `fetching` or `packaging` at the same time |
| Ingest | The process that reads the mailbox and creates requests |
| Jev | The System One classifier of TypeSafe. It answers typed questions with probabilities. |
| Kill switch | `ragent pause`. All requests park and the outbox sends nothing. |
| Matter | The case identifier of a regulator: a UARB matter, an OEB case or a FERC docket |
| MTA | Mail transfer agent. Here, the Postfix server on the host. |
| MVP | Minimum viable product. The agent is an MVP for evaluation. |
| Noul | A yes-or-no question type of TypeSafe Jev |
| OEB | Ontario Energy Board |
| Operator | The person who runs the agent |
| Operator-correct | An eval metric: the action is correct, and a fetch gets the correct matter and category |
| Outbox | The `outbound` table and its delivery logic. The worker stores each email before the outbox sends it. |
| Park | To put a job back on the queue. The job does not use an attempt. |
| Portal visit | 1 matter lookup, 1 list or 1 download batch on a portal. It counts against the daily budget. |
| Progress page | The page `/r/<token>` that shows the state of 1 request |
| Provider | The adapter for 1 regulator portal |
| PSL | Public Suffix List. It gives the organizational domain of a host name. |
| Requester | The person who sends a request email |
| Retry-After | An HTTP header from a dependency. The agent never retries sooner than this value. |
| Settled | The states `done`, `clarify`, `rejected` and `failed`. Nothing more happens until the requester writes again. |
| Single-flight | A lock that lets only 1 job fetch the same documents. The other jobs read the cache. |
| SLA | Service level agreement. The agent has none. |
| SOCKS tunnel | The SSH tunnel on `127.0.0.1:1080` that carries UARB traffic to Canada |
| SPF | Sender Policy Framework. The list in DNS of the IPs that can send mail for a domain. |
| Suppression list | HMACs of addresses and domains whose mail the gate drops without a reply |
| Sweeper | The worker job that finds requests that did not move for 15 min |
| Triage | The decision about what an email wants: rules, then Jev, then the LLM |
| TypeSafe | The vendor of the Jev classifier |
| UARB | Nova Scotia Utility and Review Board |
| WebDirect | The FileMaker web technology of the UARB portal |
| WebDrawer | The HPE Content Manager web technology of the OEB portal |
| ZDR | Zero data retention. The vendor does not keep or train on the prompts. |
