"""The gate with all three regulators registered (UARB, OEB, FERC): FERC dockets and categories,
and no ambiguity between the regulators' matter numbers."""

from typing import Any

import pytest
from pydantic import BaseModel

import agent.llm
from agent.gate import rules
from agent.gate.classify import clarification_for, classify, system_prompt
from agent.llm import LLMUnavailable
from agent.models import Intent
from agent.providers import base as providers_base
from agent.providers import ferc, oeb
from agent.providers.browser import BrowserPool
from agent.providers.ferc import FercProvider
from agent.providers.oeb import OebProvider
from agent.providers.uarb import UarbProvider

FERC_TYPES = (
    "Orders and Decisions, Notices, Applications and Filings, Comments and Protests, Motions and Pleadings, "
    "Interventions, Evidence and Testimony, Correspondence"
)


@pytest.fixture(autouse=True)
async def providers(monkeypatch: pytest.MonkeyPatch):
    """The three regulators as the worker registers them; nothing here touches a portal."""
    uarb = UarbProvider(BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000))
    oeb_client, ferc_client = oeb.make_client(), ferc.make_client()
    registered = {"uarb": uarb, "oeb": OebProvider(oeb_client), "ferc": FercProvider(ferc_client)}
    monkeypatch.setattr(providers_base, "_REGISTRY", {n: (lambda p=p: p) for n, p in registered.items()})
    yield registered
    await oeb_client.aclose()
    await ferc_client.aclose()


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


# ---------------------------------------------------------------- registry and matter numbers


def test_ferc_has_a_clean_vocabulary(providers):
    provider = providers["ferc"]
    names = [c.name for c in provider.categories]
    aliases = [a for c in provider.categories for a in c.aliases]
    assert len(set(names)) == len(names)
    assert len(set(aliases)) == len(aliases)
    assert all(a == a.lower().strip() and a for a in aliases)
    assert all(c.description for c in provider.categories)


# Written the ways people write them, with the regulator each belongs to.
SAMPLES = [
    ("M12205", ("uarb", "M12205")),
    ("m 12205", ("uarb", "M12205")),
    ("matter no. 12205", ("uarb", "M12205")),
    ("EB-2024-0111", ("oeb", "EB-2024-0111")),
    ("eb 2024 0111", ("oeb", "EB-2024-0111")),
    ("EB2024-0111", ("oeb", "EB-2024-0111")),
    ("EB–2024–0111", ("oeb", "EB-2024-0111")),
    ("ER24-1234-000", ("ferc", "ER24-1234-000")),
    ("er24-1234-000", ("ferc", "ER24-1234-000")),
    ("rm22-14", ("ferc", "RM22-14")),
    ("EL16-92-001", ("ferc", "EL16-92-001")),
    ("ER24–1234–000", ("ferc", "ER24-1234-000")),
    ("EB24-0111", None),  # FERC-shaped, but EB is not a FERC docket prefix
    ("12205", None),
    ("EB-2024-111", None),
    ("ER24-1234-0001", None),
]


@pytest.mark.parametrize(("raw", "expected"), SAMPLES)
def test_normalise_matter_picks_the_regulator(raw, expected):
    assert providers_base.normalise_matter(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), [s for s in SAMPLES if s[1]])
def test_no_matter_number_belongs_to_two_regulators(providers, raw, expected):
    owners = [p.name for p in providers.values() if providers_base.normalise_with(p, raw)]
    assert owners == [expected[0]]
    canonical_owners = [p.name for p in providers.values() if p.matter_pattern.fullmatch(expected[1])]
    assert canonical_owners == [expected[0]]
    assert providers_base.provider_for_matter(expected[1]) is providers[expected[0]]


def test_provider_for_matter_uses_the_canonical_form(providers):
    assert providers_base.provider_for_matter("ER24-1234-000") is providers["ferc"]
    assert providers_base.provider_for_matter("RM22-14") is providers["ferc"]
    assert providers_base.provider_for_matter("rm22-14") is None


