# Change Management Policy

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05. SOC 2: CC8.1, CC3.4, CC5.3.

## 1. Changes in scope

A change is each modification to what runs, or to how it runs:

- application code, dependencies and lockfiles;
- the Dockerfile and the base images;
- `docker-compose.yml` and all files in `deploy/`;
- database migrations and grants, and the Redis ACL;
- secrets in `.env`;
- LLM models, Jev versions, prompts and thresholds;
- the configuration of Caddy, Postfix, Dovecot, ufw and sshd on the host;
- a new vendor.

## 2. Normal change

1. Make a branch. Open a pull request (PR) with the template `.github/pull_request_template.md`.
2. In the PR, write what changes and why, the risk, the rollback and the security checklist.
3. Make sure that CI passes (`.github/workflows/ci.yml`). Section 2.1 lists the CI checks.
4. Review and merge the PR. While there is 1 person, section 5 applies.
5. Deploy with `deploy/deploy.sh --change <PR number or commit>`. Section 2.2 lists what the script does.
6. Do not record the evidence by hand. The script writes it (section 2.2).

### 2.1 CI checks

CI runs on each push to `main` or `master`, on each PR and 1 time each week:

- ruff;
- unit and adversarial tests;
- integration tests against real Postgres and Redis, with the Postgres role model;
- pip-audit on the hashed lockfiles, and a check that the lockfiles agree with `pyproject.toml`;
- gitleaks over the full Git history;
- Trivy on the repository, and on the built image (HIGH or CRITICAL with a fix makes CI fail);
- an image build with the commit SHA, and the unit tests inside the image.

### 2.2 Deploy script

`deploy/deploy.sh` does these steps:

1. It stops if the repository has uncommitted changes.
2. It builds `regulatory-agent:candidate` and runs the tests inside it.
3. It scans the candidate with Trivy.
4. It keeps the current image as `regulatory-agent:previous`.
5. It runs the migrations as `agent_migrator` and applies the grants again.
6. It restarts the services 1 at a time and waits for each healthcheck.
7. It does a smoke test of `/health` on the local address and on the public address.
8. If a step after the promotion fails, it puts `:previous` into service again.

The evidence comes from the script:

- a line in `deploy/deploys.log` (time, result, stage, SHA, dirty flag, user, change reference, image id);
- a `deploy` event in the Postgres audit log;
- the CI run and the PR.

Schema changes are idempotent SQL files in `migrations/`. Only the one-shot `migrate` service applies them, as `agent_migrator`. The application roles cannot run DDL. For a migration with a high risk (rewrites, drops), run `deploy/backup.sh` by hand first. Also write in the PR how to undo the migration.

## 3. Standard (pre-approved) changes

These changes have a low risk. They use the normal flow, but they need no separate discussion of the risk:

- Dependabot patch and minor updates that pass CI;
- certificate renewals (automatic);
- the rotation of a secret with the documented procedure ([encryption](encryption.md));
- a new row in the vendor register for a vendor that receives no Confidential data.

## 4. Emergency change

Use an emergency change for an active incident, an exploited vulnerability or an outage. Use it only when the normal flow is too slow.

1. Deploy with the emergency prefix.

   ```bash
   deploy/deploy.sh --change "emergency:<incident or reason>" [--allow-dirty] [--skip-scan]
   ```

   The tests still run. The script refuses `--allow-dirty` and `--skip-scan` without the `emergency:` prefix. Thus, `deploys.log` and the audit log identify each emergency deploy.

2. Use `--skip-scan` only when the scanner itself is the problem. An example is a scanner database that is not available.
3. Within 2 business days, open a retrospective PR. Add the same diff or the deployed commit, and a link to the incident record.
4. Make sure that CI runs on the retrospective PR.

The monthly reconciliation (section 5) makes sure that each `emergency:` line has its retrospective PR.

## 5. Substitute controls for 1 operator

There is 1 person, so nobody independent approves a change before the deploy. Until there is a second person, these controls apply:

| Control | How | Evidence |
|---|---|---|
| Machine gates in place of a second approver | Required CI checks on the default branch. No direct pushes and no force pushes. | GitHub branch protection (Open: configure it when the repository is on GitHub) |
| Deploys only through the script | `deploy/deploy.sh` records each attempt, successful or not | `deploy/deploys.log`, `deploy` events |
| Drift detection | The weekly `deploy/compliance_check.sh` finds deploy configuration that is not committed, containers that `docker compose up` replaces, and an image that is not from the last recorded deploy | `/home/deploy/compliance/compliance-<date>.txt` (Partial: `regagent-compliance.timer` was not installed on the host on 2026-10-05) |
| A wait period for changes with a high risk | Changes to mail authentication, outbound mail, cryptography, roles and ACLs, or the deploy tools wait 24 h between the PR and the merge. Then the operator reviews them again against the checklist. This does not apply to emergencies. | PR timestamps |
| Automatic review | An LLM code review on diffs with a high risk, recorded on the PR. It finds mistakes, but it is not an independent approver. | PR comments |
| Monthly reconciliation | Each `result=ok` line in `deploys.log` has a merged PR. Each `emergency:` line has its retrospective PR. | A monthly note in the PR history |

When a second person joins, do these steps:

1. Add the person to the security-sensitive paths in `.github/CODEOWNERS`.
2. Set branch protection to require 1 approval from a reviewer.
3. Stop the 24 h wait period.

## 6. Rollback

`deploy/deploy.sh --rollback --change <ref>` tags `regulatory-agent:previous` as `:latest` again. Then it restarts the services with the same health waits. Migrations are additive, so the previous image operates with the newer schema. If a migration is not additive, its PR must tell how to return to the previous version.
