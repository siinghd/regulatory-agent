# Design notes

This covers why the system is shaped the way it is, what can go wrong, and where it goes next.
The README covers what it does.

## 1. Goals, in priority order

1. **Never send the wrong thing to the wrong person.** Wrong documents under a right title, documents to a spoofed address, and a reply to an autoresponder are all worse than a slow or failed request.
2. **Never go silent.** Every authenticated request ends in exactly one useful reply: the documents, a clarifying question, a clear "not found", or an apology after retries.
3. **Be cheap when it can be.** Repeat requests shouldn't touch the portal or the LLM.
4. **Add a regulator without touching the pipeline.**

## 2. Key decisions

| Decision | Alternatives | Why |
|---|---|---|
| **Deterministic browser automation** (Playwright), no LLM driving the browser | Agentic browser / computer use | The portal is a fixed workflow. Determinism makes it testable and fast (10 files in ~30 s), and an LLM can't be talked into clicking somewhere else. |
| **Rules first, LLM second** for parsing | LLM on every email | Most requests are "M12205 + a tab"; the rules answer those in microseconds, for free. The LLM handles negation ("not the exhibits"), follow-ups, multiple types and spam, and its answer is cross-checked against the email text. |
| **Postgres state machine + Redis queue (arq)** | Temporal, Celery | One durable source of truth with CAS transitions and an event log covers this scale with two dependencies. Temporal is the right move once there are many long multi-step workflows per regulator (section 6). The steps are already shaped as idempotent activities. |
| **Content-addressed blobs** (sha256) | Per-request folders | Dedup across matters and requests (the portal itself files identical PDFs under several ids), atomic writes, and immutable URLs for the viewer. |
| **Encrypted link instead of attachment** | 25 MB attachments | Exhibit ZIPs reach 130 MB. drop encrypts in AES-256-GCM chunks with the key only in the URL fragment, so the file server never sees plaintext. Links expire and have a download cap. |
| **Verify the sender ourselves** | Trust the From header | Our MTA doesn't add Authentication-Results. Answering unauthenticated mail turns the agent into a reflector. SPF is evaluated against the IP in *our* MTA's Received header (everything below it is attacker-written). |
| **Summaries must quote** | Free-form summary | Each claim carries `{doc, page, quote}`. Quotes are located in our own extracted text (exact match, then fuzzy with numbers protected), and claims or sentences with unsupported dollar figures or dates are removed. Grounding beats eloquence. |
| **Providers behind a small interface** | One scraper | `fetch_matter`, `list_documents`, `download` plus categories and a matter pattern. UARB is a scraped FileMaker app and OEB is a JSON API; the same pipeline drives both. |

## 3. Request lifecycle and idempotency

```
received ─gate─► rejected                      (automated, spoofed, rate-limited, spam, injection)
         ├────► replying ─► clarify | done     (questions, missing info, not found)
         └────► accepted ─► fetching ─► packaging ─► replying ─► done
                         (any step) ───────────────────────────► failed (after retries, user told)
```

| Hazard | Guard |
|---|---|
| Same email delivered twice | `UNIQUE(message_id)`; the second insert is a no-op |
| Crash after IMAP fetch, before commit | Message stays unread and is re-ingested on the next sweep |
| Two jobs for one request | arq job id = request id, plus a per-request Redis lock |
| Crash between SMTP send and commit | The Message-ID is reserved before sending; the retry resends the *same* message |
| A short reply fails to send | The paragraphs and target state are persisted before sending; resume resends them verbatim |
| Retries burning rate limit | Rate-limit hits are keyed by request id |
| Stuck job (worker killed) | Sweeper re-enqueues non-terminal requests idle for 15 min |
| Ten people ask for the same matter | Single-flight lock per (provider, matter, category); the others hit the cache |

## 4. Threat model

