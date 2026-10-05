"""US Federal Energy Regulatory Commission: eLibrary, through the JSON API behind its web app.

eLibrary's front end is a single-page app over a public JSON API (eLibraryWebAPI), so this provider
is plain HTTP like the OEB's: a docket's description comes from one GET, its documents from a paged
search, each file from one POST. Metadata, per-category counts and listings are all derived from
the docket's full hit list.

A matter is a docket, either whole ("RM22-14": every sub-docket) or one sub-docket
("ER24-1234-000").

One eLibrary document (an accession number such as 20240212-5063) can carry several files: a
tariff filing's transmittal letter plus clean and marked tariff sheets, a court record split into
46 parts of ~165 MB each. Each accession becomes one DocumentRef and only its primary file is
fetched: the first PDF, else the first DOCX, else the first file. The first PDF is the document
itself or its cover letter, the rest are attachments that would blow the size of a reply; the
README links the docket sheet, where the other files are one click away.

FERC issues its own orders and notices as DOCX, and older filings come as TXT or DOC. All are
delivered; the summary reads PDFs and DOCX (agent.citations.extract), so TXT and DOC are not cited.

Non-public documents (availCode other than "P": privileged, CEII) are listed with access
"Non-public", so the reply can say they were left out, and are never downloaded.

Files over max_file_bytes (by the transmittal's fileSize, the Content-Length, or what actually
arrives) and file types outside allowed_file_exts are skipped per file, like any failed download.

Failure classification matters as much as for the other portals: a slow or failing server must
surface as PortalUnavailable or ScrapeError (retried), never as MatterNotFound (which we tell the
user).
"""

import asyncio
import os
import re
import secrets
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from agent.models import (
    AgentError,
    DocumentRef,
    DownloadedFile,
    MatterInfo,
    MatterNotFound,
    PortalUnavailable,
    ScrapeError,
)
from agent.providers import http
from agent.providers.base import Category, nfkc
from agent.providers.files import FilePolicy, error_to_raise, finalise_download
from agent.providers.http import raise_for_status, served_filename

log = structlog.get_logger()

API_URL = "https://elibrary.ferc.gov/eLibraryWebAPI/api/"
WEB_URL = "https://elibrary.ferc.gov/eLibrary/"
PORTAL_URL = f"{WEB_URL}search"
USER_AGENT = http.USER_AGENT
# FERC's docket prefixes (the industry/program code before the fiscal year). Only these form a
# docket, so "x86-64", "FY24-25" or "SB24-123" in an email are never read as one. CD, EF, EY, FC,
# GP, PR and UL were confirmed live on 2026-10-04 besides the common ones; hydro projects
# ("P-2114") have no fiscal year and aren't supported.
DOCKET_PREFIXES = (
    "AC", "AD", "AI", "CD", "CP", "DI", "EC", "EF", "EG", "EL", "EM", "EO", "EP", "ER", "ES", "EV", "EX",
    "EY", "FA", "FC", "GP", "HB", "IC", "IN", "IS", "LA", "NJ", "OA", "OR", "PA", "PF", "PL", "PR", "QF",
    "RC", "RD", "RM", "RP", "RR", "RT", "SA", "SC", "TO", "TS", "TX", "UL",
)
_PREFIX = "(?:" + "|".join(DOCKET_PREFIXES) + ")"
# A docket ("ER24-1234") with an optional sub-docket ("-000"). re.ASCII: 0-9 digits only.
MATTER_RE = re.compile(rf"({_PREFIX}\d{{2}}-\d{{1,6}})(?:-(\d{{3}}))?", re.ASCII)
# "ER24-1234-000", "rm22-14", and dash variants pasted from PDFs ("ER24–1234–000"); not inside
# longer tokens ("ABCD24-1234", "ER24-1234-0001").
MENTION_RE = re.compile(
    rf"(?<![A-Za-z0-9])({_PREFIX}\d{{2}})[-‐-―](\d{{1,6}})(?:[-‐-―](\d{{3}}))?"
    r"(?![A-Za-z0-9]|[-‐-―]\d)",
    re.IGNORECASE | re.ASCII,
)
# Words right after a whole docket that scope it to one sub-docket: "ER24-1234 (the -000 sub-docket
# only)", "ER24-1234, sub-docket 001", "ER24-1234 (-000 only)". Not a list of sub-dockets ("sub-
# dockets 000 and 001"): that is the whole docket.
_SUB_DOCKET_SCOPE = re.compile(
    r"\s*[(,:;]?\s*(?:the\s+|only\s+)?"
    r"(?:sub-?docket\s*(?:no\.?|number|#)?\s*[-‐-―]?(\d{3})|[-‐-―](\d{3})\s+(?:sub-?docket|only)\b)"
    r"(?![\d-]|\s*(?:,|&|and|or|to|through)\s*[-‐-―]?\d{3})",
    re.IGNORECASE | re.ASCII,
)
PAGE_SIZE = 100
MAX_HITS = 5_000
CANARY_MATTER = "ER24-1234-000"  # a settled tariff filing: always has documents
NOT_FOUND_TEXT = "Applicant not Found."  # the description eLibrary gives an unknown docket
NON_PUBLIC = "Non-public"
TITLE_MAX = 160
_FILES_REMEMBERED = 20_000


