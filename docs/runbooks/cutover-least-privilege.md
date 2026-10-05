# Runbook: 1-time cutover to least-privilege roles, Redis ACL and hardened containers

This document is written in ASD-STE100 Simplified Technical English.

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentRedisUnauthenticatedAccess` | `warning` | For 10 min, Redis answers an unauthenticated PING on 127.0.0.1:6392 and does not refuse it with `NOAUTH`. |

This alert is expected until `deploy/redis-cutover.sh` has run. After the cutover, the alert is a regression: start at section 4 of this runbook.

This cutover is a planned normal change ([Change management](../policies/change-management.md)). The maintenance window is approximately 15 min. [Operations, section 4](../guide/operations.md#4-cutover-to-least-privilege-roles-and-the-redis-acl) gives a short form of these steps.

| | Before the cutover | After the cutover |
|---|---|---|
| Postgres | All processes connect as the bootstrap superuser. | 1 role for each process. Postgres refuses the superuser over TCP. |
| Redis | No password | An ACL. The default user is off. |
| Secrets | All containers read `.env`. | 1 env file for each service (`deploy/env/<service>.env`) |
| Containers | Writable, with the default capabilities | Read-only, no capabilities, with healthchecks, and a 1-time `migrate` service |

The developer rehearsed all steps on a throwaway stack. The stack was a restore of a dump of the live database (2026-10-04).

NOTE: On 2026-10-05, the cutover was not done on the live host. `.env` had no `WEB_DATABASE_URL` and no `REDIS_ADMIN_PASSWORD`.

## 0. Prerequisites

The code prerequisites are done:

| Prerequisite | State |
|---|---|
| `store.get_by_token` reads only named columns (`PROGRESS_COLUMNS`). The web role can read only these columns of `requests`. | Done |
| The app processes do not run migrations at start. `ragent migrate` uses `MIGRATION_DATABASE_URL`. | Done |
| Ingest writes `ingest:heartbeat` at least each 60 s while IMAP is healthy. The worker has `health_check_interval = 60`. | Done. `deploy/deploy.sh` can wait for `worker ingest web` (the default). |

1. Commit all changes. For a normal change, `deploy/deploy.sh` refuses uncommitted changes.

## 1. Prepare (no effect on the service)

1. Go to the repository root.

   ```bash
   cd ~/senpilot-agent
   ```

2. Make a fresh, restore-tested backup.

   ```bash
   deploy/backup.sh && tail -1 ~/backups/regulatory-agent/backup.log
   ```

   Expected result: a new line with `ok`.

3. Build and test the candidate image.

   ```bash
   make build test-image integration
   ```

## 2. Start of the window: stop the app

Mail waits in IMAP while the app is stopped.

1. Stop the app processes.

   ```bash
   docker compose stop ingest worker web
   ```

## 3. Secrets and roles

1. Make the database roles and write their DSNs into `.env`. The script also makes the tables the property of `agent_owner` and examines the result.

   ```bash
   deploy/db-cutover.sh
   ```

2. Make the Redis passwords and the new `REDIS_URL` (user `agent`).

   ```bash
   deploy/redis-cutover.sh
   ```

3. Add `AUDIT_HMAC_KEY` and `DATA_ENCRYPTION_KEY` if they are missing. The loop does not change a key that exists.

   ```bash
   for k in AUDIT_HMAC_KEY DATA_ENCRYPTION_KEY; do
     python3 deploy/lib/envtool.py get .env $k >/dev/null ||
       printf '%s=%s\n' $k "$(python3 deploy/lib/envtool.py gen)" | python3 deploy/lib/envtool.py set .env
   done
   ```

4. Write the env files for each service: `deploy/env/{ingest,worker,web,migrate,db-grants,retention}.env`.

   ```bash
   deploy/split-env.sh
   ```

5. Remove the access of other users to the data directory.

   ```bash
   mkdir -p data/tmp && chmod -R o-rwx data
   ```

6. Put a copy of the new `.env` in the password manager now.

## 4. Data stores with the hardened configuration

1. Start Postgres and Redis again. Compose makes new containers with `pg_hba.conf`, fewer capabilities, a read-only file system and the ACL.

   ```bash
   docker compose up -d --wait postgres redis
   ```

2. Prove the database roles.

   ```bash
   deploy/verify-db-roles.sh
   ```

   Expected result: `PASS bootstrap superuser is refused over TCP` and `ALL ROLE CHECKS PASSED`.

3. Prove the Redis ACL for arq, the limits and the breakers, and the refused commands.

   ```bash
   make validate-redis
   ```

   Expected result: `ALL REDIS ACL CHECKS PASSED`.

4. Write test keys, restart Redis, and make sure that the keys are still there after the AOF reload.

   ```bash
   export REDIS_URL=$(python3 deploy/lib/envtool.py get .env REDIS_URL)
   .venv/bin/python deploy/validate_redis_acl.py --persist-write && docker compose restart redis \
     && .venv/bin/python deploy/validate_redis_acl.py --persist-check; unset REDIS_URL
   ```

5. Clear the ACL log as the `admin` user.

   ```bash
   REDISCLI_AUTH=$(python3 deploy/lib/envtool.py get .env REDIS_ADMIN_PASSWORD) \
     docker compose exec -e REDISCLI_AUTH redis redis-cli --user admin --no-auth-warning acl log reset
   ```

## 5. Deploy the application

1. Deploy the candidate.

   ```bash
   deploy/deploy.sh --change "<PR or commit>: least-privilege cutover"
   ```

   The script builds, tests and scans the candidate. It promotes the candidate and keeps the old image as `:previous`. It runs `migrate` and `db-grants`. It restarts `worker`, `web` and `ingest` and waits until they are healthy. It tests `/health` on the host and on `https://uarb.hsingh.app`. It records the deploy.

