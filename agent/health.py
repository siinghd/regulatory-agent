"""Health checks: `ragent healthcheck web|worker|ingest` (container healthchecks; exit 0 = healthy),
the facts behind /health/deep (loopback only) and the components of the public /status page.

- web: GET 127.0.0.1:{web_port}/health answers with db true;
- worker: arq's health key exists (the worker rewrites it every health_check_interval with a TTL
  of one interval, so a worker whose loop stalls loses it);
- ingest: Redis `ingest:heartbeat` (unix time, written every IMAP loop iteration, at least every
  IDLE cycle of 5 minutes) is younger than INGEST_MAX_AGE_S.
"""

import asyncio
import json
import shutil
import time
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import structlog
from arq.constants import default_queue_name, health_check_key_suffix
from redis.asyncio import Redis

from agent.admin import PAUSE_KEY
from agent.config import Settings, get_settings
from agent.mail.ingest import HEARTBEAT_KEY

log = structlog.get_logger()

INGEST_HEARTBEAT_KEY = HEARTBEAT_KEY
INGEST_MAX_AGE_S = 600
WORKER_HEALTH_KEY = default_queue_name + health_check_key_suffix
TIMEOUT_S = 3.0


def _redis(s: Settings) -> Redis:
    return Redis.from_url(s.redis_url, socket_timeout=TIMEOUT_S, socket_connect_timeout=TIMEOUT_S)


async def ingest_heartbeat_age(redis: Redis) -> float | None:
    raw = await redis.get(INGEST_HEARTBEAT_KEY)
    if raw is None:
        return None
    try:
        return max(time.time() - float(raw), 0.0)
    except ValueError:
        return None


def _check_web(s: Settings) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{s.web_port}/health"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            body = json.load(r)
    except (OSError, ValueError) as e:
        return False, f"{url}: {type(e).__name__}: {e}"
    ok = bool(body.get("ok") and body.get("db"))
    return ok, f"{url}: {body}"


async def _check_worker(s: Settings) -> tuple[bool, str]:
    redis = _redis(s)
    try:
        info = await redis.get(WORKER_HEALTH_KEY)
    finally:
        await redis.aclose()
    if not info:
        return False, f"{WORKER_HEALTH_KEY} missing: no worker has reported within its health interval"
    return True, info.decode(errors="replace")


async def _check_ingest(s: Settings) -> tuple[bool, str]:
    redis = _redis(s)
    try:
        age = await ingest_heartbeat_age(redis)
    finally:
        await redis.aclose()
    if age is None:
        return False, f"{INGEST_HEARTBEAT_KEY} missing"
    return age < INGEST_MAX_AGE_S, f"heartbeat {age:.0f}s ago (limit {INGEST_MAX_AGE_S}s)"


async def healthcheck(service: str, settings: Settings | None = None) -> tuple[bool, str]:
    s = settings or get_settings()
    try:
        if service == "web":
            return await asyncio.to_thread(_check_web, s)
        if service == "worker":
            return await _check_worker(s)
        if service == "ingest":
            return await _check_ingest(s)
    except Exception as e:  # noqa: BLE001 - any failure to check is "unhealthy", with the reason
        return False, f"{type(e).__name__}: {e}"
    raise ValueError(f"unknown service {service!r}")


# ------------------------------------------------------------------ deep health


def last_backup(path: str) -> dict:
    """Age and status of the newest line of the backup log (`<ISO time> ok ...`)."""
    try:
        lines = Path(path).read_text().strip().splitlines()
    except OSError as e:
        return {"readable": False, "error": type(e).__name__}
    if not lines:
        return {"readable": True, "age_s": None}
    stamp, _, rest = lines[-1].partition(" ")
    try:
        at = datetime.fromisoformat(stamp)
    except ValueError:
        return {"readable": True, "age_s": None, "unparsed": True}
    return {"readable": True, "age_s": round((datetime.now(UTC) - at).total_seconds()),
            "ok": rest.startswith("ok")}


def disk_free(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


async def redis_facts(s: Settings) -> dict:
    redis = _redis(s)
    try:
        await redis.ping()
        age = await ingest_heartbeat_age(redis)
        return {
            "ok": True,
            "queue_depth": await redis.zcard(default_queue_name),
            "ingest_heartbeat_age_s": round(age) if age is not None else None,
            "worker_reporting": bool(await redis.exists(WORKER_HEALTH_KEY)),
            "paused": bool(await redis.exists(PAUSE_KEY)),
        }
    except Exception as e:  # noqa: BLE001 - reported, not raised
        return {"ok": False, "error": type(e).__name__}
    finally:
        await redis.aclose()


async def component_facts(s: Settings, dependencies: Sequence[str]) -> dict:
    """What the public /status page says about the processes: the ingest heartbeat's age, whether
    a worker reports, and which of `dependencies` have their circuit breaker open. Never raises:
    {"ok": False} when Redis can't be read within TIMEOUT_S."""
    redis = _redis(s)
    try:
        async with asyncio.timeout(TIMEOUT_S):
            age = await ingest_heartbeat_age(redis)
            worker = bool(await redis.exists(WORKER_HEALTH_KEY))
            now = time.time()
            breakers_open = [d for d in dependencies
                             if float(await redis.hget(f"breaker:{d}", "open_until") or 0) > now]
        return {"ok": True, "ingest_heartbeat_age_s": age, "worker_reporting": worker, "breakers_open": breakers_open}
    except Exception as e:  # noqa: BLE001 - reported as unknown, not raised
        log.warning("health.component_facts_failed", error=type(e).__name__)
        return {"ok": False}
    finally:
        await redis.aclose()
