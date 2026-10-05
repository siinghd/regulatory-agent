"""The job queue (arq on Redis): connection settings, the JSON job format and job ids in one place.

Jobs are JSON, never pickle: anything that can write to Redis must not be able to run code in
a worker. Every pool that enqueues (worker, ingest, tests) uses `create_pool` here.
"""

import json
from typing import Any
from uuid import UUID

from arq import create_pool as arq_create_pool
from arq.connections import ArqRedis, RedisSettings

from agent.config import get_settings

REDIS_SOCKET_TIMEOUT_S = 5.0


def serialize(obj: Any) -> bytes:
    # default=repr: arq stores a failed job's exception as its result; it must not break the queue
    return json.dumps(obj, default=repr, separators=(",", ":")).encode()


def deserialize(data: bytes) -> Any:
    return json.loads(data)


def redis_settings(url: str | None = None) -> RedisSettings:
    return RedisSettings.from_dsn(url or get_settings().redis_url)


def apply_socket_timeout(redis: ArqRedis, seconds: float = REDIS_SOCKET_TIMEOUT_S) -> ArqRedis:
    """arq's RedisSettings has no read timeout: a hung Redis would hang every caller. Set one on
    the pool (new connections) and on the connections it already holds."""
    pool = redis.connection_pool
    pool.connection_kwargs["socket_timeout"] = seconds
    for conn in [*getattr(pool, "_available_connections", ()), *getattr(pool, "_in_use_connections", ())]:
        conn.socket_timeout = seconds
    return redis


async def create_pool(url: str | None = None) -> ArqRedis:
    redis = await arq_create_pool(redis_settings(url), job_serializer=serialize, job_deserializer=deserialize)
    return apply_socket_timeout(redis)


def request_job_id(request_id: UUID | str) -> str:
    return f"req:{request_id}"


def outbound_job_id(message_id: str) -> str:
    return f"out:{message_id}"


async def enqueue_request(redis: ArqRedis, request_id: UUID | str, *, defer_s: float | None = None) -> None:
    """Idempotent: the job id is the request id, so a queued or running job is never duplicated."""
    await redis.enqueue_job("process_request", str(request_id), _job_id=request_job_id(request_id),
                            _defer_by=defer_s or None)


async def enqueue_outbound(redis: ArqRedis, message_id: str, *, defer_s: float | None = None) -> None:
    await redis.enqueue_job("send_outbound", message_id, _job_id=outbound_job_id(message_id),
                            _defer_by=defer_s or None)
