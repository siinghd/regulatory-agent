"""FERC eLibrary provider against recorded responses (respx): parsing, categories, ordering, failures.

Fixtures (fetched 2026-10-04 from elibrary.ferc.gov/eLibraryWebAPI):
- ferc_RM22-14_page.json: the search response for docket RM22-14 (395 documents) trimmed to 30
  chosen to cover every category present, multi-class documents, non-public documents, multi-file
  documents (up to 46 files), DOCX/TXT-only documents and filed/issued date mismatches; its totals
  were rewritten to match the trimmed list.
- ferc_ER24-1234-000_page.json: the full search response for ER24-1234-000 (3 documents).
- ferc_ER24-1234_description.json: the docket description response for ER24-1234.
"""

import asyncio
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from agent.models import (
    DocumentRef,
    MatterNotFound,
    PortalUnavailable,
    ProviderRejected,
    ScrapeError,
    TooLarge,
)
from agent.providers import ferc
from agent.providers.ferc import CATEGORIES, FercProvider, categorise, primary_file, trim_title
from agent.providers.files import FilePolicy, UnsupportedFileType

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PAGE: dict[str, Any] = json.loads((FIXTURES / "ferc_RM22-14_page.json").read_text())
ER_PAGE: dict[str, Any] = json.loads((FIXTURES / "ferc_ER24-1234-000_page.json").read_text())
ER_DESCRIPTION: dict[str, Any] = json.loads((FIXTURES / "ferc_ER24-1234_description.json").read_text())
RM_DESCRIPTION = {"DataList": ["NOPR", "DKT"], "ErrorList": []}  # the real response for RM22-14
NOT_FOUND = {"DataList": ["Applicant not Found.", ""], "ErrorList": []}  # the real response for ZZ99-999999 (and any unknown docket)
# A few real rows of Search/GetClassTypes, including classes no category names.
CLASS_TYPES = [
    {"Class": c, "Type": t, "Library": "E/G/O/H/Gen", "Category": k, "Accession_Number": None}
    for c, t, k in [
        ("Order/Opinion", "Delegated Order", "Issuance"),
        ("Applicant Correspondence", "General Correspondence", "Submittal"),
        ("FERC Memo", "Memo to Commission", "Issuance"),
        ("Subpoena", "Subpoena", "Submittal"),
        ("Drawing/Maps", "Drawing/Maps", "Submittal"),
    ]
]
MATTER = "RM22-14"
PDF = b"%PDF-1.6\n" + b"x" * 2_000
DOCX = b"PK\x03\x04" + b"w" * 500


@pytest.fixture
async def portal():
    """eLibrary with two dockets: RM22-14 (whole) and ER24-1234-000; anything else is unknown."""
    async with respx.mock(base_url=ferc.API_URL, assert_all_called=False) as router:
        router.get(url__regex=r"Docket/getDocketDescription/(?P<docket>[^/]+)$", name="describe").mock(
            side_effect=describe({"RM22-14": RM_DESCRIPTION, "ER24-1234": ER_DESCRIPTION})
        )
        router.post("Search/AdvancedSearch", name="search").mock(
            side_effect=search({"RM22-14": PAGE["searchHits"], "ER24-1234-000": ER_PAGE["searchHits"]})
        )
        router.get("Search/GetClassTypes", name="classes").respond(200, json=CLASS_TYPES)
        yield router


@pytest.fixture
async def provider():
    client = ferc.make_client()
    yield FercProvider(client, max_concurrency=2)
    await client.aclose()


def describe(descriptions: dict[str, dict]):
    def respond(request: httpx.Request, docket: str) -> httpx.Response:
        return httpx.Response(200, json=descriptions.get(docket, NOT_FOUND))

    return respond


def search(dockets: dict[str, list[dict]]):
    """Answer AdvancedSearch like eLibrary: by docket (+ sub-docket) or by accession, paged."""

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["docketSearches"]:
            (wanted,) = body["docketSearches"]
            key = "-".join([wanted["docketNumber"], *wanted["subDocketNumbers"]])
            hits = dockets.get(key, [])
        else:
            every = (h for hs in dockets.values() for h in hs)
            hits = [h for h in every if h["acesssionNumber"] == body["accessionNumber"]][:1]
        per, page = body["resultsPerPage"], body["curPage"]
        chunk = hits[(page - 1) * per : page * per]
        return httpx.Response(200, json={**PAGE, "searchHits": chunk, "totalHits": len(hits), "numHits": len(chunk)})

    return respond


def search_bodies(portal: respx.MockRouter) -> list[dict]:
    return [json.loads(c.request.content) for c in portal["search"].calls]


# ---------------------------------------------------------------- docket numbers and categories


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ER24-1234-000", "ER24-1234-000"),
        ("er24-1234-000", "ER24-1234-000"),
        ("ER24-1234", "ER24-1234"),
        ("rm22-14", "RM22-14"),
        ("EL16-92-001", "EL16-92-001"),
        ("ER24–1234–000", "ER24-1234-000"),  # en dashes, as pasted from a PDF
        ("es24-12-000", "ES24-12-000"),
    ],
)
def test_normalise_accepts_the_ways_people_write_dockets(raw, expected):
    assert FercProvider.normalise(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["ER24-1234-0001", "ER24-1234-00", "ER2024-1234", "ER24-1234567", "ABCD24-1", "ER24", "24-1234",
     "ER 24-1234", "EB-2024-0111", "EB2024-0111", "M12205"],
)
def test_normalise_rejects_other_numbers(raw):
    assert FercProvider.normalise(raw) is None


