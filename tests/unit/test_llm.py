"""agent.llm.structured with a fake OpenRouter transport: deadlines, 429s, breakers, privacy, meta."""

import json
from typing import Any

import httpx
import pytest
import structlog
from pydantic import BaseModel

import agent.llm
from agent import breaker
from agent.config import Settings
from agent.llm import LLMUnavailable, structured


class Out(BaseModel):
    answer: str


def ok_body(model: str, **extra: Any) -> dict:
    return {"model": model, "provider": "Together", "choices": [{"message": {"content": json.dumps({"answer": "42"})},
            "finish_reason": "stop"}], "usage": {"cost": 0.0001, "prompt_tokens": 10, "completion_tokens": 5}, **extra}


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch):
    """Install a handler(model, payload) -> httpx.Response; returns the list of payloads sent."""
    sent: list[dict] = []
    state: dict[str, Any] = {}

    def wrapped(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        sent.append(payload)
        return state["handler"](payload["model"], payload)

    client = httpx.AsyncClient(base_url="https://openrouter.test/api/v1", transport=httpx.MockTransport(wrapped))
    monkeypatch.setattr(agent.llm, "_http", lambda: client)

    def install(handler):
        state["handler"] = handler
        return sent

    return install


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch):
    s = Settings(_env_file=None, llm_models=["a", "b"], llm_zero_data_retention=True)
    monkeypatch.setattr(agent.llm, "get_settings", lambda: s)
    monkeypatch.setattr(agent.llm, "_no_zdr", set())  # what earlier tests learned about models
    breaker.install(None)
    yield s
    breaker.install(None)


async def ask(**kw):
    return await structured(system="s", user="u", schema=Out, **kw)


async def test_meta_records_model_provider_purpose_and_cost(transport):
    transport(lambda m, p: httpx.Response(503) if m == "a" else httpx.Response(200, json=ok_body("served/b")))

    out, meta = await ask(purpose="gate")

    assert out.answer == "42"
    assert meta | {"latency_ms": 0} == {
        "model": "served/b", "model_requested": "b", "provider": "Together", "purpose": "gate", "latency_ms": 0,
        "cost": 0.0001, "cost_total": 0.0001, "attempts": 2, "prompt_tokens": 10, "completion_tokens": 5, "zdr": True,
    }


async def test_zero_data_retention_is_requested_from_openrouter(transport, settings):
    sent = transport(lambda m, p: httpx.Response(200, json=ok_body(m)))
    await ask()
    assert sent[0]["provider"] == {"require_parameters": True, "data_collection": "deny", "zdr": True}

    settings.llm_zero_data_retention = False
    await ask()
    assert sent[1]["provider"] == {"require_parameters": True}


NO_ZDR = {"error": {"message": "No endpoints found matching your data policy (Zero data retention).", "code": 404}}


async def test_a_model_without_a_zdr_endpoint_is_skipped(transport):
    sent = transport(lambda m, p: httpx.Response(404, json=NO_ZDR) if m == "a" else httpx.Response(200, json=ok_body(m)))

    _, meta = await ask()

    assert [p["model"] for p in sent] == ["a", "b"] and meta["model_requested"] == "b"


async def test_models_the_startup_check_left_out_are_never_asked(transport, monkeypatch):
    sent = transport(lambda m, p: httpx.Response(200, json=ok_body(m)))
    monkeypatch.setattr(agent.llm, "_no_zdr", {"a"})
    _, meta = await ask()
    assert [p["model"] for p in sent] == ["b"] and meta["model_requested"] == "b"

    monkeypatch.setattr(agent.llm, "_no_zdr", {"a", "b"})  # none left: no request at all
    with pytest.raises(LLMUnavailable, match="no usable models"):
        await ask()
    assert len(sent) == 1


def test_the_default_models_all_have_zdr_endpoints_and_deepseek_comes_first():
    # a model that is not on OpenRouter's zero-data-retention endpoint list
    assert Settings(_env_file=None).llm_models == ["deepseek/deepseek-v4.1-flash", "qwen/qwen3.8-27b"]


def _zdr_list(monkeypatch: pytest.MonkeyPatch, handler) -> list[str]:
    paths: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return handler(request)

    client = httpx.AsyncClient(base_url="https://openrouter.test/api/v1", transport=httpx.MockTransport(wrapped))
    monkeypatch.setattr(agent.llm, "_http", lambda: client)
    return paths


async def test_startup_check_leaves_out_models_without_a_zdr_endpoint(monkeypatch, settings):
    settings.llm_models = ["a", "vendor/no-zdr-model", "b"]
    endpoints = {"data": [{"name": "X | a", "model_id": "a"}, {"name": "Y | b", "model_id": "b"},
                          {"name": "Z | b", "model_id": "b:free"}]}
    paths = _zdr_list(monkeypatch, lambda r: httpx.Response(200, json=endpoints))

    with structlog.testing.capture_logs() as logs:
        usable = await agent.llm.check_zero_data_retention()

    assert paths == ["/api/v1/endpoints/zdr"]
    assert usable == ["a", "b"] == agent.llm.usable_models()
    warning = next(e for e in logs if e["event"] == "llm.models_usable")
    assert warning["log_level"] == "warning" and warning["usable_models"] == ["a", "b"]
    assert warning["excluded"] == ["vendor/no-zdr-model"]


