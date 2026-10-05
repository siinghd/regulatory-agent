"""OpenRouter failure handling in agent.llm.structured (the real function, fake transport)."""

import asyncio
import json
import time

import httpx
import pytest
from pydantic import BaseModel

import agent.llm
from agent.llm import LLMUnavailable
from agent.llm import structured as real_structured  # bound before the fixture swaps in FakeLLM

pytestmark = pytest.mark.integration


class Out(BaseModel):
    answer: str


def ok_body(model: str) -> dict:
    return {"model": model, "choices": [{"message": {"content": json.dumps({"answer": "42"})},
                                         "finish_reason": "stop"}], "usage": {"cost": 0.0001}}


def use_transport(monkeypatch, handler) -> list[tuple[str, float]]:
    """Route agent.llm's client through `handler`; returns [(model, t)] for every request."""
    calls: list[tuple[str, float]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append((model, time.monotonic()))
        return handler(model, len(calls))

    client = httpx.AsyncClient(base_url="https://openrouter.test/api/v1", transport=httpx.MockTransport(wrapped))
    monkeypatch.setattr(agent.llm, "_http", lambda: client)
    return calls


async def ask(models, **kw):
    return await real_structured(system="s", user="u", schema=Out, models=models, **kw)


async def test_429_fails_over_to_the_next_model_immediately(h, monkeypatch):
    """Evidence: 429 + Retry-After on model A -> model B at once (no backoff, header ignored)."""
    calls = use_transport(monkeypatch, lambda m, n: httpx.Response(429, headers={"Retry-After": "5"})
                          if m == "a" else httpx.Response(200, json=ok_body(m)))
    out, meta = await ask(["a", "b"])
    assert out.answer == "42" and meta["attempts"] == 2
    assert calls[1][1] - calls[0][1] < 0.5


async def test_a_short_rate_limit_is_waited_out(h, monkeypatch):
    use_transport(monkeypatch, lambda m, n: httpx.Response(429, headers={"Retry-After": "1"})
                  if n == 1 else httpx.Response(200, json=ok_body(m)))
    out, _ = await ask(["only-model"])
    assert out.answer == "42"


@pytest.mark.parametrize(("name", "response"), [
    ("5xx", lambda m: httpx.Response(503, text="upstream down")),
    ("malformed-json", lambda m: httpx.Response(200, json={**ok_body(m), "choices": [
        {"message": {"content": '{"answer": "4'}, "finish_reason": "length"}]})),
    ("empty-content", lambda m: httpx.Response(200, json={**ok_body(m), "choices": [
        {"message": {"content": ""}, "finish_reason": "stop"}]})),
    ("schema-mismatch", lambda m: httpx.Response(200, json={**ok_body(m), "choices": [
        {"message": {"content": '{"other": 1}'}, "finish_reason": "stop"}]})),
    ("html-error-page", lambda m: httpx.Response(200, text="<html>Bad gateway</html>")),
])
async def test_every_model_failing_raises_llm_unavailable(h, monkeypatch, name, response):
    """Evidence (correct): each failure shape falls through every model, then LLMUnavailable."""
    calls = use_transport(monkeypatch, lambda m, n: response(m))
    with pytest.raises(LLMUnavailable):
        await ask(["a", "b", "c"])
    assert [m for m, _ in calls] == ["a", "b", "c"]


async def test_connect_error_is_unavailable(h, monkeypatch):
    def boom(m, n):
        raise httpx.ConnectError("dns failure")
    use_transport(monkeypatch, boom)
    with pytest.raises(LLMUnavailable):
        await ask(["a", "b"])


# ------------------------------------------------------------------ total deadline


async def _trickle_server(trickle_s: float):
    """HTTP server that answers like OpenRouter under load: 200, then whitespace keep-alives, then
    the JSON. Each byte arrives well inside httpx's read timeout."""
    writers = []

    async def handle(reader, writer):
        writers.append(writer)
        head = await reader.readuntil(b"\r\n\r\n")
        length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length")))
        await reader.readexactly(length)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
        t0 = time.monotonic()
        while time.monotonic() - t0 < trickle_s:
            writer.write(b"1\r\n \r\n")
            await writer.drain()
            await asyncio.sleep(0.2)
        body = json.dumps(ok_body("slow")).encode()
        writer.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
        await writer.drain()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], writers


async def test_timeout_is_a_total_deadline(h, monkeypatch):
    server, port, writers = await _trickle_server(trickle_s=4.0)
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    monkeypatch.setattr(agent.llm, "_http", lambda: client)
    t0 = time.monotonic()
    try:
        try:
            await ask(["slow"], timeout_s=1.0)
        except LLMUnavailable:
            pass
        elapsed = time.monotonic() - t0
    finally:
        await client.aclose()
        for w in writers:
            w.close()
        server.close()
    assert elapsed < 2.0, f"a 1 s timeout took {elapsed:.1f} s"


# ------------------------------------------------------------------ classifier outage in the pipeline


async def test_llm_outage_does_not_fetch_the_category_the_user_excluded(h):
    from tests.integration.harness import make_email

    h.llm.parse = None  # every classifier model unavailable
    rid = await h.ingest(make_email("I need the hearing evidence for matter 12205, not the transcripts"))
    await h.run_job(rid)
    row = await h.request(rid)
    assert row["doc_type"] != "Transcripts", f"state={row['state']} doc_type={row['doc_type']}"


async def test_llm_outage_turns_an_unparseable_request_into_a_question(h):
    """Evidence: no retry of the gate while the LLM is down; the user gets a final clarifying reply."""
    from tests.integration.harness import make_email

    h.llm.parse = None
    rid = await h.ingest(make_email("Could you send me the evidence filed in M12205?"))
    defers = await h.run_job(rid)
    row = await h.request(rid)
    assert defers == [] and row["state"] == "clarify" and row["parsed"]["source"] == "rules_degraded"
