"""arq worker: runs the pipeline for each request with retries, delivers the outbox, and sweeps
up stuck work.

Retry policy (attempts are counted in Postgres, because arq's job_try restarts at 1 whenever
the sweeper re-enqueues a job):
- an attempt is final when requests.attempts >= max_attempts or the request is older than
  request_deadline_s; a final attempt that fails ends with one apology (to a verified sender);
- backoff = max(retry_after, U(0.5, 1) * min(retry_cap_s, retry_base_s * 2^(n-1)));
- MatterNotFound, ProviderRejected, TooLarge and DeliveryFailed are never retried;
- a parked try (a dependency's breaker is open, the request or its single-flight is locked)
  is deferred without using an attempt;
- infrastructure errors (Postgres, Redis, OS) anywhere, error handling included, become a
  retry rather than an arq failure. arq never decides on its own: its max_tries is effectively
  unbounded, and on its very last try nothing raises Retry.
"""

import random
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from uuid import UUID

import asyncpg
import structlog
from arq import Retry, cron
from arq.worker import func
from redis.exceptions import RedisError

from agent import (
    audit,
    breaker,
    db,
    limits,
    llm,
    metrics,
    outbox,
    queue,
    reconcile,
    retention,
    store,
    typesafe,
)
from agent.config import Settings, get_settings
from agent.delivery.drop import DropClient
from agent.limits import Limits, LockTimeout
from agent.logs import configure_logging
from agent.models import AgentError
from agent.pipeline import Deps, Parked, finish_exhausted, process, retry_cause
from agent.providers.base import Provider, register
from agent.providers.browser import BrowserPool
from agent.providers.ferc import FercProvider
from agent.providers.ferc import make_client as make_ferc_client
from agent.providers.oeb import OebProvider, make_client
from agent.providers.uarb import UarbProvider

log = structlog.get_logger()

ARQ_MAX_TRIES = 1000  # arq never gives up by itself; attempts and the deadline (in Postgres) do
REQUEST_LOCK_TTL_S = 120  # renewed while held: a crashed worker frees the request within 2 min
REQUEST_LOCK_WAIT_S = 2
LOCK_BUSY_DEFER_S = 30.0
STUCK_AFTER = timedelta(minutes=15)
SWEEP_JITTER_S = 60.0  # spread re-enqueued work so a sweep doesn't stampede the portal
OUTBOUND_OVERDUE = timedelta(minutes=5)
INFRA_ERRORS = (OSError, asyncpg.PostgresError, asyncpg.InterfaceError, RedisError)


class Park(Retry):
    """Retry later without having used an attempt (arq treats it as any Retry)."""


def backoff_s(attempt: int, retry_after: float | None = None, settings: Settings | None = None) -> float:
    s = settings or get_settings()
    step = min(s.retry_cap_s, s.retry_base_s * 2 ** max(attempt - 1, 0))
    return max(retry_after or 0.0, random.uniform(0.5, 1.0) * step)


async def startup(ctx: dict) -> None:
    configure_logging()
    s = get_settings()
    queue.apply_socket_timeout(ctx["redis"])
    await db.create_pool(max_size=20)  # schema changes are `ragent migrate`'s job, as the owner role
    limiter = Limits(ctx["redis"])
    breakers = breaker.Breakers(ctx["redis"], s)
    breaker.install(breakers)
    browsers = BrowserPool(proxy=s.uarb_proxy, max_sessions=s.uarb_max_concurrent_sessions,
                           nav_timeout_ms=s.browser_nav_timeout_ms)
    uarb = UarbProvider(
        browsers,
        sessions_per_matter=s.uarb_sessions_per_matter,
        # the portal's shared download state is per client IP, so the lock is global to the egress
        download_lock=lambda: limiter.lock("uarb:download", ttl_s=180, wait_s=600, poll_s=0.1),
    )
    ctx["oeb_client"] = make_client(s.oeb_proxy)
    oeb_provider = OebProvider(ctx["oeb_client"], max_concurrency=s.oeb_max_concurrency)
    ctx["ferc_client"] = make_ferc_client(s.ferc_proxy)
    ferc_provider = FercProvider(ctx["ferc_client"], max_concurrency=s.ferc_max_concurrency)
    providers: dict[str, Provider] = {"uarb": uarb, "oeb": oeb_provider, "ferc": ferc_provider}
    for name, provider in providers.items():
        register(name, lambda p=provider: p)
    ctx["browsers"] = browsers
    ctx["deps"] = Deps(settings=s, providers=providers, limits=limiter, drop=DropClient.from_settings(s),
                       queue=ctx["redis"], breakers=breakers)
    limits.install(ctx["deps"].budgets)  # every LLM call agent.audit records counts against the daily budget
    metrics.start_server(s.metrics_port)
    # The gate and the summaries run here: refuse any model without a zero-data-retention endpoint
    # before the first request, and say which remain (none: rules-only gate, no summaries).
    await llm.check_zero_data_retention()
    log_model_policy(s)
    log.info("worker.ready")


