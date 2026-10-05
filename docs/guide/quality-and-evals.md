# Quality and evals

This document is written in ASD-STE100 Simplified Technical English.

This document describes the 2 evals of the agent, their method, their results and their limits. It also gives the procedures to run them. The datasets, runners and reports are in `evals/`.

| Eval | Question that it answers | Reports |
|---|---|---|
| Gate eval | Does the agent understand the request correctly, and does it do the correct action? | `evals/gate/report.md`, `evals/gate/report_jev.md` |
| Output eval | Are the cited summaries true to their sources, and is the reply text correct? | `evals/output/report.md`, `evals/output/report_run1_full_judge.md`, `evals/output/report_jev_check.md` |

WARNING: Summaries are machine-written. Each key point links to its source passage. The reader must examine the cited source before they use a summary for a decision.

## 1. Headline results

| Measure | Result | Source |
|---|---|---|
| Gate, 190 in-sample emails: exact match, before and after the fixes | 86.8% before, 100% after | First run (report not in the repository); `evals/gate/report.md` |
| Gate, 40 held-out emails, DeepSeek: exact / operator-correct / wrong fetches | 97.5% / 100% / 0 | `evals/gate/report_jev.md` |
| Gate, 40 held-out emails, Jev alone, first run: exact / operator-correct / wrong fetches | 92.5% / 95.0% / 2 | `evals/gate/report_jev.md` |
| Gate, 40 held-out emails, production gate (Jev with LLM escalation) | 97.5% / 100% / 0 | `evals/gate/report_jev.md` |
| Output: kept claims that the judge rated "supported", before and after the fixes | 88% (43 of 49) before; 92% (46 of 50) and 96% (51 of 53) after | `evals/output/report.md`, `evals/output/report_run1_full_judge.md` |
| Output: kept claims that the judge rated "not supported" | 0 in both later runs | Same |
| Output: quotes found exactly in the page text | 100% | Same |
| Citation check on 60 real claims, Jev against the LLM check: precision / recall | 100% / 88% against 67% / 75% | `evals/output/report_jev_check.md` |

NOTE: The in-sample result of 100% is not an estimate of real accuracy. The developer tuned the prompt, the rules and the Jev thresholds on the same 190 emails. The held-out numbers are the honest estimate.

## 2. Gate eval

### 2.1 What it measures

The gate eval runs the real gate code on each email of a dataset. It scores the parsed request (intent, matter, category, count, extra matters, extra categories, clarification) and the action (fetch, clarify, answer, reject or help).

| Metric | Definition |
|---|---|
| Exact match | All relevant fields are correct |
| Operator-correct | The action is correct, and a fetch gets the correct matter and category |
| Action accuracy | The action is correct |
| Wrong fetches | The agent downloads something that the sender did not ask for. This must be 0. |
| Invented matters | The output has a matter that the email does not contain. This must be 0. |

`evals/gate/score.py` contains the full rules for the scores.

### 2.2 Datasets

| Dataset | Cases | Notes |
|---|---|---|
| `evals/gate/dataset.jsonl` | 190, written by hand | 51 cases have acceptable alternatives. 40 have a note about the label. |
| `evals/gate/heldout.jsonl` | 40 new cases | Written before the first Jev run |

The 190 in-sample cases expect these intents: 151 document requests, 15 questions, 12 injection attempts, 6 spam and 6 unrelated. The matters are UARB (65), OEB (49), FERC (41) and none (35). The rules decide 85 cases. 105 cases go to a model. The tags include negation (17), synonyms (19), counts (20), injection (13), non-English text (7), quoted history (6), FERC false positives (7) and full-width digits (2).

### 2.3 Method

1. The runner registers the providers as the worker does, without a portal visit.
2. Each case on the model path runs 2 times at temperature 0. This measures consistency.
3. The runner records each model call, its cost and its latency.
4. `score.py` compares each result with the label and its alternatives.

### 2.4 Results: before and after

The first in-sample run had an exact match of 86.8%. Prompt and rule fixes then gave 100% in-sample. The report of the first run is not in the repository.

Results from `evals/gate/report_jev.md` (2026-10-05 version):

| Metric | LLM gate (DeepSeek) | Jev | Jev with LLM escalation (production) |
|---|---|---|---|
| In-sample exact match | 100% | 100% | 100% |
| In-sample wrong fetches | 0 | 0 | 0 |
| In-sample injections labelled | 12 of 12 | 12 of 12 | 12 of 12 |
| Held-out exact match | 97.5% | 97.5% | 97.5% |
| Held-out operator-correct | 100% | 100% | 100% |
| Held-out wrong fetches | 0 | 0 | 0 |
| Jev runs escalated to the LLM | Not applicable | Not applicable | 4.3% in-sample, 7.9% held-out |
| Latency p50 / p95, in-sample | 1.05 s / 2.90 s | 0.25 s / 0.29 s | 0.25 s / 0.35 s |
| Cost for each 1000 emails, in-sample mix | USD 0.035 | USD 0.044 | USD 0.046 |
| Identical output on both runs, in-sample | 99.0% | 99.0% | 100% |

