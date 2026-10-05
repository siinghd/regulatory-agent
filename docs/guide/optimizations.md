# Optimizations

This document gives each optimization of the agent and its measured effect. If no measurement exists, the table says "Not measured separately". The goal of each optimization is goal 3 of [DESIGN.md](../../DESIGN.md): use the portals and the models only when it is necessary.

NOTE: The measurements come from single runs on 1 host. They are not benchmarks. A portal can be slower or faster on a different day.

## 1. Summary

| # | Optimization | Measured effect | Source |
|---|---|---|---|
| 1 | Rules first in triage | 85 of 190 eval emails need no model. The rules take 0.17 ms (p50) at no cost. | `evals/gate/report.md` |
| 2 | Jev for triage | Model path p50 0.25 s in place of 1.05 s, p95 0.35 s in place of 2.90 s. Cost USD 0.046 in place of USD 0.035 for each 1000 emails. | `evals/gate/report_jev.md` |
| 3 | Smaller Jev requests | Mean input tokens for each Jev call: 2027 to 1907 (in-sample), 2092 to 1972 (held-out) | `evals/gate/report_jev.md` |
| 4 | Model order change, no chain-of-thought output | Summary time approximately 19 s before, approximately 4 s after. Cost for each summary USD 0.001 to USD 0.0021 after. | README; `evals/output/report*.md` |
| 5 | Jev for citation checks | p95 for each call 307 ms in place of 2.48 s. USD 0.000041 in place of USD 0.000066 for each claim. | `evals/output/report_jev_check.md` |
| 6 | Summary at the same time as the package and the upload | Before: 32 s portal work plus 19 s summary, approximately 52 s in total. After: the summary runs in parallel. The new total is not measured separately. | README |
| 7 | Single-flight for each (provider, matter, category) | 4 requests at the same time for 2 matters, cold and warm: 69 s wall clock, all on the first try | README |
| 8 | Content-addressed file store and list cache | A repeat request takes approximately 3 s | README |
| 9 | Summary cache keyed by document versions | A repeat request makes no model call | Commit `ce4d42e`; README |
| 10 | Smaller scope of the UARB download lock | A session holds the lock for approximately 0.7 s to 1.8 s for each file. The transfers run outside the lock. | Measured on the live portal (no report in the repository) |
| 11 | Matter data and list in 1 portal session | 1 portal round less for each request | Not measured separately |
| 12 | Parallel UARB download sessions | Up to 3 sessions for each matter | Not measured separately |
| 13 | The browser blocks images, fonts and media | Less traffic through the Canadian tunnel | Not measured separately |
| 14 | Page text extracted 1 time for each file version | No second extraction of the same file | Not measured separately |
| 15 | Drop link used again on a retry | No second upload of the same ZIP | Not measured separately |
| 16 | Canary result kept for 900 s | A "not found" answer needs 1 canary search at most each 15 min | Not measured separately |

## 2. Details

### 2.1 Rules first in triage

`agent/gate/rules.py` decides an email when it is unambiguous: exactly 1 matter, exactly 1 category of that regulator, a request phrase and no negation. It also answers a short "thanks" in a thread, so that the agent does not fetch the earlier request again. All other emails go to a model.

In the in-sample gate eval, the rules decided 85 of 190 cases with 100% exact match. The rules latency was 0.17 ms at p50 and 0.53 ms at p95.

### 2.2 Jev for triage

1 Jev request replaces 1 LLM call for each email that the rules cannot decide. Jev answers all questions of 1 email in parallel. With escalation, 4.3% (in-sample) and 7.9% (held-out) of the Jev calls also used the LLM.

| | LLM gate | Jev with escalation |
|---|---|---|
| Latency p50 / p95, in-sample | 1.05 s / 2.90 s | 0.25 s / 0.35 s |
| Cost for each 1000 emails, in-sample | USD 0.035 | USD 0.046 |

The cost is a little higher. The latency and the consistency are better.

### 2.3 Smaller Jev requests

TypeSafe bills input tokens. The agent does not send a question whose answer code cannot use for that email:

- the specific-document question, while its threshold is off;
- the exclusion question, when the rules already see a negation;
- the count question, when the rules read a count.

Each per-category question names the other names of its category 1 time, without a singular next to its plural.

