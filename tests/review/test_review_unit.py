"""Code-review findings, demonstrated as strict xfail tests (each one fails today because of a bug).

When a bug is fixed its test starts passing and pytest reports XPASS(strict) as a failure:
remove the xfail marker then. Nothing here touches the network, Postgres or Redis.
"""

import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pymupdf
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

import agent.llm
from agent.citations.extract import extract_pages
from agent.citations.ground import locate_quote
from agent.config import Settings, get_settings
from agent.gate import rules
from agent.gate.classify import classify
from agent.llm import LLMUnavailable
from agent.mail import outbound
from agent.models import DocumentRef, DownloadedFile, MatterInfo, MatterNotFound
from agent.providers import base as providers_base
from agent.providers import oeb
from agent.providers.browser import BrowserPool
from agent.providers.oeb import OebProvider
from agent.providers.uarb import UarbProvider
from agent.web import app as web
from agent.web import progress

# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
async def providers(monkeypatch: pytest.MonkeyPatch):
    uarb = UarbProvider(BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=1_000))  # never launched
    client = oeb.make_client()
    registered = {"uarb": uarb, "oeb": OebProvider(client)}
    monkeypatch.setattr(providers_base, "_REGISTRY", {n: (lambda p=p: p) for n, p in registered.items()})
    yield registered
    await client.aclose()


class FakeLLM:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer = answer

    async def structured(self, *, system: str, user: str, schema: type[BaseModel], **_: Any):
        if self.answer is None:
            raise LLMUnavailable("fake: down")
        return schema.model_validate(self.answer), {"model": "fake/llm"}


def llm_answer(matter=None, doc_type=None, *, intent="document_request", clarification=None) -> dict[str, Any]:
    return {
        "intent": intent, "matter": matter, "other_matters": [], "doc_type": doc_type,
        "other_doc_types": [], "max_docs": 10, "clarification": clarification, "confidence": 0.9,
    }


UARB_INFO = MatterInfo(
    provider="uarb",
    matter="M12205",
    title="Halifax Regional Water Commission - Windsor Street Exchange Redevelopment Project",
    status="Open",
    type="Water",
    category="Capital Expenditure Approvals",
    date_received=date(2025, 4, 7),
    counts={"Exhibits": 1, "Key Documents": 6, "Other Documents": 43, "Transcripts": 0, "Recordings": 0},
    portal_url="https://uarb.novascotia.ca/fmi/webd/UARB15",
    fetched_at=datetime(2026, 10, 4, tzinfo=UTC),
)


# ---------------------------------------------------------------- citations: extraction


def test_pdf_with_overstated_page_count_does_not_crash_extraction(tmp_path: Path):
    doc = pymupdf.open()
    for i in range(2):
        doc.new_page().insert_text((72, 72), f"The Board approves page {i + 1} of the application.")
    data = doc.tobytes(garbage=0, deflate=False, no_new_id=True).replace(b"/Count 2", b"/Count 3", 1)
    path = tmp_path / "damaged.pdf"
    path.write_bytes(data)

    pages = extract_pages(str(path))  # IndexError today

    assert len(pages) in (2, 3)


# ---------------------------------------------------------------- citations: grounding


def test_fuzzy_grounding_rejects_a_dropped_negation():
    page = "the Board does not approve the capital cost of $4,500,000 for the substation"
    quote = "the Board does approve the capital cost of $4,500,000 for the substation"

    assert locate_quote(page, quote) is None


# ---------------------------------------------------------------- gate: rules


def test_fullwidth_digits_are_not_a_distinct_matter_number():
    assert rules.find_matters("Please send the Key Documents for M１２２０５") in ((), ("M12205",))


def test_count_is_not_taken_from_an_unrelated_sentence():
    r = rules.parse("", "Please send the Key Documents for M12205. I have 2 files already.")

    assert r.parsed is not None and r.parsed.max_docs == 10


def test_a_specific_exhibit_is_not_read_as_the_whole_tab():
    r = rules.parse("", "Please send exhibit H-1 of M12205")

    assert r.parsed is None or r.parsed.max_docs == 1


# ---------------------------------------------------------------- gate: classifier


async def test_llm_category_is_not_overridden_by_a_negated_category(monkeypatch):
    fake = FakeLLM(llm_answer("M12205", "Key Documents"))
    monkeypatch.setattr(agent.llm, "structured", fake.structured)

    parsed = await classify("M12205", "For M12205 I don't need the Exhibits, please send me the Board's decision.")

    assert parsed.doc_type == "Key Documents"


async def test_degraded_parse_does_not_fetch_an_excluded_category(monkeypatch):
    monkeypatch.setattr(agent.llm, "structured", FakeLLM(None).structured)

    parsed = await classify("", "Please send everything except the Exhibits for M12205")

    assert parsed.doc_type != "Exhibits" or parsed.needs_clarification


async def test_complete_llm_parse_is_not_turned_into_a_clarification(monkeypatch):
    fake = FakeLLM(llm_answer("M12205", "Key Documents", clarification="Do you want all of them?"))
    monkeypatch.setattr(agent.llm, "structured", fake.structured)

    parsed = await classify("", "Hey, for M12205 could you dig up the key docs and the exhibits?")

    assert parsed.needs_clarification is None