2. Send a real request from a known sender.
3. Watch the request until it is `done`.

   ```bash
   docker compose logs -f worker
   ```

4. Open the progress link of the request. Make sure that the page shows the request.

## 6. Host timers, backups, compliance

1. Install the compliance and retention units.

   ```bash
   sudo cp deploy/regagent-compliance.service deploy/regagent-compliance.timer \
     deploy/regagent-retention.service deploy/regagent-retention.timer /etc/systemd/system/
   ```

2. Load the units and start the timers.

   ```bash
   sudo systemctl daemon-reload && sudo systemctl enable --now regagent-compliance.timer regagent-retention.timer
   ```

   The retention timer runs `ragent purge` each day at 03:30 UTC as `agent_retention`.

3. Make a backup. The dump now uses the role `agent_backup`.

   ```bash
   deploy/backup.sh && tail -1 ~/backups/regulatory-agent/backup.log
   ```

4. Do a dry run of the purge with the new role.

   ```bash
   docker compose --profile ops run --rm retention purge --dry-run
   ```

   Expected result: a JSON object with counts.

5. Run the technical control checks.

   ```bash
   deploy/compliance_check.sh
   ```

   Expected result: no `FAIL` line.

## 7. Rotate the old superuser password

Do this step 1 day after the cutover, after normal operation. The old value was in the environment of each app container.

CAUTION: The rotation makes a new Postgres container, because its environment changes. The database is not available for a few seconds.

1. Rotate the superuser password, write the env files again and start Postgres.

   ```bash
   deploy/db-cutover.sh --rotate-superuser && deploy/split-env.sh && docker compose up -d --wait postgres
   ```

2. Prove the database roles again.

   ```bash
   deploy/verify-db-roles.sh
   ```

## 8. security.txt and access log retention (done)

This step is done in the code and in the Caddy configuration. No action is necessary.

- The web process serves `/.well-known/security.txt` (`agent/web/app.py`). Caddy needs no special block for it.
- The access log of `uarb.hsingh.app` has `roll_keep_for 720h` (30 days) in `deploy/caddy/uarb.caddy` and in `/etc/caddy/sites.d/uarb.caddy`. The earlier value was `2160h` (90 days).

1. After the deploy, make sure that the file is available.

   ```bash
   curl -s https://uarb.hsingh.app/.well-known/security.txt
   ```

   Expected result: the file starts with `Contact:`.

## Rollback

Until step 7, you can fully reverse the cutover.

1. Stop the app processes.

   ```bash
   docker compose stop ingest worker web
   ```

2. Copy the `.env` from before the cutover. Step 3 made this copy.

   ```bash
   cp deploy/env/backups/<stamp>-db-cutover.env .env
   ```

3. Get the `docker-compose.yml` from before the cutover with `git stash` or `git checkout`. Select the method that agrees with the state of your Git checkout.
4. Start the services.

   ```bash
   docker compose up -d
   ```

The new roles can stay, unused. The tables now belong to `agent_owner`. The superuser ignores ownership, so the old code continues to operate.

After step 7, the `.env` copy has an old `POSTGRES_PASSWORD`. Continue with the new configuration, or set the old password again:

```bash
docker compose exec postgres psql -U agent -c "\password agent"
```