| Dataset | Before | After |
|---|---|---|
| In-sample, mean input tokens for each call | 2027 | 1907 |
| Held-out, mean input tokens for each call | 2092 | 1972 |

Shorter wordings saved 5% to 10% more, but the developer rejected them. In A/B runs they changed the answer on cases near a threshold. Approximately 300 tokens of each call are a fixed overhead of TypeSafe.

### 2.4 Direct model calls, no chain-of-thought output

The agent uses DeepSeek v4.1 Flash first and Qwen 3.8 27B second. Each request sets `reasoning.enabled` to `false`, because extraction needs no chain of thought.

| | Before | After |
|---|---|---|
| Summary time in a cold UARB request | Approximately 19 s (README) | Approximately 4 s mean (output eval: 4.1 s and 4.2 s) |
| Cost for each summary | Not in a report | USD 0.001 to USD 0.0021 mean |
| Chain-of-thought tokens in the gate eval | Not in a report | 0 |

### 2.5 Jev for citation checks

The LLM check made 1 call for each summary. Jev makes 1 small request for each claim, and all claims run at the same time. Each request contains only the claim, the quote and the page text near the quote.

| | LLM check | Jev |
|---|---|---|
| Latency p50 / p95 for each call | 0.97 s / 2.48 s | 243 ms / 307 ms |
| Wall time for each summary, p50 / p95 | Same as for each call | 268 ms / 315 ms |
| Input tokens | 1557 for each call | 969 for each claim |
| Cost for each claim | USD 0.000066 | USD 0.000041 |
| Precision / recall on 60 real claims | 67% / 75% | 100% / 88% |

### 2.6 Summary at the same time as the package

The summary does not depend on the ZIP, and the ZIP does not depend on the summary. The worker runs the 2 tasks in 1 task group (`pipeline._together`). If 1 task fails, the worker stops the other task.

The first end-to-end measurement was 32 s for the portal work and 19 s for the summary, approximately 52 s in total. With the parallel tasks and the faster summary model, the package step hides most of the summary time. The new total was not measured separately.

### 2.7 Single-flight

The lock `sf:<provider>:<matter>:<category>` lets only 1 job visit the portal for the same documents. Other jobs wait up to 300 s and then read the cache. 10 people who ask for M12205 at the same time cost 1 portal visit.

README: 4 requests at the same time, for 2 matters, cold and warm, took 69 s wall clock. All 4 succeeded on the first try.

### 2.8 Caches

| Cache | Key | Lifetime |
|---|---|---|
| Matter data and listings | (provider, matter, category) | 6 h (`matter_cache_ttl_s`) |
| Files | SHA-256 | 395 days without use (`blob_retention_days`) |
| Page text | SHA-256 and page number | As long as the file |
| Summaries | Summary version, matter and the sorted SHA-256 values of the files | Until the summary version changes |

A new or changed document gives a new SHA-256, so the agent writes a new summary. The store keeps the same file in 2 matters 1 time.

README: a repeat request takes approximately 3 s.

### 2.9 Smaller scope of the UARB download lock

The UARB portal shares its prepared files across guest sessions from 1 client IP. For this reason, only 1 session at a time can go from the GO GET IT click to the start of the correct file. The lock covers only this step. When the portal starts to send a file with the requested name, the session releases the lock. The transfer continues outside the lock, and other sessions can click.

A session holds the lock for approximately 0.7 s to 1.8 s for each file. The repository has no report of this measurement.

### 2.10 Other optimizations

- `list_matter_and_documents` reads the matter header and the category list in 1 portal session.
- UARB downloads use up to 3 browser sessions for each matter. Navigation runs in parallel. The lock serialises only the click-to-file step.
- The browser does not load images, fonts or media. They are not necessary and they are slow through the tunnel.
- A retry uses the stored drop link if the files are the same.
- OEB keeps the file sizes from recent listings. FERC keeps the primary file of each recent accession. A later download needs no second lookup.
- The "not found" canary result of OEB and FERC stays valid for 900 s.
- Ingest applies the pre-authentication limits before it stores a body or asks DNS. For mail over the limits, ingest does no DNS lookups and stores only the headers.
- The outbox sends the acknowledgement immediately after the commit. README: the acknowledgement arrives approximately 3 s after the email.