@pytest.mark.parametrize(
    ("class_types", "category"),
    [
        ([("Order/Opinion", "Delegated Order")], "Orders and Decisions"),
        ([("ALJ Issuance", "ALJ Initial Decision")], "Orders and Decisions"),
        ([("Notice", "Notice of Proposed Rulemaking")], "Notices"),
        ([("Application/Petition/Request", "Tariff Filing")], "Applications and Filings"),
        ([("Agreement/Understanding/Contract", "Settlement Agreement (Stipulation and Agreement)")],
         "Applications and Filings"),
        ([("Report/Form", "Annual Generation Report")], "Applications and Filings"),
        ([("Other Submittal", "Congressional Submittal")], "Applications and Filings"),
        ([("A Class eLibrary Adds Later", "Gas and Oil Tariff Filing")], "Applications and Filings"),
        ([("Comments/Protest", "Rulemaking Comment")], "Comments and Protests"),
        ([("Pleading/Motion", "Request for Rehearing or Appeal")], "Motions and Pleadings"),
        ([("Briefing/Arguments of Law", "Brief")], "Motions and Pleadings"),
        ([("Intervention", "Motion to Intervene Out of Time")], "Interventions"),
        ([("Testimony", "Initial Testimony")], "Evidence and Testimony"),
        ([("Exhibit", "Exhibit")], "Evidence and Testimony"),
        ([("Transcript", "Hearing Transcript")], "Evidence and Testimony"),
        ([("Interrogatory/Data Request", "Response to Data Request")], "Evidence and Testimony"),
        ([("Applicant Correspondence", "Deficiency Letter/Data Response")], "Correspondence"),
        ([("FERC Correspondence With Applicant", "Deficiency Letter")], "Correspondence"),
        ([("Informational Correspondence", "News Release")], "Correspondence"),
        ([("FERC Memo", "Memo to Commission")], "Correspondence"),
        ([("Status Report", "Status Report")], "Correspondence"),
        ([("Court Related Documents", "Court Related Documents")], "Motions and Pleadings"),
        ([("FERC Report/Study", "Environmental Assessment")], "Correspondence"),  # not a decision
        ([("FERC Comment", "FERC Comment")], "Comments and Protests"),
        ([("Deposition Document", "Deposition")], "Evidence and Testimony"),
        ([("A Class eLibrary Adds Later", "Something")], "Correspondence"),
        ([], "Correspondence"),
        ([(None, None)], "Correspondence"),
        # Several classes: the first category claiming one wins...
        ([("Notice", "Formal Notice"), ("Order/Opinion", "Delegated Order")], "Orders and Decisions"),
        ([("Intervention", "Motion/Notice of Intervention"), ("Comments/Protest", "Rulemaking Comment")],
         "Comments and Protests"),
        # ...but a catch-all class only when nothing more specific is named.
        ([("Comments/Protest", "Rulemaking Comment"), ("Other Submittal", "Congressional Submittal")],
         "Comments and Protests"),
        ([("Other Submittal", "Congressional Submittal"), ("Applicant Correspondence", "Response")],
         "Correspondence"),
        ([("Other Submittal", "Congressional Submittal"), ("A Class eLibrary Adds Later", "Something")],
         "Applications and Filings"),
        ([("comments/protest", "rulemaking comment")], "Comments and Protests"),  # case-insensitive
    ],
)
def test_categorise(class_types, category):
    assert categorise(class_types) == category


@pytest.mark.parametrize(
    ("class_types", "expected"),
    [
        ([("Order/Opinion", "Order on Rehearing")], "Order/Opinion: Order on Rehearing"),
        # the pair that decided the category, not the first one listed
        ([("Notice", "Formal Notice"), ("Order/Opinion", "Delegated Order")], "Order/Opinion: Delegated Order"),
        ([("Other Submittal", "Congressional Submittal"), ("Comments/Protest", "Rulemaking Comment")],
         "Comments/Protest: Rulemaking Comment"),
        ([("A Class eLibrary Adds Later", "Something")], "A Class eLibrary Adds Later: Something"),
        ([("Notice", None)], "Notice"),
        ([], None),
        ([(None, None)], None),
    ],
)
def test_source_type_is_the_class_and_type_that_decided_the_category(class_types, expected):
    assert ferc.source_type(class_types) == expected


