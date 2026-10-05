"""Citation viewer and matter pages, served behind Caddy at `public_base_url`.

Read-only over public regulator records. Every input is a path parameter, validated before it
reaches SQL or the filesystem; anything malformed or unknown gets the same uninformative 404.
Routes live on `router`; other features add their own routers in `create_app`. Every route but
/health, /health/deep and /metrics is rate-limited per client IP (agent.web.ratelimit): 429 +
Retry-After. /health/deep and /metrics answer loopback clients only (everyone else: 404).
"""

import argparse
import asyncio
import functools
import hashlib
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import asyncpg
import jinja2
import structlog
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent import db, health, metrics
from agent.config import Settings, get_settings
from agent.web.progress import router as progress_router
from agent.web.ratelimit import WebLimiter, client_ip, route_class
from agent.web.status import router as status_router

log = structlog.get_logger()
HERE = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class CdnAsset:
    url: str
    sri: str


_CDN = "https://cdnjs.cloudflare.com/ajax/libs"
# PDF.js 4.10.38: newest cdnjs build that runs on the phone browsers people read mail on (5.x
# needs Map.getOrInsertComputed and wasm for JBIG2/JPX) and past CVE-2024-4367 (fixed in 4.2.67).
CDN_ASSETS = {
    "pdfjs": CdnAsset(
        f"{_CDN}/pdf.js/4.10.38/pdf.min.mjs",
        "sha384-+0ti2moQlmLN7WZHE2RHIf5lV8hHxhxEalN0il3YZceG26fUPyOkR0hp9daxk1i7",
    ),
    "pdfjs_worker": CdnAsset(
        f"{_CDN}/pdf.js/4.10.38/pdf.worker.min.mjs",
        "sha384-ToeVvShCxKc6CEvhHeMt0Q8A06pSPDbAlngO9nokrDmh914gk/pYd0N7D0a4Lz2o",
    ),
    "markjs": CdnAsset(
        f"{_CDN}/mark.js/8.11.1/mark.min.js",
        "sha384-t9DGTa+HJ3fETmsPZ37+56VRxK0NsOgFTQ1B4fxaxA+BM48rM5oXrg0/ncF/B3VX",
    ),
}

CSP = (
    "default-src 'none'; "
    "script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self'; "
    # Browsers refuse cross-origin worker scripts: viewer.js fetches the PDF.js worker from cdnjs
    # with an SRI check (connect-src) and starts it from a same-origin blob: URL.
    "worker-src blob:; "
    "connect-src 'self' https://cdnjs.cloudflare.com; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)
_COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}
_HTML_HEADERS = {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY"}
# A document id's current file can change when the regulator replaces it; links we send carry the
# version's sha256 instead. Either way a day of caching is plenty.
_PDF_HEADERS = {"Cache-Control": "public, max-age=86400"}
_DOWNLOAD_HEADERS = {**_PDF_HEADERS, "X-Content-Type-Options": "nosniff"}

_OOXML = b"PK\x03\x04"  # Word/Excel 2007+: a zip package
_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # Word/Excel 97-2003


@dataclass(frozen=True, slots=True)
class FileType:
    media_type: str
    magic: bytes | None = None  # what the stored file must start with to be served as this type
    label: str = "file"


# Downloads (Content-Disposition: attachment) of the other types settings.allowed_file_exts lets in.
# PDFs are served inline at /files/{id}.pdf, for the viewer.
FILE_TYPES: dict[str, FileType] = {
    "docx": FileType("application/vnd.openxmlformats-officedocument.wordprocessingml.document", _OOXML, "Word file"),
    "doc": FileType("application/msword", _OLE2, "Word file"),
    "xlsx": FileType("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", _OOXML, "spreadsheet"),
    "xlsm": FileType("application/vnd.ms-excel.sheet.macroEnabled.12", _OOXML, "spreadsheet"),
    "xls": FileType("application/vnd.ms-excel", _OLE2, "spreadsheet"),
    "csv": FileType("text/csv; charset=utf-8", None, "spreadsheet"),
    "txt": FileType("text/plain; charset=utf-8", None, "text file"),
    "mp3": FileType("audio/mpeg", None, "recording"),
    "mp4": FileType("video/mp4", None, "recording"),
    "wav": FileType("audio/wav", None, "recording"),
}

_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ ()-]+")

