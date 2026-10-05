"""agent.typesafe.ask against a mocked TypeSafe API (respx): typed answers, retries, deadline, breaker, meta."""

import asyncio
import json

import httpx
import pytest
import respx

import agent.llm
import agent.typesafe
from agent import breaker
from agent.config import Settings
from agent.typesafe import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    TypeSafeUnavailable,
    ask,
)

URL = "https://typesafe.test/v1/systemone"

QUESTIONS = {
    "intent": Choice(instructions="Which?", criteria={"a": "first", "b": None}),
    "urgent": Noul(instructions="Urgent?", true="time-sensitive"),
    "tone": Score(instructions="How calm?", levels=["calm", "upset", "angry"]),
}


def body(**override) -> dict:
    answers = {
        "intent": {"type": "choice", "choice": "a", "probabilities": {"a": 0.9, "b": 0.1}, "confidence": 0.8},
        "urgent": {"type": "noul", "noul": 0.95},
        "tone": {"type": "score", "score": 1.1, "legend": {"0": "calm", "1": "upset", "2": "angry"},
                 "probabilities": {"0": 0.0, "1": 0.9, "2": 0.1}, "confidence": 0.85},
    }
    answers.update(override)
    return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": 1000, "output_tokens": 30}}


@pytest.fixture(autouse=True)
async def settings(monkeypatch: pytest.MonkeyPatch):
    s = Settings(_env_file=None, typesafe_api_key="test-key", typesafe_base_url="https://typesafe.test",
                 typesafe_deadline_s=2.0)
    monkeypatch.setattr(agent.typesafe, "get_settings", lambda: s)
    monkeypatch.setattr(agent.typesafe, "BACKOFF_BASE_S", 0.0)
    await agent.typesafe.aclose()
    breaker.install(None)
    yield s
    breaker.install(None)
    await agent.typesafe.aclose()


@respx.mock
async def test_answers_are_typed_and_meta_is_ready_for_the_audit_log():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=body(), headers={"x-typesafe-request-id": "r1"}))

    result = await ask({"email": "hi"}, QUESTIONS, purpose="gate")

    intent, urgent, tone = result.answers["intent"], result.answers["urgent"], result.answers["tone"]
    assert isinstance(intent, ChoiceAnswer) and intent.choice == "a" and intent.p("b") == 0.1
    assert isinstance(urgent, NoulAnswer) and result.noul("urgent") == 0.95
    assert isinstance(tone, ScoreAnswer) and tone.probabilities == {0: 0.0, 1: 0.9, 2: 0.1}
    assert result.meta == {
        "model": "jev-1.13.0", "model_requested": "jev-1.13.0", "provider": "typesafe", "purpose": "gate",
        "latency_ms": result.meta["latency_ms"], "input_tokens": 1000, "output_tokens": 30,
        "cost": 0.000042, "cost_total": 0.000042, "attempts": 1, "questions": 3, "typesafe_request_id": "r1", "zdr": False,
    }
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer test-key"
    sent = json.loads(request.content)
    assert sent["state"] == {"email": "hi"} and sent["model"] == "jev-1.13.0"
    assert sent["questions"]["intent"] == {"type": "choice", "instructions": "Which?", "criteria": {"a": "first", "b": None}}
    assert sent["questions"]["urgent"] == {"type": "noul", "instructions": "Urgent?", "criteria": {"true": "time-sensitive"}}
    assert sent["questions"]["tone"]["criteria"] == ["calm", "upset", "angry"]


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 502, 503, 529])
async def test_busy_answers_are_retried(status):
    route = respx.post(URL).mock(side_effect=[httpx.Response(status), httpx.Response(200, json=body())])

    result = await ask("s", QUESTIONS, purpose="gate")

    assert route.call_count == 2 and result.meta["attempts"] == 2


@respx.mock
async def test_retries_stop_after_max_attempts():
    route = respx.post(URL).mock(return_value=httpx.Response(503))

    with pytest.raises(TypeSafeUnavailable, match="HTTP 503"):
        await ask("s", QUESTIONS, purpose="gate")

    assert route.call_count == agent.typesafe.MAX_ATTEMPTS