async def test_startup_check_with_no_model_left_says_so(monkeypatch, settings):
    _zdr_list(monkeypatch, lambda r: httpx.Response(200, json={"data": [{"model_id": "other"}]}))
    with structlog.testing.capture_logs() as logs:
        assert await agent.llm.check_zero_data_retention() == []
    assert any(e["event"] == "llm.no_usable_models" and e["log_level"] == "error" for e in logs)


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.Response(200, json={"unexpected": True}),
                                      httpx.Response(200, text="not json")])
async def test_a_failed_startup_check_leaves_the_models_as_configured(monkeypatch, response):
    _zdr_list(monkeypatch, lambda r: response)
    assert await agent.llm.check_zero_data_retention() == ["a", "b"]


async def test_startup_check_is_skipped_without_zero_data_retention(monkeypatch, settings):
    settings.llm_zero_data_retention = False
    paths = _zdr_list(monkeypatch, lambda r: httpx.Response(200, json={"data": []}))
    assert await agent.llm.check_zero_data_retention() == ["a", "b"] and paths == []


@pytest.mark.parametrize("body", [
    {"choices": None},
    {"choices": [{"message": "just text"}]},
    {"choices": [{"message": {"content": ["not", "a", "string"]}}]},
    {"choices": [None]},
    ["not", "an", "object"],
    {"error": {"message": "upstream exploded", "code": 502}},
])
async def test_odd_response_shapes_fall_through_to_the_next_model(transport, body):
    transport(lambda m, p: httpx.Response(200, json=body) if m == "a" else httpx.Response(200, json=ok_body(m)))

    _, meta = await ask()

    assert meta["model_requested"] == "b"


async def test_a_long_retry_after_moves_on_and_a_short_one_is_waited_out(transport):
    sent = transport(lambda m, p: httpx.Response(429, headers={"Retry-After": "30"}) if m == "a"
                     else httpx.Response(200, json=ok_body(m)))
    _, meta = await ask()
    assert [p["model"] for p in sent] == ["a", "b"]

    calls = []

    def flaky(m, p):
        calls.append(m)
        return httpx.Response(429, headers={"Retry-After": "0"}) if len(calls) == 1 else httpx.Response(200, json=ok_body(m))

    transport(flaky)
    _, meta = await ask()
    assert calls == ["a", "a"] and meta["model_requested"] == "a" and meta["attempts"] == 2


async def test_rate_limit_waits_are_bounded_per_call(transport, monkeypatch):
    monkeypatch.setattr(agent.llm, "RATE_LIMIT_WAIT_BUDGET_S", 0.0)
    sent = transport(lambda m, p: httpx.Response(429, headers={"Retry-After": "0.5"}))
    with pytest.raises(LLMUnavailable):
        await ask()
    assert [p["model"] for p in sent] == ["a", "b"]


class FakeBreaker:
    def __init__(self, open_: bool = False):
        self.open = open_
        self.records: list[BaseException | None] = []

    async def before_call(self) -> bool:
        if self.open:
            raise breaker.Open("x", 30)
        return False

    async def record(self, exc, *, probe=False):
        self.records.append(exc)

    async def release_probe(self):
        pass


class FakeBreakers:
    def __init__(self, **by_model: FakeBreaker):
        self.by_model = by_model

    def get(self, name: str) -> FakeBreaker:
        return self.by_model.setdefault(name.removeprefix("openrouter:"), FakeBreaker())


async def test_a_model_with_an_open_breaker_is_skipped(transport):
    sent = transport(lambda m, p: httpx.Response(200, json=ok_body(m)))
    breaker.install(FakeBreakers(a=FakeBreaker(open_=True)))  # type: ignore[arg-type]

    _, meta = await ask()

    assert [p["model"] for p in sent] == ["b"] and meta["attempts"] == 1


async def test_breakers_count_only_availability_failures(transport):
    fakes = FakeBreakers()
    breaker.install(fakes)  # type: ignore[arg-type]
    transport(lambda m, p: httpx.Response(503) if m == "a" else httpx.Response(200, json={"choices": None}))

    with pytest.raises(LLMUnavailable):
        await ask()

    (a_failure,) = fakes.by_model["a"].records
    assert breaker.is_availability_failure(a_failure)
    assert fakes.by_model["b"].records == [None]  # it answered, even if uselessly


async def test_with_no_usable_model_the_gate_answers_from_its_rules_and_summaries_are_skipped(monkeypatch):
    from datetime import UTC, datetime

    from agent.citations.claims import summarize_with_citations
    from agent.gate.classify import classify_with_meta
    from agent.models import MatterInfo

    def no_request():
        raise AssertionError("no request may leave with no usable model")

    monkeypatch.setattr(agent.llm, "_no_zdr", {"a", "b"})
    monkeypatch.setattr(agent.llm, "_http", no_request)

    parsed, meta = await classify_with_meta("Request", "What was decided? Send whatever has the decision, not the rest.")
    assert parsed.source == "rules_degraded" and meta is None

    info = MatterInfo(provider="uarb", matter="M12205", title="t", counts={}, portal_url="https://x",
                      fetched_at=datetime.now(UTC))
    with pytest.raises(LLMUnavailable):  # the pipeline sends its reply without a summary
        await summarize_with_citations(info, [])
