"""Prometheus metrics, on 127.0.0.1 only (never public): the worker on {metrics_port}, ingest on
{metrics_ingest_port}, and the web process at GET /metrics for loopback clients.

Names, labels and label values follow deploy/observability/METRICS_CONTRACT.md, which the
dashboards and alert rules are written against: change a name there first.

Counters move where the facts are recorded (state transitions, the call itself), so each event is
counted once, by the process it happens in, and every process exposes what it counted;
Prometheus sums the processes. Gauges for the queue, the circuit breakers, the daily budgets and
the ingest heartbeat are refreshed once a minute by a worker cron job (`refresh`); the unlabelled
ones are the worker's alone (`registry_for`), so web and ingest never report a zero queue depth or
heartbeat age.

The dashboards are public, so labels never carry an address, domain, IP, matter number, title,
request id or free text: only names from small fixed sets (tests/metrics_privacy.py checks it).
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)
from prometheus_client.registry import Collector
from redis.exceptions import RedisError

from agent.models import MatterNotFound, ProviderRejected

log = structlog.get_logger()

# The client library's own python_info{version,...} and python_gc_*{generation} use labels outside
# the contract's list (and would publish the exact interpreter version); process_* has none and stays.
for _collector in (PLATFORM_COLLECTOR, GC_COLLECTOR):
    try:
        REGISTRY.unregister(_collector)
    except KeyError:  # already gone (module re-imported)
        pass

REQUESTS = Counter("requests", "Requests that reached a final state", ["final_state"])
# cause: one of RETRY_CAUSES (agent.pipeline.retry_cause maps the exception to it).
RETRIES = Counter("retries", "Request attempts that failed and will be retried, by cause", ["cause"])
LLM_COST = Counter("llm_cost_usd", "LLM spend in US dollars (as reported by OpenRouter)")
STAGE_SECONDS = Histogram(
    "stage_duration_seconds", "Time a request spent in each state before moving on", ["stage"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 7200),
)
QUEUE_DEPTH = Gauge("queue_depth", "Jobs waiting in the arq queue")
BREAKER_OPEN = Gauge("breaker_open", "1 while the dependency's circuit breaker is open", ["dependency"])
# One per limiter decision. limiter: sender_hour, sender_day, domain_hour, domain_day, global_hour,
# global_day, preauth_ip, preauth_domain, inbound_minute, inflight, llm_budget, portal_budget:<p>,
# bytes_budget, web_<route class>; decision: allowed | limited | deferred | unavailable.
LIMITER = Counter("limiter_decisions", "Rate-limit and budget decisions", ["limiter", "decision"])
BUDGET_USED = Gauge("budget_used", "Today's (UTC) use of each daily budget", ["budget"])
BUDGET_LIMIT = Gauge("budget_limit", "Each daily budget's limit", ["budget"])
BUDGET_EXHAUSTED = Counter("budget_exhausted", "Daily budgets that ran out (once per budget and day)", ["budget"])

# The latency objective: 95 % of replies within 180 s of the email arriving (deploy/observability/
# rules/slo.yml needs the le=180 bucket). Once per request, when its reply is accepted by SMTP.
REQUEST_E2E = Histogram(
    "request_e2e_seconds", "Email received to its reply accepted by SMTP", ["outcome"],
    buckets=(5, 10, 20, 30, 60, 90, 120, 180, 240, 300, 600, 1200, 1800, 3600),
)
E2E_OUTCOMES = frozenset({"done", "failed", "clarify"})
# With requests_total: once per request at its final state, by its regulator ("none": it never got one).
PROVIDER_REQUESTS = Counter("provider_requests", "Requests that reached a final state, by regulator",
                            ["provider", "state"])
# One per call to a regulator's portal: a matter lookup, a listing, or one downloaded file.
PROVIDER_FETCH = Histogram(
    "provider_fetch_seconds", "Calls to a regulator's portal", ["provider", "outcome"],
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600),
)
PROVIDER_VISITS = Counter("provider_visits", "Portal visits counted against the daily budget", ["provider"])
# kind: jev (TypeSafe, once per call: its short internal retries are inside it) | llm (OpenRouter,
# once per attempt: each model tried, again after a waited-out 429). model: as configured.
# outcome: ok | error | timeout | refused (a 4xx: rate limited, no zero-retention endpoint...).
MODEL_CALLS = Counter("model_calls", "Calls to TypeSafe Jev and OpenRouter models", ["kind", "model", "outcome"])
MODEL_CALL_SECONDS = Histogram(
    "model_call_seconds", "Duration of each model call", ["kind", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)
# One per email a classifier read. classifier: whose answer was used (rules | jev | llm); escalated:
# Jev handed the email to the LLM classifier; outcome: accept (documents fetched or a question
# answered) | reject | clarify.
GATE_DECISIONS = Counter("gate_decisions", "Gate decisions", ["classifier", "escalated", "outcome"])
# kind: ack | reply | notice; outcome: sent | undeliverable | deferred (tried again later) |
# suppressed (a queued ack or notice dropped because the reply went first).
OUTBOUND_MESSAGES = Counter("outbound_messages", "Outbound email attempt results", ["kind", "outcome"])
# kind: drop (a download link) | attachment; outcome: ok | error.
DELIVERIES = Counter("deliveries", "Document deliveries", ["kind", "outcome"])
AUTH_VERDICTS = Counter("auth_verdicts", "Sender authentication verdicts (pass | fail | none)", ["verdict"])
WEB_RATE_LIMITED = Counter("web_rate_limited", "Web requests answered 429, by route class", ["kind"])
INGEST_HEARTBEAT_AGE = Gauge(
    "ingest_heartbeat_age_seconds", "Seconds since ingest last wrote its heartbeat (+Inf: none in Redis)"
)
# Claims a fresh summary proposed: kept, dropped (grounding, figures, limits), or dropped by the
# check that the quote supports the claim.
CITATIONS = Counter("citations", "Summary claims by outcome (kept | dropped | support_failed)", ["outcome"])

RETRY_CAUSES = frozenset({
    "portal_unavailable", "scrape_error", "timeout", "llm_unavailable", "smtp_temp", "drop_unavailable",
    "db", "redis", "lock_contention", "breaker_open", "budget", "disk_low", "auth_temperror", "internal",
})
# Unlabelled gauges only the worker's refresh job sets: another process would export them as 0.
WORKER_ONLY = frozenset({"queue_depth", "ingest_heartbeat_age_seconds"})

_started_on: int | None = None


class _WithoutWorkerGauges(Collector):
    def collect(self):
        return [m for m in REGISTRY.collect() if m.name not in WORKER_ONLY]


_SHARED = CollectorRegistry(auto_describe=False)
_SHARED.register(_WithoutWorkerGauges())


def registry_for(component: str) -> CollectorRegistry:
    """What a process exposes: everything for the worker, all but WORKER_ONLY for ingest and web."""
    return REGISTRY if component == "worker" else _SHARED


def exposition(component: str) -> bytes:
    """The Prometheus text format of `component`'s metrics (the web process's GET /metrics)."""
    return generate_latest(registry_for(component))


def start_server(port: int, component: str = "worker") -> bool:
    """Expose /metrics on 127.0.0.1:`port` (once per process; 0 = off). Never fatal."""
    global _started_on
    if not port or _started_on == port:
        return False
    try:
        start_http_server(port, addr="127.0.0.1", registry=registry_for(component))
    except OSError as e:  # port taken (a second process on this host): run without metrics
        log.warning("metrics.unavailable", port=port, error=str(e))
        return False
    _started_on = port
    log.info("metrics.listening", port=port, component=component)
    return True


def observe_retry(cause: str) -> None:
    RETRIES.labels(cause=cause if cause in RETRY_CAUSES else "internal").inc()


def observe_limiter(limiter: str, decision: str) -> None:
    LIMITER.labels(limiter=limiter[:40], decision=decision).inc()


def observe_transition(
    from_state: str | None, to_state: str, stage_s: float | None, *, final: bool, provider: str | None = None
) -> None:
    if from_state and from_state != to_state and stage_s is not None and stage_s >= 0:
        STAGE_SECONDS.labels(stage=from_state).observe(stage_s)
    if final:
        REQUESTS.labels(final_state=to_state).inc()
        PROVIDER_REQUESTS.labels(provider=provider or "none", state=to_state).inc()


def observe_reply_sent(outcome: str, seconds: float | None) -> None:
    """A request's reply was accepted by SMTP `seconds` after its email arrived. Replies that close
    a request as rejected (a slow-down notice, the help text) are not part of the objective."""
    if outcome in E2E_OUTCOMES and seconds is not None and seconds >= 0:
        REQUEST_E2E.labels(outcome=outcome).observe(seconds)


def fetch_outcome(error: BaseException | None) -> str:
    """provider_fetch_seconds' outcome: ok | not_found | blocked | timeout | error."""
    if error is None:
        return "ok"
    if isinstance(error, MatterNotFound):
        return "not_found"
    if isinstance(error, ProviderRejected):  # the portal refused the request outright
        return "blocked"
    if isinstance(error, TimeoutError) or not isinstance(error, Exception):  # its deadline cancelled it
        return "timeout"
    return "error"


