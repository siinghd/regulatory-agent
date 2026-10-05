# Models

This document is written in ASD-STE100 Simplified Technical English.

This document tells which model does which job, why, what the evals measured, and what data each model vendor receives.

## 1. Summary

| Job | Model | Constraint | If the model does not answer |
|---|---|---|---|
| Triage, when the rules cannot decide | TypeSafe Jev (`jev-1.13.0`, pinned), a System One classifier. 1 request with typed questions for each email. | Jev only selects: a matter from the numbers in the email, a category from the list of that regulator | The LLM triage |
| Triage, when Jev is not sure or does not answer | DeepSeek v4.1 Flash through OpenRouter. Qwen 3.8 27B is the fallback model. | Strict JSON schema, the same closed sets, the matter must occur in the email | Jev's question, or the cautious answer of the rules |
| Citation support check | Jev, 1 request for each claim, all claims at the same time | The claim stays only if P(supported) is 0.5 or more | The LLM check. If that also fails, the agent drops the claim. |
| Summaries | DeepSeek v4.1 Flash through OpenRouter (Qwen as fallback) | Each quote must be in the page text. Figures must be in the sources. | The reply goes without a summary |

All OpenRouter calls go only to zero-data-retention (ZDR) endpoints. TypeSafe is not ZDR on our plan (section 6).

No model selects a recipient, a link, a file or an SQL statement. Code writes all text that asks the requester a question.

## 2. Triage with Jev

### 2.1 Why Jev

Jev answers typed questions with probabilities. Each answer must be 1 of the options that the agent sent. The agent never parses free text from Jev. The confidence values let code decide when to ask a question in place of a guess.

Measured in `evals/gate/report_jev.md`:

| | LLM gate (DeepSeek) | Jev with LLM escalation (production) |
|---|---|---|
| In-sample exact match | 100% | 100% |
| Held-out exact / operator-correct / wrong fetches | 97.5% / 100% / 0 | 97.5% / 100% / 0 |
| Latency p50 / p95, in-sample | 1.05 s / 2.90 s | 0.25 s / 0.35 s |
| Cost for each 1000 emails, in-sample mix | USD 0.035 | USD 0.046 |
| Identical output on 2 runs, in-sample | 99.0% | 100% |

Jev alone, on its first held-out run, had 92.5% exact, 95.0% operator-correct and 2 wrong fetches. The developer added the threshold `category_unnamed` after that run. [Quality and evals](quality-and-evals.md) gives the caveats.

### 2.2 How the Jev gate works

1. The rules try first. If they decide, the agent calls no model.
2. The rules find the candidate matters in the email (`rules.find_matters`). Jev can select only 1 of them.
3. The agent sends 1 request with all questions. TypeSafe answers them in parallel.
4. The agent does not send a question whose answer code cannot use for this email. TypeSafe bills input tokens.
5. Code applies the confidence thresholds.
6. If the text names exactly 1 category and Jev selected another category, the text wins.

| Question | Type | Purpose |
|---|---|---|
| Intent | Choice: document request, question, acknowledgement, unrelated, spam | What the sender wants |
| Injection | Noul (yes or no) | Does the email try to change how the assistant works? |
| Exclusion | Noul | Does the sender exclude a category? Not sent if the rules already see a negation. |
| Count | Choice: 1 to 10 or "not stated" | Not sent if the rules read a count |
| Matter | Choice among the candidates, or "none" | Which matter the sender wants now |
| Category | Choice among the categories of the regulator, or "none stated" | 1 for each regulator that the email mentions |
| Category wanted | 1 Noul for each category | Makes sure that an excluded category is never the answer |
| Specific document | Noul | Off (threshold above 1). The rules decide. |

### 2.3 Thresholds

The thresholds are in `agent/gate/jev.py` (`Thresholds`). The developer tuned them on `jev-1.13.0`.

| Threshold | Value | Effect below the value |
|---|---|---|
| `injection` | 0.5 | At or above: the email is an injection attempt |
| `intent` | 0.6 | Ask a question. Do not fetch. |
| `matter` | 0.6 | No matter (ask which) |
| `matter_none` | 0.9 | At or above, with 1 candidate: no matter |
| `extra_matter` | 0.5 | Another mentioned matter is not offered |
| `category` | 0.5 | No category from the Choice |
| `category_unnamed` | 0.8 | A category that the text does not name by an alias needs this value |
| `wanted` | 0.5 | A category is not taken as wanted |
| `extra_category` | 0.6 | A further named category is not offered |
| `excludes` | 0.5 | The sender does not exclude a category |
| `count` | 0.9 | No count (all, up to 10) |
| `specific` | 1.01 | Off |

When the `intent`, `matter` or `category` gate fires, Jev is "not sure". With `gate_jev_low_confidence=llm` (the default), the LLM triage then decides. With `clarify`, the agent asks the requester.

### 2.4 Request limits

| Parameter | Value |
|---|---|
| `typesafe_deadline_s` | 10 s for the whole call, retries included |
| `typesafe_connect_timeout_s` | 5 s |
| Attempts | At most 3, for HTTP 408, 429, 500, 502, 503, 504 and 529, and for transport errors |
| `Retry-After` | Honoured up to 2 s. The total of the waits is at most 3 s. |
| Circuit breaker | `typesafe` |
| Body size | At most 12,000 characters, as for the LLM gate |
| Price | USD 0.042 for each million input tokens. Output tokens are free. |