# ---------------------------------------------------------------- categories

_Claim = Callable[[str, str], bool]  # (documentClass, documentType), both casefolded


def _classes(*names: str) -> _Claim:
    wanted = frozenset(n.casefold() for n in names)
    return lambda document_class, _document_type: document_class in wanted


_APPLICATION_CLASS = "application/petition/request"
_FILING_CLASSES = _classes(
    "Application/Petition/Request", "Agreement/Understanding/Contract", "Report/Form", "Other Submittal"
)


def _is_filing(document_class: str, document_type: str) -> bool:
    return _FILING_CLASSES(document_class, document_type) or "tariff filing" in document_type


# Categories with the eLibrary document classes each one covers, in presentation order. A document
# can carry several classes ("Notice" + "Order/Opinion"); it goes to the first category claiming one.
_CLAIMS: tuple[tuple[Category, _Claim], ...] = (
    (
        Category(
            "Orders and Decisions",
            # No bare "order": "in order to", "the order of the documents" are not requests for orders.
            aliases=(
                "orders and decisions", "orders", "commission orders", "commission order", "letter orders",
                "letter order", "decisions", "decision", "rulings", "ruling",
            ),
            description="Commission orders and opinions, delegated letter orders, ALJ initial decisions and "
            "other ALJ issuances",
        ),
        _classes("Order/Opinion", "ALJ Issuance"),
    ),
    (
        Category(
            "Notices",
            aliases=("notices", "notice", "nopr", "noprs"),
            description="Commission notices: of filings, comment dates, hearings, proposed rulemakings (NOPRs)",
        ),
        _classes("Notice"),
    ),
    (
        Category(
            "Applications and Filings",
            aliases=(
                "applications and filings", "applications", "application", "filings", "tariff filings",
                "tariff filing", "petitions", "petition",
            ),
            description="Applications, petitions and requests, tariff filings, agreements and settlements, "
            "reports and forms, other submittals",
        ),
        _is_filing,
    ),
    (
        Category(
            "Comments and Protests",
            aliases=("comments and protests", "comments", "comment", "protests", "protest"),
            description="Comments on filings and rulemakings, and protests",
        ),
        _classes("Comments/Protest", "FERC Comment"),
    ),
    (
        Category(
            "Motions and Pleadings",
            aliases=(
                "motions and pleadings", "motions", "motion", "pleadings", "pleading", "rehearing requests",
                "rehearing request", "requests for rehearing", "request for rehearing", "briefs",
            ),
            description="Motions, answers, requests for rehearing, petitions for review and other pleadings, "
            "briefs, and court documents (e.g. the record of an appeal)",
        ),
        _classes("Pleading/Motion", "Briefing/Arguments of Law", "Court Related Documents"),
    ),
    (
        Category(
            "Interventions",
            aliases=("interventions", "intervention", "motions to intervene", "motion to intervene"),
            description="Motions and notices of intervention, late interventions and their withdrawals",
        ),
        _classes("Intervention"),
    ),
    (
        Category(
            "Evidence and Testimony",
            aliases=(
                "evidence and testimony", "evidence", "testimony", "exhibits", "exhibit", "transcripts",
                "transcript", "data requests", "data request", "data responses",
            ),
            description="Testimony, exhibits, hearing and meeting transcripts, data requests and responses",
        ),
        _classes("Testimony", "Exhibit", "Transcript", "Interrogatory/Data Request", "Deposition Document"),
    ),
)
# Everything else: correspondence with applicants, agencies and tribes, informational issuances
# and news releases, FERC memos, status reports, FERC staff reports and studies, and any class
# eLibrary adds later. Staff reports and studies (environmental assessments and the like) are
# FERC issuances but not decisions: in Orders and Decisions they would set a docket's decision
# date and bury the orders people ask for.
_CORRESPONDENCE = Category(
    "Correspondence",
    aliases=("correspondence", "letters", "letter", "memos", "status reports", "staff reports", "staff report"),
    description="Correspondence with FERC staff, informational issuances and news releases, memos, "
    "status reports, FERC staff reports and studies, and anything else",
)
CATEGORIES = (*(category for category, _ in _CLAIMS), _CORRESPONDENCE)
_ORDERS = CATEGORIES[0].name
_CORRESPONDENCE_CLASSES = frozenset({"ferc memo", "status report", "ferc report/study"})
# Catch-all classes: a document that also carries a more specific class goes by that one (a
# rulemaking comment that is also an "Other Submittal" is a comment).
_GENERIC_CLASSES = frozenset({"other submittal"})


