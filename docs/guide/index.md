# Documentation map

This guide is for 2 groups of readers:

- Senpilot engineers who review this MVP.
- The operator who runs the agent.

NOTE: The agent is an MVP for evaluation. It is not a production service, and it has no service level agreement (SLA). Read [Limitations and disclaimers](limitations-and-disclaimers.md) first.

## Start here

| If you want to | Read |
|---|---|
| Know what the agent does and try it | [README](../../README.md) |
| Know why the agent has this design | [DESIGN.md](../../DESIGN.md) |
| Know what the agent cannot do, and the risks | [Limitations and disclaimers](limitations-and-disclaimers.md) |

## Guide for reviewers

| Document | Contents |
|---|---|
| [Architecture](architecture.md) | Processes, data stores, external services, networks, data flow |
| [Request flow](request-flow.md) | Each step from the inbound email to the reply, and the state machine |
| [Providers](providers.md) | UARB, OEB and FERC: matter formats, categories, downloads, portal problems, and how to add a provider |
| [Security](security.md) | Threat model and controls |
| [Reliability](reliability.md) | Retries, backoff formulas, circuit breakers, timeouts, outbox, idempotency |
| [Abuse protection](abuse-protection.md) | Each limit, its default value and the result when a sender is over it |
| [Quality and evals](quality-and-evals.md) | The gate eval and the output eval: method, results, caveats, how to run them |
| [Models](models.md) | TypeSafe Jev and DeepSeek: roles, reasons, numbers, privacy |
| [Optimizations](optimizations.md) | Each optimization and its measured effect |
| [Decisions log](decisions-log.md) | Short records of each design decision |

## Guide for the operator

| Document | Contents |
|---|---|
| [Operations](operations.md) | Procedures: deploy, cutover, backup, restore, purge, DSAR, kill switch, health checks |
| [Observability](observability.md) | Metrics, logs, audit trail, dashboards and alerts |
| [Glossary](glossary.md) | Terms and abbreviations |

## Other documents in this repository

| Location | Contents | Owner |
|---|---|---|
| `docs/policies/` | Security, privacy and operations policies | Not part of this guide |
| `docs/runbooks/` | Incident runbooks. Each alert rule links to 1 runbook (`runbook_url`). Other runbooks: credential leak, spoofed mail wave, LLM provider incident, wrong documents, least-privilege cutover. | Not part of this guide |
| `docs/soc2/` | SOC 2 system description and control matrix | Not part of this guide |
| `SECURITY.md` | How to report a vulnerability | Not part of this guide |
| `evals/` | Eval datasets, runners and reports | Source for [Quality and evals](quality-and-evals.md) |

NOTE: On 2026-10-05, a review made `docs/policies/`, `docs/runbooks/` and `docs/soc2/` agree with the code. When a policy and the code do not agree, the code is the current state. This guide describes the code.

## Conventions

- Names in `code font` are file names, commands, parameters, states or identifiers.
- Parameter names are in lower case in `agent/config.py`. The same names in upper case are environment variables (for example `max_attempts` and `MAX_ATTEMPTS`).
- All times are UTC. All "day" budgets reset at 00:00 UTC.
- "WARNING" tells about a risk to people or data. "CAUTION" tells about a risk to equipment or to the system. "NOTE" gives more information.
- Numbers come from the code, the eval reports, the README or measurements on the live system. Most tables name their source. Some measurements have no report in the repository. The text says so.
