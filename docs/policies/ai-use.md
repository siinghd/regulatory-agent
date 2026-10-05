# AI / LLM Use Policy

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05.

SOC 2: PI1.2–PI1.4, CC3.2, C1.1, P6. Review this policy 1 time each year, and after each change of a model or a provider.

[Models](../guide/models.md) gives the roles of the models, the reasons for each choice and the measured numbers.

## 1. Where the system uses models

| Use | Input | Output and its limits |
|---|---|---|
| Request classification and citation checks with TypeSafe Jev (`agent/gate/jev.py`, `agent/citations/jev_check.py`) | Classification: the email subject and body, when the rules cannot parse the email. Citation check: a claim, its quote and the page text near the quote. | Typed answers. Each answer must be 1 of the options that we send. The options are a matter from the numbers in the email, a category of the provider, or a probability. Below the confidence thresholds, the email goes to the LLM classifier (or the agent asks the sender). If a Jev citation check fails, the LLM check decides. If no check can decide, the agent drops the claim. |
| Request classification fallback with the LLM (`agent/gate/classify.py`) | The email text, when the rules cannot parse it and Jev is not sure or not available. The prompt fences the text as untrusted data. | JSON that code validates against a schema. The matter number must occur in the email. The document category must come from the closed set of the provider. The recipient never comes from model output. |
| Cited summaries (`agent/citations/`) | Page text of public documents, fenced as data | Each claim has `{document, page, quote}`. Code finds each quote in our own extracted text (exact, then fuzzy with protected numbers). Code removes claims that it cannot find. It also removes sentences with figures or dates that the quotes do not support. This occurs before the agent writes the reply. |
| Development | Code, tests and documents (AI assistants for code) | Review the output as for any other change. The CI gates apply. |

Models never control the browser, never select recipients and never decide authentication. Models never get credentials.

## 2. Approved providers and models

- **TypeSafe** (`api.typesafe.ai`). The model is pinned in `TYPESAFE_MODEL` (`jev-1.13.0`). It classifies requests and checks citations.
  - TypeSafe is not zero-data-retention on our plan. The owner accepted this for the MVP on 2026-10-05.
  - The [privacy notice](privacy-notice.md) discloses it, and each worker start writes it to the log (`typesafe.data_policy`).
  - `GATE_CLASSIFIER=llm` and `CITATION_CHECK=llm` stop all traffic to TypeSafe.
- **OpenRouter**, with the models in `LLM_MODELS`, in this order: `deepseek/deepseek-v4.1-flash`, then `qwen/qwen3.8-27b`.
  - OpenRouter routes only to endpoints that do not keep or train on prompts (`llm_zero_data_retention = true`).
  - This setting applies only to OpenRouter. It does not apply to TypeSafe.
- A new or changed model or provider is a change ([change management](change-management.md)). The PR must contain the eval results (section 4) and a check of the [vendor register](vendor-register.md).

## 3. Data rules

- **Confidential data** (email text) goes to a model only when the rules cannot parse the request. Only the necessary text goes:
  - to TypeSafe (not zero-data-retention, refer to section 2);
  - to the zero-data-retention endpoints of OpenRouter, when Jev is not sure or not available.
- **Restricted data** (any secret) never goes to a model. Document text is Public.
- The logs do not contain prompts or responses. The `llm.*` log events contain the model, the purpose, the result and the time (`agent/llm.py`).
- If a model has no zero-data-retention endpoint, the agent does not call it (`llm.no_zdr_endpoint`). It never sends the call to a different endpoint.
- **Development assistants:** never put `.env`, `deploy/env/*`, keys, database dumps or real emails into an AI tool. For examples, use the fixtures in `tests/` (example.org addresses). Review, test and attribute generated code in the commit, as for all other code.

## 4. Quality and evaluation

- The evals in `evals/` measure the accuracy of the gate and the citation precision of the summaries. The adversarial corpus is in `tests/adversarial/`. Run them before a model or prompt change goes into production. A regression stops the change.
- [Quality and evals](../guide/quality-and-evals.md) gives the method, the results and the caveats.
- Users can see the source of each claim. "view source" opens the page with the quote highlighted. The replies show the summary as key points with sources, not as advice.

## 5. Incidents

For wrong output that reached a requester, a data incident at a provider, or a misuse of a key, use the [LLM provider incident runbook](../runbooks/llm-provider-incident.md).

## 6. Transparency

The [privacy notice](privacy-notice.md) tells senders these facts:

- An AI model can process the text of their email to understand the request.
- An AI model writes the summaries from public documents.
- TypeSafe is not zero-data-retention on our plan.
