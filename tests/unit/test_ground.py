import pytest

from agent.citations import ground
from agent.citations.ground import (
    ClaimDraft,
    DropReason,
    GroundedClaim,
    check_support,
    locate_quote,
    normalise_text,
    verify_claims,
)
from agent.llm import LLMUnavailable

# Page text as PyMuPDF gives it for UARB Board Order 102674 (M12205), plus a hyphenated line
# break and a ligature, which this order happens not to contain.
PAGE_1 = """ORDER M12205

Halifax Regional Water Commission (Halifax Water) applied to the Nova Scotia
Regulatory and Appeals Board (Board) for approval of the Windsor Street Exchange
Redevelopment Project – Construction for a cost of $64,769,000.

After reviewing Halifax Water’s responses, the Board issued a letter on December 9,
2025, confirming that, while the application was approved in principle, it was subject to a
further Compliance Filing when ﬁnal costing was substantially completed."""

PAGE_2 = """The Board notes the updated Compliance Filing indicates an overall reduction in the
estimated project cost. The Board is satisﬁed that the updated Compliance Filing reflects
the Board’s Decision.

The Board approves the application, as amended in the Decision and reflected in the
updated Compliance Filing, for a total project cost of $59,143,000, inclusive of net HST,
and orders that:

Halifax Water is directed to continue to ﬁle semi-annual (every six months) reports includ-
ing updated project costs, and an updated project timeline, until project completion."""

PAGE_3 = "DATED at Halifax, Nova Scotia, this 8th day of July 2026."

PAGES = {"102674": [PAGE_1, PAGE_2, PAGE_3]}

APPROVAL = "The Board approves the application, as amended in the Decision"


def _located(page: str, quote: str) -> str:
    span = locate_quote(page, quote)
    assert span is not None, quote
    return page[span.start : span.end]


# ---------------------------------------------------------------- locate_quote


def test_exact_quote_offsets_slice_the_original_text():
    quote = "for a total project cost of $59,143,000, inclusive of net HST"
    span = locate_quote(PAGE_2, quote)
    assert span is not None and span.score == 100.0
    assert PAGE_2[span.start : span.end] == quote


@pytest.mark.parametrize(
    "quote",
    [
        # whitespace: the model joins lines and doubles spaces
        (
            "The Board approves the application,  as amended in the Decision and reflected in the "
            "updated Compliance Filing"
        ),
        # line-break hyphenation: "includ-\ning" quoted as one word, or with the hyphen kept
        "reports including updated project costs, and an updated project timeline",
        "reports includ- ing updated project costs, and an updated project timeline",
        # ligatures: "ﬁ" in the PDF, "fi" from the model (and the other way round)
        "when final costing was substantially completed",
        "The Board is satisﬁed that the updated Compliance Filing",
        "The Board is satisfied that the updated Compliance Filing",
        # typography: straight quote for ’, hyphen for the en dash, different case
        "After reviewing Halifax Water's responses, the Board issued a letter",
        "Redevelopment Project - Construction for a cost of $64,769,000",
        "THE BOARD APPROVES THE APPLICATION, AS AMENDED IN THE DECISION",
    ],
)
def test_variants_match_exactly_and_map_back(quote: str):
    page = PAGE_1 if quote.lower().startswith(("when", "after", "redevelopment")) else PAGE_2
    span = locate_quote(page, quote)
    assert span is not None and span.score == 100.0
    assert normalise_text(page[span.start : span.end]) == normalise_text(quote)


def test_hyphenated_span_covers_both_halves_of_the_word():
    original = _located(PAGE_2, "reports including updated project costs")
    assert original == "reports includ-\ning updated project costs"


def test_near_miss_is_accepted_and_reported_as_fuzzy():
    quote = "Halifax Water is directed to continue to file semi-anual (every six months) reports"
    span = locate_quote(PAGE_2, quote)
    assert span is not None and 92 <= span.score < 100
    assert PAGE_2[span.start : span.end].startswith("Halifax Water is directed")