@respx.mock
async def test_a_short_retry_after_is_waited_out_and_a_long_one_fails_fast():
    route = respx.post(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "0"}),
                                              httpx.Response(200, json=body())])
    assert (await ask("s", QUESTIONS, purpose="gate")).meta["attempts"] == 2

    route.mock(side_effect=None, return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    route.reset()
    with pytest.raises(TypeSafeUnavailable):
        await ask("s", QUESTIONS, purpose="gate")
    assert route.call_count == 1


@respx.mock
async def test_retry_waits_are_bounded_per_call(monkeypatch):
    monkeypatch.setattr(agent.typesafe, "RETRY_WAIT_BUDGET_S", 0.5)
    route = respx.post(URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "1"}))

    with pytest.raises(TypeSafeUnavailable):
        await ask("s", QUESTIONS, purpose="gate")

    assert route.call_count == 1  # waiting 1 s would overspend the 0.5 s budget


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 422])
async def test_a_rejected_request_is_not_retried(status):
    route = respx.post(URL).mock(return_value=httpx.Response(status, json={"detail": "bad"}))

    with pytest.raises(TypeSafeUnavailable, match=f"HTTP {status}"):
        await ask("s", QUESTIONS, purpose="gate")

    assert route.call_count == 1


@respx.mock
async def test_transport_errors_are_retried():
    route = respx.post(URL).mock(side_effect=[httpx.ConnectError("refused"), httpx.Response(200, json=body())])

    assert (await ask("s", QUESTIONS, purpose="gate")).meta["attempts"] == 2
    assert route.call_count == 2


@respx.mock
async def test_the_deadline_covers_the_whole_call(settings):
    settings.typesafe_deadline_s = 0.05

    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=body())

    respx.post(URL).mock(side_effect=slow)

    with pytest.raises(TypeSafeUnavailable, match="within 0.05s"):
        await ask("s", QUESTIONS, purpose="gate")


@respx.mock
@pytest.mark.parametrize(
    "bad",
    [
        {"intent": {"type": "choice", "choice": "c", "probabilities": {"c": 1.0}, "confidence": 1.0}},  # not offered
        {"intent": {"type": "noul", "noul": 0.5}},  # wrong type
        {"urgent": {"type": "noul", "noul": 1.7}},  # not a probability
        {"urgent": {"type": "noul", "noul": True}},
        {"tone": {"type": "score", "score": 9, "legend": {}, "probabilities": {"9": 1.0}, "confidence": 1}},
        {"urgent": None},  # missing
    ],
)
async def test_answers_outside_the_questions_are_errors_not_answers(bad):
    respx.post(URL).mock(return_value=httpx.Response(200, json=body(**bad)))

    with pytest.raises(TypeSafeUnavailable):
        await ask("s", QUESTIONS, purpose="gate")


@respx.mock
async def test_a_non_json_body_is_an_error():
    respx.post(URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))

    with pytest.raises(TypeSafeUnavailable, match="not JSON"):
        await ask("s", QUESTIONS, purpose="gate")


def test_question_limits_are_checked_before_sending():
    with pytest.raises(ValueError):
        Choice(instructions="?", criteria={"only": None}).payload()
    with pytest.raises(ValueError):
        Score(instructions="?", levels=[str(i) for i in range(11)]).payload()


class FakeBreaker:
    def __init__(self, open_: bool = False):
        self.open = open_
        self.records: list[BaseException | None] = []

    async def before_call(self) -> bool:
        if self.open:
            raise breaker.Open("typesafe", 30)
        return False

    async def record(self, exc, *, probe=False):
        self.records.append(exc)

    async def release_probe(self):
        pass


class FakeBreakers:
    def __init__(self, b: FakeBreaker):
        self.b = b
        self.names: list[str] = []

    def get(self, name: str) -> FakeBreaker:
        self.names.append(name)
        return self.b


