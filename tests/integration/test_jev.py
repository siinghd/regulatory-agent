"""The Jev path end to end (gate_classifier / citation_check = "jev"), against real Postgres and Redis
with a fake TypeSafe API: the hybrid gate's escalation, the audit trail of every model call
(provider, model, tokens, cost, escalation) and TypeSafe's cost in the daily model budget."""

import json
from typing import Any

import httpx
import pytest

import agent.typesafe
from agent import limits, store

from .harness import MATTER, SUMMARY_TEXT, body_text, llm_parse, make_email

pytestmark = pytest.mark.integration

VAGUE = "Could you send me stuff for M12205?"  # no document type: the rules hand it to the classifier
JEV_TOKENS = 2000
JEV_COST = JEV_TOKENS * agent.typesafe.PRICE_PER_MTOK_INPUT_USD / 1e6


class FakeTypeSafe:
    """Answers every question asked: a confident document request for the first candidate matter and
    `category` (a {option: p} map, default none), Nouls 0.05, support checks `supported`."""

    def __init__(self) -> None:
        self.category: dict[str, float] = {}
        self.supported = 0.95
        self.status = 200
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.status != 200:
            return httpx.Response(self.status)
        answers = {}
        for qid, q in payload["questions"].items():
            if q["type"] == "noul":
                answers[qid] = {"type": "noul", "noul": 0.05}
                continue
            options = list(q["criteria"])
            want = {"intent": {"document_request": 1.0}, "count": {"not_stated": 1.0},
                    "support": {"supported": self.supported, "partially": 1 - self.supported}}.get(qid)
            if qid == "matter":
                want = {options[0]: 1.0}
            elif qid.startswith("category."):
                want = self.category or {"none": 1.0}
            probs = {o: float((want or {}).get(o, 0.0)) for o in options}
            top = max(probs, key=probs.get)
            answers[qid] = {"type": "choice", "choice": top, "probabilities": probs, "confidence": probs[top]}
        return httpx.Response(200, json={"model": "jev-1.13.0", "answers": answers,
                                         "usage": {"input_tokens": JEV_TOKENS, "output_tokens": 100}})

    def asked(self, purpose: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if ("support" in r["questions"]) == (purpose == "support_check")]


@pytest.fixture
async def jev(h, monkeypatch: pytest.MonkeyPatch):
    h.configure(gate_classifier="jev", gate_jev_low_confidence="llm")
    fake = FakeTypeSafe()
    client = httpx.AsyncClient(base_url="https://typesafe.test", transport=httpx.MockTransport(fake))
    monkeypatch.setattr(agent.typesafe, "_http", lambda: client)
    monkeypatch.setattr(agent.typesafe, "BACKOFF_BASE_S", 0.0)
    yield fake
    await client.aclose()


async def served(h, body: str, from_addr: str = "alice@example.com"):
    rid = await h.ingest(make_email(body, from_addr=from_addr))
    await h.run_job(rid)
    return rid


async def model_calls(rid) -> list[dict]:
    return [dict(e["data"]) for e in await store.events(rid) if e["kind"] == "llm.call"]


async def test_a_jev_parse_is_audited_with_typesafes_model_tokens_and_cost(h, jev):
    jev.category = {"Other Documents": 0.97, "none": 0.03}

    rid = await served(h, VAGUE)

    row = await h.request(rid)
    assert row["state"] == "done" and row["doc_type"] == "Other Documents"
    assert h.llm.calls["_LLMParse"] == 0 and len(jev.asked("gate")) == 1
    (gate,) = [c for c in await model_calls(rid) if c["purpose"] == "gate"]
    assert (gate["provider"], gate["model"], gate["data_class"], gate["zdr"]) == (
        "typesafe", "jev-1.13.0", "email_body", False)
    assert (gate["input_tokens"], gate["output_tokens"], gate["escalated"], gate["outcome"]) == (
        JEV_TOKENS, 100, False, "ok")
    assert gate["cost"] == pytest.approx(JEV_COST)
    assert await limits.Budgets(h.redis).llm_spent() == pytest.approx(JEV_COST)  # the fake LLM costs $0


async def test_an_unsure_jev_parse_goes_to_the_llm_and_both_calls_are_audited(h, jev):
    jev.category = {"Other Documents": 0.45, "none": 0.4, "Exhibits": 0.15}  # too weak to act on
    h.llm.parse = llm_parse(matter=MATTER, doc_type="Other Documents")

    rid = await served(h, VAGUE)

    row = await h.request(rid)
    assert row["state"] == "done" and row["doc_type"] == "Other Documents"
    assert h.llm.calls["_LLMParse"] == 1
    jev_call, llm_call = [c for c in await model_calls(rid) if c["purpose"] == "gate"]
    assert (jev_call["provider"], jev_call["escalated"], jev_call["escalation_reason"]) == (
        "typesafe", True, "low_confidence")
    assert (llm_call["model"], llm_call["escalated_from"], llm_call["zdr"]) == ("fake/llm", "typesafe", True)


async def test_with_typesafe_down_the_llm_answers_and_the_trail_says_why(h, jev):
    jev.status = 503
    h.llm.parse = llm_parse(matter=MATTER, doc_type="Other Documents")

    rid = await served(h, VAGUE)

    assert (await h.request(rid))["state"] == "done" and h.llm.calls["_LLMParse"] == 1
    jev_call, llm_call = [c for c in await model_calls(rid) if c["purpose"] == "gate"]
    assert (jev_call["provider"], jev_call["outcome"], jev_call["escalation_reason"]) == (
        "typesafe", "unavailable", "unavailable")
    assert jev_call["cost"] is None and llm_call["outcome"] == "ok"


async def test_typesafe_spend_counts_against_the_daily_budget(h, jev):
    h.configure(llm_daily_budget_usd=JEV_COST / 2)  # one Jev call spends the day
    jev.category = {"Other Documents": 0.97, "none": 0.03}

    first = await served(h, VAGUE)
    second = await served(h, "Could you send me things for M12205?", from_addr="bob@example.org")

    assert len(jev.asked("gate")) == 1  # the second email got the rules alone
    assert (await h.request(first))["state"] == "done"
    assert "summary_skipped" in await h.event_kinds(first)  # the budget was spent by the gate
    assert (await h.request(second))["state"] == "clarify"
    assert await limits.Budgets(h.redis).llm_spent() == pytest.approx(JEV_COST)


async def test_citation_check_jev_checks_the_summary_claims_with_typesafe(h, jev):
    h.configure(citation_check="jev")
    jev.category = {"Other Documents": 0.97, "none": 0.03}

    rid = await served(h, VAGUE)

    assert SUMMARY_TEXT in body_text(h.reply(rid))
    assert h.llm.calls["_Verdicts"] == 0 and jev.asked("support_check")
    (check,) = [c for c in await model_calls(rid) if c["purpose"] == "support_check"]
    assert (check["provider"], check["data_class"], check["zdr"], check["escalated"]) == (
        "typesafe", "public_document", False, False)
    assert check["input_tokens"] == JEV_TOKENS * len(jev.asked("support_check"))