@pytest.mark.parametrize(
    ("text", "matters"),
    [
        ("Please send docket ER24-1234-000", ("ER24-1234-000",)),
        ("rm22-14 comments please", ("RM22-14",)),
        ("M12205, EB-2024-0111 and ER24-1234-000", ("M12205", "EB-2024-0111", "ER24-1234-000")),
        ("ER24-1234-000 then eb 2024 0111 then matter 12205", ("ER24-1234-000", "EB-2024-0111", "M12205")),
        ("EB-2024-0111 and EB2024-0111 are the same OEB case", ("EB-2024-0111",)),
        ("RM22-14 (that is, rm22-14)", ("RM22-14",)),
        ("RM22-14 and its sub-docket RM22-14-001", ("RM22-14", "RM22-14-001")),
        ("Order No. 2023 under Docket No. RM22-14-000.", ("RM22-14-000",)),
        ("not ours: ER24-1234-0001, ABCD24-1, AM123456, XEB-2024-0111", ()),
        # a whole docket scoped to one sub-docket by the words right after it
        ("the orders for ER24-1234 (the -000 sub-docket only)", ("ER24-1234-000",)),
        ("ER24-1234, sub-docket 001, please", ("ER24-1234-001",)),
        ("RM22-14 (-002 only)", ("RM22-14-002",)),
        ("ER24-1234 (sub-dockets 000 and 001)", ("ER24-1234",)),  # several: the whole docket
        ("ER24-1234, sub-docket 000 and 001", ("ER24-1234",)),
        ("ER24-1234 and its -000 sub-docket", ("ER24-1234",)),  # not a scope, just a mention
        ("ER24-1234-001 (the -000 sub-docket only)", ("ER24-1234-001",)),  # written whole: kept
    ],
)
def test_find_matters_across_three_regulators_in_mention_order(text, matters):
    assert rules.find_matters(text) == matters


def test_rules_keep_the_sub_docket_the_email_scopes_to():
    result = rules.parse("Document request", "Please send the orders for ER24-1234 (the -000 sub-docket only).")
    assert result.reason == "ok"
    assert (result.parsed.matter, result.parsed.doc_type) == ("ER24-1234-000", "Orders and Decisions")


async def test_the_llm_cannot_widen_a_scoped_docket_to_the_whole_one(llm):
    llm.answer = llm_answer("ER24-1234", "Orders and Decisions")  # the model drops the scope
    parsed = await classify("Orders", "Hi, could you send me orders for ER24-1234 (the -000 sub-docket only)? "
                                      "Not the notices.")
    assert parsed.source == "llm" and parsed.matter == "ER24-1234-000"


# ---------------------------------------------------------------- rules


@pytest.mark.parametrize(
    ("body", "matter", "doc_type", "max_docs"),
    [
        ("Can you send me the orders in docket ER24-1234-000?", "ER24-1234-000", "Orders and Decisions", 10),
        ("rm22-14 comments please", "RM22-14", "Comments and Protests", 10),
        ("Please send the latest 3 motions to intervene in EL16-92", "EL16-92", "Interventions", 3),
        ("Could you send the letter order for ER24-1234-000?", "ER24-1234-000", "Orders and Decisions", 10),
        ("Please send the rehearing requests in RM22-14-001", "RM22-14-001", "Motions and Pleadings", 10),
        ("Can you get me the tariff filing for ER24-1234-000?", "ER24-1234-000", "Applications and Filings", 10),
        ("Send 2 protests on ER24–1234–000 please", "ER24-1234-000", "Comments and Protests", 2),
        ("Please send the testimony for EL16-92", "EL16-92", "Evidence and Testimony", 10),
        ("Could you send the NOPR for RM22-14?", "RM22-14", "Notices", 10),
    ],
)
def test_rules_fast_path_for_ferc_phrasings(body, matter, doc_type, max_docs):
    result = rules.parse("Document request", body)

    assert result.reason == "ok", result
    assert (result.parsed.matter, result.parsed.doc_type, result.parsed.max_docs) == (matter, doc_type, max_docs)
    assert result.parsed.source == "rules"


@pytest.mark.parametrize(
    "body",
    [
        "Can you send me the key documents for ER24-1234-000?",  # a UARB category
        "Can you send me the undertakings for RM22-14?",  # an OEB category
        "Can you send me the protests for EB-2024-0111?",  # a FERC category, OEB case
        "Can you send me the interventions for M12205?",  # a FERC category, UARB matter
    ],
)
def test_another_regulators_category_is_not_recognised(body):
    assert rules.parse("Request", body).reason == "no_doc_type"


