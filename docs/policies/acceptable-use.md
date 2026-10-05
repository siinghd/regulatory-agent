# Acceptable Use Policy

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05. SOC 2: CC1.1, CC2.2, CC2.3, CC6.1.

## Part A: persons who operate or develop the system

Part A applies to the operator and to each person who gets access later (employees, contractors).

1. **Use only personal accounts.** Use your own SSH key and your own vendor accounts with MFA. Do not share credentials. Do not use the session of a different person.
2. **Keep production data in production.** Do not copy Confidential data (emails, request rows, raw MIME, dumps) to laptops, chat, tickets, AI tools or personal storage. Use the test fixtures to find bugs. If you must examine real data, do it on the host. Delete all temporary copies on the same day.
3. **Keep secrets in `.env`** and in the password manager. Do not put secrets in code, commits, command lines, screenshots, chat or AI prompts. If a secret escapes, use the [credential-leak runbook](../runbooks/credential-leak.md) immediately. A report of a mistake is always correct.
4. **Protect the devices** that administer the system. They must have full-disk encryption, a screen lock after 5 min or less, and automatic OS updates. Use a password manager. Do not leave an unlocked session with an SSH agent.
5. **Use the change process** ([change management](change-management.md)). Do not make manual changes on the host that are not in the repository. The only exception is a recorded emergency.
6. **Use break-glass access** (Postgres superuser, Redis admin, root) only for a planned change or an incident. Record each use.
7. **Use AI tools** only as the [AI use policy](ai-use.md) specifies.
8. **Report** a possible incident, phishing, a lost device or a gap in a policy to the operator (`security@hsingh.app`) immediately.

A breach of these rules causes the loss of access. The operator can report deliberate misuse to the authorities.

## Part B: persons who use the service

Part B applies to each person who sends email to `agent@hsingh.app` or uses `uarb.hsingh.app`.

1. Use the service to request public regulatory documents and information about them.
2. Do not send mail in the name of a different person.
3. Do not try to make the agent send email to third parties.
4. Do not send a flood of automatic requests. Do not try to bypass the authentication or the rate limits.
5. Do not send personal data of other persons, or confidential material that you have no right to share. The agent needs only a matter number and a document type.
6. Security research is welcome under [SECURITY.md](../../SECURITY.md). Obey its rules.
7. The operator can limit, ignore or block senders who do not obey these rules, without notice.