def test_fuzzy_match_expands_to_whole_words():
    original = _located(PAGE_2, "The Board notes the updated Compliance Filing indicates an overal reduction")
    assert original.startswith("The Board notes") and original.endswith("reduction")


@pytest.mark.parametrize(
    "quote",
    [
        "The Board approved the project for a total cost of $59,143,000 including net HST",
        "The Board approved the application for a total project cost of $59,143,000",
        "Halifax Water must file quarterly reports on project costs and timelines",
    ],
)
def test_paraphrase_is_rejected(quote: str):
    assert locate_quote(PAGE_2, quote) is None


def test_near_match_with_a_changed_number_is_rejected():
    # One character off would pass the similarity threshold; numbers must not change at all.
    assert locate_quote(PAGE_2, "for a total project cost of $59,143,800, inclusive of net HST") is None


@pytest.mark.parametrize("quote", ["The Board approves", "x" * 401, "   ", ""])
def test_quotes_outside_length_bounds_are_rejected(quote: str):
    assert locate_quote(PAGE_2, quote) is None


def test_quote_longer_than_page_is_rejected():
    assert locate_quote("short page text here", "short page text here and a lot more besides") is None


# ---------------------------------------------------------------- verify_claims


def _draft(quote: str, page: int, doc: str = "102674", claim: str = "A claim.") -> ClaimDraft:
    return ClaimDraft(claim=claim, doc_external_id=doc, page=page, quote=quote)


def test_claim_on_the_cited_page_is_kept_with_our_own_text():
    kept, dropped = verify_claims([_draft(APPROVAL + "  and reflected in the updated", 2)], PAGES)
    assert dropped == []
    (c,) = kept
    assert c.page == 2 and c.page_corrected_from is None
    assert c.quote == PAGE_2[c.char_start : c.char_end]
    assert "\n" in c.quote  # verbatim page text, not the model's re-spaced copy
    assert len(c.id) == 12


@pytest.mark.parametrize("cited", [1, 3])
def test_off_by_one_page_is_corrected(cited: int):
    kept, dropped = verify_claims([_draft(APPROVAL, cited)], PAGES)
    assert dropped == []
    assert kept[0].page == 2 and kept[0].page_corrected_from == cited


def test_quote_two_pages_away_is_dropped():
    kept, dropped = verify_claims([_draft("this 8th day of July 2026 at Halifax", 1)], PAGES)
    assert kept == []
    assert dropped[0].reason is DropReason.QUOTE_NOT_FOUND


@pytest.mark.parametrize(
    ("draft", "reason"),
    [
        (_draft(APPROVAL, 2, doc="999999"), DropReason.UNKNOWN_DOCUMENT),
        (_draft(APPROVAL, 9), DropReason.PAGE_OUT_OF_RANGE),
        (_draft(APPROVAL, 0), DropReason.QUOTE_NOT_FOUND),  # 0 -> neighbour 1 searched, not found
        (_draft("The Board approves", 2), DropReason.QUOTE_TOO_SHORT),
        (_draft("word " * 300, 2), DropReason.QUOTE_TOO_LONG),
        (_draft(APPROVAL, 2, claim="  "), DropReason.EMPTY_CLAIM),
        (_draft("The Board rejected the application and fined Halifax Water", 2), DropReason.QUOTE_NOT_FOUND),
    ],
)
def test_ungrounded_claims_are_dropped_with_a_reason(draft: ClaimDraft, reason: DropReason):
    kept, dropped = verify_claims([draft], PAGES)
    assert kept == []
    assert dropped[0].reason is reason and dropped[0].draft == draft


def test_duplicate_citations_are_dropped():
    kept, dropped = verify_claims([_draft(APPROVAL, 2), _draft(APPROVAL, 1, claim="Same again.")], PAGES)
    assert len(kept) == 1
    assert [d.reason for d in dropped] == [DropReason.DUPLICATE]


