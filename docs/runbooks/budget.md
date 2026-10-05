# Runbook: daily budget high or exhausted

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentBudgetHigh` | `warning` | For 5 min, a daily budget is more than 80% used. |
| `RegagentBudgetExhausted` | `page` | A budget reached its limit in the last 1 h (`budget_exhausted_total`), or its use is at its limit now. |

Incident severity: SEV3. SEV2 if a budget reaches its limit early in the UTC day and requests wait.

The gauges `budget_used{budget}` and `budget_limit{budget}` show the budgets. All budgets reset at 00:00 UTC.

| Budget | Parameter | Default | What it counts |
|---|---|---|---|
| `llm_usd` | `LLM_DAILY_BUDGET_USD` | USD 2.00 | The cost of all model calls: OpenRouter and TypeSafe |
| `portal:uarb`, `portal:oeb`, `portal:ferc` | `PORTAL_DAILY_VISITS` | UARB 400, OEB 2000, FERC 2000 | Portal visits: a listing, a matter lookup or a download batch |

## 1. Find if the use is expected

1. Open the "Models" dashboard. Compare the spend for each day with the budget. Examine the calls and the errors for each model. Retries also cost money.
2. Open the "Regulator portals" dashboard. Compare the visits for each provider with the budget.
3. Open the "Abuse & rate limits" dashboard. Look for a burst from 1 sender or 1 domain. The limits for each sender and each domain must stop it. Refer to [Spoofing wave](spoofing-wave.md).

## 2. What occurs at 100%

| Budget | Result until 00:00 UTC |
|---|---|
| `llm_usd` | The agent makes no more model calls. The gate uses only its rules. Replies have no new summary. The documents still go out, and cached summaries are still used. |
| `portal:<provider>` | Requests that need that portal wait. The worker examines the budget again each 10 min. Each request that waits gets 1 "delayed" email. At the 2 h deadline, the requester gets 1 apology. |

The worker writes 1 `budget.exhausted` log line and 1 `budget_exhausted` audit event for each budget and day.

## 3. Increase a budget

CAUTION: Increase a budget only for a known and legitimate cause. A budget is also an abuse control.

1. Change the parameter in `.env`. Example: `PORTAL_DAILY_VISITS={"uarb": 600, "oeb": 2000, "ferc": 2000}`.
2. Write the env files for each service again.

   ```bash
   deploy/split-env.sh
   ```

3. Restart the worker with the new environment.

   ```bash
   docker compose up -d --no-deps worker
   ```

4. Record the change. Refer to [Change management](../policies/change-management.md).
