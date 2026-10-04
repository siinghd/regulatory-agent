"""Ontario Energy Board: Regulatory Document Search (RDS), an HPE Content Manager WebDrawer.

WebDrawer answers searches as JSON, so this provider is plain HTTP: a case's records come from a
paged search and each file from one GET. Metadata, per-category counts and listings are all
derived from the case's full record list.

Failure classification matters as much as for the UARB: a slow or failing server must surface as
PortalUnavailable or ScrapeError (retried), never as MatterNotFound (which we tell the user).
"""

import asyncio
import mimetypes
import os
import re
import secrets
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.message import Message
from typing import Any
from urllib.parse import quote

import anyio
import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from agent.models import (
    DocumentRef,
    DownloadedFile,
    MatterInfo,
    MatterNotFound,
    PortalUnavailable,
    ScrapeError,
)
from agent.providers.base import Category
from agent.providers.files import finalise_download

log = structlog.get_logger()

API_URL = "https://rds.oeb.ca/CMWebDrawer/"
PORTAL_URL = "https://www.rds.oeb.ca/"
USER_AGENT = "regulatory-agent/0.1 (+https://uarb.hsingh.app/bot)"
MATTER_RE = re.compile(r"EB-\d{4}-\d{4}")
# "EB-2024-0111", "eb 2024 0111", "EB2024-0111", and dash variants pasted from PDFs ("EB–2024–0111")
MENTION_RE = re.compile(
    r"(?<![A-Za-z0-9])EB[-‐-―\s]?(\d{4})[-‐-―\s]?(\d{4})(?!\d)", re.IGNORECASE
)
PAGE_SIZE = 700  # the server serves pages this large in one response
CANARY_MATTER = "EB-2024-0111"  # a large, settled case: always has records
MAX_RECORDS = 5_000
# Never add RecordContainer: it is access-denied for the public and fails the whole search.
_PROPERTIES = (
    "RecordTitle,RecordNumber,RecordDateRegistered,RecordDocumentSize,RecordMimeType,CaseNumber,"
    "SIDocumentType,Applicant,EnergyType,PrimaryApplicationType,DateIssued,fDateReceived"
)


# ---------------------------------------------------------------- categories


def _any_of(*document_types: str) -> Callable[[str], bool]:
    return frozenset(t.casefold() for t in document_types).__contains__


def _is_decision(document_type: str) -> bool:
    return "decision" in document_type or document_type.endswith("rate order")


# Categories with the SIDocumentType values each one covers, in presentation order. A record can
# carry several types ("Decision; Procedural Order"); it goes to the first category claiming one.
_CLAIMS: tuple[tuple[Category, Callable[[str], bool]], ...] = (
    (
        Category(
            "Decisions and Orders",
            aliases=(
                "decisions and orders", "decision and order", "decisions", "decision", "board decisions",
                "board decision", "rulings", "ruling", "orders", "rate orders", "rate order",
            ),
            description="The Board's decisions, decisions and orders (incl. on cost awards) and rate orders",
        ),
        _is_decision,
    ),
    (
        Category(
            "Procedural Orders",
            aliases=(
                "procedural orders", "procedural order", "notices", "notices of hearing", "notice of hearing",
                "letters of direction", "letter of direction",
            ),
            description="Procedural orders, notices, letters of direction and acknowledgement letters",
        ),
        _any_of("Procedural Order", "Notice", "Letter of Direction", "Acknowledgement Letter"),
    ),
    (
        Category(
            "Application and Evidence",
            aliases=(
                "application and evidence", "applications", "application", "evidence", "pre-filed evidence",
                "prefiled evidence", "intervenor evidence",
            ),
            description="The application and pre-filed evidence, exhibits, and intervenor evidence",
        ),
        _any_of("Application and Evidence", "Intervenor Evidence", "Exhibits", "Exhibit List"),
    ),
    (
        Category(
            "Interrogatories",
            aliases=(
                "interrogatories", "interrogatory", "interrogatory responses", "interrogatory response",
                "ir responses", "ir response", "irs",
            ),
            description="Interrogatories (IRs) to the applicant or intervenors, and their responses",
        ),
        _any_of(
            "Interrogatories to Applicant", "Interrogatories to Intervenor",
            "Interrogatory Response from Applicant", "Interrogatory Response from Intervenor",
        ),
    ),
    (
        Category(
            "Undertakings",
            aliases=("undertakings", "undertaking", "undertaking responses", "undertaking response"),
            description="Undertaking responses and lists, declarations and undertakings",
        ),
        _any_of("Undertaking Responses", "Undertaking List", "Declaration and Undertakings"),
    ),
    (
        Category(
            "Submissions and Arguments",
            aliases=(
                "submissions and arguments", "submissions", "submission", "arguments", "argument",
                "argument in chief", "reply argument", "settlement proposals", "settlement proposal",
                "letters of comment", "letter of comment", "motions",
            ),
            description=(
                "Submissions, argument in chief and reply argument, comments and letters of comment, "
                "settlement proposals, motions, post-hearing filings"
            ),
        ),
        _any_of(
            "Submission", "Applicant Argument in Chief", "Applicant Reply Argument", "Comments",
            "Letter of Comment", "Settlement Proposal", "Motion", "Post Hearing Filings",
        ),
    ),
    (
        Category(
            "Transcripts",
            aliases=("transcripts", "transcript", "hearing transcripts", "hearing transcript"),
            description="Hearing transcripts and cross-examination material",
        ),
        _any_of("Transcripts", "Cross Examination Material"),
    ),
    (
        Category(
            "Cost Claims",
            aliases=("cost claims", "cost claim", "costs"),
            description="Intervenor cost claims, objections to them and replies",
        ),
        _any_of("Cost Claim", "Cost Claim Objection", "Cost Claim Objection Reply"),
    ),
)
# Everything else: Correspondence, Intervenor Request Letter, Intervenor List, Affidavits of
# Service, and any type the portal adds later.
_CORRESPONDENCE = Category(
    "Correspondence",
    aliases=("correspondence", "letters", "letter"),
    description="Letters and other correspondence, intervenor requests and lists, affidavits of service",
)
CATEGORIES = (*(category for category, _ in _CLAIMS), _CORRESPONDENCE)
_DECISIONS = CATEGORIES[0].name