def _place(document_class: str, document_type: str) -> int | None:
    """Index in CATEGORIES of the category a (class, type) pair names, None if it names none."""
    for i, (_, claims) in enumerate(_CLAIMS):
        if claims(document_class, document_type):
            return i
    if "correspondence" in document_class or document_class in _CORRESPONDENCE_CLASSES:
        return len(_CLAIMS)
    return None


def _deciding_pair(class_types: Iterable[tuple[str | None, str | None]]) -> tuple[int, str, str] | None:
    """(category index, documentClass, documentType) of the pair naming the most specific
    category (generic classes last), then the first in presentation order; None if none does."""
    best: tuple[tuple[bool, int], str, str] | None = None
    for c, t in class_types:
        cls, typ = (c or "").strip(), (t or "").strip()
        i = _place(cls.casefold(), typ.casefold())
        if i is None:
            continue
        key = (cls.casefold() in _GENERIC_CLASSES, i)
        if best is None or key < best[0]:
            best = (key, cls, typ)
    return (best[0][1], best[1], best[2]) if best else None


def categorise(class_types: Iterable[tuple[str | None, str | None]]) -> str:
    """Category name for a document's (documentClass, documentType) pairs: the pair naming the
    most specific category (generic classes last), then the first in presentation order."""
    decided = _deciding_pair(class_types)
    return CATEGORIES[decided[0]].name if decided else _CORRESPONDENCE.name


def source_type(class_types: Iterable[tuple[str | None, str | None]]) -> str | None:
    """The document's own type as eLibrary classes it ("Order/Opinion: Order on Rehearing"): the
    pair that decided its category, else the first pair given."""
    pairs = list(class_types)
    decided = _deciding_pair(pairs)
    if decided:
        _, cls, typ = decided
    elif pairs:
        cls, typ = ((x or "").strip() for x in pairs[0])
    else:
        return None
    return f"{cls}: {typ}" if cls and typ else (cls or typ or None)


def _is_named(document_class: str) -> bool:
    """Whether a class is placed by name (not just by falling through to Correspondence)."""
    return _place(document_class.strip().casefold(), "") is not None


def _meaningful_status(status: str | None) -> str | None:
    """eLibrary's docket "status" is mostly an internal code ("DKT"): not worth showing."""
    if status is None or re.fullmatch(r"[A-Z]{2,5}", status):
        return None
    return status


# ---------------------------------------------------------------- API responses


class _ClassType(BaseModel):
    document_class: str | None = Field(None, alias="documentClass")
    document_type: str | None = Field(None, alias="documentType")


class _Transmittal(BaseModel):
    file_id: str = Field(alias="fileId")
    file_name: str | None = Field(None, alias="fileName")
    file_type: str | None = Field(None, alias="fileType")
    file_size: int | None = Field(None, alias="fileSize")  # bytes