# ---------------------------------------------------------------- outbound wording


def _doc(n: int = 1) -> list[outbound.DocLine]:
    return [outbound.DocLine(title=f"H-{i}", filed="2025-04-07", url=None) for i in range(1, n + 1)]


def test_single_document_reply_is_grammatical(providers):
    draft = outbound.documents_reply(
        name="Ana", subject="Request", info=UARB_INFO, provider=providers["uarb"], doc_type="Exhibits",
        docs=_doc(1), requested=10, summary=None, claims=(), download_url=None, download_expires=None,
        download_size=150_000, attachment_path="/tmp/x.zip", track_url="https://x/r/t",
    )

    assert "all 1 " not in draft.text and "1 Exhibits" not in draft.text


def test_counts_sentence_is_grammatical_for_one(providers):
    assert "1 Exhibits" not in outbound.matter_sentence(UARB_INFO, providers["uarb"])


def test_progress_outcome_is_grammatical_for_one():
    assert progress._outcome({"state": "done"}, {"files": 1}) == "Sent 1 document."


def test_zip_readme_does_not_claim_newest_first_for_oldest_first_tabs(providers):
    from agent.pipeline import _readme

    refs = [DocumentRef(provider="uarb", matter="M12205", doc_type="Exhibits", external_id=f"H-{i}",
                        title=f"H-{i}", filed_on=date(2025, 4, i)) for i in (1, 2, 3)]
    files = [DownloadedFile(ref=r, path="/x", sha256="0" * 64, size=1, filename=f"{r.external_id}.pdf") for r in refs]

    assert "newest first" not in _readme(UARB_INFO, providers["uarb"], "Exhibits", files)


def test_attachment_fallback_fits_the_mta_size_limit(tmp_path: Path, providers):
    limit = Settings().attach_inline_max_bytes
    zip_path = tmp_path / "docs.zip"
    zip_path.write_bytes(os.urandom(limit))
    draft = outbound.Draft("reply", "Re: x", "text", "<p>html</p>", attachments=(str(zip_path),))
    msg = outbound.build_message(draft, request_id=__import__("uuid").uuid4(), to_addr="a@example.com",
                                 in_reply_to="<a@example.com>", references=())

    assert len(msg.as_bytes()) <= 10_240_000


# ---------------------------------------------------------------- viewer


def test_citation_page_has_no_dead_internal_links(monkeypatch, tmp_path: Path):
    view = web.CitationView(
        id="AbCdEfGhIjKl", claim="c", quote="q" * 30, page=1, document_id="0b6f3c1e-8d2a-4f7b-9c41-5e2d7a9b1c3f",
        provider="uarb", matter="M12205", doc_type="Other Documents", external_id="102674", doc_title="Order",
    )

    async def load_citation(_):
        return view

    monkeypatch.setattr(web, "load_citation", load_citation)
    web.app.dependency_overrides[get_settings] = lambda: Settings(data_dir=str(tmp_path))
    try:
        client = TestClient(web.app)
        page = client.get("/c/AbCdEfGhIjKl")
        assert page.status_code == 200 and "q" * 30 in page.text
        html = page.text
        links = [h for h in re.findall(r'href="(/[^"#?]*)', html) if not h.startswith(("/static/", "/files/"))]
        # (was: a link to /m/{provider}/{matter}, removed on purpose; the page needs no internal links)
        assert "/m/" not in html and all(client.get(h).status_code != 404 for h in links), links
    finally:
        web.app.dependency_overrides.clear()


# ---------------------------------------------------------------- providers


class _FakePool:
    @asynccontextmanager
    async def session(self):
        yield object()


async def test_uarb_fetch_matter_rechecks_a_not_found(monkeypatch):
    provider = UarbProvider(_FakePool())
    opened = []

    async def open_matter(page, matter):
        opened.append(matter)
        if len(opened) == 1:
            raise MatterNotFound(matter)  # the portal's documented search race

    async def read_matter(page, matter):
        return UARB_INFO

    monkeypatch.setattr(provider, "_open_matter", open_matter)
    monkeypatch.setattr(provider, "_read_matter", read_matter)

    assert (await provider.fetch_matter("M12205")).matter == "M12205"


async def test_uarb_download_session_not_found_is_not_final(monkeypatch, tmp_path: Path):
    provider = UarbProvider(_FakePool())

    async def open_matter(page, matter):
        raise MatterNotFound(matter)

    monkeypatch.setattr(provider, "_open_matter", open_matter)
    ref = DocumentRef(provider="uarb", matter="M12205", doc_type="Other Documents", external_id="102674", title="t")

    with pytest.raises(Exception) as info:
        async for _ in provider.download("M12205", [ref], str(tmp_path)):
            pass
    assert not isinstance(info.value, MatterNotFound)


def test_oeb_registration_date_is_the_toronto_calendar_date():
    raw = {
        "Uri": 1,
        "RecordNumber": {"Value": "D24-1"},
        "RecordTitle": {"Value": "Late filing"},
        "RecordDateRegistered": {"IsClear": False, "DateTime": "2024-11-06T02:30:00.0000000Z"},
        "Fields": {"SIDocumentType": {"Value": "Submission"}},
    }

    assert oeb._parse_record(raw).filed_on == date(2024, 11, 5)
