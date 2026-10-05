# Runbook: LLM provider incident

This document is written in ASD-STE100 Simplified Technical English.

No alert links here. The signals are in each section. Parent document: [Incident response](../policies/incident-response.md). Policy: [AI use](../policies/ai-use.md).

This runbook covers 2 vendors:

| Vendor | Models | Jobs | Data retention | Breaker |
|---|---|---|---|---|
| OpenRouter, and the model providers behind it | `LLM_MODELS`, in this sequence: `deepseek/deepseek-v4.1-flash`, then `qwen/qwen3.8-27b` | Summaries. Triage when Jev is not sure or not available. Citation checks that Jev cannot answer. | Zero-data-retention endpoints only (`llm_zero_data_retention`) | `openrouter:<model>` |
| TypeSafe | Jev, `TYPESAFE_MODEL` = `jev-1.13.0` (pinned) | Triage when the rules cannot decide. Citation support checks. | Not zero-data-retention on our plan. The owner accepted this for the MVP. The privacy notice discloses it. | `typesafe` |

[Models](../guide/models.md) gives the roles and the reasons.

## A. Outage or degraded service

Signals: an `openrouter:<model>` or `typesafe` breaker opens, model timeouts in the worker log, errors on the "Models" dashboard, or replies without a summary. Incident severity: SEV3. The agent continues with less function.

What the agent does without help:

| Failure | Triage | Summaries and citation checks |
|---|---|---|
| OpenRouter or its models fail | The rules decide simple requests. Jev decides the others. If Jev is not sure, the agent sends the question of Jev to the sender. | The documents go out without a cited summary. |
| TypeSafe fails | The rules decide simple requests. The LLM classifier decides the others. | The LLM check examines the claims. |
| Both fail | The rules give their cautious answer. | The documents go out without a cited summary. |

The breakers probe the vendor again with longer intervals. When the vendor answers, the breaker closes.

To put a healthier model first, do these steps:

1. Change the sequence in `LLM_MODELS` in `.env`.
2. Write the env files for each service again.

   ```bash
   deploy/split-env.sh
   ```

3. Start the worker with the new environment.

   ```bash
   docker compose up -d worker
   ```

4. Record the change in the pull request or in the incident note. A model change is a change. Run the evals after it ([Quality and evals, section 6.5](../guide/quality-and-evals.md#65-when-to-run-the-evals)).

Tell the users nothing, unless the outage continues for more than 1 day. Then add a short note to the reply text through a normal change.

## B. Bad output reached a requester

Signals: a requester reports a wrong claim, or a review finds a wrong claim. Incident severity: SEV3, or higher if the claim caused harm.

The citation checks remove claims whose quote is not in the page text. They also remove summary sentences with figures that the sources do not contain. A bad claim therefore has a real quote but a wrong interpretation of it, or it found a gap in the checks.

1. Find the request and its citations: the `citations` rows with the `request_id` of the request.
2. Send a correction to the requester. Use only the authenticated address of the request.
3. Delete the cached summary, so that the agent does not use it again. Delete the `summaries` row whose key covers these documents. Do this as the superuser.
4. Add the case to the evals (`evals/`).
5. Correct the prompt or the checks through a normal change.

## C. Data incident at a model vendor

Signals: a breach notice from a vendor, news, or a change of terms. For OpenRouter, an example is a change that removes zero data retention. Incident severity: SEV2 until you know the scope.

1. Find what the vendor received.

   | Vendor | Data sent | Data not sent |
   |---|---|---|
   | OpenRouter and its model providers | The email text, when the rules did not decide and Jev was not sure or not available. Public document text for summaries. Claims, quotes and page text for citation checks that Jev did not answer. | Attachments, headers other than the subject, credentials, the data of other requesters |
   | TypeSafe | The email subject and the first 12,000 characters of the body, when the rules did not decide. Each claim, its quote and up to 500 characters of page text on each side. | Same as OpenRouter |

   Email text is Confidential. It can contain names, signatures and phone numbers.

2. Find the time window and the requests. Each model call writes an `llm.call` audit event with the request id, the provider and the model. The `parsed` JSON of each request shows how the gate parsed it.
3. Contain the incident with the applicable step:
   - For 1 model provider: remove its model from `LLM_MODELS`, or exclude its endpoints in OpenRouter.
   - For OpenRouter: set `LLM_MODELS=[]`. The agent then sends nothing to OpenRouter. Triage uses the rules and Jev, and replies have no summary. Rotate `OPENROUTER_API_KEY` ([Credential leak](credential-leak.md)).
   - For TypeSafe: set `GATE_CLASSIFIER=llm` and `CITATION_CHECK=llm`. The agent then sends nothing to TypeSafe. Rotate `TYPESAFE_API_KEY`.
4. After each change to `.env`, run `deploy/split-env.sh`. Then run `docker compose up -d worker web`. The web process reads `GATE_CLASSIFIER` and `CITATION_CHECK` for the privacy page.
5. Do the notification assessment of the incident plan. Find the affected senders and the data that the vendor says that it kept. Decide if there is a real risk of significant harm.
6. Review the vendor in the [Vendor register](../policies/vendor-register.md). Decide to keep, limit or replace the vendor.

NOTE: Do not set `OPENROUTER_API_KEY` to an empty value to stop the traffic. With an empty key, the worker still sends each request body to OpenRouter, and OpenRouter refuses it. `LLM_MODELS=[]` stops the calls before they leave the host.

## D. Key misuse or spend out of control

Signals: an OpenRouter spend alert, usage that does not agree with the request volume, or a `budget.exhausted` log line early in the UTC day ([Budget](budget.md)).

1. Rotate the key immediately.
2. Set a credit limit on the new key.
3. Investigate the incident as a credential leak ([Credential leak](credential-leak.md)).
4. Look for a loop in the agent. A loop shows as many `summary` events for 1 request id.
5. If you find a loop, stop the worker.

   ```bash
   docker compose stop worker
   ```

6. Correct the cause before you start the worker again.