class _Call:
    skip = False  # set inside the block: nothing to count (a download stream that just ended)


@contextmanager
def provider_call(provider: str) -> Iterator[_Call]:
    """Time one call to `provider`'s portal into provider_fetch_seconds, by outcome."""
    call = _Call()
    started = time.perf_counter()
    try:
        yield call
    except BaseException as e:
        PROVIDER_FETCH.labels(provider=provider, outcome=fetch_outcome(e)).observe(time.perf_counter() - started)
        raise
    if not call.skip:
        PROVIDER_FETCH.labels(provider=provider, outcome="ok").observe(time.perf_counter() - started)


def observe_portal_visit(provider: str) -> None:
    PROVIDER_VISITS.labels(provider=provider).inc()


def observe_gate(classifier: str, *, escalated: bool, outcome: str) -> None:
    GATE_DECISIONS.labels(classifier=classifier, escalated="true" if escalated else "false", outcome=outcome).inc()


def observe_model_call(kind: str, model: str, outcome: str, seconds: float | None = None) -> None:
    MODEL_CALLS.labels(kind=kind, model=model, outcome=outcome).inc()
    if seconds is not None:
        MODEL_CALL_SECONDS.labels(kind=kind, model=model).observe(seconds)


def model_outcome(status: int | None) -> str:
    """model_calls_total's outcome for an HTTP error status (None: no answer at all)."""
    if status == 408:
        return "timeout"
    if status is not None and 400 <= status < 500:
        return "refused"
    return "error"