## 3. Triage with the LLM

`agent/gate/classify.py` is the LLM triage. It is the fallback when Jev is not sure or not available. With `GATE_CLASSIFIER=llm`, it is the only model triage.

- The email is in a delimited block of untrusted data.
- The answer must validate against a strict JSON schema.
- The intent and the category come from closed sets.
- A matter number must be a matter of a registered regulator, and the email must mention it.
- The request sets the temperature to 0 and `reasoning.enabled` to `false`.
- If the text names exactly 1 category and the LLM selected another, the text wins.

In the in-sample gate run, DeepSeek answered all 210 model-path calls. No call needed the fallback model. The mean cost was USD 0.000063 for each call.

## 4. Summaries

`agent/citations/claims.py` writes the cited summary with 1 LLM call.

| Limit | Value |
|---|---|
| Context | At most 60,000 characters, at most 4 documents, at most 8,000 characters for each page |
| Documents listed in the prompt | At most 40 |
| Output | At most 4 summary sentences and 5 claims. At most 8000 tokens. |
| Minimum claims | 2. With fewer, the reply shows no claims. |
| Deadline for each attempt | 90 s (`llm_timeout_s`) |

Code ranks the documents by their portal document type and title. The most decision-relevant pages go first.

The output eval measured a mean generator cost of USD 0.001 to USD 0.0021 for each summary. The mean latency was 4.1 s to 4.2 s. DeepSeek answered all 12 cases.

## 5. Citation support check

After code finds the quote on the page, 1 question remains: does the quote, in its context, support the claim?

1. The check runs only on claims whose quote is not 1 exact sentence of the page.
2. Jev gets 1 request for each claim. The request contains only the claim, the quote and the page text near the quote.
3. Jev selects "supported", "partially" or "not supported".
4. The claim stays only if Jev selects "supported" with P(supported) of 0.5 or more.
5. If the Jev request fails, the LLM check examines the claim.
6. If the LLM check also fails, the agent drops the claim (`support_check_failed`).

Measured in `evals/output/report_jev_check.md` on 60 real claims: Jev precision 100% and recall 88%; LLM check precision 67% and recall 75%. Latency p95 for each call: 307 ms for Jev, 2.48 s for the LLM.

## 6. Privacy and vendors

| Vendor | Data sent | Retention |
|---|---|---|
| OpenRouter, then DeepSeek or Qwen | Email subject and body (LLM triage), public document text (summaries, LLM check) | ZDR endpoints only. `provider.zdr=true`, `data_collection=deny`, `require_parameters=true`. |
| TypeSafe | Email subject and body (Jev triage), public document excerpts (citation check) | Not ZDR on our plan |

At start, the worker asks OpenRouter for its list of ZDR endpoints (`GET /endpoints/zdr`). It removes each configured model without a ZDR endpoint. If no model remains, triage uses only Jev and the rules, and replies go without summaries.

WARNING: TypeSafe keeps no zero-retention agreement on our plan. TypeSafe offers ZDR only on enterprise plans. The owner accepted this for the MVP. The privacy notice discloses it, and each worker start writes a `typesafe.data_policy` log line. To send TypeSafe nothing, set `GATE_CLASSIFIER=llm` and `CITATION_CHECK=llm`.

## 7. Budgets and breakers

- The daily LLM budget is USD 2.00 (`llm_daily_budget_usd`). It includes the OpenRouter cost (as OpenRouter reports it) and the TypeSafe cost (input tokens multiplied by the list price).
- When the budget is spent, triage uses only the rules and no new summary is written, until 00:00 UTC.
- Each OpenRouter model has its own circuit breaker (`openrouter:<model>`). The client skips a model with an open breaker.
- Each model call writes an audit event with its purpose, the data class, the model, the outcome and the token counts.

## 8. Procedure: change the model mode

CAUTION: The developer tuned the thresholds on `jev-1.13.0`. Do not change `TYPESAFE_MODEL` without a new gate eval and a new citation check eval.

1. Open `.env` on the host.
2. Set the mode that you want.

   | Variable | Values | Default |
   |---|---|---|
   | `GATE_CLASSIFIER` | `jev`, `llm` | `jev` |
   | `GATE_JEV_LOW_CONFIDENCE` | `llm`, `clarify` | `llm` |
   | `CITATION_CHECK` | `jev`, `llm` | `jev` |
   | `LLM_MODELS` | JSON list, in fallback order | `["deepseek/deepseek-v4.1-flash","qwen/qwen3.8-27b"]` |

3. Write the per-service env files.

   ```bash
   deploy/split-env.sh
   ```

4. Restart the worker and the web process. The web process shows the vendors in the privacy notice.

   ```bash
   docker compose up -d --no-deps worker web
   ```

5. Find the `worker.models` line in the worker log.

   ```bash
   docker compose logs worker | grep worker.models
   ```

   Expected result: the line shows the new `gate_classifier`, `citation_check` and `llm_models`.