@pytest.mark.parametrize(
    ("description", "opening", "title"),
    [
        ("NOPR", ("Notice of Proposed Rulemaking re Improvements to Generator Interconnection Procedures and "
                  "Agreements under RM22-14. Commissioner Danly and Commissioner Christie are dissenting."),
         "Improvements to Generator Interconnection Procedures and Agreements"),
        ("Formal Complaint", "(doc-less) Complaint of Acme Power LLC against Example Transmission Co. under EL16-92.",
         "Complaint of Acme Power LLC against Example Transmission Co"),
        ("NOPR", "Errata Notice under RM22-14.", "FERC docket RM22-14: NOPR"),  # nothing meaningful to take
        (None, None, "FERC docket RM22-14"),
        ("Tariff filing by Example Utility Co. for its transmission rates", "Anything",
         "Tariff filing by Example Utility Co. for its transmission rates"),  # meaningful: kept as is
    ],
)
def test_a_generic_docket_description_is_replaced_by_its_opening_documents_subject(description, opening, title):
    docs = [ferc._Doc(accession="20220616-3069", title=opening, category="Notices", filed_on=date(2022, 6, 16),
                      application_type=None, public=True, file=ferc._File("f", "x.docx", ".docx"))] if opening else []
    assert ferc._docket_title("RM22-14", description, docs) == title


def transmittals(*files: tuple[str, str]) -> list[ferc._Transmittal]:
    return [ferc._Transmittal(fileId=f"id-{i}", fileName=name, fileType=kind) for i, (name, kind) in enumerate(files)]


@pytest.mark.parametrize(
    ("files", "chosen"),
    [
        # Real transmittal lists (EL16-92, ER24-1234, RM22-14).
        ([("687003_Interv.TXT", "TXT"), ("12240208.PDF", "PDF")], ("id-1", "12240208.PDF", ".pdf")),
        ([("EL16-92-000.DOC", "DOC"), ("12230018.PDF", "PDF")], ("id-1", "12230018.PDF", ".pdf")),
        ([("TransmittalLetter_AMPS_Agreement_Final.pdf", "PDF"), ("RS27_Marked.pdf", "PDF"),
          ("FERC GENERATED TARIFF FILING.rtf", "RTF")],
         ("id-0", "TransmittalLetter_AMPS_Agreement_Final.pdf", ".pdf")),
        ([("CECONY ORU SCR Testimony.DOCX", "DOCX"), ("Griffin SCR Affidavit.DOCX", "DOCX")],
         ("id-0", "CECONY ORU SCR Testimony.DOCX", ".docx")),
        ([("FERC GENERATED TARIFF FILING.rtf", "RTF"), ("ER24-1234-000.docx", "DOCX")],
         ("id-1", "ER24-1234-000.docx", ".docx")),
        ([("687003_Interv.TXT", "TXT")], ("id-0", "687003_Interv.TXT", ".txt")),
        ([("no extension", "")], ("id-0", "no extension", "")),
    ],
)
def test_primary_file_is_the_first_pdf_then_docx_then_anything(files, chosen):
    file = primary_file(transmittals(*files))
    assert (file.file_id, file.name, file.ext) == chosen


def test_a_document_without_files_has_no_primary_file():
    assert primary_file([]) is None


def test_long_docket_descriptions_are_trimmed_at_a_word_boundary():
    description = ER_DESCRIPTION["DataList"][0]
    assert len(description) == 175

    title = trim_title(description)

    # e-filing boilerplate ("submitted on <timestamp>", "Filing Type code") is not part of a title
    assert title == (
        "NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii): RS 27 - Third Amended and "
        "Restated AMPS Agreement"
    )
    assert len(title) <= ferc.TITLE_MAX + 1
    assert trim_title("  NOPR \n") == "NOPR"
    assert trim_title("x" * 200) == "x" * 160 + "…"


# ---------------------------------------------------------------- metadata


async def test_matter_info_counts_every_category_in_order(portal, provider):
    info = await provider.fetch_matter(MATTER)

    assert info.counts == {
        "Orders and Decisions": 5,
        "Notices": 2,
        "Applications and Filings": 0,
        "Comments and Protests": 10,  # incl. two rulemaking comments that are also congressional submittals
        "Motions and Pleadings": 9,  # incl. a court record
        "Interventions": 2,
        "Evidence and Testimony": 0,
        "Correspondence": 2,
    }
    assert list(info.counts) == [c.name for c in CATEGORIES]


async def test_matter_info_for_a_whole_docket(portal, provider):
    info = await provider.fetch_matter(MATTER)

    # eLibrary describes the docket only as "NOPR": the title is the subject of its opening document
    assert (info.provider, info.matter, info.title) == (
        "ferc", MATTER, "Improvements to Generator Interconnection Procedures and Agreements",
    )
    # A rulemaking has no application to name its type, and "DKT" is a status code, not a category.
    assert (info.type, info.category, info.status) == (None, None, None)
    assert info.date_received == date(2022, 6, 16)  # the NOPR, the earliest document
    assert info.decision_date == date(2024, 8, 20)  # the latest order
    assert info.portal_url == "https://elibrary.ferc.gov/eLibrary/docketsheet?docket_number=RM22-14"


async def test_matter_info_for_a_sub_docket(portal, provider):
    info = await provider.fetch_matter("ER24-1234-000")

    assert info.title.startswith("NorthWestern Corporation submits tariff filing")
    assert info.title.endswith("AMPS Agreement")  # no e-filing timestamp
    # The opening filing, not FERC's same-day notice of it (whose accession number sorts first).
    assert info.type == "Tariff Filing"
    assert info.category is None  # "DKT"
    assert (info.date_received, info.decision_date) == (date(2024, 2, 12), date(2024, 4, 8))
    assert {k: v for k, v in info.counts.items() if v} == {
        "Orders and Decisions": 1, "Notices": 1, "Applications and Filings": 1,
    }
    assert info.portal_url.endswith("docket_number=ER24-1234-000")