Other safety results of the in-sample LLM run (`evals/gate/report.md`): 0 false injection flags, 0 invented matters, 0 wrong-regulator routes, and 0 fetches of an excluded category in 17 negation cases. 1 spurious matter occurred: the case `spam-matter-bait`, which is spam and is never fetched.

### 2.5 Caveats

- The held-out set has only 40 cases. 1 case changes the exact match by 2.5 points.
- The first held-out run of Jev had 2 wrong fetches. Both were a FERC category for text that names no category. The developer added the threshold `category_unnamed` (0.8) after this run. For this reason, the later Jev held-out numbers are not out-of-sample. Quote the first run: 92.5% / 95.0% / 2 wrong fetches.
- The 1 held-out failure of the production gate (`ho-specific-exhibit`) asks a question with a count of 1 in place of 10. It is not a wrong fetch.
- Jev answers change slightly between identical requests. A case near a threshold can change between runs.
- The gate eval gives the raw body to the gate. In production, the MIME parser first removes quoted history.

## 3. Output eval

### 3.1 What it measures

The output eval summarises real documents exactly as `pipeline._summarise` does. It then renders the reply with fake links.

| Measure | Description |
|---|---|
| Check (a) | Each quote is the exact page text at its offsets |
| Check (b) | Each figure is in the sources |
| Check (c) | Each cited document was in the context of the model |
| Judge | SUPPORTED, PARTIALLY or NOT_SUPPORTED for each kept claim. Faithfulness and coverage for each summary. |
| NC1 | A negative control: a false dollar amount in a summary |
| NC2 | A negative control: a claim with a quote that does not support it |
| NC3 | A negative control: a claim with a quote from another document |
| Reply text | Counts, singular and plural, articles, order words, size strings and other text rules |

### 3.2 Dataset

12 cases with 84 documents: UARB, OEB and FERC matters. Each case is 1 request for up to 10 documents of 1 category. 5 cases use documents from the production store. The runner fetched 7 cases live. It caches all inputs in `evals/output/cache/`.

### 3.3 Method

1. `evals.output.fetch` gets each case 1 time and caches it.
2. `evals.output.run` summarises each case with the production code and the production model.
3. The judge is `anthropic/claude-sonnet-5.5` through OpenRouter, at temperature 0. It is of a different model family than the generator.
4. Deterministic checks and the judge give the report.

### 3.4 Results: before and after

| Metric | Fix pass C (baseline) | Fix pass D, run 1 (full judge) | Fix pass D, run 2 (final code, claims judged only) |
|---|---|---|---|
| Judge | `claude-opus-5.5` | `claude-sonnet-5.5` | `claude-sonnet-5.5` |
| Kept claims rated SUPPORTED | 88% (43 of 49) | 96% (51 of 53) | 92% (46 of 50) |
| Kept claims rated NOT_SUPPORTED | Not in the report | 0 | 0 |
| The quote alone is enough | 61% (30 of 49) | 76% | 68% |
| Quotes equal to the page text | Not in the report | 100% | 100% |
| Summaries with all figures in the sources | Not in the report | 100% | 100% |
| Generator cost for each summary (mean) | Not in the report | USD 0.0021 | USD 0.001 |
| Generator latency (mean) | Not in the report | 4.1 s | 4.2 s |
| Negative controls caught | Not in the report | 3 of 3 | NC2 and NC3. NC1 shows "not caught" because the summary judge did not run. |

The fixes after the baseline are in the code:

- The summary reads DOCX files. FERC issues many of its orders as DOCX.
- Code extends a located quote to its full sentence, so the marked passage reads on its own.
- A summary sentence can state figures from all pages that the model saw, not only from the kept quotes.
- A filter removes words such as "metadata" and "provided pages" from summaries.
- Code ranks documents by the portal document type (OEB, FERC), not only by the title.
- The ZIP README describes the real order of the files.
- The reply tells on how many of the documents the summary is based.

### 3.5 Caveats

- The baseline used a different judge model (Opus) than the later runs (Sonnet). The numbers are not strictly comparable.
- Each case had 1 generator run. Run-to-run variance is not measured. DeepSeek at temperature 0 is not deterministic.
- The judge is 1 model. It is the reference, not the ground truth.
- 16 of 84 documents were not readable by the summariser, for example spreadsheets and scanned PDFs.
- The extractor read only 400 of the 2406 pages of 1 OEB application (`MAX_PAGES`).

## 4. Citation support check eval

This eval compares 2 checkers of "does the quote support the claim": Jev (`agent/citations/jev_check.py`) and the LLM check (`agent/citations/ground.py`). The judge gives the reference answer for each claim. The positive class is a claim that must be dropped (PARTIALLY or NOT_SUPPORTED).