def categorise(document_type: str | None) -> str:
    """Category name for a record's SIDocumentType (which may list several, "A; B")."""
    parts = [p.strip().casefold() for p in (document_type or "").split(";") if p.strip()]
    return next((c.name for c, claims in _CLAIMS if any(map(claims, parts))), _CORRESPONDENCE.name)


# ---------------------------------------------------------------- records


class _Page(BaseModel):
    results: list[dict[str, Any]] = Field(alias="Results")
    total: int = Field(alias="TotalResults")
    has_more: bool = Field(alias="HasMoreItems")
    status: dict[str, Any] = Field(default_factory=dict, alias="ResponseStatus")


@dataclass(frozen=True)
class _Record:
    external_id: str  # RecordNumber ("D25-18072"), else the numeric Uri
    title: str
    category: str
    filed_on: date | None
    mime: str | None
    applicant: str | None
    energy_type: str | None
    application_type: str | None


def _field(record: dict[str, Any], name: str) -> dict[str, Any]:
    """WebDrawer puts built-in properties at the top level and custom fields under "Fields"."""
    fields = record.get("Fields")
    value = record.get(name, fields.get(name) if isinstance(fields, dict) else None)
    return value if isinstance(value, dict) else {}


def _text(record: dict[str, Any], name: str) -> str | None:
    value = _field(record, name).get("Value")
    return (str(value).strip() or None) if value is not None else None


def _date(record: dict[str, Any], name: str) -> date | None:
    value = _field(record, name)
    if value.get("IsClear", True) or not value.get("DateTime"):  # unset dates come back as 0001-01-01
        return None
    try:
        return date.fromisoformat(str(value["DateTime"])[:10])  # UTC calendar date
    except ValueError as e:
        raise ScrapeError(f"unreadable {name}: {value['DateTime']!r}") from e


def _applicant(record: dict[str, Any]) -> str | None:
    location = _field(record, "Applicant").get("LocationFormattedName")
    name = location.get("Value") if isinstance(location, dict) else None
    # Locations read "Enbridge Gas Inc. - Gas Distributor": the part after the last " - " is a role.
    return (str(name).rsplit(" - ", 1)[0].strip() or None) if name else None