async def test_search_asks_for_the_docket_politely(portal, provider):
    await provider.fetch_matter("ER24-1234-000")

    (body,) = search_bodies(portal)
    assert body["docketSearches"] == [{"docketNumber": "ER24-1234", "subDocketNumbers": ["000"]}]
    assert body["dateSearches"]  # the API returns nothing without one
    assert (body["resultsPerPage"], body["curPage"], body["searchText"]) == (100, 1, "*")
    assert portal["describe"].calls.last.request.url.path.endswith("/getDocketDescription/ER24-1234")
    for call in portal.calls:
        assert call.request.headers["User-Agent"] == ferc.USER_AGENT


async def test_a_whole_docket_searches_every_sub_docket(portal, provider):
    await provider.fetch_matter(MATTER)

    (body,) = search_bodies(portal)
    assert body["docketSearches"] == [{"docketNumber": "RM22-14", "subDocketNumbers": []}]


async def test_all_pages_are_fetched_for_the_counts(portal, provider, monkeypatch):
    monkeypatch.setattr(ferc, "PAGE_SIZE", 12)

    info = await provider.fetch_matter(MATTER)

    assert sorted(b["curPage"] for b in search_bodies(portal)) == [1, 2, 3]
    assert sum(info.counts.values()) == 30


async def test_documents_beyond_the_cap_are_not_fetched(portal, provider, monkeypatch):
    monkeypatch.setattr(ferc, "PAGE_SIZE", 10)
    monkeypatch.setattr(ferc, "MAX_HITS", 20)

    info = await provider.fetch_matter(MATTER)

    assert portal["search"].call_count == 2
    assert sum(info.counts.values()) == 20


async def test_class_list_is_checked_once_and_unmapped_classes_are_logged(portal, provider):
    with capture_logs() as logs:
        await provider.fetch_matter(MATTER)
        await provider.fetch_matter(MATTER)

    assert portal["classes"].call_count == 1
    (entry,) = [e for e in logs if e["event"] == "ferc.unmapped_classes"]
    assert entry["log_level"] == "info"
    assert entry["classes"] == ["Drawing/Maps", "Subpoena"]


async def test_a_failing_class_list_never_fails_a_request(portal, provider):
    portal["classes"].respond(500)

    info = await provider.fetch_matter(MATTER)

    assert sum(info.counts.values()) == 30


# ---------------------------------------------------------------- listings


async def test_listing_is_newest_first_and_includes_non_public_documents(portal, provider):
    info, refs = await provider.list_matter_and_documents(MATTER, "Motions and Pleadings", 10)

    assert [(r.external_id, r.access) for r in refs] == [
        ("20251016-5022", "Public"),
        ("20250414-4001", "Public"),  # a court record
        ("20240927-5178", "Non-public"),
        ("20240520-0032", "Public"),
        ("20240425-0007", "Public"),
        ("20230912-5190", "Public"),
        ("20230831-5267", "Public"),
        ("20230828-5139", "Public"),
        ("20230825-5227", "Non-public"),
    ]
    assert [r.row_index for r in refs] == list(range(9))
    assert [r.file_ext for r in refs] == [".pdf"] * 6 + [".docx", ".pdf", ".pdf"]
    assert refs[0].filed_on == date(2025, 10, 16)
    assert refs[0].title == "Arizona Public Service Company submits request to update service lists under ER21-2885, et al."
    assert {(r.provider, r.matter, r.doc_type) for r in refs} == {("ferc", MATTER, "Motions and Pleadings")}
    assert info.counts["Motions and Pleadings"] == 9
    # eLibrary's own class and type, for ranking documents in the summary
    assert refs[0].source_type == "Pleading/Motion: Procedural Motion"
    assert refs[1].source_type == "Court Related Documents: Court Related Documents"


async def test_listing_stops_at_the_limit_of_public_documents(portal, provider):
    refs = await provider.list_documents(MATTER, "Comments and Protests", 5)

    # Same-day documents: the later accession number first.
    assert [(r.external_id, r.access) for r in refs] == [
        ("20250228-4001", "Public"),
        ("20230912-4000", "Public"),
        ("20230724-4001", "Public"),
        ("20230615-5029", "Public"),
        ("20221214-5203", "Non-public"),
        ("20221214-5122", "Non-public"),
        ("20221107-5017", "Public"),
    ]
    assert [r.external_id for r in await provider.list_documents(MATTER, "Comments and Protests", 1)] == [
        "20250228-4001"
    ]
    assert await provider.list_documents(MATTER, "Comments and Protests", 0) == []


async def test_documents_are_dated_by_when_they_were_filed(portal, provider):
    refs = await provider.list_documents(MATTER, "Correspondence", 10)

    # 20231011-4003 is dated 08/11/2023 ("issued") but joined the record on 10/11/2023 ("filed").
    assert [(r.external_id, r.filed_on) for r in refs] == [
        ("20250402-4003", date(2025, 4, 2)),
        ("20231011-4003", date(2023, 10, 11)),
    ]