CitationId = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9_-]{8,32}$")]
Sha256 = Annotated[str, PathParam(pattern=r"^[0-9a-f]{64}$")]
ProviderName = Annotated[str, PathParam(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
FileExt = Annotated[str, PathParam(pattern="^(?:" + "|".join(FILE_TYPES) + ")$")]
MatterNumber = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9-]{1,32}$")]


# ---------------------------------------------------------------- templates


def _human_date(d: date | None) -> str:
    return f"{d:%b} {d.day}, {d.year}" if d else ""


def _human_size(n: int | None) -> str:
    if n is None:
        return ""
    return f"{n / 1_000_000:.1f} MB" if n >= 1_000_000 else f"{max(n // 1000, 1)} KB"


templates = Jinja2Templates(
    env=jinja2.Environment(
        loader=jinja2.FileSystemLoader(HERE / "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
    )
)
def _static_url(name: str) -> str:
    """Content-hashed URL: Cloudflare caches /static for hours, so every change needs a new URL."""
    path = HERE / "static" / name
    return f"/static/{name}?v={hashlib.sha256(path.read_bytes()).hexdigest()[:10]}"


templates.env.filters.update(human_date=_human_date, human_size=_human_size)
templates.env.globals.update(cdn=CDN_ASSETS, static_url=functools.cache(_static_url))


# ---------------------------------------------------------------- data access


class CitationView(BaseModel):
    id: str
    claim: str
    quote: str
    page: int
    document_id: uuid.UUID
    provider: str
    matter: str
    doc_type: str
    external_id: str
    doc_title: str
    filed_on: date | None = None
    page_count: int | None = None
    matter_title: str | None = None
    portal_url: str | None = None
    sha256: str | None = None  # the file version the quote's offsets refer to
    filename: str | None = None  # the stored file's name; its extension says how to show it
    context_before: str | None = None  # the sentence before the quote on its page
    context_after: str | None = None

    @property
    def ext(self) -> str:
        """"pdf", "docx", ...: from the stored file name (citations made before it was recorded are on PDFs)."""
        ext = Path(self.filename or "").suffix.lower().lstrip(".")
        return ext if ext in FILE_TYPES else "pdf"

    @property
    def is_pdf(self) -> bool:
        return self.ext == "pdf"

    @property
    def file_label(self) -> str:
        return "PDF" if self.is_pdf else FILE_TYPES[self.ext].label

    @property
    def file_url(self) -> str:
        """The exact version the citation was grounded in, when it was recorded."""
        if self.sha256 and _SHA256.fullmatch(self.sha256):
            return f"/files/{self.document_id}/{self.sha256}.{self.ext}"
        return f"/files/{self.document_id}.{self.ext}"

    @property
    def pdf_url(self) -> str:
        return self.file_url


class StoredFile(BaseModel):
    external_id: str
    sha256: str | None = None
    filename: str | None = None


_CITATION_SQL = """
SELECT c.id, c.claim, c.quote, c.page, c.sha256, c.context_before, c.context_after,
       d.id AS document_id, d.provider, d.matter, d.doc_type, d.external_id, d.title AS doc_title,
       d.filed_on, d.page_count, d.filename,
       m.info->>'title' AS matter_title, m.info->>'portal_url' AS portal_url
FROM citations c
JOIN documents d ON d.id = c.document_id
LEFT JOIN matters m ON m.provider = d.provider AND m.matter = d.matter
WHERE c.id = $1
"""
_MATTER_DOCUMENTS_SQL = """
SELECT id, doc_type, external_id, title, filed_on, page_count, size_bytes
FROM documents
WHERE provider = $1 AND matter = $2 AND sha256 IS NOT NULL
ORDER BY filed_on DESC NULLS LAST, external_id DESC
"""


async def load_citation(citation_id: str) -> CitationView | None:
    row = await db.fetchrow(_CITATION_SQL, citation_id)
    return CitationView.model_validate(dict(row)) if row else None


async def load_stored_file(document_id: uuid.UUID) -> StoredFile | None:
    row = await db.fetchrow("SELECT external_id, sha256, filename FROM documents WHERE id = $1", document_id)
    return StoredFile.model_validate(dict(row)) if row else None


async def load_pinned_file(document_id: uuid.UUID, sha256: str) -> StoredFile | None:
    """A specific version of a document: its current one, or one a citation was grounded in."""
    row = await db.fetchrow(
        """
        SELECT d.external_id, $2::text AS sha256, d.filename FROM documents d
        WHERE d.id = $1 AND (d.sha256 = $2 OR EXISTS (
            SELECT 1 FROM citations c WHERE c.document_id = d.id AND c.sha256 = $2))
        """,
        document_id,
        sha256,
    )
    return StoredFile.model_validate(dict(row)) if row else None




async def db_ok() -> bool:
    try:
        async with asyncio.timeout(2):
            return await db.fetchrow("SELECT 1") is not None
    except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
        log.warning("web.health_db_down", error=repr(e))
        return False


# ---------------------------------------------------------------- helpers


def _https_url(url: str | None) -> str | None:
    """Only ever link out over https (a stored `javascript:` URL must not become a link)."""
    return url if url and urlsplit(url).scheme == "https" else None


def _download_name(stored: StoredFile, ext: str = "pdf") -> str:
    name = _UNSAFE_FILENAME_CHARS.sub("_", stored.filename or "").strip(" ._")
    if name.lower().endswith(f".{ext}"):
        return name
    return f"{_UNSAFE_FILENAME_CHARS.sub('_', stored.external_id).strip(' ._') or 'document'}.{ext}"


def _blob(data_dir: str, sha256: str | None) -> tuple[Path, bytes] | None:
    """(path, first 1 KiB) of a stored file, or None. The path is built only from a well-formed hash."""
    if not sha256 or not _SHA256.fullmatch(sha256):
        return None
    path = Path(data_dir) / "blobs" / sha256[:2] / sha256
    try:
        with path.open("rb") as f:
            return path, f.read(1024)
    except FileNotFoundError:
        log.error("web.blob_missing", sha256=sha256)
        return None


def _servable_blob(data_dir: str, sha256: str | None) -> Path | None:
    """Path of a stored PDF, or None."""
    found = _blob(data_dir, sha256)
    # Excel/Word filings share the documents table; never label those application/pdf.
    return found[0] if found and b"%PDF-" in found[1] else None


def _servable_download(data_dir: str, stored: StoredFile, ext: str) -> Path | None:
    """Path of a stored file to download as `ext`, or None: the file must have been stored under
    that extension and, for Office formats, look like one."""
    if Path(stored.filename or "").suffix.lower() != f".{ext}":
        return None
    found = _blob(data_dir, stored.sha256)
    if found is None:
        return None
    magic = FILE_TYPES[ext].magic
    return found[0] if magic is None or found[1].startswith(magic) else None


def _error_page(request: Request, status_code: int, headers: dict[str, str] | None = None) -> Response:
    return templates.TemplateResponse(
        request, "error.html", {"status_code": status_code}, status_code=status_code, headers=headers
    )


# ---------------------------------------------------------------- routes

router = APIRouter()
# HEAD too: mail clients and link scanners probe links before (or instead of) opening them.
_READ = ["GET", "HEAD"]


@router.api_route("/c/{citation_id}", methods=_READ)
async def citation_page(request: Request, citation_id: CitationId) -> Response:
    view = await load_citation(citation_id)
    if view is None:
        raise HTTPException(status_code=404)
    context = {
        "c": view,
        "portal_url": _https_url(view.portal_url),
        "download_name": _download_name(StoredFile(external_id=view.external_id, filename=view.filename), view.ext),
    }
    return templates.TemplateResponse(request, "citation.html", context)


@router.api_route("/files/{document_id}.pdf", methods=_READ)
async def document_pdf(
    document_id: uuid.UUID, settings: Annotated[Settings, Depends(get_settings)]
) -> FileResponse:
    return await _pdf_response(await load_stored_file(document_id), settings)


@router.api_route("/files/{document_id}/{sha256}.pdf", methods=_READ)
async def document_version_pdf(
    document_id: uuid.UUID, sha256: Sha256, settings: Annotated[Settings, Depends(get_settings)]
) -> Response:
    pinned = await load_pinned_file(document_id, sha256)
    if pinned is None and await load_stored_file(document_id) is not None:
        # a version we no longer track (the regulator replaced the file): the current one, not a dead link
        return RedirectResponse(f"/files/{document_id}.pdf", status_code=307)
    return await _pdf_response(pinned, settings)


@router.api_route("/files/{document_id}.{ext}", methods=_READ)
async def document_download(
    document_id: uuid.UUID, ext: FileExt, settings: Annotated[Settings, Depends(get_settings)]
) -> FileResponse:
    return await _download_response(await load_stored_file(document_id), ext, settings)


@router.api_route("/files/{document_id}/{sha256}.{ext}", methods=_READ)
async def document_version_download(
    document_id: uuid.UUID, sha256: Sha256, ext: FileExt, settings: Annotated[Settings, Depends(get_settings)]
) -> Response:
    pinned = await load_pinned_file(document_id, sha256)
    if pinned is None and await load_stored_file(document_id) is not None:
        return RedirectResponse(f"/files/{document_id}.{ext}", status_code=307)
    return await _download_response(pinned, ext, settings)


async def _download_response(stored: StoredFile | None, ext: str, settings: Settings) -> FileResponse:
    """A Word file, spreadsheet or recording: always a download (never rendered by the browser)."""
    path = await asyncio.to_thread(_servable_download, settings.data_dir, stored, ext) if stored else None
    if stored is None or path is None:
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type=FILE_TYPES[ext].media_type,
        filename=_download_name(stored, ext),
        content_disposition_type="attachment",
        headers=_DOWNLOAD_HEADERS,
    )


async def _pdf_response(stored: StoredFile | None, settings: Settings) -> FileResponse:
    path = await asyncio.to_thread(_servable_blob, settings.data_dir, stored.sha256) if stored else None
    if stored is None or path is None:
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=_download_name(stored),
        content_disposition_type="inline",
        headers=_PDF_HEADERS,
    )