@pytest.mark.parametrize(
    ("body", "matters", "doc_types"),
    [
        ("Please send the Exhibits for M12205 and the comments on RM22-14.", ("M12205", "RM22-14"), ("Exhibits",)),
        ("Please send the comments on RM22-14 and the decisions for EB-2024-0111.", ("RM22-14", "EB-2024-0111"),
         ("Comments and Protests", "Orders and Decisions")),
        ("Decisions for EB-2024-0111 and orders in ER24-1234-000 please", ("EB-2024-0111", "ER24-1234-000"),
         ("Decisions and Orders",)),
    ],
)
def test_mixed_regulators_go_to_the_llm_with_the_first_matters_categories(body, matters, doc_types):
    result = rules.parse("Request", body)

    assert result.parsed is None and result.reason == "multiple_matters"
    assert (result.matters, result.doc_types) == (matters, doc_types)


# ---------------------------------------------------------------- classifier


async def test_rules_answer_ferc_requests_without_the_llm(llm):
    parsed = await classify("Request", "Can you send me the orders in docket ER24-1234-000?")

    assert (parsed.matter, parsed.doc_type, parsed.source) == ("ER24-1234-000", "Orders and Decisions", "rules")
    assert llm.systems == []


async def test_llm_prompt_lists_ferc_with_its_format_and_categories(llm, providers):
    llm.answer = llm_answer("RM22-14", "Orders and Decisions")

    await classify("Request", "What did FERC finally decide in RM22-14? Send me whatever has it.")

    (system,) = llm.systems
    assert system == system_prompt(list(providers.values()))
    for provider in providers.values():
        assert f"{provider.display_name}: matter numbers look like {provider.matter_example}" in system
        for category in provider.categories:
            assert f"  - {category.name}: {category.description}" in system
    assert "Federal Energy Regulatory Commission (US): matter numbers look like ER24-1234-000" in system


async def test_llm_matter_is_normalised_to_the_docket_format(llm):
    llm.answer = llm_answer("rm22-14", "orders and decisions")

    parsed = await classify("Request", "What did FERC finally decide in rm22-14? Send me whatever has it.")

    assert (parsed.matter, parsed.doc_type, parsed.source) == ("RM22-14", "Orders and Decisions", "llm")
    assert parsed.needs_clarification is None


async def test_mixed_regulators_handle_the_first_matter_and_note_the_others(llm):
    llm.answer = llm_answer(
        "RM22-14", "Comments and Protests", other_matters=["EB-2024-0111", "M12205"],
        other_doc_types=["Decisions and Orders", "Interventions", "Exhibits"],
    )

    parsed = await classify(
        "Request", "Please send the comments and interventions on RM22-14, the decisions for EB-2024-0111 and "
        "the exhibits for M12205.",
    )

    assert (parsed.matter, parsed.doc_type) == ("RM22-14", "Comments and Protests")
    assert parsed.extra_matters == ("EB-2024-0111", "M12205")
    assert parsed.extra_doc_types == ("Interventions",)  # the others belong to the other regulators


async def test_llm_category_from_another_regulator_becomes_a_clarification(llm):
    llm.answer = llm_answer("ER24-1234-000", "Decisions and Orders")  # the OEB's name, not FERC's

    parsed = await classify("Request", "Send me the important stuff for ER24-1234-000")

    assert parsed.matter == "ER24-1234-000" and parsed.doc_type is None
    assert parsed.needs_clarification == f"Which document type would you like for ER24-1234-000: {FERC_TYPES}?"


async def test_llm_cannot_invent_a_docket(llm):
    llm.answer = llm_answer("RM22-14", "Notices")

    parsed = await classify("Request", "Send me the notices for the interconnection rulemaking")

    assert parsed.matter is None
    assert "ER24-1234-000 (Federal Energy Regulatory Commission (US))" in parsed.needs_clarification