def log_model_policy(s: Settings) -> None:
    """Which model does what, and where data goes that isn't zero-retention. llm_zero_data_retention
    governs OpenRouter routing only; TypeSafe is outside it by an owner decision (disclosed in the
    privacy notice), so every start says so."""
    log.info("worker.models", gate_classifier=s.gate_classifier, gate_jev_low_confidence=s.gate_jev_low_confidence,
             citation_check=s.citation_check, check_support=s.llm_check_support, typesafe_model=s.typesafe_model,
             llm_models=llm.usable_models(), llm_zero_data_retention=s.llm_zero_data_retention)
    if s.gate_classifier == "jev":
        log.info("typesafe.data_policy", purpose="gate", zdr=False,
                 note="TypeSafe Jev: email text is sent to TypeSafe for classification; not zero-retention")
    if s.citation_check == "jev" and s.llm_check_support:
        log.info("typesafe.data_policy", purpose="support_check", zdr=False,
                 note="TypeSafe Jev: public document excerpts are sent to TypeSafe for citation checks; "
                 "not zero-retention")
    if "jev" in (s.gate_classifier, s.citation_check) and not s.typesafe_api_key.get_secret_value():
        log.warning("typesafe.no_api_key", effect="every Jev call fails over to the LLM (TYPESAFE_API_KEY unset)")


async def shutdown(ctx: dict) -> None:
    await ctx["browsers"].close()
    await ctx["oeb_client"].aclose()
    await ctx["ferc_client"].aclose()
    await typesafe.aclose()
    if ctx["deps"].drop:
        await ctx["deps"].drop.aclose()
    breaker.install(None)
    limits.install(None)
    await db.close_pool()


async def process_request(ctx: dict, request_id: str) -> None:
    rid = UUID(request_id)
    deps: Deps = ctx["deps"]
    s = deps.settings
    job_try = ctx.get("job_try", 1)
    attempt = job_try  # until Postgres tells us (it may be down)
    structlog.contextvars.bind_contextvars(request_id=request_id, job_try=job_try)
    try:
        # One job per request at a time. arq's job id stops duplicate *queued* jobs, but a
        # sweeper re-enqueue can still overlap a slow running job; the lock closes that gap.
        async with deps.limits.lock(f"request:{rid}", ttl_s=REQUEST_LOCK_TTL_S, wait_s=REQUEST_LOCK_WAIT_S):
            started = await store.begin_attempt(rid)
            if started is None:
                return  # settled already: a duplicate or late job
            attempt, received_at = started
            deadline = received_at + timedelta(seconds=s.request_deadline_s)
            final = attempt >= s.max_attempts or datetime.now(UTC) >= deadline or job_try >= ARQ_MAX_TRIES
            structlog.contextvars.bind_contextvars(attempt=attempt)
            try:
                await process(deps, rid, final_attempt=final, deadline=deadline)
            except Parked:
                await store.refund_attempt(rid)
                raise
    except LockTimeout:
        _park(job_try, LOCK_BUSY_DEFER_S + random.uniform(0, LOCK_BUSY_DEFER_S), "request busy")
    except Parked as p:
        _park(job_try, p.defer_s, p.reason)
    except AgentError as e:  # retryable and not final: process() only lets those out
        delay = backoff_s(attempt, e.retry_after, s)
        log.warning("request.retry", error=str(e)[:300], dependency=e.dependency, defer_s=round(delay, 1))
        raise Retry(defer=delay) from e
    except INFRA_ERRORS as e:  # process() couldn't even record its retry (Postgres or Redis down)
        delay = backoff_s(attempt, None, s)
        log.warning("request.infra_retry", error=f"{type(e).__name__}: {str(e)[:300]}", defer_s=round(delay, 1))
        metrics.observe_retry(retry_cause(e))
        raise Retry(defer=delay) from e
    finally:
        structlog.contextvars.clear_contextvars()


