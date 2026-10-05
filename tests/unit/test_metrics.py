"""Metrics: the names, labels and buckets of deploy/observability/METRICS_CONTRACT.md, how failures map
to fixed label values, what each process exposes and where, and the privacy rules over everything
recorded (tests/metrics_privacy.py)."""

import asyncio
import json
import socket
import time
import urllib.request
from types import SimpleNamespace

import aiosmtplib
import asyncpg
import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY, CollectorRegistry, Counter
from prometheus_client.metrics import MetricWrapperBase
from pydantic import BaseModel
from redis.exceptions import ConnectionError as RedisConnectionError

import agent.llm
import agent.typesafe
from agent import breaker, cli, metrics, pipeline
from agent.config import Settings
from agent.delivery.choose import DeliveryDeferred
from agent.delivery.drop import DropError
from agent.limits import LockTimeout
from agent.llm import LLMUnavailable
from agent.models import (
    Intent,
    MatterNotFound,
    ParsedRequest,
    PortalUnavailable,
    ProviderRejected,
    ScrapeError,
)
from agent.typesafe import Noul, TypeSafeUnavailable
from agent.web import app as web
from agent.web.ratelimit import EXEMPT, route_class
from tests.metrics_privacy import ALLOWED_LABELS, personal, violations


def _sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ---------------------------------------------------------------- the contract

CONTRACT = {  # registered name (counters without _total) -> (type, labels)
    "requests": ("counter", ("final_state",)),
    "retries": ("counter", ("cause",)),
    "llm_cost_usd": ("counter", ()),
    "stage_duration_seconds": ("histogram", ("stage",)),
    "queue_depth": ("gauge", ()),
    "breaker_open": ("gauge", ("dependency",)),
    "limiter_decisions": ("counter", ("limiter", "decision")),
    "budget_used": ("gauge", ("budget",)),
    "budget_limit": ("gauge", ("budget",)),
    "budget_exhausted": ("counter", ("budget",)),
    "request_e2e_seconds": ("histogram", ("outcome",)),
    "ingest_heartbeat_age_seconds": ("gauge", ()),
    "provider_requests": ("counter", ("provider", "state")),
    "provider_fetch_seconds": ("histogram", ("provider", "outcome")),
    "provider_visits": ("counter", ("provider",)),
    "model_calls": ("counter", ("kind", "model", "outcome")),
    "model_call_seconds": ("histogram", ("kind", "model")),
    "gate_decisions": ("counter", ("classifier", "escalated", "outcome")),
    "outbound_messages": ("counter", ("kind", "outcome")),
    "deliveries": ("counter", ("kind", "outcome")),
    "auth_verdicts": ("counter", ("verdict",)),
    "web_rate_limited": ("counter", ("kind",)),
}


def _ours() -> dict[str, MetricWrapperBase]:
    return {m._name: m for m in vars(metrics).values() if isinstance(m, MetricWrapperBase)}


def test_every_contract_metric_is_exported_with_its_type_and_labels():
    ours = _ours()
    for name, (kind, labels) in CONTRACT.items():
        assert name in ours, name
        assert (ours[name]._type, tuple(ours[name]._labelnames)) == (kind, labels), name
    # the one addition beyond the contract (asked for by the instrumentation brief)
    assert set(ours) - set(CONTRACT) == {"citations"}
    assert all(set(m._labelnames) <= ALLOWED_LABELS for m in ours.values())


def test_histogram_buckets_are_the_contracts():
    def bounds(h) -> list[float]:
        return list(h._upper_bounds[:-1])  # without +Inf

    e2e = bounds(metrics.REQUEST_E2E)
    assert 180 in e2e  # deploy/observability/rules/slo.yml
    assert {5, 10, 20, 30, 60, 90, 120, 180, 240, 300, 600, 1800, 3600} <= set(e2e)  # the contract's
    assert {300, 600, 1200, 3600} <= set(e2e)  # and the brief's
    assert bounds(metrics.PROVIDER_FETCH) == [0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600]
    assert bounds(metrics.MODEL_CALL_SECONDS) == [0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120]


# ---------------------------------------------------------------- fixed label values