@respx.mock
async def test_an_open_breaker_fails_without_a_request():
    route = respx.post(URL).mock(return_value=httpx.Response(200, json=body()))
    breaker.install(FakeBreakers(FakeBreaker(open_=True)))  # type: ignore[arg-type]

    with pytest.raises(TypeSafeUnavailable, match="circuit open"):
        await ask("s", QUESTIONS, purpose="gate")

    assert route.call_count == 0


@respx.mock
async def test_the_breaker_counts_only_availability_failures():
    fake = FakeBreaker()
    breakers = FakeBreakers(fake)
    breaker.install(breakers)  # type: ignore[arg-type]
    route = respx.post(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(TypeSafeUnavailable):
        await ask("s", QUESTIONS, purpose="gate")
    route.mock(return_value=httpx.Response(200, json=body(urgent={"type": "noul", "noul": 2})))
    with pytest.raises(TypeSafeUnavailable):
        await ask("s", QUESTIONS, purpose="gate")
    route.mock(return_value=httpx.Response(200, json=body()))
    await ask("s", QUESTIONS, purpose="gate")

    busy, malformed, ok = fake.records
    assert breakers.names == ["typesafe"] * 3
    assert breaker.is_availability_failure(busy)  # one record per call, after its retries
    assert not breaker.is_availability_failure(malformed)  # it answered, if uselessly
    assert ok is None


# ---------------------------------------------------------------- settings and the worker's start/stop


def test_the_model_is_pinned_and_jev_is_the_default_gate_and_checker():
    s = Settings(_env_file=None)
    assert s.typesafe_model == "jev-1.13.0"  # the version the thresholds were tuned on, not an alias
    assert (s.gate_classifier, s.gate_jev_low_confidence, s.citation_check) == ("jev", "llm", "jev")
    assert s.typesafe_base_url == "https://api.typesafe.ai" and s.typesafe_api_key.get_secret_value() == ""


def test_the_worker_says_at_start_what_goes_to_typesafe_without_zero_retention(monkeypatch):
    from structlog.testing import capture_logs

    from agent import worker

    s = Settings(_env_file=None, typesafe_api_key="k", llm_models=["deepseek/deepseek-v4.1-flash"])
    monkeypatch.setattr(agent.llm, "get_settings", lambda: s)
    with capture_logs() as logs:
        worker.log_model_policy(s)

    (models,) = [e for e in logs if e["event"] == "worker.models"]
    assert (models["gate_classifier"], models["gate_jev_low_confidence"], models["citation_check"]) == (
        "jev", "llm", "jev")
    assert models["typesafe_model"] == "jev-1.13.0" and models["llm_models"] == ["deepseek/deepseek-v4.1-flash"]
    notes = [e["note"] for e in logs if e["event"] == "typesafe.data_policy"]
    assert notes == [
        "TypeSafe Jev: email text is sent to TypeSafe for classification; not zero-retention",
        "TypeSafe Jev: public document excerpts are sent to TypeSafe for citation checks; not zero-retention",
    ]
    assert not [e for e in logs if e["event"] == "typesafe.no_api_key"]

    with capture_logs() as logs:
        worker.log_model_policy(Settings(_env_file=None, gate_classifier="llm", citation_check="llm"))
    assert [e["event"] for e in logs] == ["worker.models"]  # nothing goes to TypeSafe

    with capture_logs() as logs:
        worker.log_model_policy(Settings(_env_file=None))  # Jev on, no key: every call would fail over
    assert [e["log_level"] for e in logs if e["event"] == "typesafe.no_api_key"] == ["warning"]


async def test_the_worker_closes_the_typesafe_client_on_shutdown(monkeypatch):
    from types import SimpleNamespace

    from agent import db, worker

    class Closable:
        async def close(self) -> None: ...

        async def aclose(self) -> None: ...

    async def no_pool() -> None: ...

    monkeypatch.setattr(db, "close_pool", no_pool)
    client = agent.typesafe._http()
    assert agent.typesafe._client is client
    ctx = {"browsers": Closable(), "oeb_client": Closable(), "ferc_client": Closable(),
           "deps": SimpleNamespace(drop=None)}

    await worker.shutdown(ctx)

    assert agent.typesafe._client is None and client.is_closed