def _parse_record(raw: dict[str, Any]) -> _Record:
    uri = raw.get("Uri")
    external_id = _text(raw, "RecordNumber") or (str(uri) if isinstance(uri, int) else None)
    if external_id is None:
        raise ScrapeError(f"record without a number or Uri: {str(raw)[:200]}")
    return _Record(
        external_id=external_id,
        title=_text(raw, "RecordTitle") or external_id,
        category=categorise(_text(raw, "SIDocumentType")),
        filed_on=(
            _date(raw, "DateIssued") or _date(raw, "fDateReceived") or _date(raw, "RecordDateRegistered")
        ),
        mime=_text(raw, "RecordMimeType"),
        applicant=_applicant(raw),
        energy_type=_text(raw, "EnergyType"),
        application_type=_text(raw, "PrimaryApplicationType"),
    )


def _newest_first(records: Iterable[_Record]) -> list[_Record]:
    # Stable sort over the server's newest-registered-first order: same-day records keep it.
    return sorted(records, key=lambda r: r.filed_on or date.min, reverse=True)


def _most_common(values: Iterable[str | None]) -> str | None:
    top = Counter(v for v in values if v).most_common(1)
    return top[0][0] if top else None


def _title(records: list[_Record]) -> str:
    """E.g. "Enbridge Gas Inc. – Gas rates application": RDS has no case title to show."""
    applicant = _most_common(r.applicant for r in records)
    energy = _most_common(r.energy_type for r in records)
    kind = _most_common(r.application_type for r in records)
    application = " ".join(x for x in (energy, kind.lower() if kind else None, "application") if x)
    application = application[0].upper() + application[1:]
    return f"{applicant} – {application}" if applicant else application


def case_url(matter: str) -> str:
    """The case's document list on the public RDS website."""
    return f"{PORTAL_URL}CMWebDrawer/Record?q=CaseNumber={matter}&sortBy=recRegisteredOn-&pageSize=400"


def _raise_for_status(r: httpx.Response, what: str) -> None:
    if r.status_code >= 500 or r.status_code == 429:
        raise PortalUnavailable(f"{what}: HTTP {r.status_code}")
    if r.status_code != 200:
        raise ScrapeError(f"{what}: HTTP {r.status_code}")


def _served_filename(headers: httpx.Headers) -> str:
    msg = Message()
    msg["content-disposition"] = headers.get("content-disposition", "")
    return msg.get_filename() or ""


def make_client(proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=API_URL,
        proxy=proxy,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(60, connect=10),
        follow_redirects=True,
    )


# ---------------------------------------------------------------- provider


