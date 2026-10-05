## What and why

<!-- One paragraph: the change, the reason, the issue/incident it closes. -->

## Risk and rollback

- Risk: low / medium / high, because …
- Rollback: `deploy/deploy.sh --rollback --change <this PR>` / data migration to undo: …
- Emergency change? If this PR is the retrospective record of an emergency deploy, link the
  `deploys.log` line and the incident.

## Security checklist

- [ ] No secrets, tokens, real email addresses or personal data in code, tests, fixtures or logs
      (gitleaks runs in CI; fixtures use example.org / example.invalid).
- [ ] Untrusted input (email headers/bodies, portal HTML/JSON, PDFs, LLM output) is validated or
      fenced; the reply recipient still comes only from the authenticated sender.
- [ ] New or changed SQL keeps to the role model: the app needs no DELETE/DDL; `events` stays
      append-only; a new table the viewer reads is added to `deploy/sql/grants.sql`.
- [ ] New Redis commands are added to `deploy/redis/users.acl.template` and
      `deploy/validate_redis_acl.py`.
- [ ] New environment variables are placed in `deploy/env/services.toml` (which service gets them)
      and documented in `.env.example`; secrets are `SecretStr`.
- [ ] Dependencies changed only through `make lock` (hashed lockfiles), and pip-audit / Trivy pass.
- [ ] Data handling: anything new stored about senders has a retention rule
      (`docs/policies/data-retention.md`) and is not sent to a new vendor without a register entry.
- [ ] LLM changes: prompts keep untrusted text fenced, output is schema-validated, quotes grounded
      (`docs/policies/ai-use.md`).
- [ ] Tests added or updated; unit + integration pass in CI.

## Evidence

<!-- Test output, screenshots, `deploy/compliance_check.sh` lines touched by this change. -->