@pytest.mark.parametrize(("error", "cause"), [
    (PortalUnavailable("portal 503"), "portal_unavailable"),
    (ScrapeError("count 3 but empty listing"), "scrape_error"),
    (pipeline.PipelineTimeout("hung"), "timeout"),
    (TimeoutError(), "timeout"),
    (LLMUnavailable("no model"), "llm_unavailable"),
    (TypeSafeUnavailable("down"), "llm_unavailable"),
    (DropError("drop 502", status=502), "drop_unavailable"),
    (DeliveryDeferred("drop down, too big to attach"), "drop_unavailable"),
    (aiosmtplib.SMTPServerDisconnected("lost"), "smtp_temp"),
    (asyncpg.InterfaceError("pool closed"), "db"),
    (asyncpg.exceptions.ConnectionDoesNotExistError("gone"), "db"),
    (RedisConnectionError("refused"), "redis"),
    (LockTimeout("sf:uarb:M12205"), "lock_contention"),
    (breaker.Open("uarb", 30), "breaker_open"),
    (pipeline.SenderAuthTemperror("DNS temperror for alice@example.com"), "auth_temperror"),
    (pipeline.DiskLow("disk"), "disk_low"),
    (RuntimeError("bug near alice@example.com 198.51.100.7 M12205"), "internal"),
])
def test_retry_causes_come_from_exception_types_never_messages(error, cause):
    assert pipeline.retry_cause(error) == cause
    assert cause in metrics.RETRY_CAUSES


@pytest.mark.parametrize(("dependency", "cause"), [
    ("drop", "drop_unavailable"), ("smtp", "smtp_temp"), ("openrouter:qwen/qwen3.8-27b", "llm_unavailable"),
    ("uarb", "portal_unavailable"), (None, "internal"),
])
def test_a_generic_retry_is_named_by_its_dependency(dependency, cause):
    err = pipeline.TransientError("no downloads succeeded for M12205/Exhibits")
    err.dependency = dependency
    assert pipeline.retry_cause(err) == cause


def test_an_unknown_retry_cause_is_counted_as_internal():
    before = _sample("retries_total", cause="internal")
    metrics.observe_retry("PortalUnavailable: timeout for alice@example.com")
    assert _sample("retries_total", cause="internal") == before + 1


@pytest.mark.parametrize(("error", "outcome"), [
    (None, "ok"),
    (MatterNotFound("M12205"), "not_found"),
    (ProviderRejected("HTTP 403"), "blocked"),
    (TimeoutError(), "timeout"),
    (asyncio.CancelledError(), "timeout"),  # the try's deadline cancelled the call
    (PortalUnavailable("503"), "error"),
    (ScrapeError("layout"), "error"),
    (ValueError("bug"), "error"),
])
def test_fetch_outcomes(error, outcome):
    assert metrics.fetch_outcome(error) == outcome


def test_provider_calls_are_timed_by_outcome_and_a_finished_stream_is_not_a_call():
    def count(outcome: str) -> float:
        return _sample("provider_fetch_seconds_count", provider="oeb", outcome=outcome)

    ok, not_found = count("ok"), count("not_found")
    with metrics.provider_call("oeb"):
        pass
    with metrics.provider_call("oeb") as call:
        call.skip = True  # anext() returned None: the download stream ended
    with pytest.raises(MatterNotFound), metrics.provider_call("oeb"):
        raise MatterNotFound("EB-2024-0111")
    assert (count("ok"), count("not_found")) == (ok + 1, not_found + 1)


def test_final_states_are_counted_per_regulator():
    before = _sample("provider_requests_total", provider="uarb", state="done")
    none = _sample("provider_requests_total", provider="none", state="rejected")
    metrics.observe_transition("replying", "done", 1.0, final=True, provider="uarb")
    metrics.observe_transition(None, "rejected", None, final=True)
    metrics.observe_transition("received", "accepted", 1.0, final=False, provider="uarb")
    assert _sample("provider_requests_total", provider="uarb", state="done") == before + 1
    assert _sample("provider_requests_total", provider="none", state="rejected") == none + 1
    assert _sample("provider_requests_total", provider="uarb", state="accepted") == 0


