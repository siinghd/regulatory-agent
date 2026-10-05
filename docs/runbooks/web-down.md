# Runbook: viewer (web) or drop health fails

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentWebHealthDown` | `page` | For 5 min, the probe `uarb-health` (https://uarb.hsingh.app/health through Cloudflare and Caddy) or `web-local-health` (http://127.0.0.1:8710/health on the host) fails. |
| `RegagentDropHealthDown` | `warning` | For 5 min, https://drop.hsingh.app/health fails. |

Incident severity: SEV2 for the viewer, because citation links and progress pages do not operate. SEV3 for drop.

## 1. Find the location of the fault

| `uarb-health` | `web-local-health` | Location of the fault |
|---|---|---|
| Down | Up | Cloudflare, DNS or Caddy. Run `systemctl status caddy` and read `/var/log/caddy/caddy.log`. |
| Down | Down | The web process or its database |

1. Examine the web process on the host.

   ```bash
   curl -s 127.0.0.1:8710/health
   ```

   Expected result: `{"ok": true, "db": true}`.

2. Examine the container and read its log.

   ```bash
   docker compose ps web && docker compose logs --since 30m web | tail -50
   ```

## 2. Repair

| Fault | Repair |
|---|---|
| `"db": false` | Postgres. Refer to [Postgres down](postgres-down.md). |
| The web container is unhealthy | Run `docker compose up -d --no-deps web`. |
| Caddy | Do the 2 steps below. |
| drop | Drop is a separate service on 127.0.0.1:3060 (`drop.hsingh.app`). Refer to the text below. |

To repair Caddy, do these steps:

1. Validate the configuration.

   ```bash
   sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   ```

2. Reload Caddy.

   ```bash
   sudo timeout 150 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force
   ```

The site file is `deploy/caddy/uarb.caddy`. The installed copy is in `/etc/caddy/sites.d/`.

While drop is down, the agent attaches a ZIP to the reply if the ZIP is not larger than 7,000,000 bytes. A larger ZIP cannot go out. Its request retries, or parks while the `drop` breaker is open. At the 2 h deadline, the requester gets 1 apology.
