"""The gate with both real regulators registered: matter numbers, categories, the LLM's limits."""

from typing import Any

import pytest
from pydantic import BaseModel

import agent.llm
from agent.gate import rules
from agent.gate.classify import clarification_for, classify, system_prompt
from agent.llm import LLMUnavailable
from agent.models import Intent
from agent.providers import base as providers_base
from agent.providers import oeb
from agent.providers.browser import BrowserPool
from agent.providers.oeb import OebProvider
from agent.providers.uarb import UarbProvider

OEB_TYPES = (
    "Decisions and Orders, Procedural Orders, Application and Evidence, Interrogatories, Undertakings, "
    "Submissions and Arguments, Transcripts, Cost Claims, Correspondence"
)


@pytest.fixture(autouse=True)
async def providers(monkeypatch: pytest.MonkeyPatch):
    """Both regulators as the worker registers them; nothing here touches a portal."""
    uarb = UarbProvider(BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000))
    client = oeb.make_client()
    registered = {"uarb": uarb, "oeb": OebProvider(client)}
    monkeypatch.setattr(providers_base, "_REGISTRY", {n: (lambda p=p: p) for n, p in registered.items()})
    yield registered
    await client.aclose()


class FakeLLM:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer  # None: every model is down
        self.systems: list[str] = []

    async def structured(self, *, system: str, user: str, schema: type[BaseModel], **_: Any):
        self.systems.append(system)
        if self.answer is None:
            raise LLMUnavailable("fake: down")
        return schema.model_validate(self.answer), {"model": "fake/llm"}


def llm_answer(matter=None, doc_type=None, *, intent="document_request", other_matters=(), other_doc_types=(),
               clarification=None, max_docs=10) -> dict[str, Any]:
    return {
        "intent": intent, "matter": matter, "other_matters": list(other_matters), "doc_type": doc_type,
        "other_doc_types": list(other_doc_types), "max_docs": max_docs, "clarification": clarification,
        "confidence": 0.9,
    }


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM(None)
    monkeypatch.setattr(agent.llm, "structured", fake.structured)
    return fake


# ---------------------------------------------------------------- registry and vocabularies


def test_every_provider_has_a_clean_vocabulary(providers):
    for provider in providers.values():
        names = [c.name for c in provider.categories]
        aliases = [a for c in provider.categories for a in c.aliases]
        assert len(set(names)) == len(names), provider.name
        assert len(set(aliases)) == len(aliases), provider.name
        assert all(a == a.lower().strip() and a for a in aliases), provider.name
        assert all(c.description for c in provider.categories), provider.name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("M12205", ("uarb", "M12205")),
        ("m 12205", ("uarb", "M12205")),
        ("matter no. 12205", ("uarb", "M12205")),
        ("eb 2024 0111", ("oeb", "EB-2024-0111")),
        ("EB2024-0111", ("oeb", "EB-2024-0111")),
        ("12205", None),
        ("EB-2024-111", None),
    ],
)
def test_normalise_matter_picks_the_regulator(raw, expected):
    assert providers_base.normalise_matter(raw) == expected


def test_provider_for_matter_uses_the_canonical_form(providers):
    assert providers_base.provider_for_matter("EB-2024-0111") is providers["oeb"]
    assert providers_base.provider_for_matter("M12205") is providers["uarb"]
    assert providers_base.provider_for_matter("eb 2024 0111") is None


# ---------------------------------------------------------------- rules


@pytest.mark.parametrize(
    ("text", "matters"),
    [
        ("Please send EB-2024-0111", ("EB-2024-0111",)),
        ("eb 2023 0195 interrogatories please", ("EB-2023-0195",)),
        ("M12205 and then EB-2024-0111", ("M12205", "EB-2024-0111")),
        ("EB-2024-0111 and then M12205", ("EB-2024-0111", "M12205")),
        ("EB-2024-0111, i.e. eb 2024 0111", ("EB-2024-0111",)),
        ("not ours: XEB-2024-0111, EB-2024-01112, AM123456", ()),
    ],
)
def test_find_matters_across_regulators_in_mention_order(text, matters):
    assert rules.find_matters(text) == matters


@pytest.mark.parametrize(
    ("body", "matter", "doc_type", "max_docs"),
    [
        ("Can you send me the decisions for EB-2024-0111?", "EB-2024-0111", "Decisions and Orders", 10),
        ("eb 2023 0195 interrogatories please", "EB-2023-0195", "Interrogatories", 10),
        ("Please send the first 3 IR responses for EB-2025-0064", "EB-2025-0064", "Interrogatories", 3),
        ("Could you send the procedural orders for EB-2024-0111?", "EB-2024-0111", "Procedural Orders", 10),
        ("Please send the latest 2 letters of comment on EB-2024-0111", "EB-2024-0111",
         "Submissions and Arguments", 2),
        ("Can you send me the Other Documents for M12205?", "M12205", "Other Documents", 10),
        ("Please send 4 key docs for matter 12205", "M12205", "Key Documents", 4),
    ],
)
def test_rules_fast_path_uses_the_matters_own_categories(body, matter, doc_type, max_docs):
    result = rules.parse("Document request", body)

    assert result.reason == "ok", result
    assert (result.parsed.matter, result.parsed.doc_type, result.parsed.max_docs) == (matter, doc_type, max_docs)
    assert result.parsed.source == "rules"