class _Hit(BaseModel):
    accession: str = Field(alias="acesssionNumber")  # sic: the API's spelling
    description: str | None = None
    category: str | None = None  # "Submittal" (filed with FERC) | "Issuance" (issued by FERC)
    filed_date: str | None = Field(None, alias="filedDate")  # MM/DD/YYYY
    issued_date: str | None = Field(None, alias="issuedDate")
    class_types: list[_ClassType] = Field(default_factory=list, alias="classTypes")
    avail_code: str | None = Field(None, alias="availCode")  # "P" = public
    transmittals: list[_Transmittal] = Field(default_factory=list)

    @field_validator("class_types", "transmittals", mode="before")
    @classmethod
    def none_as_empty(cls, value: Any) -> Any:
        return [] if value is None else value


class _Page(BaseModel):
    hits: list[_Hit] | None = Field(alias="searchHits")  # null when the search failed
    total: int = Field(alias="totalHits")
    success: bool = True
    error: str | None = Field(None, alias="errorMessage")


class _Description(BaseModel):
    data: list[str | None] = Field(alias="DataList")  # [description, status]


@dataclass(frozen=True)
class _Docket:
    description: str | None  # None: eLibrary doesn't know the docket
    status: str | None


@dataclass(frozen=True)
class _File:
    file_id: str
    name: str  # as uploaded, e.g. "TransmittalLetter_AMPS_Agreement_Final.pdf"
    ext: str  # ".pdf"
    size: int | None = None  # bytes, as eLibrary lists it


@dataclass(frozen=True)
class _Doc:
    accession: str
    title: str
    category: str
    filed_on: date | None
    application_type: str | None  # documentType of an Application/Petition/Request class
    public: bool
    file: _File
    source_type: str | None = None  # "documentClass: documentType" that decided the category


def primary_file(transmittals: list[_Transmittal], allowed_exts: Iterable[str] = ()) -> _File | None:
    """The one file we fetch for a document: the first PDF, else the first DOCX, else the first
    of an allowed type (when `allowed_exts` is given), else the first."""

    def ext(t: _Transmittal) -> str:
        kind = (t.file_type or "").strip().lower()
        return f".{kind}" if kind else os.path.splitext(t.file_name or "")[1].lower()

    allowed = frozenset(allowed_exts)
    chosen = (
        next((t for t in transmittals if ext(t) == ".pdf"), None)
        or next((t for t in transmittals if ext(t) == ".docx"), None)
        or next((t for t in transmittals if ext(t) in allowed), None)
        or next(iter(transmittals), None)
    )
    if chosen is None:
        return None
    return _File(chosen.file_id, (chosen.file_name or "").strip(), ext(chosen), chosen.file_size)


def _date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    try:
        month, day, year = value.split()[0].split("/")  # "04/08/2024", maybe followed by a time
        return date(int(year), int(month), int(day))
    except ValueError as e:
        raise ScrapeError(f"unreadable eLibrary date {value!r}") from e


def _parse_hit(hit: _Hit, allowed_exts: Iterable[str] = ()) -> _Doc | None:
    file = primary_file(hit.transmittals, allowed_exts)
    if file is None:
        return None  # nothing to send (very old paper records)
    pairs = [(c.document_class, c.document_type) for c in hit.class_types]
    return _Doc(
        accession=hit.accession,
        title=" ".join((hit.description or "").split()) or hit.accession,
        category=categorise(pairs),
        # The filed date is when the document joined the record (what the docket sheet sorts by);
        # a submittal's issued date is the date written on it.
        filed_on=_date(hit.filed_date) or _date(hit.issued_date),
        application_type=next(
            (t.strip() for c, t in pairs
             if (c or "").strip().casefold() == _APPLICATION_CLASS and t and t.strip()),
            None,
        ),
        public=hit.avail_code == "P",
        file=file,
        source_type=source_type(pairs),
    )


def _newest_first(docs: Iterable[_Doc]) -> list[_Doc]:
    # Same-day documents: the higher accession number was entered later.
    return sorted(docs, key=lambda d: (d.filed_on or date.min, d.accession), reverse=True)


def _opening_application(docs: list[_Doc]) -> _Doc | None:
    """The docket's opening filing: its earliest application, petition or request (not FERC's
    notice of it, nor a comment that happens to come first in a rulemaking)."""
    pool = [d for d in docs if d.application_type]
    return min(pool, key=lambda d: (d.filed_on or date.max, d.accession), default=None)