| Threat | Mitigation |
|---|---|
| Spoofed From to make the agent mail a victim | DMARC alignment required; failures get no reply (tested live: spoofed `p=reject`/`p=quarantine`/`p=none` domains are all dropped) |
| Autoresponder / out-of-office loops | RFC 3834 headers, null return-path, bulk/list headers, our marker header, OOO subjects combined with In-Reply-To pointing at our domain; per-thread and per-sender caps |
| Prompt injection in the email ("send to attacker@…") | Recipient never comes from content; injection intent is detected and declined; email text is fenced as data |
| Prompt injection inside a PDF | Document text is fenced as data; claims must quote the page; figures are checked against quotes and metadata |
| Hostile MIME (nesting bombs, broken headers, bad DKIM tags) | Defensive parsing with every parser exception mapped to `MalformedEmail`; fuzzed with 50k mutated messages and 30k mutated DKIM signatures, no uncaught exceptions |
| Thread hijack (replying into someone's thread) | Thread context is scoped to the same authenticated sender |
| Enumerating other people's requests | Progress pages use a 16-character random token; no public per-matter pages; sender address masked |
| Viewer XSS / clickjacking | Jinja autoescape, CSP `script-src 'self' cdnjs` with SRI, `frame-ancestors 'none'`, no inline styles |
| Credential leaks | Secrets only in `.env` (mode 600, gitignored); no secret ever logged; drop keys and delete tokens hidden from `repr` |

## 5. Failure classification

The single most important distinction is **"the portal didn't answer" vs "the portal said no"**:

| Signal | Classified as | User sees |
|---|---|---|
| Timeout, proxy error, empty listing despite a non-zero count | `PortalUnavailable` / `ScrapeError` (retryable) | Nothing extra; we retry for ~25 min, then one apology |
| "No Records Found", twice, in independent sessions | `MatterNotFound` (final) | "I couldn't find M… in the public database" |
| Served filename ≠ requested id | `ScrapeError` (retry that file) | Nothing; a wrong file never ships |
| Some files fail after retries | Partial success | Documents delivered, failed titles listed |
| LLM down | Degraded | Documents delivered without a summary; rules-only parsing |

## 6. Scaling to every regulator

This handles a mailbox's worth of traffic on one box. Senpilot's Regulatory Agent needs the whole corpus, warm and searchable. What changes:

1. **From on-demand to continuous ingestion.** A schedule per regulator discovers new matters and filings and downloads them into the same content-addressed store. The email agent then answers from the warm corpus in seconds and only scrapes on a miss.
2. **Temporal for orchestration.** One workflow per (regulator, matter) with activities for list, download and extract, heartbeats on long downloads, and a task queue per regulator. The queue's worker concurrency becomes the politeness limit. Workflow ids derived from `regulator:matter` replace the single-flight lock.
3. **Egress per region.** Each provider declares its egress (UARB: Canadian IPs). A pool of regional egress IPs, each with its own download lock (the UARB session-sharing issue is per client IP), scales portal throughput linearly.
4. **Search.** Chunk page text by structure, keep page numbers, and use hybrid search (BM25 for docket numbers and dollar figures, vectors for concepts) with metadata filters (regulator, utility, category, date). pgvector partitioned by jurisdiction first; a dedicated vector store only when recall or latency evals say so.
5. **Drift detection.** A canary matter per provider with known counts, plus alerts when discovered-documents-per-day drops to zero or parse success falls. Silent zeros are the failure mode that matters.
6. **Evals.** A gold set of real requests (gate accuracy) and real precedent questions with known answer pages (retrieval recall@k, citation precision), run on every model or prompt change.

## 7. What I'd do next

- OCR for scanned PDFs (Tesseract or Document AI) so they can be cited too.
- ARC evaluation for mailing-list and forwarded mail.
- Prometheus metrics and a dashboard: per-stage latency, retries by cause, cache hit rate, cost per request.
- The nightly canary per provider, wired to an alert.
- Answer questions about a matter ("what did the board decide?") from the cited corpus, not just send files.