class OebProvider:
    name = "oeb"
    display_name = "Ontario Energy Board"
    portal_url = PORTAL_URL
    matter_pattern = MATTER_RE
    mention_pattern = MENTION_RE
    matter_example = "EB-2024-0111"
    categories = CATEGORIES

    @staticmethod
    def normalise(raw: str) -> str | None:
        m = MENTION_RE.fullmatch(raw)
        return f"EB-{m.group(1)}-{m.group(2)}" if m else None

    def __init__(self, client: httpx.AsyncClient, *, max_concurrency: int = 4):
        self._client = client
        # Politeness cap per worker, shared by searches and downloads.
        self._sem = asyncio.Semaphore(max_concurrency)
        self._search_verified_at = 0.0

    # ------------------------------------------------------------------ metadata and listings

    async def fetch_matter(self, matter: str) -> MatterInfo:
        return self._matter_info(matter, await self._records(matter))

    async def list_documents(self, matter: str, doc_type: str, limit: int) -> list[DocumentRef]:
        _, refs = await self.list_matter_and_documents(matter, doc_type, limit)
        return refs

    async def list_matter_and_documents(
        self, matter: str, doc_type: str, limit: int
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        records = await self._records(matter)
        wanted = [r for r in _newest_first(records) if r.category == doc_type][: max(limit, 0)]
        refs = [
            DocumentRef(
                provider=self.name,
                matter=matter,
                doc_type=doc_type,
                external_id=r.external_id,
                title=r.title,
                filed_on=r.filed_on,
                file_ext=mimetypes.guess_extension(r.mime or "", strict=False) or "",
                row_index=i,
            )
            for i, r in enumerate(wanted)
        ]
        return self._matter_info(matter, records), refs

    def _matter_info(self, matter: str, records: list[_Record]) -> MatterInfo:
        counts = Counter(r.category for r in records)
        dated = [r.filed_on for r in records if r.filed_on]
        decided = [r.filed_on for r in records if r.filed_on and r.category == _DECISIONS]
        return MatterInfo(
            provider=self.name,
            matter=matter,
            title=_title(records),
            type=_most_common(r.energy_type for r in records),
            category=_most_common(r.application_type for r in records),
            date_received=min(dated, default=None),
            decision_date=max(decided, default=None),
            counts={c.name: counts[c.name] for c in CATEGORIES},
            portal_url=case_url(matter),
            fetched_at=datetime.now(UTC),
        )

    async def _records(self, matter: str) -> list[_Record]:
        """Every record of the case (up to MAX_RECORDS), newest registered first."""
        page = await self._search(matter, start=1)
        if page.total == 0:
            await self._ensure_search_works()
            raise MatterNotFound(matter)
        raw = list(page.results)
        while page.has_more and page.results and len(raw) < MAX_RECORDS:
            page = await self._search(matter, start=len(raw) + 1)
            raw += page.results
        if page.has_more and len(raw) >= MAX_RECORDS:
            log.warning("oeb.records_capped", matter=matter, total=page.total, kept=len(raw))
        if not raw:
            raise ScrapeError(f"{matter}: {page.total} records reported but none returned")
        return [_parse_record(r) for r in raw]

    async def _ensure_search_works(self) -> None:
        """Zero results is also what WebDrawer returns for a query it doesn't understand (e.g. if
        the CaseNumber field were renamed). Before telling anyone their case doesn't exist, check
        that a known case still returns records. Cached briefly so not-founds stay cheap."""
        if time.monotonic() - self._search_verified_at < 900:
            return
        if (await self._search(CANARY_MATTER, start=1)).total == 0:
            raise ScrapeError(f"search returned nothing for canary {CANARY_MATTER}: query format changed?")
        self._search_verified_at = time.monotonic()

    async def _search(self, matter: str, *, start: int) -> _Page:
        what = f"OEB search for {matter}"
        params: dict[str, str | int] = {
            "q": f"CaseNumber={matter}",
            "sortBy": "recRegisteredOn-",
            "format": "json",
            "pageSize": min(PAGE_SIZE, MAX_RECORDS - start + 1),
            "start": start,  # 1-based record offset
            "properties": _PROPERTIES,
        }
        async with self._sem:
            try:
                r = await self._client.get("Record", params=params)
            except httpx.RequestError as e:
                raise PortalUnavailable(f"{what}: {e!r}") from e
        _raise_for_status(r, what)
        try:
            page = _Page.model_validate_json(r.content)
        except ValidationError as e:
            raise ScrapeError(f"{what}: unexpected response: {str(e)[:300]}") from e
        if page.status.get("ErrorCode"):
            raise ScrapeError(f"{what}: {page.status.get('Message') or page.status['ErrorCode']}")
        return page

    # ------------------------------------------------------------------ downloads

    async def download(
        self, matter: str, refs: list[DocumentRef], dest_dir: str
    ) -> AsyncIterator[DownloadedFile]:
        """Yield files as they land. One failed file is logged and skipped; all failing raises."""
        tasks = [asyncio.create_task(self._download_one(ref, dest_dir)) for ref in refs]
        errors: list[PortalUnavailable | ScrapeError] = []
        try:
            for finished in asyncio.as_completed(tasks):
                try:
                    file = await finished
                except (PortalUnavailable, ScrapeError) as e:
                    log.warning("oeb.download_failed", matter=matter, error=str(e)[:200])
                    errors.append(e)
                    continue
                yield file
        finally:
            for t in tasks:
                t.cancel()
        if errors and len(errors) == len(refs):
            raise errors[0]

    async def _download_one(self, ref: DocumentRef, dest_dir: str) -> DownloadedFile:
        what = f"OEB download of {ref.external_id}"
        tmp = os.path.join(dest_dir, f".{secrets.token_hex(8)}.part")
        async with self._sem:
            try:
                # RDS resolves a record by its number as well as by its Uri, so the stored id is
                # enough to fetch the file again later (e.g. from a cached listing).
                url = f"Record/{quote(ref.external_id, safe='')}/File/document"
                async with self._client.stream("GET", url) as r:
                    _raise_for_status(r, what)
                    # An unknown record is a 200 with an HTML error page, not a 404.
                    if r.headers.get("content-type", "").startswith("text/html"):
                        raise ScrapeError(f"{what}: got an HTML page instead of the file")
                    async with await anyio.open_file(tmp, "wb") as f:
                        async for chunk in r.aiter_bytes():
                            await f.write(chunk)
            except httpx.RequestError as e:
                raise PortalUnavailable(f"{what}: {e!r}") from e
        return finalise_download(tmp, ref, _served_filename(r.headers), dest_dir)
