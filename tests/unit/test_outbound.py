"""Reply templates follow the provider: its category order, its name, its portal link."""

import html
from datetime import UTC, date, datetime

import pytest

from agent.mail import outbound
from agent.models import MatterInfo
from agent.providers.browser import BrowserPool
from agent.providers.oeb import OebProvider, make_client
from agent.providers.uarb import PORTAL_URL as UARB_PORTAL_URL
from agent.providers.uarb import UarbProvider

UARB = UarbProvider(BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000))  # never launched

INFO = MatterInfo(
    provider="oeb",
    matter="EB-2023-0195",
    title="Toronto Hydro-Electric System Limited – Electricity rates application",
    type="Electricity",
    category="Rates",
    date_received=date(2023, 11, 17),
    decision_date=date(2025, 3, 13),
    # JSONB hands cached counts back in its own key order, not the portal's
    counts={"Transcripts": 8, "Undertakings": 0, "Correspondence": 87, "Decisions and Orders": 28},
    portal_url="https://www.rds.oeb.ca/CMWebDrawer/Record?q=CaseNumber=EB-2023-0195",
    fetched_at=datetime(2026, 10, 4, tzinfo=UTC),
)


@pytest.fixture
async def oeb():
    client = make_client()
    yield OebProvider(client)
    await client.aclose()


def test_matter_sentence_lists_counts_in_the_providers_order(oeb):
    assert outbound.matter_sentence(INFO, oeb) == (
        "EB-2023-0195 is about Toronto Hydro-Electric System Limited – Electricity rates application. "
        "It is an Electricity matter in the Rates category. "
        "The matter was received on November 17, 2023 and decided on March 13, 2025. "
        "I found 28 Decisions and Orders, 8 Transcripts, 87 Correspondence, and no Procedural Orders, "
        "Application and Evidence, Interrogatories, Undertakings, Submissions and Arguments or Cost Claims."
    )


def test_ack_and_footer_name_the_matters_regulator(oeb):
    draft = outbound.ack(name="Ana Lee", subject="Request", matter="EB-2023-0195", doc_type="Transcripts",
                         provider=oeb, track_url="https://agent.example/r/token")

    assert "collecting the Transcripts for EB-2023-0195 from the Ontario Energy Board database" in draft.text
    assert '<a href="https://www.rds.oeb.ca/">Ontario Energy Board database</a>' in draft.html
    assert UARB_PORTAL_URL not in draft.html


def test_footer_links_the_uarb_portal_for_uarb_and_nothing_without_a_provider():
    uarb = outbound.simple_reply(name="", subject="Request", paragraphs=["Hello."], provider=UARB)
    generic = outbound.simple_reply(name="", subject="Request", paragraphs=["Hello."])

    assert f'<a href="{UARB_PORTAL_URL}">Nova Scotia Utility and Review Board database</a>' in uarb.html
    assert "database</a>" not in generic.html and "Automated reply from the" in generic.html


def test_every_email_links_the_privacy_notice_in_text_and_html(oeb):
    drafts = [
        outbound.ack(name="Ana", subject="Request", matter="EB-2023-0195", doc_type="Transcripts", provider=oeb,
                     track_url="https://agent.example/r/token"),
        outbound.simple_reply(name="", subject="Request", paragraphs=["Hello."]),
        outbound.simple_reply(name="", subject="Request", paragraphs=["Hello."], provider=UARB,
                              track_url="https://agent.example/r/token"),
    ]
    privacy = outbound.privacy_url()
    assert privacy.endswith("/privacy")
    for d in drafts:
        assert d.text.rstrip().endswith(f"Privacy notice: {privacy}")
        assert f'<a href="{privacy}" style="color:#6b7785">Privacy notice</a>' in d.html


# ---------------------------------------------------------------- counts, size, summary basis, headers