async def test_an_empty_category_lists_nothing(portal, provider):
    info, refs = await provider.list_matter_and_documents(MATTER, "Evidence and Testimony", 10)

    assert refs == [] and info.counts["Evidence and Testimony"] == 0


# ---------------------------------------------------------------- failures


async def test_unknown_docket_is_matter_not_found(portal, provider):
    with pytest.raises(MatterNotFound):
        await provider.fetch_matter("ER99-999999")


async def test_unknown_sub_docket_of_a_known_docket_is_matter_not_found(portal, provider):
    with pytest.raises(MatterNotFound):
        await provider.fetch_matter("ER24-1234-005")


async def test_zero_results_for_everything_is_a_scrape_error_not_not_found(portal, provider):
    # The search answers a request it no longer understands with zero hits. If even the canary
    # docket comes back empty, telling the user "not found" would be a lie: retry instead.
    portal["search"].mock(side_effect=search({}))

    with pytest.raises(ScrapeError):
        await provider.fetch_matter("ER99-999999")


async def test_no_docket_descriptions_at_all_is_a_scrape_error_not_not_found(portal, provider):
    portal["describe"].mock(side_effect=describe({}))

    with pytest.raises(ScrapeError):
        await provider.fetch_matter("ER99-999999")


async def test_a_described_docket_with_no_documents_is_a_scrape_error(portal, provider):
    portal["search"].mock(side_effect=search({"ER24-1234-000": ER_PAGE["searchHits"]}))

    with pytest.raises(ScrapeError):
        await provider.fetch_matter(MATTER)


async def test_canary_is_checked_once_for_a_while(portal, provider):
    for _ in range(3):
        with pytest.raises(MatterNotFound):
            await provider.fetch_matter("ER99-999999")

    canary = [b for b in search_bodies(portal) if b["docketSearches"][0]["docketNumber"] == "ER24-1234"]
    assert len(canary) == 1


@pytest.mark.parametrize("status", [500, 502, 503, 429])
@pytest.mark.parametrize("route", ["search", "describe"])
async def test_server_errors_are_portal_unavailable(portal, provider, status, route):
    portal[route].respond(status, text="Service Unavailable")

    with pytest.raises(PortalUnavailable):
        await provider.list_matter_and_documents(MATTER, "Notices", 10)


@pytest.mark.parametrize("route", ["search", "describe"])
async def test_timeouts_are_portal_unavailable(portal, provider, route):
    portal[route].mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(PortalUnavailable):
        await provider.fetch_matter(MATTER)


async def test_a_later_page_failing_fails_the_listing(portal, provider, monkeypatch):
    monkeypatch.setattr(ferc, "PAGE_SIZE", 10)
    serve = search({"RM22-14": PAGE["searchHits"]})

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503) if json.loads(request.content)["curPage"] == 3 else serve(request)

    portal["search"].mock(side_effect=flaky)

    with pytest.raises(PortalUnavailable):
        await provider.fetch_matter(MATTER)


def _hit(**changes: Any) -> dict[str, Any]:
    return {**PAGE["searchHits"][0], **changes}


@pytest.mark.parametrize(
    "body",
    [
        b"<!doctype html><title>Attention Required! | Cloudflare</title>",
        json.dumps({"totalHits": 3}).encode(),
        json.dumps({**PAGE, "searchHits": "not a list"}).encode(),
        json.dumps({**PAGE, "searchHits": None, "totalHits": 0, "success": False,
                    "errorMessage": "Object reference not set to an instance of an object."}).encode(),
        json.dumps({**PAGE, "searchHits": [_hit(filedDate="2024-13-45")]}).encode(),
        json.dumps({**PAGE, "searchHits": [{"description": "no accession number"}]}).encode(),
        json.dumps({**PAGE, "searchHits": [], "totalHits": 12}).encode(),
    ],
    ids=["html", "no-hits-field", "hits-not-a-list", "search-failed", "bad-date", "no-accession",
         "count-but-no-hits"],
)
async def test_unexpected_search_responses_are_scrape_errors(portal, provider, body):
    portal["search"].respond(200, content=body)

    with pytest.raises(ScrapeError):
        await provider.fetch_matter(MATTER)


@pytest.mark.parametrize(
    "body",
    [b"<html>Error</html>", json.dumps({"DataList": []}).encode(), json.dumps({"Data": ["NOPR"]}).encode()],
    ids=["html", "empty", "renamed"],
)
async def test_unexpected_description_responses_are_scrape_errors(portal, provider, body):
    portal["describe"].respond(200, content=body)

    with pytest.raises(ScrapeError):
        await provider.fetch_matter(MATTER)


async def test_null_lists_in_a_hit_are_read_as_empty(portal, provider):
    hits = [_hit(classTypes=None), *PAGE["searchHits"][1:]]
    portal["search"].mock(side_effect=search({"RM22-14": hits}))

    info = await provider.fetch_matter(MATTER)

    assert info.counts["Motions and Pleadings"] == 8 and info.counts["Correspondence"] == 3