@router.get("/health")
async def health_check() -> dict[str, bool]:
    return {"ok": True, "db": await db_ok()}


_LOOPBACK = {"127.0.0.1", "::1"}


def _from_loopback(request: Request) -> bool:
    """A client on this host talking to us directly, not through Caddy (which always sets
    X-Real-IP and X-Forwarded-For to the real client)."""
    if (request.client.host if request.client else None) not in _LOOPBACK:
        return False
    real = request.headers.get("x-real-ip")
    forwarded = request.headers.get("x-forwarded-for")
    return (real is None or real.strip() in _LOOPBACK) and (
        forwarded is None or all(p.strip() in _LOOPBACK for p in forwarded.split(","))
    )


async def oldest_open_request_age() -> float | None:
    """Seconds since the oldest request that hasn't settled arrived (None: there is none)."""
    async with asyncio.timeout(3):
        return await db.fetchval(
            "SELECT extract(epoch FROM now() - min(received_at)) FROM requests "
            "WHERE state NOT IN ('rejected', 'done', 'failed', 'clarify')"
        )


async def deep_facts(settings: Settings) -> dict:
    database: dict = {"ok": await db_ok()}
    if database["ok"]:
        try:
            age = await oldest_open_request_age()
            database["oldest_open_request_age_s"] = round(age) if age is not None else None
        except (OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as e:
            database["error"] = type(e).__name__
    redis = await health.redis_facts(settings)
    free = await asyncio.to_thread(health.disk_free, settings.data_dir)
    backup = await asyncio.to_thread(health.last_backup, settings.backup_log_path)
    return {
        "ok": bool(database["ok"] and redis.get("ok")),
        "db": database,
        "redis": redis,
        "disk_free_bytes": free,
        "backup": backup,
        "app_version": settings.app_version,
    }


@router.get("/health/deep")
async def health_deep(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    """Operational detail for whoever is on the host; the outside world gets the plain 404."""
    if not _from_loopback(request):
        raise HTTPException(status_code=404)
    return JSONResponse(await deep_facts(settings), headers={"Cache-Control": "no-store"})


@router.get("/metrics")
async def metrics_endpoint(request: Request) -> Response:
    """This process's Prometheus metrics (web_rate_limited_total, limiter decisions...) for the
    Prometheus on this host. Stricter than /health/deep: a loopback peer with no forwarding headers
    at all, so nothing that came through a proxy is ever answered (Caddy also answers 404)."""
    forwarded = any(h in request.headers for h in ("x-forwarded-for", "x-real-ip", "forwarded"))
    if forwarded or not _from_loopback(request):
        raise HTTPException(status_code=404)
    return Response(metrics.exposition("web"), media_type=CONTENT_TYPE_LATEST, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- privacy and disclosure


def _privacy_context(s: Settings) -> dict:
    return {
        "agent_address": s.agent_mail_address, "privacy": s.privacy_contact, "security": s.security_contact,
        "zdr": s.llm_zero_data_retention, "typesafe_gate": s.gate_classifier == "jev",
        "typesafe_check": s.citation_check == "jev" and s.llm_check_support, "raw_days": s.raw_mime_retention_days,
        "rejected_days": s.rejected_raw_retention_days, "pseudonymise_days": s.request_pseudonymise_days,
        "delete_days": s.request_delete_days, "audit_days": max(s.audit_retention_days, 400),
        "link_days": max(round(s.drop_expiry_s / 86400), 1), "access_log_days": s.access_log_retention_days,
        "backup_days": s.backup_retention_days, "offsite_backup_days": s.offsite_backup_retention_days,
        "blob_days": s.blob_retention_days,
    }


@router.api_route("/privacy", methods=_READ)
async def privacy_page(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    return templates.TemplateResponse(request, "privacy.html", {"p": _privacy_context(settings)})


@router.api_route("/.well-known/security.txt", methods=_READ)
async def security_txt(settings: Annotated[Settings, Depends(get_settings)]) -> Response:
    """RFC 9116. Expires is always a year ahead, so it never lapses while the service runs."""
    base = settings.public_base_url.rstrip("/")
    expires = (datetime.now(UTC) + timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)
    body = (
        f"Contact: mailto:{settings.security_contact}\n"
        f"Expires: {expires:%Y-%m-%dT%H:%M:%S.000Z}\n"
        "Preferred-Languages: en\n"
        f"Policy: {base}/privacy#security\n"
        f"Canonical: {base}/.well-known/security.txt\n"
    )
    return PlainTextResponse(body, headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- app


async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers.update(_COMMON_HEADERS)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.update(_HTML_HEADERS)
    return response


async def rate_limit(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Token buckets per client IP and route class; without a limiter (no lifespan: tests) every
    request goes ahead."""
    limiter: WebLimiter | None = getattr(request.app.state, "rate_limiter", None)
    found = route_class(request.url.path, get_settings()) if limiter is not None else None
    if found is None:
        return await call_next(request)
    name, buckets = found
    ip = client_ip(request)
    retry_after = await limiter.check(name, buckets, ip)
    if retry_after is None:
        return await call_next(request)
    log.info("web.rate_limited", route=name, retry_after_s=retry_after)
    metrics.observe_web_rate_limited(name)
    headers = {"Retry-After": str(int(retry_after)), "Cache-Control": "no-store"}
    if request.url.path.endswith(".json"):
        return JSONResponse({"error": "rate limited"}, status_code=429, headers=headers)
    return _error_page(request, 429, headers)


async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    return _error_page(request, exc.status_code, exc.headers)


async def validation_error(request: Request, exc: Exception) -> Response:
    # Every input is a path parameter, so a malformed one names nothing: a plain 404, no details.
    return _error_page(request, 404)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    await db.create_pool()
    app.state.rate_limiter = WebLimiter.from_settings(get_settings())
    try:
        yield
    finally:
        await app.state.rate_limiter.aclose()
        app.state.rate_limiter = None
        await db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Regulatory source viewer", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.middleware("http")(rate_limit)
    app.middleware("http")(security_headers)  # outermost: a 429 carries the security headers too
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)
    app.include_router(progress_router)
    app.include_router(status_router)
    return app


app = create_app()


def main() -> None:
    """Loopback by default. In a container pass --host 0.0.0.0 and publish the port on
    127.0.0.1 only, so Caddy remains the only way in."""
    parser = argparse.ArgumentParser(description="Citation viewer")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    uvicorn.run("agent.web.app:app", host=args.host, port=get_settings().web_port, server_header=False)


if __name__ == "__main__":
    main()