def _park(job_try: int, defer_s: float, reason: str) -> None:
    if job_try >= ARQ_MAX_TRIES:
        # Never raise Retry on arq's last try (it would drop the job): the sweeper takes over.
        log.error("request.park_exhausted", reason=reason, alert=True)
        return
    log.info("request.parked", reason=reason, defer_s=round(defer_s, 1))
    raise Park(defer=defer_s)


async def send_outbound(ctx: dict, message_id: str) -> None:
    """Deliver one queued email; this job's own retries are the outbox's backoff (up to 48 h)."""
    deps: Deps = ctx["deps"]
    try:
        outcome = await outbox.attempt(message_id, breakers=deps.breakers)
        if outcome.status == "rerender" and outcome.request_id:
            await queue.enqueue_request(ctx["redis"], outcome.request_id)
    except INFRA_ERRORS as e:
        delay = outbox.backoff_s(ctx.get("job_try", 1))
        log.warning("outbound.infra_retry", message_id=message_id, error=f"{type(e).__name__}: {e}")
        raise Retry(defer=delay) from e
    if outcome.status == "queued" and ctx.get("job_try", 1) < ARQ_MAX_TRIES:
        raise Retry(defer=outcome.retry_in_s or outbox.RETRY_BASE_S)


async def sweep(ctx: dict) -> None:
    """Pick up work that stopped moving (worker killed mid-job, Redis flushed, every try timed out).

    Requests out of attempts or past their deadline are finished here (one apology to a verified
    sender, then a terminal state); the rest are re-enqueued with jitter. The job id is the
    request id, so this never duplicates a job that is still queued or running.
    """
    deps: Deps = ctx.get("deps") or _sweeper_deps(ctx)
    s = deps.settings
    now = datetime.now(UTC)
    for row in await store.stuck_requests(STUCK_AFTER):
        rid = row["id"]
        exhausted = (row["attempts"] >= s.max_attempts
                     or now >= row["received_at"] + timedelta(seconds=s.request_deadline_s))
        try:
            if exhausted:
                async with deps.limits.lock(f"request:{rid}", ttl_s=REQUEST_LOCK_TTL_S, wait_s=0):
                    await finish_exhausted(deps, rid)
                log.warning("sweep.finished", request_id=str(rid), attempts=row["attempts"])
            else:
                await queue.enqueue_request(ctx["redis"], rid, defer_s=random.uniform(0, SWEEP_JITTER_S))
                log.info("sweep.requeued", request_id=str(rid))
        except LockTimeout:
            log.info("sweep.busy", request_id=str(rid))  # a job is on it right now
        except Exception:  # one bad request must not stop the sweep
            log.exception("sweep.failed", request_id=str(rid))
    for message_id in await store.due_outbound(OUTBOUND_OVERDUE):
        await queue.enqueue_outbound(ctx["redis"], message_id, defer_s=random.uniform(0, SWEEP_JITTER_S))
        log.info("sweep.requeued_outbound", message_id=message_id)


def _sweeper_deps(ctx: dict) -> Deps:
    """Enough of a Deps to finish requests (no providers: the sweeper never fetches)."""
    s = get_settings()
    return Deps(settings=s, providers={}, limits=Limits(ctx["redis"]), drop=None, queue=ctx["redis"])


class WorkerSettings:
    functions: ClassVar = [process_request, func(send_outbound, max_tries=ARQ_MAX_TRIES, timeout=300)]
    cron_jobs: ClassVar = [
        cron(sweep, minute=set(range(0, 60, 5)), run_at_startup=True),
        cron(metrics.refresh, run_at_startup=True),  # every minute
        cron(audit.export_job, hour={0}, minute={30}, run_at_startup=True),  # yesterday's events (and any missed)
        cron(retention.purge_job, hour={3}, minute={15}, timeout=3600),  # only where the role may delete
        cron(reconcile.reconcile_job, hour={6}, minute={0}),
    ]
    health_check_interval = 60  # `ragent healthcheck worker`: the key lives one interval
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = queue.redis_settings()
    job_serializer = staticmethod(queue.serialize)
    job_deserializer = staticmethod(queue.deserialize)
    max_jobs = 12  # mostly waiting on the portal/LLM; browser sessions are capped separately
    job_timeout = get_settings().job_timeout_s  # above pipeline_timeout_s: the pipeline times out first
    max_tries = ARQ_MAX_TRIES
    keep_result = 0  # so the sweeper can re-enqueue a finished request id if it ever needs to
    allow_abort_jobs = True