def test_reply_latency_counts_the_objectives_outcomes_only():
    done = _sample("request_e2e_seconds_count", outcome="done")
    metrics.observe_reply_sent("done", 42.0)
    metrics.observe_reply_sent("rejected", 3.0)  # a slow-down notice or the help text
    metrics.observe_reply_sent("done", None)
    assert _sample("request_e2e_seconds_count", outcome="done") == done + 1
    assert _sample("request_e2e_seconds_bucket", outcome="done", le="60.0") >= 1
    assert _sample("request_e2e_seconds_count", outcome="rejected") == 0


def _parsed(intent: Intent, **fields) -> ParsedRequest:
    return ParsedRequest(intent=intent, **fields)


UARB = SimpleNamespace(name="uarb")


@pytest.mark.parametrize(("parsed", "provider", "outcome"), [
    (_parsed(Intent.SPAM), None, "reject"),
    (_parsed(Intent.INJECTION, matter="M12205"), UARB, "reject"),
    (_parsed(Intent.UNRELATED), None, "reject"),
    (_parsed(Intent.DOCUMENT_REQUEST, matter="M99999"), None, "clarify"),  # not a matter we can look up
    (_parsed(Intent.DOCUMENT_REQUEST, matter="M12205"), UARB, "clarify"),  # no document type
    (_parsed(Intent.DOCUMENT_REQUEST, matter="M12205", doc_type="Exhibits", needs_clarification="Which?"), UARB,
     "clarify"),
    (_parsed(Intent.DOCUMENT_REQUEST, matter="M12205", doc_type="Exhibits"), UARB, "accept"),
    (_parsed(Intent.QUESTION, matter="M12205", doc_type="Exhibits"), UARB, "accept"),  # answered
    (_parsed(Intent.QUESTION, matter="M12205"), UARB, "clarify"),
])
def test_gate_outcomes_follow_the_gates_branches(parsed, provider, outcome):
    assert pipeline._gate_outcome(parsed, provider) == outcome


@pytest.mark.parametrize(("status", "outcome"), [
    (429, "refused"), (404, "refused"), (400, "refused"), (408, "timeout"), (503, "error"), (529, "error"),
    (None, "error"),
])
def test_model_call_outcomes(status, outcome):
    assert metrics.model_outcome(status) == outcome


# ---------------------------------------------------------------- model calls, where they happen


class Out(BaseModel):
    answer: str


def _openrouter(monkeypatch, handler) -> None:
    def wrapped(request: httpx.Request) -> httpx.Response:
        return handler(json.loads(request.content)["model"])

    client = httpx.AsyncClient(base_url="https://openrouter.test/api/v1", transport=httpx.MockTransport(wrapped))
    s = Settings(_env_file=None, llm_models=["m/a", "m/b"], llm_zero_data_retention=False)
    monkeypatch.setattr(agent.llm, "_http", lambda: client)
    monkeypatch.setattr(agent.llm, "get_settings", lambda: s)
    breaker.install(None)


def _ok(model: str) -> httpx.Response:
    return httpx.Response(200, json={"model": model, "choices": [{"message": {"content": '{"answer": "42"}'}}],
                                     "usage": {"cost": 0.0}})


async def test_every_openrouter_attempt_is_counted_by_model_and_outcome(monkeypatch):
    def count(model: str, outcome: str) -> float:
        return _sample("model_calls_total", kind="llm", model=model, outcome=outcome)

    before = (count("m/a", "error"), count("m/b", "ok"), _sample("model_call_seconds_count", kind="llm", model="m/b"))
    _openrouter(monkeypatch, lambda m: httpx.Response(503) if m == "m/a" else _ok(m))
    await agent.llm.structured(system="s", user="u", schema=Out, purpose="gate")
    after = (count("m/a", "error"), count("m/b", "ok"), _sample("model_call_seconds_count", kind="llm", model="m/b"))
    assert after == tuple(x + 1 for x in before)

    refused = count("m/a", "refused")
    _openrouter(monkeypatch, lambda m: httpx.Response(429, headers={"Retry-After": "60"}))
    with pytest.raises(LLMUnavailable):
        await agent.llm.structured(system="s", user="u", schema=Out, purpose="summary")
    assert count("m/a", "refused") == refused + 1