# ---------------------------------------------------------------- check_support (LLM, mocked)


def _grounded(claim: str) -> GroundedClaim:
    kept, _ = verify_claims([_draft(APPROVAL, 2, claim=claim)], PAGES)
    return kept[0]


async def test_support_check_drops_claims_the_quote_does_not_entail(monkeypatch):
    claims = [_grounded("The Board approved the application."), _grounded("The Board rejected it.")]
    calls = []

    async def fake_structured(*, system, user, schema, **_):
        calls.append(user)
        return schema(verdicts=[{"item": 0, "supported": True}, {"item": 1, "supported": False}]), {}

    monkeypatch.setattr(ground, "structured", fake_structured)
    kept, dropped = await check_support(claims)
    assert kept == [claims[0]]
    assert [d.reason for d in dropped] == [DropReason.NOT_ENTAILED]
    assert "<<<QUOTE" in calls[0]  # claim and quote travel as untrusted data blocks


async def test_support_check_fails_closed(monkeypatch):
    async def unavailable(**_):
        raise LLMUnavailable("all models down")

    monkeypatch.setattr(ground, "structured", unavailable)
    kept, dropped = await check_support([_grounded("The Board approved the application.")])
    assert kept == []
    assert dropped[0].reason is DropReason.SUPPORT_CHECK_FAILED


async def test_support_check_treats_missing_or_contradictory_verdicts_as_unsupported(monkeypatch):
    claims = [_grounded("One."), _grounded("Two."), _grounded("Three.")]

    async def fake_structured(*, schema, **_):
        verdicts = [
            {"item": 0, "supported": True},
            {"item": 1, "supported": True},
            {"item": 1, "supported": False},
        ]
        return schema(verdicts=verdicts), {}

    monkeypatch.setattr(ground, "structured", fake_structured)
    kept, dropped = await check_support(claims)
    assert kept == [claims[0]]
    assert len(dropped) == 2


# ---------------------------------------------------------------- quotes widened to their sentence


@pytest.mark.parametrize(
    ("quote", "sentence"),
    [
        ("for a total project cost of $59,143,000, inclusive of net HST",
         ("The Board approves the application, as amended in the Decision and reflected in the\nupdated "
          "Compliance Filing, for a total project cost of $59,143,000, inclusive of net HST,\nand orders that:")),
        ("The Board is satisﬁed that the updated Compliance Filing reflects",
         "The Board is satisﬁed that the updated Compliance Filing reflects\nthe Board’s Decision."),
        ("semi-annual (every six months) reports",
         ("Halifax Water is directed to continue to ﬁle semi-annual (every six months) reports includ-\ning "
          "updated project costs, and an updated project timeline, until project completion.")),
    ],
)
def test_a_grounded_quote_is_the_whole_sentence(quote, sentence):
    (claim,) = verify_claims([_draft(quote, 2)], PAGES)[0]
    assert claim.quote == sentence == PAGE_2[claim.char_start : claim.char_end]
    assert not claim.whole_sentence


def test_a_quote_that_already_is_the_sentence_is_marked_so():
    sentence = "The Board is satisﬁed that the updated Compliance Filing reflects\nthe Board’s Decision."
    (claim,) = verify_claims([_draft(sentence, 2)], PAGES)[0]
    assert claim.quote == sentence and claim.whole_sentence


def test_abbreviations_and_bullets_do_not_end_or_extend_a_sentence():
    page = ("The OEB approved it on Nov. 12, 2024. Mr. Smith, of Enbridge Gas Inc., said rate No. 27 stands. "
            "Next sentence.\nThe OEB orders:\n• Environmental Defence $151,718.65\n• FRPO $98,640.21")
    assert page[slice(*ground.widen_to_sentence(page, *_span(page, "said rate No. 27 stands")))] == (
        "Mr. Smith, of Enbridge Gas Inc., said rate No. 27 stands.")
    assert page[slice(*ground.widen_to_sentence(page, *_span(page, "FRPO $98,640.21")))] == "FRPO $98,640.21"


