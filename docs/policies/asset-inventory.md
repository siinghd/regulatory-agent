# Asset Inventory

This document is written in ASD-STE100 Simplified Technical English.

Owner: the system operator (`@siinghd`). Version 1.1. Effective 2026-10-04. Last reviewed 2026-10-05. SOC 2: CC6.1, CC3.2, A1.1.

The operator reviews this inventory each quarter, with the access review. The classes come from the [data classification](data-classification.md).

## 1. Infrastructure

| Asset | Details | Owner | Class | Notes |
|---|---|---|---|---|
| Main host `ubuntu-16gb-hel1-1` | Hetzner Cloud, Helsinki. Ubuntu 24.04.3 LTS, aarch64, 8 vCPU, 16 GB RAM, 150 GB disk (ext4, not encrypted). | Operator | Restricted | Other projects of the operator use the same host. This inventory covers only the directories and containers of the agent. ufw is active. SSH accepts only keys. |
| Egress VM `azuretest` | Azure, Canada. SOCKS endpoint for UARB through `uarb-egress-tunnel.service`. | Operator | Internal | Open: add the VM to the patch process, the access reviews and the weekly check |
| Docker Engine 29.1.3 | Container runtime. gVisor `runsc` is installed (runtimes `runsc`, `runsc-net`, `runsc-untrusted`). | Operator | Restricted | Membership of the `docker` group is equal to root access |
| Caddy | TLS origin for `uarb.hsingh.app` (Cloudflare Origin CA certificate). The configuration is in `/etc/caddy/sites.d/`. The copy in the repository is `deploy/caddy/uarb.caddy`. | Operator | Internal | Also gives the public read-only `/grafana/` path |
| Postfix and Dovecot | `mail.hsingh.app`, mailbox `agent@hsingh.app` | Operator | Restricted | Open: the configuration is not in this repository |

## 2. Application components (`docker-compose.yml`, project `regulatory-agent`)

| Service | Image | Role | Data access |
|---|---|---|---|
| postgres | `postgres:16-alpine@sha256:7218…80ea` | System of record. Volume `pgdata`. | All |
| redis | `redis:7-alpine@sha256:858f…3499` | Queue, locks, rate limits. Volume `redisdata`, AOF. | Temporary |
| migrate (one-shot) | `regulatory-agent` | Schema migrations as `agent_migrator` | DDL |
| db-grants (one-shot) | `postgres:16-alpine` | Applies `deploy/sql/grants.sql` again after each migration | Grants |
| ingest | `regulatory-agent` | Reads mail with IMAP IDLE. Metrics on 127.0.0.1:9711. | `data/` (writes `data/raw`), `agent_app` |
| worker | `regulatory-agent` | Gate, providers (Chromium), packages, models, outbound mail. Metrics on 127.0.0.1:9710. | `data/`, `agent_app`, mailbox, OpenRouter, TypeSafe, drop |
| web | `regulatory-agent` | Viewer, progress pages, `/status` and `/privacy` on 127.0.0.1:8710. `GET /metrics` for loopback clients only. | `data/blobs` read-only, `agent_web` |
| retention (profile `ops`) | `regulatory-agent` | `ragent purge` and `ragent dsar delete`. The timer `regagent-retention.timer` starts the purge each day at 03:30 UTC. | `data/`, `agent_retention` |
| prometheus | `prom/prometheus:v3.15.0` (digest pin) | Metrics on 127.0.0.1:9090. Volume `promdata` (30 days or 5 GB). | Aggregate metrics |
| alertmanager | `prom/alertmanager:v0.34.1` (digest pin) | Alert routing on 127.0.0.1:9093. Volume `alertmanagerdata`. | Alerts |
| grafana and grafana-init (one-shot) | `grafana/grafana-oss:13.0.2` (digest pin) | Dashboards on 127.0.0.1:3310. Public read-only through Caddy at `/grafana/`. Volume `grafanadata`. | Aggregate metrics |
| node-exporter, blackbox-exporter | `prom/node-exporter:v1.12.1`, `prom/blackbox-exporter:v0.28.0` (digest pins) | Host metrics (127.0.0.1:9100) and probes (127.0.0.1:9117) | Host metrics |
| postgres-exporter, redis-exporter (profile `db-exporters`) | `prometheuscommunity/postgres-exporter:v0.20.1`, `oliver006/redis_exporter:v1.93.0` (digest pins) | Database metrics (127.0.0.1:9187, 127.0.0.1:9121). Not started by default. | Statistics only (`agent_monitor`) |
| drop | A separate service on 127.0.0.1:3060 | Encrypted file delivery | Ciphertext |

The build makes the `regulatory-agent` image from this repository (`Dockerfile`, base `mcr.microsoft.com/playwright/python:v1.63.0-noble@sha256:72bd…a1f0`). The label `org.opencontainers.image.revision` of the image and `deploy/deploys.log` give the revision that runs.

NOTE: On 2026-10-05, `regagent-retention.timer` was not installed on the host. Until the operator installs it, run the purge by hand ([Operations](../guide/operations.md), section 7.1).

## 3. Data stores

| Store | Location | Class | Backup |
|---|---|---|---|
| Postgres database `agent` | Docker volume `regulatory-agent_pgdata` | Confidential | Nightly, encrypted |
| Raw MIME | `~/senpilot-agent/data/raw` | Confidential | Nightly, encrypted |
| Blobs (regulator files) | `~/senpilot-agent/data/blobs` | Public | Nightly, encrypted |
| Scratch files | `~/senpilot-agent/data/tmp` | Confidential (temporary) | No |
| Redis | Docker volume `regulatory-agent_redisdata` | Internal | No (the system makes it again) |
| Metrics, alerts, dashboards | Docker volumes `regulatory-agent_promdata`, `regulatory-agent_alertmanagerdata`, `regulatory-agent_grafanadata` | Internal | No |
| Backups | `/home/deploy/backups/regulatory-agent` (and R2: Open) | Confidential (age-encrypted) | Not applicable |
| Secrets | `~/senpilot-agent/.env`, `deploy/env/*.env`, `deploy/observability/.env`, and an offline escrow | Restricted | Escrow only |
| Evidence | `deploy/deploys.log`, `/home/deploy/compliance/`, incident and DSAR directories | Internal or Confidential | Open: with a backup of the home directory |

## 4. Domains, certificates and endpoints

| Name | Purpose | Certificate |
|---|---|---|
| `agent@hsingh.app` | The mailbox of the agent | Not applicable |
| `mail.hsingh.app` | IMAP and SMTP | Let's Encrypt, automatic renewal. The weekly check examines the expiry date. |
| `uarb.hsingh.app` | Viewer, `/status`, `/privacy` and `/grafana/` (through Cloudflare) | Cloudflare edge, and Cloudflare Origin CA (until 2035) |
| `drop.hsingh.app` | Encrypted delivery links | Cloudflare edge and origin |
| `hsingh.app` DNS | Cloudflare | SPF, DKIM and DMARC records for the mail domain |

## 5. Accounts and code

The accounts are Hetzner, Cloudflare (DNS, proxy, R2), Azure, OpenRouter, TypeSafe, GitHub (repository, Actions, Dependabot) and Let's Encrypt (ACME). The [vendor register](vendor-register.md) gives the details and the assurance. The [key inventory](encryption.md#4-key-inventory) gives the secrets for each account.