@respx.mock
async def test_typesafe_calls_are_counted_once_per_call(monkeypatch):
    s = Settings(_env_file=None, typesafe_api_key="k", typesafe_base_url="https://typesafe.test", typesafe_deadline_s=2.0)
    monkeypatch.setattr(agent.typesafe, "get_settings", lambda: s)
    monkeypatch.setattr(agent.typesafe, "BACKOFF_BASE_S", 0.0)
    await agent.typesafe.aclose()
    breaker.install(None)

    def count(outcome: str) -> float:
        return _sample("model_calls_total", kind="jev", model=s.typesafe_model, outcome=outcome)

    q = {"yes": Noul(instructions="Yes?")}
    ok, err, refused = count("ok"), count("error"), count("refused")
    respx.post("https://typesafe.test/v1/systemone").mock(side_effect=[
        httpx.Response(200, json={"model": s.typesafe_model, "answers": {"yes": {"type": "noul", "noul": 0.9}}}),
        httpx.Response(503), httpx.Response(503), httpx.Response(503),  # one call, three attempts
        httpx.Response(400),
    ])
    await agent.typesafe.ask("state", q, purpose="gate")
    for _ in range(2):
        with pytest.raises(TypeSafeUnavailable):
            await agent.typesafe.ask("state", q, purpose="gate")
    assert (count("ok"), count("error"), count("refused")) == (ok + 1, err + 1, refused + 1)
    await agent.typesafe.aclose()


# ---------------------------------------------------------------- what each process exposes


def test_web_and_ingest_never_export_the_workers_unlabelled_gauges():
    worker = metrics.exposition("worker").decode()
    web_text = metrics.exposition("web").decode()
    assert "queue_depth" in worker and "ingest_heartbeat_age_seconds" in worker
    assert "queue_depth" not in web_text and "ingest_heartbeat_age_seconds" not in web_text
    assert "# TYPE limiter_decisions_total counter" in web_text and "web_rate_limited_total" in web_text
    assert "python_info" not in worker and "python_gc" not in worker  # labels outside the contract


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_ingest_endpoint_serves_its_metrics_on_loopback(monkeypatch):
    monkeypatch.setattr(metrics, "_started_on", None)
    port = _free_port()
    assert metrics.start_server(port, component="ingest") is True
    assert metrics.start_server(port, component="ingest") is False  # once per process
    metrics.observe_limiter("preauth_ip", "limited")
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as r:
        body = r.read().decode()
    assert 'limiter_decisions_total{decision="limited",limiter="preauth_ip"}' in body
    assert "queue_depth" not in body


def test_ingest_starts_its_metrics_endpoint_on_metrics_ingest_port(monkeypatch):
    started = []

    class Stop(Exception):
        pass

    async def no_db(*_a, **_k):
        raise Stop

    monkeypatch.setattr(metrics, "start_server", lambda port, component="worker": started.append((port, component)))
    monkeypatch.setattr("agent.db.create_pool", no_db)
    monkeypatch.setattr("agent.config.get_settings", lambda: Settings(_env_file=None))
    with pytest.raises(Stop):
        asyncio.run(cli._async("ingest"))
    assert started == [(9711, "ingest")]


def test_settings_have_the_ingest_port_and_no_fallback_proxy():
    s = Settings(_env_file=None)
    assert (s.metrics_port, s.metrics_ingest_port) == (9710, 9711)
    assert "uarb_fallback_proxy" not in Settings.model_fields


def test_refresh_reports_the_ingest_heartbeat_age(monkeypatch):
    class FakeRedis:
        def __init__(self, beat):
            self.beat = beat

        async def zcard(self, key):
            return 0

        async def hget(self, key, field):
            return None

        async def get(self, key):
            return self.beat

    monkeypatch.setattr("agent.providers.base.all_providers", list)
    asyncio.run(metrics.refresh({"redis": FakeRedis(f"{time.time() - 42:.0f}")}))
    assert 40 <= _sample("ingest_heartbeat_age_seconds") <= 45
    asyncio.run(metrics.refresh({"redis": FakeRedis(None)}))
    assert _sample("ingest_heartbeat_age_seconds") == float("inf")


# ---------------------------------------------------------------- web: /metrics and 429s


