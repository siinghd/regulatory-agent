# Runbook: worker metrics down

| Alert | Alert severity | Condition |
|---|---|---|
| `RegagentWorkerMetricsDown` | `page` | For 5 min, Prometheus cannot scrape the worker on 127.0.0.1:9710 (`METRICS_PORT`). |

Incident severity: SEV2 until you know that the worker itself is healthy. Most app alerts have no data while this alert fires: queue, ingest heartbeat, breakers, budgets and failed requests.

## 1. Find if the fault is in the worker or only in its metrics

1. Examine the container.

   ```bash
   docker compose ps worker
   ```

2. Run the worker healthcheck.

   ```bash
   docker compose exec worker ragent healthcheck worker
   ```

3. Find a listener on port 9710.

   ```bash
   ss -ltn 'sport = :9710'
   ```

4. Read the metrics lines of the worker log.

   ```bash
   docker compose logs worker | grep -E 'metrics\.(listening|unavailable)'
   ```

   Expected result: a `metrics.listening` line with port 9710.

## 2. Repair

| Fault | Repair |
|---|---|
| The worker is down or unhealthy | Read `docker compose logs worker`. Then run `docker compose up -d --no-deps worker`. |
| The worker is healthy and the log has `metrics.unavailable` | A different process uses the port, for example a second worker on this host. Run `ss -ltnp 'sport = :9710'` to find the process. Stop it. Then restart the worker. |
| The worker is healthy and the log has no metrics line | `METRICS_PORT=0` is in `deploy/env/worker.env`, or the image is older than the metrics code. Correct the value or deploy a new image. Then restart the worker. |

## 3. After the repair

1. Show the state of the observability stack.

   ```bash
   make metrics-status
   ```

   Expected result: `regagent-worker` is up.

2. Open the "Overview" dashboard. Make sure that the panels show data again.