@pytest.mark.parametrize(
    "body",
    [
        "Can you send me the exhibits for EB-2024-0111?",  # an OEB case has no Exhibits category
        "Can you send me the recordings for EB-2024-0111?",
        "Can you send me the decisions for M12205?",  # nor a UARB matter Decisions and Orders
    ],
)
def test_another_regulators_category_is_not_recognised(body):
    assert rules.parse("Request", body).reason == "no_doc_type"


def test_mixed_regulators_go_to_the_llm_with_the_first_matters_categories():
    result = rules.parse("Request", "Please send the Exhibits for M12205 and the decisions for EB-2024-0111.")

    assert result.parsed is None and result.reason == "multiple_matters"
    assert result.matters == ("M12205", "EB-2024-0111")
    assert result.doc_types == ("Exhibits",)


# ---------------------------------------------------------------- classifier


async def test_rules_answer_without_calling_the_llm(llm):
    parsed = await classify("Request", "Can you send me the decisions for EB-2024-0111?")

    assert (parsed.matter, parsed.doc_type, parsed.source) == ("EB-2024-0111", "Decisions and Orders", "rules")
    assert llm.systems == []


async def test_llm_prompt_lists_each_regulator_with_its_format_and_categories(llm, providers):
    llm.answer = llm_answer("EB-2024-0111", "Decisions and Orders")

    await classify("Request", "What did the Board conclude in EB-2024-0111? Send me whatever has it.")

    (system,) = llm.systems
    assert system == system_prompt(list(providers.values()))
    for provider in providers.values():
        assert f"{provider.display_name}: matter numbers look like {provider.matter_example}" in system
        for category in provider.categories:
            assert f"  - {category.name}: {category.description}" in system


async def test_llm_category_is_matched_case_insensitively_to_the_matters_regulator(llm):
    llm.answer = llm_answer("eb 2024 0111", "decisions and orders")

    parsed = await classify("Request", "What did the Board conclude in eb 2024 0111? Send me whatever has it.")

    assert (parsed.matter, parsed.doc_type, parsed.source) == ("EB-2024-0111", "Decisions and Orders", "llm")
    assert parsed.needs_clarification is None


async def test_llm_category_from_another_regulator_becomes_a_clarification(llm):
    llm.answer = llm_answer("EB-2024-0111", "Key Documents")

    parsed = await classify("Request", "Send me the important stuff for EB-2024-0111")

    assert parsed.matter == "EB-2024-0111" and parsed.doc_type is None
    assert parsed.needs_clarification == f"Which document type would you like for EB-2024-0111: {OEB_TYPES}?"


async def test_llm_cannot_invent_a_matter(llm):
    llm.answer = llm_answer("EB-2023-0195", "Transcripts")

    parsed = await classify("Request", "Send me the hearing records for the Enbridge case")

    assert parsed.matter is None
    assert "EB-2024-0111 (Ontario Energy Board)" in parsed.needs_clarification
    assert "M12205 (Nova Scotia Utility and Review Board)" in parsed.needs_clarification


async def test_mixed_regulators_handle_the_first_matter_and_note_the_other(llm):
    llm.answer = llm_answer(
        "M12205", "Exhibits", other_matters=["EB-2024-0111"], other_doc_types=["Decisions and Orders", "Transcripts"]
    )

    parsed = await classify("Request", "Please send the Exhibits for M12205 and the decisions for EB-2024-0111.")

    assert (parsed.matter, parsed.doc_type) == ("M12205", "Exhibits")
    assert parsed.extra_matters == ("EB-2024-0111",)
    assert parsed.extra_doc_types == ("Transcripts",)  # the OEB category belongs to the other matter


async def test_llm_cannot_override_a_category_the_text_names_unambiguously(llm):
    llm.answer = llm_answer("EB-2024-0111", "Transcripts")

    parsed = await classify("Request", "Why is this so hard? Please send the interrogatories for EB-2024-0111")

    assert parsed.doc_type == "Interrogatories"


async def test_llm_down_falls_back_to_what_the_rules_saw(llm):
    parsed = await classify("Request", "Decisions for EB-2024-0111 and EB-2023-0195 please")

    assert (parsed.matter, parsed.doc_type, parsed.source) == ("EB-2024-0111", "Decisions and Orders", "rules_degraded")
    assert parsed.extra_matters == ("EB-2023-0195",)


async def test_llm_down_without_a_category_asks_for_the_regulators_own(llm):
    parsed = await classify("Request", "Could you help with EB-2024-0111? Not sure what I need.")

    assert parsed.intent is Intent.DOCUMENT_REQUEST and parsed.doc_type is None
    assert parsed.needs_clarification == f"Which document type would you like for EB-2024-0111: {OEB_TYPES}?"


def test_clarification_without_a_matter_gives_every_regulators_format():
    assert clarification_for(None, "Transcripts") == (
        "Which matter number would you like? For example M12205 (Nova Scotia Utility and Review Board) "
        "or EB-2024-0111 (Ontario Energy Board)."
    )