@pytest.mark.parametrize(("host", "headers", "status"), [
    ("127.0.0.1", {}, 200),
    ("::1", {}, 200),
    ("203.0.113.9", {}, 404),
    ("127.0.0.1", {"X-Real-IP": "203.0.113.9"}, 404),  # through Caddy
    ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}, 404),  # any proxy at all
    ("testclient", {}, 404),
])
def test_web_metrics_answer_loopback_prometheus_only(host, headers, status):
    r = TestClient(web.app, client=(host, 50000)).get("/metrics", headers=headers)
    assert r.status_code == status
    if status == 200:
        assert r.headers["content-type"].startswith("text/plain") and "web_rate_limited_total" in r.text
        assert "queue_depth" not in r.text
    else:
        assert "web_rate_limited_total" not in r.text


def test_metrics_and_health_are_not_rate_limited_and_status_is():
    assert {"/health", "/health/deep", "/metrics"} <= EXEMPT
    s = Settings(_env_file=None)
    assert route_class("/metrics", s) is None
    assert route_class("/status", s)[0] == "default"


def test_every_429_is_counted_by_route_class():
    class Limited:
        async def check(self, name, buckets, ip):
            return 7.0

    before = _sample("web_rate_limited_total", kind="files")
    web.app.state.rate_limiter = Limited()
    try:
        r = TestClient(web.app).get("/files/00000000-0000-0000-0000-000000000000.pdf")
    finally:
        web.app.state.rate_limiter = None
    assert r.status_code == 429 and r.headers["retry-after"] == "7"
    assert _sample("web_rate_limited_total", kind="files") == before + 1


# ---------------------------------------------------------------- privacy


def test_the_privacy_check_catches_what_it_must():
    reg = CollectorRegistry()
    Counter("leaky", "x", ["email"], registry=reg).labels(email="a").inc()
    Counter("retries", "x", ["cause"], registry=reg).labels(cause="TimeoutError").inc()
    Counter("auth_verdicts", "x", ["verdict"], registry=reg).labels(verdict="temperror").inc()
    c = Counter("model_calls", "x", ["kind", "model", "outcome"], registry=reg)
    c.labels(kind="llm", model="alice@example.com", outcome="ok").inc()
    found = violations(reg)
    assert any("'email' is not in the contract's allowlist" in f for f in found)
    assert any("cause='TimeoutError' is not one of" in f for f in found)
    assert any("verdict='temperror'" in f for f in found)
    assert any("looks like an email address" in f for f in found)
    for value in ("198.51.100.7", "2001:db8::1", "10.0.0.0/8", "M12205", "EB-2024-0111", "ER24-1234-000",
                  "0b6f3c1e-8d2a-4f7b-9c41-5e2d7a9b1c3f", "bob@example.org"):
        assert personal(value), value
    for value in ("uarb", "openrouter:qwen/qwen3.8-27b", "deepseek/deepseek-v4.1-flash", "jev-1.13.0",
                  "portal_budget:uarb", "web_progress_json", "180.0", "llm_usd", "none"):
        assert personal(value) is None, value


def test_no_metric_label_breaks_the_privacy_rules():
    """Drive the instrumentation with personal data in every input it sees, then check every
    series in the registry (including what earlier tests recorded)."""
    hostile = RuntimeError("alice@example.com from 198.51.100.7 asked for M12205, request "
                           "0b6f3c1e-8d2a-4f7b-9c41-5e2d7a9b1c3f")
    metrics.observe_retry(pipeline.retry_cause(hostile))
    metrics.observe_retry(str(hostile))
    for error in (hostile, MatterNotFound("M12205"), None):
        try:
            with metrics.provider_call("uarb"):
                if error:
                    raise error
        except (RuntimeError, MatterNotFound):
            pass
    metrics.observe_gate("jev", escalated=True, outcome="accept")
    metrics.observe_auth("none")
    metrics.observe_outbound("reply", "sent")
    metrics.observe_delivery("drop", "ok")
    metrics.observe_citations(kept=3, dropped=1, support_failed=1)
    metrics.observe_web_rate_limited("progress_json")
    metrics.observe_model_call("llm", "deepseek/deepseek-v4.1-flash", "ok", 1.2)
    metrics.observe_portal_visit("uarb")
    assert violations() == []