def observe_auth(verdict: str) -> None:
    AUTH_VERDICTS.labels(verdict=verdict).inc()


def observe_outbound(kind: str, outcome: str, n: int = 1) -> None:
    OUTBOUND_MESSAGES.labels(kind=kind, outcome=outcome).inc(n)


def observe_delivery(kind: str, outcome: str) -> None:
    DELIVERIES.labels(kind=kind, outcome=outcome).inc()


def observe_citations(*, kept: int, dropped: int, support_failed: int) -> None:
    for outcome, n in (("kept", kept), ("dropped", dropped), ("support_failed", support_failed)):
        if n:
            CITATIONS.labels(outcome=outcome).inc(n)


def observe_web_rate_limited(route_class: str) -> None:
    WEB_RATE_LIMITED.labels(kind=route_class).inc()


async def refresh(ctx: dict) -> None:
    """arq cron (every minute): queue depth, breaker states, daily budgets and the ingest heartbeat
    age from Redis."""
    from arq.constants import default_queue_name

    from agent.config import get_settings
    from agent.limits import Budgets
    from agent.providers.base import all_providers

    redis = ctx["redis"]
    try:
        QUEUE_DEPTH.set(await redis.zcard(default_queue_name))
        names = [p.name for p in all_providers()] + ["drop", "smtp"]
        names += [f"openrouter:{m}" for m in get_settings().llm_models]
        now = time.time()
        for name in names:
            open_until = float(await redis.hget(f"breaker:{name}", "open_until") or 0)
            BREAKER_OPEN.labels(dependency=name).set(1 if open_until > now else 0)
    except (RedisError, OSError) as e:
        log.warning("metrics.refresh_failed", error=f"{type(e).__name__}: {e}")
    try:
        for budget, (used, limit) in (await Budgets(redis).snapshot()).items():
            BUDGET_USED.labels(budget=budget).set(used)
            BUDGET_LIMIT.labels(budget=budget).set(limit)
    except Exception as e:  # noqa: BLE001 - gauges are best effort, never a failed cron job
        log.warning("metrics.budgets_failed", error=f"{type(e).__name__}: {e}")
    try:
        from agent.health import ingest_heartbeat_age

        age = await ingest_heartbeat_age(redis)
        INGEST_HEARTBEAT_AGE.set(float("inf") if age is None else age)
    except Exception as e:  # noqa: BLE001 - as above
        log.warning("metrics.heartbeat_failed", error=f"{type(e).__name__}: {e}")