_FILING_BOILERPLATE = re.compile(
    r"\s*(?:submitted|filed)\s+on\s+\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?\s*[AP]M)?.*$"
    r"|\s*Filing Type code:\s*\d+.*$",
    re.IGNORECASE,
)


def trim_title(text: str, limit: int = TITLE_MAX) -> str:
    """Collapse whitespace and cut at a word boundary, so a long docket description reads as a title.

    eLibrary descriptions end in e-filing boilerplate ("... submitted on 2/12/2024 12:02:46 PM",
    "Filing Type code: 10"); that is metadata, not part of the matter's name.
    """
    text = " ".join(text.split())
    text = _FILING_BOILERPLATE.sub("", text).rstrip(" ,;:-.")
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    cut = head.rsplit(" ", 1)[0] if " " in head else text[:limit]
    return cut.rstrip(" ,;:-") + "…"


# What follows the subject in a document description: " under RM22-14. Commissioner Danly ...".
_UNDER_DOCKET = re.compile(rf"\s+under\s+{_PREFIX}\d{{2}}-\d+.*$", re.IGNORECASE | re.DOTALL)
_FIRST_SENTENCE_END = re.compile(r"(?<=[a-z0-9)])\.\s+(?=[A-Z])")
_RE = re.compile(r"\sre\s", re.IGNORECASE)  # "Notice of Proposed Rulemaking re Improvements to ..."


def _generic(description: str) -> bool:
    """A docket description that names a kind of document, not the matter ("NOPR", "Formal
    Complaint"): eLibrary describes rulemakings and complaints this way."""
    return len(re.findall(r"[A-Za-z]+", description)) < 3


def _subject_clause(description: str) -> str | None:
    """The first meaningful clause of a document description: its subject, without the docket
    reference and what follows it ("Improvements to Generator Interconnection Procedures and
    Agreements" for "Notice of Proposed Rulemaking re Improvements to ... under RM22-14. ...")."""
    text = re.sub(r"^\(doc-less\)\s*", "", " ".join(description.split()), flags=re.IGNORECASE)
    text = _UNDER_DOCKET.sub("", text)
    text = _FIRST_SENTENCE_END.split(text, maxsplit=1)[0]
    parts = _RE.split(text, maxsplit=1)
    clause = parts[-1].strip(" .,;:-")
    return clause if not _generic(clause) else None


def _docket_title(matter: str, description: str | None, docs: list["_Doc"]) -> str:
    """The docket's description, or (when that is generic or missing) the first meaningful clause
    of its opening document's description, else "FERC docket RM22-14: NOPR"."""
    text = " ".join((description or "").split())
    if text and not _generic(text):
        return trim_title(text)
    opening = _opening_application(docs) or min(
        docs, key=lambda d: (d.filed_on or date.max, d.accession), default=None
    )
    clause = _subject_clause(opening.title) if opening else None
    if clause:
        return trim_title(clause)
    return f"FERC docket {matter}: {text}" if text else f"FERC docket {matter}"


def split_matter(matter: str) -> tuple[str, str | None]:
    """("ER24-1234", "000") for "ER24-1234-000"; ("RM22-14", None) for a whole docket."""
    m = MATTER_RE.fullmatch(matter)
    if m is None:
        raise MatterNotFound(matter)
    return m.group(1), m.group(2)


def docket_url(matter: str) -> str:
    """The docket sheet on the public eLibrary website (it takes a sub-docket suffix too)."""
    return f"{WEB_URL}docketsheet?docket_number={matter}"


def _search_body(*, docket: str | None = None, sub: str | None = None, accession: str | None = None,
                 page: int = 1) -> dict[str, Any]:
    return {
        "searchText": "*",
        "searchFullText": True,
        "searchDescription": True,
        # Never empty: without a date range the API silently returns no hits.
        "dateSearches": [{"dateType": "filed_date", "startDate": "1960-01-01", "endDate": "2099-12-31"}],
        "availability": None,
        "affiliations": [],
        "categories": [],
        "libraries": [],
        "accessionNumber": accession,
        "eFiling": False,
        "docketSearches": [{"docketNumber": docket, "subDocketNumbers": [sub] if sub else []}] if docket else [],
        "resultsPerPage": PAGE_SIZE,
        "curPage": page,  # 1-based
        "classTypes": [],
        "sortBy": "",
        "groupBy": "NONE",
        "idolResultID": "",
        "allDates": True,
    }


