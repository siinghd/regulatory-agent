"""OEB provider against recorded responses (respx): parsing, categories, ordering, failures.

tests/fixtures/oeb_EB-2024-0111_page.json is a real search response for EB-2024-0111 (fetched
2026-10-04) trimmed to 30 records chosen to cover every category, multi-valued document types,
non-PDF files and every date field; its totals were rewritten to match the trimmed list.
"""

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from agent.models import (
    DocumentRef,
    MatterNotFound,
    PortalUnavailable,
    ProviderRejected,
    ScrapeError,
    TooLarge,
)
from agent.providers import oeb
from agent.providers.files import FilePolicy, UnsupportedFileType
from agent.providers.oeb import CATEGORIES, OebProvider, categorise

MATTER = "EB-2024-0111"
PAGE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "oeb_EB-2024-0111_page.json").read_text()
)
PDF = b"%PDF-1.6\n" + b"x" * 2_000


@pytest.fixture
async def portal():
    async with respx.mock(base_url=oeb.API_URL, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def provider():
    client = oeb.make_client()
    yield OebProvider(client, max_concurrency=2)
    await client.aclose()


def serve_pages(portal: respx.MockRouter, results: list[dict], page_size: int) -> respx.Route:
    """Serve `results` the way WebDrawer pages them: `start` is a 1-based record offset."""

    def respond(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        chunk = results[start - 1 : start - 1 + page_size]
        more = start - 1 + page_size < len(results)
        return httpx.Response(200, json={**PAGE, "Results": chunk, "TotalResults": len(results), "HasMoreItems": more})

    return portal.get("Record").mock(side_effect=respond)


# ---------------------------------------------------------------- matter numbers and categories


@pytest.mark.parametrize("raw", ["EB-2024-0111", "eb-2024-0111", "EB 2024 0111", "EB2024-0111", "EB–2024–0111"])
def test_normalise_accepts_the_ways_people_write_case_numbers(raw):
    assert OebProvider.normalise(raw) == MATTER


@pytest.mark.parametrize("raw", ["EB-2024-011", "EB-2024-01111", "M12205", "EB-24-0111", "2024-0111"])
def test_normalise_rejects_other_numbers(raw):
    assert OebProvider.normalise(raw) is None


@pytest.mark.parametrize(
    ("document_type", "category"),
    [
        ("Decision", "Decisions and Orders"),
        ("Decision and Order on Cost Awards", "Decisions and Orders"),
        ("Draft Rate Order", "Decisions and Orders"),
        ("Interim rate order", "Decisions and Orders"),
        ("Decision; Procedural Order", "Decisions and Orders"),
        ("Notice", "Procedural Orders"),
        ("Acknowledgement Letter", "Procedural Orders"),
        ("Exhibits", "Application and Evidence"),
        ("Undertaking List; Exhibit List", "Application and Evidence"),
        ("Correspondence; Application and Evidence; Undertaking List", "Application and Evidence"),
        ("Interrogatory Response from Intervenor", "Interrogatories"),
        ("Declaration and Undertakings", "Undertakings"),
        ("Motion; Correspondence", "Submissions and Arguments"),
        ("Post Hearing Filings", "Submissions and Arguments"),
        ("Cross Examination Material", "Transcripts"),
        ("Cost Claim Objection Reply", "Cost Claims"),
        ("Affidavits of Service", "Correspondence"),
        ("A Type The Portal Adds Next Year", "Correspondence"),
        (None, "Correspondence"),
        ("", "Correspondence"),
    ],
)
def test_categorise(document_type, category):
    assert categorise(document_type) == category


# ---------------------------------------------------------------- metadata


async def test_matter_info_counts_every_category_in_portal_order(portal, provider):
    portal.get("Record").respond(200, json=PAGE)

    info = await provider.fetch_matter(MATTER)

    assert info.counts == {
        "Decisions and Orders": 6,
        "Procedural Orders": 2,
        "Application and Evidence": 3,
        "Interrogatories": 4,
        "Undertakings": 2,
        "Submissions and Arguments": 4,
        "Transcripts": 2,
        "Cost Claims": 2,
        "Correspondence": 5,
    }
    assert list(info.counts) == [c.name for c in CATEGORIES]


async def test_matter_info_builds_a_title_and_dates_from_the_records(portal, provider):
    portal.get("Record").respond(200, json=PAGE)

    info = await provider.fetch_matter(MATTER)

    assert info.provider == "oeb" and info.matter == MATTER
    assert info.title == "Enbridge Gas Inc. – Gas rates application"
    assert (info.type, info.category, info.status) == ("Gas", "Rates", None)
    assert info.date_received == date(2024, 5, 8)  # earliest filing
    # The latest decision; not the later cost-awards decision (D25-18072, 2025-07-29).
    assert info.decision_date == date(2025, 5, 29)
    assert info.portal_url == oeb.case_url(MATTER)


async def test_search_asks_for_the_case_with_safe_properties(portal, provider):
    route = portal.get("Record").respond(200, json=PAGE)

    await provider.fetch_matter(MATTER)

    request = route.calls.last.request
    params = request.url.params
    assert params["q"] == f"CaseNumber={MATTER}"
    assert (params["format"], params["start"], params["sortBy"]) == ("json", "1", "recRegisteredOn-")
    assert "RecordContainer" not in params["properties"]
    assert request.headers["User-Agent"] == oeb.USER_AGENT


async def test_all_pages_are_fetched_for_the_counts(portal, provider, monkeypatch):
    monkeypatch.setattr(oeb, "PAGE_SIZE", 12)
    route = serve_pages(portal, PAGE["Results"], page_size=12)

    info = await provider.fetch_matter(MATTER)

    assert [c.request.url.params["start"] for c in route.calls] == ["1", "13", "25"]
    assert sum(info.counts.values()) == 30


async def test_records_beyond_the_cap_are_not_fetched(portal, provider, monkeypatch):
    monkeypatch.setattr(oeb, "PAGE_SIZE", 10)
    monkeypatch.setattr(oeb, "MAX_RECORDS", 20)
    route = serve_pages(portal, PAGE["Results"], page_size=10)

    info = await provider.fetch_matter(MATTER)

    assert route.call_count == 2
    assert sum(info.counts.values()) == 20


# ---------------------------------------------------------------- listings


async def test_listing_is_newest_first_within_one_category(portal, provider):
    portal.get("Record").respond(200, json=PAGE)

    info, refs = await provider.list_matter_and_documents(MATTER, "Submissions and Arguments", 10)

    # D24-30749 was registered after D24-29117 but is dated earlier; same-day filings keep the
    # server's newest-registered-first order.
    assert [r.external_id for r in refs] == ["D25-10851", "D25-10850", "D24-29117", "D24-30749"]
    assert [r.filed_on for r in refs] == [date(2025, 3, 10), date(2025, 3, 10), date(2024, 11, 4), date(2024, 5, 30)]
    assert [r.row_index for r in refs] == [0, 1, 2, 3]
    assert {(r.provider, r.matter, r.doc_type, r.access) for r in refs} == {
        ("oeb", MATTER, "Submissions and Arguments", "Public")
    }
    assert [r.file_ext for r in refs] == [".pdf", ".pdf", ".xlsx", ".pdf"]
    assert refs[0].title == "OGVG_SUB_EGI Rebasing Ph 2_20250218"
    assert all(r.source_type for r in refs)  # SIDocumentType as given, for ranking in the summary
    assert info.counts["Submissions and Arguments"] == 4


async def test_listing_stops_at_the_limit(portal, provider):
    portal.get("Record").respond(200, json=PAGE)

    refs = await provider.list_documents(MATTER, "Decisions and Orders", 3)

    assert [r.external_id for r in refs] == ["D25-18072", "D25-14480", "D24-31166"]


async def test_a_record_dated_only_by_registration_is_listed_under_that_date(portal, provider):
    portal.get("Record").respond(200, json=PAGE)

    (intervenor_list,) = [
        r for r in await provider.list_documents(MATTER, "Correspondence", 10) if r.external_id == "D24-29745"
    ]

    assert intervenor_list.filed_on == date(2024, 11, 14)


# ---------------------------------------------------------------- failures


EMPTY = {**PAGE, "Results": [], "TotalResults": 0, "HasMoreItems": False}


def _by_case(request: httpx.Request, *, canary_has_records: bool) -> httpx.Response:
    q = request.url.params.get("q", "")
    if "EB-2024-0111" in q and canary_has_records:
        return httpx.Response(200, json=PAGE)
    return httpx.Response(200, json=EMPTY)


async def test_unknown_case_is_matter_not_found(portal, provider):
    portal.get("Record").mock(side_effect=lambda r: _by_case(r, canary_has_records=True))

    with pytest.raises(MatterNotFound):
        await provider.fetch_matter("EB-2099-9999")


async def test_zero_results_for_everything_is_a_scrape_error_not_not_found(portal, provider):
    # WebDrawer answers a query it no longer understands with zero results. If even a known
    # large case comes back empty, telling the user "not found" would be a lie: retry instead.
    portal.get("Record").mock(side_effect=lambda r: _by_case(r, canary_has_records=False))

    with pytest.raises(ScrapeError):
        await provider.fetch_matter("EB-2099-9999")


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_server_errors_are_portal_unavailable(portal, provider, status):
    portal.get("Record").respond(status, text="Service Unavailable")

    with pytest.raises(PortalUnavailable):
        await provider.list_matter_and_documents(MATTER, "Decisions and Orders", 10)


async def test_timeouts_are_portal_unavailable(portal, provider):
    portal.get("Record").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(PortalUnavailable):
        await provider.fetch_matter(MATTER)


@pytest.mark.parametrize(
    "body",
    [
        b"<!doctype html><title>Error</title>",
        json.dumps({"TotalResults": 3}).encode(),
        json.dumps({**PAGE, "Results": "not a list"}).encode(),
        json.dumps({**PAGE, "ResponseStatus": {"ErrorCode": "TrimException", "Message": "Access denied."}}).encode(),
    ],
    ids=["html", "no-results", "results-not-a-list", "error-status"],
)
async def test_unexpected_responses_are_scrape_errors(portal, provider, body):
    portal.get("Record").respond(200, content=body)

    with pytest.raises(ScrapeError):
        await provider.fetch_matter(MATTER)


async def test_a_non_empty_case_with_no_records_returned_is_not_reported_as_missing(portal, provider):
    portal.get("Record").respond(200, json={**PAGE, "Results": [], "TotalResults": 12, "HasMoreItems": True})

    with pytest.raises(ScrapeError):
        await provider.fetch_matter(MATTER)


# ---------------------------------------------------------------- downloads


def ref(external_id: str, file_ext: str = ".pdf") -> DocumentRef:
    return DocumentRef(
        provider="oeb", matter=MATTER, doc_type="Decisions and Orders", external_id=external_id,
        title=f"Title of {external_id}", file_ext=file_ext,
    )


def serve_file(portal: respx.MockRouter, external_id: str, body: bytes, *, filename: str,
               content_type: str = "application/pdf", status: int = 200) -> respx.Route:
    return portal.get(f"Record/{external_id}/File/document").respond(
        status, content=body,
        headers={"Content-Type": content_type, "Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def collect(provider: OebProvider, refs: list[DocumentRef], dest: Path) -> list:
    return [f async for f in provider.download(MATTER, refs, str(dest))]


async def test_download_verifies_and_names_the_file(portal, provider, tmp_path):
    serve_file(portal, "D25-18072", PDF, filename="dec_order_cost awards_EGI Rebasing Phase 2_20250729_eSigned.PDF")

    (f,) = await collect(provider, [ref("D25-18072")], tmp_path)

    sha = hashlib.sha256(PDF).hexdigest()
    assert (f.sha256, f.size, f.filename) == (sha, len(PDF), "D25-18072.pdf")
    assert Path(f.path) == tmp_path / f"{sha}.pdf" and Path(f.path).read_bytes() == PDF
    assert not list(tmp_path.glob(".*.part"))


async def test_download_takes_the_extension_the_server_serves(portal, provider, tmp_path):
    sheet = b"PK\x03\x04" + b"y" * 100
    serve_file(portal, "D25-2019", sheet, filename="EGI_UNDERTAKING_J1.2.XLSX",
               content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    (f,) = await collect(provider, [ref("D25-2019", file_ext="")], tmp_path)

    assert f.filename == "D25-2019.xlsx" and Path(f.path).suffix == ".xlsx"


@pytest.mark.parametrize(
    ("body", "content_type", "error"),
    [
        (b"<html>not a pdf</html>", "application/pdf", ScrapeError),
        (b"<!doctype html><title>Error</title>", "text/html; charset=utf-8", ScrapeError),
        (b"", "application/pdf", PortalUnavailable),
    ],
    ids=["bad-magic", "html-error-page", "empty"],
)
async def test_a_bad_file_is_rejected(portal, provider, tmp_path, body, content_type, error):
    serve_file(portal, "D25-18072", body, filename="x.PDF", content_type=content_type)

    with pytest.raises(error):
        await collect(provider, [ref("D25-18072")], tmp_path)
    assert not list(tmp_path.glob("*.pdf"))


async def test_one_failed_download_does_not_stop_the_others(portal, provider, tmp_path):
    serve_file(portal, "D25-1", PDF, filename="one.pdf")
    serve_file(portal, "D25-2", b"", filename="two.pdf", status=503)
    serve_file(portal, "D25-3", PDF + b"3", filename="three.pdf")

    files = await collect(provider, [ref("D25-1"), ref("D25-2"), ref("D25-3")], tmp_path)

    assert sorted(f.ref.external_id for f in files) == ["D25-1", "D25-3"]


async def test_downloads_stay_within_the_concurrency_cap(portal, provider, tmp_path):
    in_flight = peak = 0

    async def slow_file(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, content=PDF + request.url.path.encode(), headers={"Content-Type": "application/pdf"})

    portal.get(url__regex=r"/Record/D25-\d+/File/document$").mock(side_effect=slow_file)

    files = await collect(provider, [ref(f"D25-{i}") for i in range(6)], tmp_path)

    assert len(files) == 6 and peak == 2


# ---------------------------------------------------------------- matter numbers, dates, decisions (fix pass B)


def test_case_numbers_are_ascii_only_but_full_width_input_is_normalised():
    assert OebProvider.normalise("ＥＢ－２０２４－０１１１") == MATTER  # NFKC first
    assert oeb.MATTER_RE.fullmatch("EB-２０２４-0111") is None
    assert oeb.MENTION_RE.search("see EB-２０２４-０１１１") is None


def test_a_late_evening_registration_is_dated_in_toronto():
    raw = {
        "Uri": 7, "RecordNumber": {"Value": "D24-7"}, "RecordTitle": {"Value": "Evening filing"},
        "RecordDateRegistered": {"IsClear": False, "DateTime": "2024-07-03T01:15:00.0000000Z"},  # 9:15 PM EDT
    }
    assert oeb._parse_record(raw).filed_on == date(2024, 7, 2)
    raw["RecordDateRegistered"]["DateTime"] = "2024-07-03T15:00:00.0000000Z"
    assert oeb._parse_record(raw).filed_on == date(2024, 7, 3)


def _record(number: str, document_type: str, title: str, issued: str) -> dict:
    return {
        "Uri": 1, "RecordNumber": {"Value": number}, "RecordTitle": {"Value": title},
        "DateIssued": {"IsClear": False, "DateTime": f"{issued}T00:00:00.0000000Z"},
        "Fields": {"SIDocumentType": {"Value": document_type}},
    }


async def test_decision_date_ignores_cost_awards_and_draft_rate_orders(portal, provider):
    results = [
        _record("D1", "Draft Rate Order", "EGI_DRO", "2025-09-01"),
        _record("D2", "Decision and Order on Cost Awards", "dec_order_costs", "2025-08-01"),
        _record("D3", "Decision and Order", "dec_order_cost awards_EGI", "2025-07-01"),  # typed loosely
        _record("D4", "Decision and Order", "dec_order_EGI Rates", "2025-05-29"),
        _record("D5", "Submission", "SUB", "2025-10-01"),
    ]
    portal.get("Record").respond(200, json={**PAGE, "Results": results, "TotalResults": 5, "HasMoreItems": False})

    info = await provider.fetch_matter(MATTER)

    assert info.counts["Decisions and Orders"] == 4  # still listed under the category
    assert info.decision_date == date(2025, 5, 29)


# ---------------------------------------------------------------- HTTP policy (fix pass B)


@pytest.mark.parametrize("status", [400, 403])
async def test_a_refused_request_is_provider_rejected_not_retried(portal, provider, status):
    portal.get("Record").respond(status, text="Forbidden")

    with pytest.raises(ProviderRejected) as info:
        await provider.fetch_matter(MATTER)
    assert not info.value.retryable


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"Retry-After": "120"}, 120.0), ({}, None), ({"Retry-After": "soon"}, None)],
)
@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_is_passed_on(portal, provider, status, headers, expected):
    portal.get("Record").respond(status, headers=headers)

    with pytest.raises(PortalUnavailable) as info:
        await provider.fetch_matter(MATTER)
    assert info.value.retry_after == expected


async def test_retry_after_as_an_http_date(portal, provider):
    later = datetime.now(UTC) + timedelta(minutes=10)
    portal.get("Record").respond(503, headers={"Retry-After": format_datetime(later, usegmt=True)})

    with pytest.raises(PortalUnavailable) as info:
        await provider.fetch_matter(MATTER)
    assert 590 <= info.value.retry_after <= 600


async def test_redirects_are_not_followed(portal, provider):
    portal.get("Record").respond(302, headers={"Location": "https://login.example.com/"})

    with pytest.raises(ScrapeError, match="redirect"):
        await provider.fetch_matter(MATTER)
    assert len(portal.calls) == 1


async def test_a_redirected_download_is_not_followed(portal, provider, tmp_path):
    portal.get("Record/D25-1/File/document").respond(301, headers={"Location": "https://elsewhere.example/x.pdf"})

    with pytest.raises(ScrapeError, match="redirect"):
        await collect(provider, [ref("D25-1")], tmp_path)


# ---------------------------------------------------------------- size budget and file types (fix pass B)


def small_provider(client: httpx.AsyncClient, max_bytes: int, **kw) -> OebProvider:
    policy = FilePolicy.of(max_bytes, [".pdf", ".xlsx", ".doc"])
    return OebProvider(client, max_concurrency=2, file_policy=policy, **kw)


async def test_a_listed_size_over_the_budget_is_skipped_before_downloading(portal, provider, tmp_path):
    portal.get("Record").respond(200, json=PAGE)
    route = serve_file(portal, "D24-32735", PDF, filename="appl.pdf")
    capped = small_provider(provider._client, max_bytes=10_000_000)  # D24-32735 is listed at 42 MB
    _, refs = await capped.list_matter_and_documents(MATTER, "Application and Evidence", 10)
    big = next(r for r in refs if r.external_id == "D24-32735")

    with pytest.raises(TooLarge):
        await collect(capped, [big], tmp_path)
    assert route.call_count == 0


async def test_a_declared_content_length_over_the_budget_stops_the_download(portal, provider, tmp_path):
    serve_file(portal, "D25-1", PDF, filename="one.pdf")

    with pytest.raises(TooLarge):
        await collect(small_provider(provider._client, max_bytes=len(PDF) - 1), [ref("D25-1")], tmp_path)
    assert not list(tmp_path.iterdir())


async def test_an_undeclared_size_is_capped_while_streaming(portal, provider, tmp_path):
    async def body():
        for _ in range(10):
            yield b"%PDF-" + b"x" * 1_000

    portal.get("Record/D25-1/File/document").mock(
        side_effect=lambda request: httpx.Response(200, content=body(), headers={"Content-Type": "application/pdf"})
    )

    with pytest.raises(TooLarge):
        await collect(small_provider(provider._client, max_bytes=4_000), [ref("D25-1")], tmp_path)
    assert not list(tmp_path.iterdir())  # no partial file left behind


async def test_a_download_that_drags_on_is_cut_off(portal, provider, tmp_path):
    async def body():
        yield b"%PDF-1.6\n"
        await asyncio.sleep(5)
        yield b"never"

    portal.get("Record/D25-1/File/document").mock(
        side_effect=lambda request: httpx.Response(200, content=body(), headers={"Content-Type": "application/pdf"})
    )
    slow = small_provider(provider._client, max_bytes=10_000, download_timeout_s=0.2)

    with pytest.raises(PortalUnavailable, match="not finished"):
        await asyncio.wait_for(collect(slow, [ref("D25-1")], tmp_path), timeout=3)
    assert not list(tmp_path.iterdir())


async def test_file_types_outside_the_allowlist_are_not_fetched(portal, provider, tmp_path):
    portal.get("Record").respond(200, json=PAGE)
    route = serve_file(portal, "D24-20799", b"\xff\xd8\xff", filename="ad.jpg", content_type="image/jpeg")
    refs = await provider.list_documents(MATTER, "Interrogatories", 10)
    (image,) = [r for r in refs if r.external_id == "D24-20799"]
    assert image.file_ext in {".jpg", ".jpeg"}

    with pytest.raises(UnsupportedFileType) as info:
        await collect(provider, [image], tmp_path)
    assert route.call_count == 0 and not info.value.retryable


async def test_a_served_type_outside_the_allowlist_is_rejected(portal, provider, tmp_path):
    serve_file(portal, "D25-1", b"MZ\x90\x00", filename="setup.exe", content_type="application/octet-stream")

    with pytest.raises(UnsupportedFileType):
        await collect(provider, [ref("D25-1", file_ext="")], tmp_path)
    assert not list(tmp_path.iterdir())


async def test_when_every_file_fails_a_retryable_error_wins(portal, provider, tmp_path):
    serve_file(portal, "D25-1", PDF, filename="one.pdf")
    serve_file(portal, "D25-2", b"", filename="two.pdf", status=503)

    with pytest.raises(PortalUnavailable):
        await collect(small_provider(provider._client, max_bytes=100), [ref("D25-1"), ref("D25-2")], tmp_path)


async def test_canary_is_checked_on_a_freshly_booted_host(portal, provider, monkeypatch):
    # monotonic time starts near 0 after boot; the guard must still run its first check
    monkeypatch.setattr("agent.providers.oeb.time.monotonic", lambda: 5.0)
    portal.get("Record").mock(side_effect=lambda r: _by_case(r, canary_has_records=False))

    with pytest.raises(ScrapeError):
        await provider.fetch_matter("EB-2099-9999")