async def test_llm_down_falls_back_to_what_the_rules_saw(llm):
    parsed = await classify("Request", "Orders for RM22-14 and EL16-92 please")

    assert (parsed.matter, parsed.doc_type, parsed.source) == ("RM22-14", "Orders and Decisions", "rules_degraded")
    assert parsed.extra_matters == ("EL16-92",)
    assert parsed.intent is Intent.DOCUMENT_REQUEST


def test_clarification_without_a_matter_gives_every_regulators_format():
    assert clarification_for(None, "Transcripts") == (
        "Which matter number would you like? For example M12205 (Nova Scotia Utility and Review Board) "
        "or EB-2024-0111 (Ontario Energy Board) or ER24-1234-000 (Federal Energy Regulatory Commission (US))."
    )


def test_clarification_for_a_docket_lists_ferc_categories():
    assert clarification_for("RM22-14", None) == f"Which document type would you like for RM22-14: {FERC_TYPES}?"


# ---------------------------------------------------------------- the email's own matter, not the model's spelling


@pytest.mark.parametrize(
    ("body", "llm_matter", "expected"),
    [
        ("Can you send me the notices for RM22-14? Thanks", "RM22-14-000", "RM22-14"),  # model added a sub-docket
        ("When are reply comments due in RM22-14?", "rm22-14-000", "RM22-14"),
        ("Please send what you have on ER24-1234-000, whatever's newest", "ER24-1234", "ER24-1234-000"),  # dropped one
        ("RM22-14 and RM22-14-001: send whatever you have", "RM22-14-001", "RM22-14-001"),
        ("Please send whatever you have on ER24-1234-000", "ER24-1234-001", None),  # another sub-docket
        ("Please send whatever you have on RM22-14-000 and RM22-14-001", "RM22-14", None),  # which one?
    ],
)
async def test_llm_matter_is_accepted_by_identity_with_the_emails_mention(llm, body, llm_matter, expected):
    llm.answer = llm_answer(llm_matter, "Notices")

    parsed = await classify("Request", body)

    assert parsed.matter == expected


async def test_llm_other_matters_are_mapped_back_to_the_emails_dockets(llm):
    llm.answer = llm_answer("ER24-1234-000", "Orders and Decisions", other_matters=["EL16-92-000"])

    parsed = await classify("Orders", "Please send the orders in ER24-1234-000 and EL16-92.")

    assert (parsed.matter, parsed.extra_matters) == ("ER24-1234-000", ("EL16-92",))


@pytest.mark.parametrize(
    ("llm_doc_type", "expected"),
    [("comments", "Comments and Protests"), ("rehearing requests", "Motions and Pleadings"),
     ("Tariff Filing", "Applications and Filings"), ("Exhibits", None)],  # UARB's category name, not an alias
)
async def test_llm_category_may_be_one_of_the_regulators_aliases(llm, llm_doc_type, expected):
    llm.answer = llm_answer("RM22-14", llm_doc_type)

    parsed = await classify("Request", "What came in on RM22-14 lately? Send me whatever has it.")

    assert parsed.doc_type == expected


def test_llm_prompt_tells_the_model_to_keep_the_docket_as_written_and_lists_ferc_synonyms(providers):
    system = system_prompt(list(providers.values()))
    assert "Never add or drop parts" in system and '"RM22-14") stays whole' in system
    assert "rehearing requests" in system and "tariff filing" in system
    assert "clarification" not in system  # nothing the model writes is sent to the sender


async def test_category_named_under_negation_does_not_override_a_ferc_parse(llm):
    llm.answer = llm_answer("RM22-14", "Orders and Decisions")

    parsed = await classify("RM22-14", "For RM22-14: no comments please, just the orders.")

    assert (parsed.matter, parsed.doc_type, parsed.needs_clarification) == ("RM22-14", "Orders and Decisions", None)


async def test_signature_unit_number_and_another_regulators_alias_do_not_change_the_category(llm):
    llm.answer = llm_answer("EL16-92-000", "Orders and Decisions")

    parsed = await classify("Orders", "Please send the orders in EL16-92.\n\nRegards,\nDana\nUnit B12-305, 1200 K St NW")

    assert (parsed.matter, parsed.doc_type) == ("EL16-92", "Orders and Decisions")