def _display_name(accession: str, original: str, ext: str) -> str:
    """"20240212-5063_TransmittalLetter_AMPS_Agreement_Final.pdf": the accession, then the name as filed."""
    stem = os.path.splitext(original)[0].removeprefix(accession)  # "20240520-0032_Part01.pdf"
    stem = re.sub(r"[^\w.\- ]+", "_", stem)
    stem = " ".join(stem.split()).strip(" ._")
    return f"{accession}_{stem}{ext}" if stem else f"{accession}{ext}"


def make_client(proxy: str | None = None) -> httpx.AsyncClient:
    """Redirects are not followed (see agent.providers.http)."""
    return http.make_client(API_URL, proxy)


# ---------------------------------------------------------------- provider


class FercProvider:
    name = "ferc"
    display_name = "Federal Energy Regulatory Commission (US)"
    portal_url = PORTAL_URL
    matter_pattern = MATTER_RE
    mention_pattern = MENTION_RE
    matter_example = "ER24-1234-000"
    categories = CATEGORIES

    @staticmethod
    def normalise(raw: str) -> str | None:
        m = MENTION_RE.fullmatch(nfkc(raw))
        if m is None:
            return None
        docket = f"{m.group(1).upper()}-{m.group(2)}"
        return f"{docket}-{m.group(3)}" if m.group(3) else docket

    @staticmethod
    def narrow(matter: str, following: str) -> str:
        """`matter` as the words right after its mention scope it: "ER24-1234 (the -000 sub-docket
        only)" asks for ER24-1234-000. A docket already written with its sub-docket stays as is."""
        m = MATTER_RE.fullmatch(matter)
        scope = _SUB_DOCKET_SCOPE.match(following)
        if m is None or m.group(2) or scope is None:
            return matter
        return f"{m.group(1)}-{scope.group(1) or scope.group(2)}"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_concurrency: int = 3,
        file_policy: FilePolicy | None = None,
        download_timeout_s: float = http.DOWNLOAD_TIMEOUT_S,
    ):
        self._client = client
        # Politeness cap per worker, shared by searches and downloads.
        self._sem = asyncio.Semaphore(max_concurrency)
        self._search_verified_at = 0.0
        self._classes_checked = False
        self._file_policy = file_policy
        self._download_timeout_s = download_timeout_s
        # accession -> primary file, from recent listings; a cached listing falls back to a lookup.
        self._files: dict[str, _File] = {}

    @property
    def _policy(self) -> FilePolicy:
        return self._file_policy or FilePolicy.from_settings()

    # ------------------------------------------------------------------ metadata and listings

    async def fetch_matter(self, matter: str) -> MatterInfo:
        docket, docs = await self._docket(matter)
        return self._matter_info(matter, docket, docs)

    async def list_documents(self, matter: str, doc_type: str, limit: int) -> list[DocumentRef]:
        _, refs = await self.list_matter_and_documents(matter, doc_type, limit)
        return refs

    async def list_matter_and_documents(
        self, matter: str, doc_type: str, limit: int
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        docket, docs = await self._docket(matter)
        # Collect until `limit` *public* documents: non-public ones on the way are listed too, so
        # the reply can say they were left out.
        refs: list[DocumentRef] = []
        public = 0
        for doc in _newest_first(d for d in docs if d.category == doc_type):
            if public >= limit:
                break
            refs.append(
                DocumentRef(
                    provider=self.name,
                    matter=matter,
                    doc_type=doc_type,
                    external_id=doc.accession,
                    title=doc.title,
                    filed_on=doc.filed_on,
                    access="Public" if doc.public else NON_PUBLIC,
                    file_ext=doc.file.ext,
                    row_index=len(refs),
                    source_type=doc.source_type,
                )
            )
            if doc.public:
                public += 1
                self._remember(doc.accession, doc.file)
        return self._matter_info(matter, docket, docs), refs

    def _matter_info(self, matter: str, docket: _Docket, docs: list[_Doc]) -> MatterInfo:
        counts = Counter(d.category for d in docs)
        filed = [d.filed_on for d in docs if d.filed_on]
        decided = [d.filed_on for d in docs if d.filed_on and d.category == _ORDERS]
        opening = _opening_application(docs)
        return MatterInfo(
            provider=self.name,
            matter=matter,
            title=_docket_title(matter, docket.description, docs),
            type=opening.application_type if opening else None,
            category=_meaningful_status(docket.status),
            date_received=min(filed, default=None),
            decision_date=max(decided, default=None),
            counts={c.name: counts[c.name] for c in CATEGORIES},
            portal_url=docket_url(matter),
            fetched_at=datetime.now(UTC),
        )

    async def _docket(self, matter: str) -> tuple[_Docket, list[_Doc]]:
        """The docket's description and every document in it (up to MAX_HITS)."""
        base, sub = split_matter(matter)
        docket, first, _ = await asyncio.gather(
            self._describe(base), self._search(_search_body(docket=base, sub=sub)), self._check_classes()
        )
        if first.total == 0:
            await self._ensure_search_works()
            # An unknown docket, or a sub-docket that isn't there (the docket itself is described).
            if docket.description is None or sub is not None:
                raise MatterNotFound(matter)
            raise ScrapeError(f"eLibrary describes {matter} but its search returned no documents")
        wanted = min(first.total, MAX_HITS)
        pages = -(-wanted // PAGE_SIZE)
        rest = await asyncio.gather(
            *(self._search(_search_body(docket=base, sub=sub, page=p)) for p in range(2, pages + 1))
        )
        hits = {h.accession: h for page in (first, *rest) for h in page.hits or ()}
        if not hits:
            raise ScrapeError(f"{matter}: {first.total} documents reported but none returned")
        if first.total > MAX_HITS:
            log.warning("ferc.hits_capped", matter=matter, total=first.total, kept=len(hits))
        elif len(hits) < wanted:
            log.warning("ferc.hits_short", matter=matter, total=first.total, got=len(hits))
        allowed = self._policy.allowed_exts
        docs = [d for d in (_parse_hit(h, allowed) for h in hits.values()) if d is not None]
        if len(docs) < len(hits):
            log.info("ferc.documents_without_files", matter=matter, count=len(hits) - len(docs))
        return docket, docs

    async def _ensure_search_works(self) -> None:
        """Zero hits is also what the search returns for a request it no longer understands (it
        already does for one without a date range), and an unknown docket's description is what a
        broken description lookup would look like. Before telling anyone their docket doesn't
        exist, check a known one still has both. Cached briefly so not-founds stay cheap."""
        if time.monotonic() - self._search_verified_at < 900:
            return
        base, sub = split_matter(CANARY_MATTER)
        docket, page = await asyncio.gather(
            self._describe(base), self._search(_search_body(docket=base, sub=sub))
        )
        if page.total == 0 or docket.description is None:
            raise ScrapeError(f"eLibrary returned nothing for canary {CANARY_MATTER}: API changed?")
        self._search_verified_at = time.monotonic()

    async def _describe(self, docket: str) -> _Docket:
        what = f"FERC docket description for {docket}"
        async with self._sem:
            try:
                r = await self._client.get(f"Docket/getDocketDescription/{docket}")
            except httpx.RequestError as e:
                raise PortalUnavailable(f"{what}: {e!r}") from e
        raise_for_status(r, what)
        try:
            data = _Description.model_validate_json(r.content).data
        except ValidationError as e:
            raise ScrapeError(f"{what}: unexpected response: {str(e)[:300]}") from e
        if not data:
            raise ScrapeError(f"{what}: empty DataList")
        description = (data[0] or "").strip()
        status = (data[1] or "").strip() if len(data) > 1 else ""
        if description.casefold() == NOT_FOUND_TEXT.casefold():
            return _Docket(None, None)
        return _Docket(description or None, status or None)

    async def _search(self, body: dict[str, Any]) -> _Page:
        searches = body["docketSearches"]
        what = f"FERC search for {searches[0]['docketNumber'] if searches else body['accessionNumber']}"
        async with self._sem:
            try:
                r = await self._client.post("Search/AdvancedSearch", json=body)
            except httpx.RequestError as e:
                raise PortalUnavailable(f"{what}: {e!r}") from e
        raise_for_status(r, what)
        try:
            page = _Page.model_validate_json(r.content)
        except ValidationError as e:
            raise ScrapeError(f"{what}: unexpected response: {str(e)[:300]}") from e
        if not page.success:
            raise ScrapeError(f"{what}: {page.error or 'search failed'}")
        return page

    async def _check_classes(self) -> None:
        """Once per worker, log any document class eLibrary lists that no category names (it lands
        in Correspondence). A sanity check only: it never fails a request."""
        if self._classes_checked:
            return
        self._classes_checked = True
        try:
            async with self._sem:
                r = await self._client.get("Search/GetClassTypes", timeout=15)
            r.raise_for_status()
            classes = {str(c["Class"]) for c in r.json()}
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            log.info("ferc.class_check_failed", error=repr(e)[:200])
            return
        unmapped = sorted(c for c in classes if not _is_named(c))
        if unmapped:
            log.info("ferc.unmapped_classes", classes=unmapped, category=_CORRESPONDENCE.name)

    # ------------------------------------------------------------------ downloads

    def _remember(self, accession: str, file: _File) -> None:
        self._files.pop(accession, None)
        self._files[accession] = file
        if len(self._files) > _FILES_REMEMBERED:
            del self._files[next(iter(self._files))]

    async def _primary_file(self, accession: str) -> _File:
        """The file to fetch for a public document: from a recent listing, else looked up."""
        if accession in self._files:
            return self._files[accession]
        page = await self._search(_search_body(accession=accession))
        hit = next((h for h in page.hits or () if h.accession == accession), None)
        if hit is None:
            raise ScrapeError(f"FERC accession {accession} not found")
        if hit.avail_code != "P":
            raise ScrapeError(f"FERC accession {accession} is not public")
        file = primary_file(hit.transmittals, self._policy.allowed_exts)
        if file is None:
            raise ScrapeError(f"FERC accession {accession} has no files")
        self._remember(accession, file)
        return file

    async def download(
        self, matter: str, refs: list[DocumentRef], dest_dir: str
    ) -> AsyncIterator[DownloadedFile]:
        """Yield files as they land. One failed file is logged and skipped; all failing raises."""
        tasks = [asyncio.create_task(self._download_one(ref, dest_dir)) for ref in refs]
        errors: list[AgentError] = []
        try:
            for finished in asyncio.as_completed(tasks):
                try:
                    file = await finished
                except AgentError as e:  # incl. TooLarge and file types we don't deliver
                    log.warning("ferc.download_failed", matter=matter, error=str(e)[:200])
                    errors.append(e)
                    continue
                yield file
        finally:
            for t in tasks:
                t.cancel()
        if errors and len(errors) == len(refs):
            raise error_to_raise(errors)

    async def _download_one(self, ref: DocumentRef, dest_dir: str) -> DownloadedFile:
        what = f"FERC download of {ref.external_id}"
        if ref.access != "Public":
            raise ScrapeError(f"{what}: not public")
        policy = self._policy
        file = await self._primary_file(ref.external_id)
        if file.ext:  # as listed: don't fetch what we won't deliver
            policy.check_ext(file.ext, what)
        policy.check_size(file.size, what)
        tmp = os.path.join(dest_dir, f".{secrets.token_hex(8)}.part")
        body = {"FileType": "", "accession": "", "fileid": 0, "FileIDAll": "", "fileidLst": [file.file_id],
                "Islegacy": False}
        async with self._sem:
            # Files come as application/octet-stream; HTML and JSON responses are errors.
            headers = await http.download_to(
                self._client, "POST", "File/DownloadP8File", tmp, what=what, policy=policy,
                deadline_s=self._download_timeout_s, error_types=("text/html", "application/json"), json=body,
            )
        served = served_filename(headers) or file.name
        ext = os.path.splitext(served)[1].lower() or file.ext
        # eLibrary serves everything as application/octet-stream: finalise_download sniffs the bytes.
        downloaded = finalise_download(tmp, ref.model_copy(update={"file_ext": ext}), served, dest_dir, policy)
        return downloaded.model_copy(update={"filename": _display_name(ref.external_id, file.name, ext)})