# ---------------------------------------------------------------- downloads


def serve_files(portal: respx.MockRouter, files: dict[str, tuple[int, bytes, str, str]]) -> respx.Route:
    """files: fileId -> (status, body, content type, served filename)."""

    def respond(request: httpx.Request) -> httpx.Response:
        (file_id,) = json.loads(request.content)["fileidLst"]
        if file_id not in files:
            return httpx.Response(401)  # what eLibrary answers for an unknown file id
        status, body, content_type, name = files[file_id]
        return httpx.Response(
            status, content=body,
            headers={"Content-Type": content_type, "Content-Disposition": f"attachment; filename={name}"},
        )

    return portal.post("File/DownloadP8File", name="download").mock(side_effect=respond)


def ref(accession: str, file_ext: str = ".pdf", access: str = "Public") -> DocumentRef:
    return DocumentRef(
        provider="ferc", matter="ER24-1234-000", doc_type="Applications and Filings", external_id=accession,
        title=f"Title of {accession}", file_ext=file_ext, access=access,
    )


async def collect(provider: FercProvider, refs: list[DocumentRef], dest: Path) -> list:
    return [f async for f in provider.download("ER24-1234-000", refs, str(dest))]


TRANSMITTAL_LETTER = "44CF14EE-AA5E-C420-9E2F-8D9E49400000"  # 20240212-5063's first PDF
LETTER_ORDER = "CB6128B5-6F22-C196-8B12-8EBEE3000000"  # 20240408-3032's only file, a DOCX


async def test_download_fetches_the_primary_file_and_names_it(portal, provider, tmp_path):
    route = serve_files(portal, {
        TRANSMITTAL_LETTER: (200, PDF, "application/octet-stream",
                             "20240212-5063_TransmittalLetter_AMPS_Agreement_Final.pdf"),
    })
    (listed,) = await provider.list_documents("ER24-1234-000", "Applications and Filings", 10)

    (f,) = await collect(provider, [listed], tmp_path)

    sha = hashlib.sha256(PDF).hexdigest()
    assert (f.sha256, f.size, f.filename) == (sha, len(PDF), "20240212-5063_TransmittalLetter_AMPS_Agreement_Final.pdf")
    assert Path(f.path) == tmp_path / f"{sha}.pdf" and Path(f.path).read_bytes() == PDF
    assert f.ref.external_id == "20240212-5063" and f.ref.file_ext == ".pdf"
    (call,) = route.calls
    assert json.loads(call.request.content) == {
        "FileType": "", "accession": "", "fileid": 0, "FileIDAll": "", "fileidLst": [TRANSMITTAL_LETTER],
        "Islegacy": False,
    }
    assert not list(tmp_path.glob(".*.part"))


async def test_download_from_a_cached_listing_looks_the_document_up(portal, provider, tmp_path):
    # A listing read back from our cache: this provider instance never saw the accession.
    serve_files(portal, {LETTER_ORDER: (200, DOCX, "application/octet-stream", "20240408-3032_ER24-1234-000.docx")})

    (f,) = await collect(provider, [ref("20240408-3032", file_ext=".docx")], tmp_path)

    (body,) = search_bodies(portal)
    assert body["accessionNumber"] == "20240408-3032" and body["docketSearches"] == []
    assert f.filename == "20240408-3032_ER24-1234-000.docx" and Path(f.path).suffix == ".docx"


async def test_only_the_primary_file_of_a_multi_file_document_is_fetched(portal, provider, tmp_path):
    (court_record,) = [h for h in PAGE["searchHits"] if h["acesssionNumber"] == "20240520-0032"]
    assert len(court_record["transmittals"]) == 46
    part_one = court_record["transmittals"][0]["fileId"]
    route = serve_files(portal, {part_one: (200, PDF, "application/octet-stream", "20240520-0032_Part01.pdf")})
    refs = await provider.list_documents(MATTER, "Motions and Pleadings", 3)

    (f,) = await collect(provider, [r for r in refs if r.external_id == "20240520-0032"], tmp_path)

    assert route.call_count == 1
    assert f.filename == "20240520-0032_Part01.pdf"  # not "20240520-0032_20240520-0032_Part01.pdf"


async def test_non_public_documents_are_never_downloaded(portal, provider, tmp_path):
    route = serve_files(portal, {})

    with pytest.raises(ScrapeError):
        await collect(provider, [ref("20240927-5178", access="Non-public")], tmp_path)
    with pytest.raises(ScrapeError):  # marked public by a stale cache, but eLibrary says otherwise
        await collect(provider, [ref("20240927-5178")], tmp_path)
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("accession", "file_id", "body", "content_type", "name", "error"),
    [
        ("20240212-5063", TRANSMITTAL_LETTER, b"<html>not a pdf</html>", "application/octet-stream", "x.pdf",
         ScrapeError),
        ("20240212-5063", TRANSMITTAL_LETTER, b"%PDF", "text/html; charset=utf-8", "x.pdf", ScrapeError),
        ("20240408-3032", LETTER_ORDER, b"\n<!DOCTYPE html><title>Error</title>", "application/octet-stream",
         "x.docx", ScrapeError),
        ("20240408-3032", LETTER_ORDER, b"%PDF-1.6 not a docx", "application/octet-stream", "x.docx",
         ScrapeError),
        ("20240212-5063", TRANSMITTAL_LETTER, b'{"error": "no"}', "application/json", "x.pdf", ScrapeError),
        ("20240212-5063", TRANSMITTAL_LETTER, b"", "application/octet-stream", "x.pdf", PortalUnavailable),
    ],
    ids=["pdf-bad-magic", "html-content-type", "html-error-page", "docx-bad-magic", "json-error", "empty"],
)
async def test_a_bad_file_is_rejected(portal, provider, tmp_path, accession, file_id, body, content_type, name,
                                      error):
    serve_files(portal, {file_id: (200, body, content_type, name)})

    with pytest.raises(error):
        await collect(provider, [ref(accession)], tmp_path)
    assert not [p for p in tmp_path.iterdir() if p.suffix in {".pdf", ".docx"}]


