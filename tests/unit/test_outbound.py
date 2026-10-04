"""Reply templates follow the provider: its category order, its name, its portal link."""

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
    assert "<a href" not in generic.html and "Automated reply from the" in generic.html
