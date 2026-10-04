"""arq worker: runs the pipeline for each request with retries, plus a sweeper for stuck work."""

from datetime import timedelta
from typing import ClassVar
from uuid import UUID

import structlog
from arq import Retry, cron
from arq.connections import RedisSettings

from agent import db, store
from agent.config import get_settings
from agent.delivery.drop import DropClient
from agent.limits import Limits
from agent.logs import configure_logging
from agent.mail.ingest import enqueue
from agent.models import AgentError
from agent.pipeline import Deps, process
from agent.providers.base import register
from agent.providers.browser import BrowserPool
from agent.providers.uarb import UarbProvider

log = structlog.get_logger()

MAX_TRIES = 6
# Retry spacing for a flaky portal: quick at first, then back off to ~10 minutes.
BACKOFF_S = [20, 60, 180, 420, 600]


async def startup(ctx: dict) -> None:
    configure_logging()
    s = get_settings()
    pool = await db.create_pool(max_size=20)
    async with pool.acquire() as conn:
        await db.migrate(conn)
    limits = Limits(ctx["redis"])
    browsers = BrowserPool(proxy=s.uarb_proxy, max_sessions=s.uarb_max_concurrent_sessions,
                           nav_timeout_ms=s.browser_nav_timeout_ms)
    uarb = UarbProvider(
        browsers,
        sessions_per_matter=s.uarb_sessions_per_matter,
        # the portal's shared download state is per client IP, so the lock is global to the egress
        download_lock=lambda: limits.lock("uarb:download", ttl_s=180, wait_s=600, poll_s=0.1),
    )
    register("uarb", lambda: uarb)
    ctx["browsers"] = browsers
    ctx["deps"] = Deps(settings=s, providers={"uarb": uarb}, limits=limits, drop=DropClient.from_settings(s))
    log.info("worker.ready")


async def shutdown(ctx: dict) -> None:
    await ctx["browsers"].close()
    if ctx["deps"].drop:
        await ctx["deps"].drop.aclose()
    await db.close_pool()


async def process_request(ctx: dict, request_id: str) -> None:
    rid = UUID(request_id)
    attempt = ctx["job_try"]
    await store.bump_attempts(rid)
    structlog.contextvars.bind_contextvars(request_id=request_id, attempt=attempt)
    try:
        await process(ctx["deps"], rid, final_attempt=attempt >= MAX_TRIES)
    except AgentError as e:
        if not e.retryable or attempt >= MAX_TRIES:
            raise
        delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
        log.warning("request.retry", error=str(e)[:300], defer_s=delay)
        raise Retry(defer=delay) from e
    finally:
        structlog.contextvars.clear_contextvars()


async def sweep(ctx: dict) -> None:
    """Re-enqueue requests that stopped moving (worker killed mid-job, Redis flushed, ...).

    The job id is the request id, so this never duplicates a job that is still queued/running.
    """
    for rid in await store.stuck_requests(timedelta(minutes=15)):
        await enqueue(ctx["redis"], rid)
        log.info("sweep.requeued", request_id=str(rid))


class WorkerSettings:
    functions: ClassVar = [process_request]
    cron_jobs: ClassVar = [cron(sweep, minute=set(range(0, 60, 5)), run_at_startup=True)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 12  # mostly waiting on the portal/LLM; browser sessions are capped separately
    job_timeout = 900
    max_tries = MAX_TRIES
    keep_result = 0  # so the sweeper can re-enqueue a finished request id if it ever needs to
    allow_abort_jobs = True