@pytest.mark.parametrize(
    ("counts", "sentence"),
    [
        ({"Decisions and Orders": 2, "Transcripts": 1, "Correspondence": 4},
         "2 Decisions and Orders, 1 Transcript, 4 Correspondence, and no "),
        ({"Decisions and Orders": 1}, "1 Decision or Order and no Procedural Orders, "),
        ({}, "no documents."),  # not "no documents, and no Decisions and Orders, ..."
    ],
)
def test_counts_sentence_puts_and_before_the_last_item(oeb, counts, sentence):
    info = INFO.model_copy(update={"counts": counts})
    assert sentence in outbound.matter_sentence(info, oeb)


def test_counts_sentence_when_every_category_has_documents(oeb):
    info = INFO.model_copy(update={"counts": {c.name: 2 for c in oeb.categories}})
    found = outbound.matter_sentence(info, oeb).split("I found ", 1)[1]
    assert found.endswith(", 2 Cost Claims, and 2 Correspondence.") and " and no " not in found


@pytest.mark.parametrize(("size", "text"), [(146_000, "(146 KB)"), (800, "(1 KB)"), (840_000, "(840 KB)"),
                                            (2_345_678, "(2.3 MB)")])
def test_documents_reply_always_states_the_size(oeb, size, text):
    draft = _documents_reply(oeb, download_size=size)
    assert f"packaged them as a ZIP {text}." in draft.text and text in draft.html


def _documents_reply(provider, **overrides):
    fields = {
        "name": "Ana", "subject": "Request", "info": INFO, "provider": provider, "doc_type": "Transcripts",
        "docs": [outbound.DocLine(title=f"T{i}", filed="2025-01-01", url=None) for i in range(3)],
        "requested": 10, "summary": "The OEB approved the rates.", "claims": [], "download_url": None,
        "download_expires": None, "download_size": 500_000, "attachment_path": None,
        "track_url": "https://agent.example/r/t",
    }
    return outbound.documents_reply(**(fields | overrides))


def test_documents_reply_says_what_the_summary_is_based_on(oeb):
    basis = outbound.SummaryBasis(used=7, total=10, unreadable=3, kinds=("scanned", "spreadsheet"))
    draft = _documents_reply(oeb, summary_basis=basis)
    line = "The summary is based on 7 of 10 documents; 3 couldn't be read (scanned or spreadsheets)."
    assert f"Summary\nThe OEB approved the rates.\n{line}\n" in draft.text
    assert html.escape(line) in draft.html
    # no summary, no line; everything read, no line
    assert "based on" not in _documents_reply(oeb, summary=None, summary_basis=basis).text
    assert "based on" not in _documents_reply(oeb, summary_basis=outbound.SummaryBasis(used=3, total=3)).text


@pytest.mark.parametrize(
    ("basis", "line"),
    [
        (outbound.SummaryBasis(used=4, total=10, unread=6),
         "The summary is based on the 4 most decision-relevant of the 10 documents."),
        (outbound.SummaryBasis(used=4, total=10, unreadable=2, unread=4, kinds=("recording",)),
         "The summary is based on the 4 most decision-relevant of the 10 documents; 2 couldn't be read (recordings)."),
        (outbound.SummaryBasis(used=1, total=2, unreadable=1, kinds=("spreadsheet",)),
         "The summary is based on 1 of 2 documents; 1 couldn't be read (a spreadsheet)."),
        (outbound.SummaryBasis(used=0, total=3, unreadable=3, kinds=("scanned",)),
         "None of the 3 documents could be read (scanned), so the summary is based on the matter's details only."),
        (outbound.SummaryBasis(used=2, total=3, unreadable=1, kinds=()),
         "The summary is based on 2 of 3 documents; 1 couldn't be read."),
    ],
)
def test_summary_basis_sentences(basis, line):
    assert basis.sentence() == line


def test_every_message_carries_x_loop_with_our_address():
    import uuid

    from agent.config import get_settings

    draft = outbound.simple_reply(name="", subject="Request", paragraphs=["Hello."])
    msg = outbound.build_message(draft, request_id=uuid.uuid4(), to_addr="a@example.com", in_reply_to="<m@x>",
                                 references=())
    assert msg["X-Loop"] == get_settings().agent_mail_address