| Item set | Checker | Items | Caught | Missed | False drops | Precision | Recall |
|---|---|---|---|---|---|---|---|
| Real claims | LLM | 60 | 6 | 2 | 3 | 67% | 75% |
| Real claims | Jev | 60 | 7 | 1 | 0 | 100% | 88% |
| Synthetic (swapped quotes, inverted verbs) | LLM | 63 | 63 | 0 | 0 | 100% | 100% |
| Synthetic | Jev | 63 | 63 | 0 | 0 | 100% | 100% |

| | LLM check | Jev |
|---|---|---|
| Calls | 1 for each summary | 1 for each claim, at the same time |
| Latency p50 / p95 for each call | 0.97 s / 2.48 s | 243 ms / 307 ms |
| Cost for each claim | USD 0.000066 | USD 0.000041 |

Caveats:

- The judge marks only 8 real claims for removal. 1 claim changes the recall by 12 points.
- The synthetic items are easy for both checkers. They show only that neither checker misses gross misuse.
- In production, the support check runs only on claims whose quote is not 1 exact sentence of the page.
- The Jev threshold (P(supported) at least 0.5) was set before the run and not changed.

## 5. Tests

On 2026-10-05, test collection gave these counts. The suites were not run for this document.

| Command | Tests collected |
|---|---|
| `.venv/bin/pytest -q` (unit, adversarial, review) | 1357 (unit 1301, adversarial 39, review 17) |
| `.venv/bin/pytest -m integration -q` | 183 (integration 116, reliability 60, review 7) |
| `.venv/bin/pytest -m live -q` | 4 |

The adversarial corpus has 29 hostile or unusual emails (`tests/adversarial/emails/`).

## 6. Procedures

WARNING: The eval runners send email text from the datasets to OpenRouter and TypeSafe. Use only the datasets in this repository. Do not add real emails of real people to a dataset.

CAUTION: The evals call paid APIs. The in-sample gate run cost USD 0.0132 for 210 model calls. 1 output eval run cost USD 0.38 to USD 0.82 for the judge. The fetch step visits the live portals.

### 6.1 Run the gate eval (LLM gate)

1. Go to the repository root. The runners read `.env` from the current directory.

   ```bash
   cd /home/deploy/senpilot-agent
   ```

2. Run the cases through the gate.

   ```bash
   .venv/bin/python -m evals.gate.run
   ```

   Expected result: the runner writes `evals/gate/raw.jsonl`.

3. Score the run.

   ```bash
   .venv/bin/python -m evals.gate.score
   ```

   Expected result: the runner writes `evals/gate/report.md` and `evals/gate/results.json`.

4. Compare the "Headline" and "Safety" tables with the earlier report. The wrong fetches and the invented matters must be 0.

### 6.2 Run the gate eval for Jev

NOTE: The shell of the operator can export an old `OPENROUTER_API_KEY`. `env -u` removes it, so the key in `.env` applies.

1. Run Jev on the in-sample dataset.

   ```bash
   env -u OPENROUTER_API_KEY .venv/bin/python -m evals.gate.run_jev run --variant jev
   ```

2. Run Jev on the held-out dataset.

   ```bash
   env -u OPENROUTER_API_KEY .venv/bin/python -m evals.gate.run_jev run --variant jev --dataset heldout
   ```

3. Run the LLM gate on the held-out dataset.

   ```bash
   env -u OPENROUTER_API_KEY .venv/bin/python -m evals.gate.run_jev run --variant llm --dataset heldout
   ```

4. Make the report. This step makes no API calls.

   ```bash
   .venv/bin/python -m evals.gate.run_jev report
   ```

   Expected result: the runner writes `evals/gate/report_jev.md` and `evals/gate/results_jev.json`.

The runner caches Jev answers in `evals/gate/jev_cache.jsonl`. Add `--fresh` to a `run` command to measure latency and consistency with live calls.

### 6.3 Run the output eval

1. Fetch the cases. This step visits the live portals for cases that are not in the cache.

   ```bash
   .venv/bin/python -m evals.output.fetch
   ```

2. Summarise, examine and judge each case.

   ```bash
   .venv/bin/python -m evals.output.run
   ```

   Expected result: the runner writes `evals/output/report.md` and `evals/output/results.json`.

3. To make the generator write all summaries again, add `--regen`.

   ```bash
   .venv/bin/python -m evals.output.run --regen
   ```

The runner caches generator and judge outputs. The cache key includes the source of `agent/citations/*.py` and `agent/llm.py`. After a code change, a plain run writes again only what changed.

### 6.4 Run the citation support check eval

1. Run both checkers. This step needs `evals/output/results.json` from section 6.3.

   ```bash
   env -u OPENROUTER_API_KEY .venv/bin/python -m evals.output.run_jev_check run
   ```

2. Make the report. This step makes no API calls.

   ```bash
   .venv/bin/python -m evals.output.run_jev_check report
   ```

   Expected result: the runner writes `evals/output/report_jev_check.md`.

### 6.5 When to run the evals

Run the gate eval and the output eval after each change to:

- a prompt, a rule or a threshold in `agent/gate/` or `agent/citations/`;
- `llm_models`, `typesafe_model` or a model provider;
- a provider category or a matter pattern.

Compare the new report with the last report before you merge the change.
