"""agent.citations.jev_check: one Choice per claim, same contract as ground.check_support, fails closed."""

import json

import httpx
import pytest
import respx

import agent.typesafe
from agent import breaker
from agent.citations import jev_check
from agent.citations.ground import SUPPORT_CONTEXT_CHARS, DroppedClaim, DropReason, GroundedClaim
from agent.config import Settings

URL = "https://typesafe.test/v1/systemone"
PAGE = "x" * 600 + "The Board approves the application for a total project cost of $59,143,000." + "y" * 600
START = PAGE.index("The Board")
END = START + len("The Board approves the application for a total project cost of $59,143,000.")


def claim(text: str, cid: str) -> GroundedClaim:
    return GroundedClaim(id=cid, claim=text, doc_external_id="D1", page=1, quote=PAGE[START:END],
                         char_start=START, char_end=END, score=100.0)


@pytest.fixture(autouse=True)
async def typesafe_settings(monkeypatch: pytest.MonkeyPatch):
    s = Settings(_env_file=None, typesafe_api_key="k", typesafe_base_url="https://typesafe.test",
                 typesafe_deadline_s=2.0)
    monkeypatch.setattr(agent.typesafe, "get_settings", lambda: s)
    monkeypatch.setattr(agent.typesafe, "BACKOFF_BASE_S", 0.0)
    await agent.typesafe.aclose()
    breaker.install(None)
    yield
    await agent.typesafe.aclose()


def answer(p_supported: float) -> dict:
    probs = {"supported": p_supported, "partially": 1 - p_supported, "not_supported": 0.0}
    top = max(probs, key=probs.get)
    return {"model": "jev-1.13.0", "usage": {"input_tokens": 900, "output_tokens": 40}, "answers": {
        "support": {"type": "choice", "choice": top, "probabilities": probs, "confidence": 0.5},
        "figure": {"type": "noul", "noul": 0.1}, "condition": {"type": "noul", "noul": 0.1}}}


@respx.mock
async def test_each_claim_is_checked_on_its_own_and_kept_only_if_supported():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        state = json.loads(request.content)["state"]
        sent.append(state)
        return httpx.Response(200, json=answer(0.95 if "59,143,000" in state["claim"] else 0.2))

    respx.post(URL).mock(side_effect=handler)
    good, bad = claim("The Board approved $59,143,000.", "a"), claim("The Board approved $61,845,000.", "b")

    kept, dropped, meta = await jev_check.verify_support([good, bad], {"D1": [PAGE]})

    assert [c.id for c in kept] == ["a"]
    assert [d.reason for d in dropped] == [DropReason.NOT_ENTAILED] and "partially" in dropped[0].detail
    assert len(sent) == 2 and all(s["quote"] == PAGE[START:END] for s in sent)
    assert sent[0]["context"] == PAGE[START - SUPPORT_CONTEXT_CHARS : END + SUPPORT_CONTEXT_CHARS]
    assert meta["requests"] == 2 and meta["input_tokens"] == 1800 and meta["provider"] == "typesafe"


@respx.mock
async def test_a_failed_request_is_rechecked_by_the_llm_check(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        state = json.loads(request.content)["state"]
        return httpx.Response(503) if "second" in state["claim"] else httpx.Response(200, json=answer(0.9))

    respx.post(URL).mock(side_effect=handler)
    seen = []

    async def llm_check(claims, pages_by_doc=None):
        seen.extend(c.id for c in claims)
        return [], [DroppedClaim(draft=c.as_draft(), reason=DropReason.SUPPORT_CHECK_FAILED) for c in claims], {}

    monkeypatch.setattr(jev_check, "llm_verify_support", llm_check)
    first, second = claim("first: approved $59,143,000.", "a"), claim("second: approved $59,143,000.", "b")

    kept, dropped = await jev_check.check_support([first, second], {"D1": [PAGE]})

    assert seen == ["b"]  # only the claim Jev couldn't answer
    assert [c.id for c in kept] == ["a"] and [d.reason for d in dropped] == [DropReason.SUPPORT_CHECK_FAILED]


@respx.mock
async def test_the_meta_names_jev_and_the_llm_recheck_as_separate_calls(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        state = json.loads(request.content)["state"]
        return httpx.Response(503) if "second" in state["claim"] else httpx.Response(200, json=answer(0.9))

    respx.post(URL).mock(side_effect=handler)

    async def llm_check(claims, pages_by_doc=None):
        return list(claims), [], {"model": "fake/llm", "provider": "deepseek", "cost_total": 0.0005}

    monkeypatch.setattr(jev_check, "llm_verify_support", llm_check)
    first, second = claim("first: approved $59,143,000.", "a"), claim("second: approved $59,143,000.", "b")

    kept, _, meta = await jev_check.verify_support([first, second], {"D1": [PAGE]})

    assert [c.id for c in kept] == ["a", "b"]
    assert (meta["provider"], meta["model"], meta["input_tokens"], meta["output_tokens"]) == (
        "typesafe", "jev-1.13.0", 900, 40)
    # Jev's own cost only: the LLM re-check is audited (and budgeted) as its own call
    assert meta["cost"] == meta["cost_total"] == 0.0000378 and meta["zdr"] is False
    assert (meta["escalated"], meta["escalation_reason"], meta["escalation"]["model"]) == (
        True, "unavailable", "fake/llm")


@respx.mock
async def test_with_everything_down_the_claims_are_dropped_and_both_calls_say_so(monkeypatch):
    respx.post(URL).mock(return_value=httpx.Response(503))

    async def llm_down(claims, pages_by_doc=None):
        return [], [DroppedClaim(draft=c.as_draft(), reason=DropReason.SUPPORT_CHECK_FAILED) for c in claims], {}

    monkeypatch.setattr(jev_check, "llm_verify_support", llm_down)

    kept, dropped, meta = await jev_check.verify_support([claim("approved $59,143,000.", "a")], {"D1": [PAGE]})

    assert kept == [] and [d.reason for d in dropped] == [DropReason.SUPPORT_CHECK_FAILED]
    assert (meta["outcome"], meta["escalated"]) == ("unavailable", True)
    assert meta["escalation"] == {"purpose": "support_check", "outcome": "unavailable"}


async def test_no_claims_no_requests():
    assert await jev_check.verify_support([]) == ([], [], {})