def test_a_sentence_too_long_to_quote_is_widened_only_as_far_as_it_fits():
    page = "Preamble " + "word " * 120 + "and the Board approves the rate of $5 for new customers only. Next."
    start, end = _span(page, "the Board approves the rate of $5")
    s, e = ground.widen_to_sentence(page, start, end, max_chars=200)
    assert (s, page[s:e]) == (start, "the Board approves the rate of $5 for new customers only.")


def _span(page: str, quote: str) -> tuple[int, int]:
    span = locate_quote(page, quote) if len(quote) >= 20 else None
    if span is None:
        at = page.index(quote)
        return at, at + len(quote)
    return span.start, span.end


def test_fuzzy_grounding_rejects_an_inserted_negation():
    page = "the Board does approve the capital cost of $4,500,000 for the substation upgrade work"
    assert locate_quote(page, "the Board does not approve the capital cost of $4,500,000 for the substation") is None


def test_fuzzy_grounding_rejects_approve_for_deny():
    page = "the Board denies the request for a capital cost of $4,500,000 for the substation upgrade"
    assert locate_quote(page, "the Board approves the request for a capital cost of $4,500,000 for the substation") is None


async def test_support_check_sees_the_text_around_the_quote(monkeypatch):
    claim = _grounded("The Board approved the application.")
    calls = []

    async def fake_structured(*, system, user, schema, **kw):
        calls.append((user, kw))
        return schema(verdicts=[{"item": 0, "supported": True}]), {"model": "fake", "purpose": kw.get("purpose")}

    monkeypatch.setattr(ground, "structured", fake_structured)
    kept, dropped, meta = await ground.verify_support([claim], PAGES)
    assert kept == [claim] and dropped == [] and meta["purpose"] == "support_check"
    user, _ = calls[0]
    assert "<<<CONTEXT" in user and "The Board notes the updated Compliance Filing" in user
    assert "leaves out a condition, qualifier or assumption" in ground._SUPPORT_SYSTEM


def test_a_quote_over_the_cap_is_located_by_its_opening_and_never_counts_as_a_whole_sentence():
    page = "The Board approves the rate " + "and the related schedule " * 20 + "for new customers only. Next."
    (claim,) = verify_claims([_draft(page[:-6], 1, doc="x")], {"x": [page]})[0]
    assert claim.quote.startswith("The Board approves the rate") and len(claim.quote) <= 400
    assert not claim.whole_sentence


# ---------------------------------------------------------------- quote context (quote-only citations)

CONTEXT_PAGE = (
    "ORDER ON REHEARING\n\nSeveral parties sought rehearing of the deposit rules. We sustain the cluster\n"
    "study deposit requirement. The deposits are refundable as set out below. Mr. Smith dissents."
)


@pytest.mark.parametrize(
    ("quote", "before", "after"),
    [
        ("We sustain the cluster\nstudy deposit requirement.", "Several parties sought rehearing of the deposit rules.",
         "The deposits are refundable as set out below."),
        ("Mr. Smith dissents.", "The deposits are refundable as set out below.", ""),
        ("ORDER ON REHEARING", "", "Several parties sought rehearing of the deposit rules."),
        ("study deposit requirement", "We sustain the cluster", "."),  # mid-sentence: the rest of it
    ],
)
def test_quote_context_is_the_neighbouring_sentences(quote, before, after):
    start = CONTEXT_PAGE.index(quote)
    assert ground.quote_context(CONTEXT_PAGE, start, start + len(quote)) == (before, after)


def test_quote_context_is_capped_away_from_the_quote():
    page = "word " * 300 + "The quote itself. " + "tail " * 300
    start = page.index("The quote itself.")
    before, after = ground.quote_context(page, start, start + len("The quote itself."), max_chars=50)
    assert before.startswith("…") and before.endswith("word") and len(before) <= 51
    assert after.endswith("…") and after.startswith("tail") and len(after) <= 51