@pytest.mark.parametrize(("status", "error"), [(401, ScrapeError), (503, PortalUnavailable), (429, PortalUnavailable)])
async def test_download_http_errors(portal, provider, tmp_path, status, error):
    serve_files(portal, {TRANSMITTAL_LETTER: (status, b"", "text/plain", "")})

    with pytest.raises(error):
        await collect(provider, [ref("20240212-5063")], tmp_path)


async def test_one_failed_download_does_not_stop_the_others(portal, provider, tmp_path):
    serve_files(portal, {
        TRANSMITTAL_LETTER: (200, PDF, "application/octet-stream", "a.pdf"),
        LETTER_ORDER: (503, b"", "text/plain", ""),
    })

    files = await collect(provider, [ref("20240212-5063"), ref("20240408-3032", file_ext=".docx")], tmp_path)

    assert [f.ref.external_id for f in files] == ["20240212-5063"]


async def test_downloads_stay_within_the_concurrency_cap(portal, provider, tmp_path):
    in_flight = peak = 0

    async def slow_file(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return httpx.Response(200, content=PDF + request.content,
                              headers={"Content-Type": "application/octet-stream",
                                       "Content-Disposition": "attachment; filename=f.pdf"})

    portal.post("File/DownloadP8File").mock(side_effect=slow_file)
    refs = await provider.list_documents(MATTER, "Comments and Protests", 6)

    files = await collect(provider, [r for r in refs if r.access == "Public"], tmp_path)

    assert len(files) == 6 and peak == 2


# ---------------------------------------------------------------- docket prefixes and vocabulary (fix pass B)


@pytest.mark.parametrize("raw", ["x86-64", "FY24-25", "SB24-123", "AB24-1234", "XX24-1234-000", "EB24-0111"])
def test_only_real_docket_prefixes_are_dockets(raw):
    assert FercProvider.normalise(raw) is None
    assert ferc.MATTER_RE.fullmatch(raw.upper()) is None


def test_numbers_that_merely_look_like_dockets_are_not_mentions():
    text = "Our x86-64 servers, the FY24-25 budget and bill SB24-123; the docket is ER24-1234-000."
    assert [m.group(0) for m in ferc.MENTION_RE.finditer(text)] == ["ER24-1234-000"]


@pytest.mark.parametrize("prefix", ["CP", "EL", "ER", "RM", "RP", "QF", "EF", "PR", "UL"])
def test_common_prefixes_are_accepted(prefix):
    assert FercProvider.normalise(f"{prefix.lower()}24-12-000") == f"{prefix}24-12-000"


def test_dockets_are_ascii_only_but_full_width_input_is_normalised():
    assert FercProvider.normalise("ＥＲ２４－１２３４－０００") == "ER24-1234-000"  # NFKC first
    assert ferc.MATTER_RE.fullmatch("ER２４-1234") is None
    assert ferc.MENTION_RE.search("docket ER２４-１２３４") is None


def test_a_bare_order_is_not_an_alias():
    aliases = CATEGORIES[0].aliases
    assert "order" not in aliases
    assert {"orders", "commission orders", "commission order", "letter order"} <= set(aliases)


# ---------------------------------------------------------------- matter type and status (fix pass B)


async def test_matter_type_is_the_earliest_application_and_status_codes_are_hidden(portal, provider):
    hits = [
        _hit(acesssionNumber="20240101-5000", filedDate="01/01/2024",
             classTypes=[{"documentClass": "Comments/Protest", "documentType": "Comment"}]),
        _hit(acesssionNumber="20240301-5000", filedDate="03/01/2024",
             classTypes=[{"documentClass": "Application/Petition/Request", "documentType": "Complaint"}]),
        _hit(acesssionNumber="20240201-5000", filedDate="02/01/2024",
             classTypes=[{"documentClass": "Application/Petition/Request", "documentType": "Petition"}]),
    ]
    portal["search"].mock(side_effect=search({"RM22-14": hits}))
    portal["describe"].mock(side_effect=describe({"RM22-14": {"DataList": ["A complaint", "Closed"], "ErrorList": []}}))

    info = await provider.fetch_matter(MATTER)

    assert info.type == "Petition"  # the earliest application, not the earlier comment
    assert info.category == "Closed"  # words are kept; codes such as "DKT" are not


# ---------------------------------------------------------------- HTTP policy (fix pass B)


@pytest.mark.parametrize("status", [400, 403])
@pytest.mark.parametrize("route", ["search", "describe"])
async def test_a_refused_request_is_provider_rejected_not_retried(portal, provider, route, status):
    portal[route].respond(status)

    with pytest.raises(ProviderRejected) as info:
        await provider.fetch_matter(MATTER)
    assert not info.value.retryable


@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_is_passed_on(portal, provider, status):
    portal["search"].respond(status, headers={"Retry-After": "300"})

    with pytest.raises(PortalUnavailable) as info:
        await provider.fetch_matter(MATTER)
    assert info.value.retry_after == 300


async def test_a_download_retry_after_is_passed_on(portal, provider, tmp_path):
    portal.post("File/DownloadP8File").respond(429, headers={"Retry-After": "45"})

    with pytest.raises(PortalUnavailable) as info:
        await collect(provider, [ref("20240212-5063")], tmp_path)
    assert info.value.retry_after == 45


async def test_redirects_are_not_followed(portal, provider):
    portal["describe"].respond(302, headers={"Location": "https://www.ferc.gov/maintenance"})

    with pytest.raises(ScrapeError, match="redirect"):
        await provider.fetch_matter(MATTER)


# ---------------------------------------------------------------- size budget and file types (fix pass B)


def capped(provider: FercProvider, max_bytes: int, **kw) -> FercProvider:
    return FercProvider(provider._client, max_concurrency=2,
                        file_policy=FilePolicy.of(max_bytes, [".pdf", ".docx", ".txt"]), **kw)


async def test_a_listed_file_size_over_the_budget_is_skipped_before_downloading(portal, provider, tmp_path):
    route = serve_files(portal, {})
    small = capped(provider, max_bytes=100_000_000)  # 20240520-0032's first part is 165 MB
    refs = await small.list_documents(MATTER, "Motions and Pleadings", 3)

    with pytest.raises(TooLarge):
        await collect(small, [r for r in refs if r.external_id == "20240520-0032"], tmp_path)
    assert route.call_count == 0


async def test_a_declared_content_length_over_the_budget_stops_the_download(portal, provider, tmp_path):
    serve_files(portal, {TRANSMITTAL_LETTER: (200, PDF, "application/octet-stream", "a.pdf")})

    with pytest.raises(TooLarge):
        await collect(capped(provider, max_bytes=len(PDF) - 1), [ref("20240212-5063")], tmp_path)
    assert not list(tmp_path.iterdir())


async def test_an_undeclared_size_is_capped_while_streaming(portal, provider, tmp_path):
    async def body():
        for _ in range(10):
            yield b"%PDF-" + b"x" * 1_000

    portal.post("File/DownloadP8File").mock(side_effect=lambda request: httpx.Response(
        200, content=body(), headers={"Content-Type": "application/octet-stream"}))

    with pytest.raises(TooLarge):
        await collect(capped(provider, max_bytes=4_000), [ref("20240212-5063")], tmp_path)
    assert not list(tmp_path.iterdir())


async def test_a_download_that_drags_on_is_cut_off(portal, provider, tmp_path):
    async def body():
        yield b"%PDF-1.6\n"
        await asyncio.sleep(5)
        yield b"never"

    portal.post("File/DownloadP8File").mock(side_effect=lambda request: httpx.Response(
        200, content=body(), headers={"Content-Type": "application/octet-stream"}))
    slow = capped(provider, max_bytes=1_000_000, download_timeout_s=0.2)

    with pytest.raises(PortalUnavailable, match="not finished"):
        await asyncio.wait_for(collect(slow, [ref("20240212-5063")], tmp_path), timeout=3)


def test_the_primary_file_prefers_an_allowed_type():
    files = transmittals(("FERC GENERATED TARIFF FILING.rtf", "RTF"), ("cover.txt", "TXT"))
    assert primary_file(files).name == "FERC GENERATED TARIFF FILING.rtf"  # no allowlist given
    assert primary_file(files, {".pdf", ".txt"}).name == "cover.txt"


async def test_file_types_outside_the_allowlist_are_not_fetched(portal, provider, tmp_path):
    route = serve_files(portal, {})
    hit = _hit(acesssionNumber="20240601-5000", transmittals=[
        {"fileId": "rtf-only", "fileType": "RTF", "fileName": "tariff.rtf", "fileSize": 900}])
    portal["search"].mock(side_effect=search({"RM22-14": [hit]}))

    with pytest.raises(UnsupportedFileType):
        await collect(provider, [ref("20240601-5000")], tmp_path)
    assert route.call_count == 0


def test_title_drops_efiling_boilerplate():
    from agent.providers.ferc import trim_title

    raw = ("NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii): RS 27 - Third Amended and "
           "Restated AMPS Agreement submitted on 2/12/2024 12:02:46 PM. Filing Type code: 10")
    assert trim_title(raw) == (
        "NorthWestern Corporation submits tariff filing per 35.13(a)(2)(iii): RS 27 - Third Amended and "
        "Restated AMPS Agreement"
    )
