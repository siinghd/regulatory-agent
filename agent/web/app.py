"""Citation viewer and matter pages, served behind Caddy at `public_base_url`.

Read-only over public regulator records. Every input is a path parameter, validated before it
reaches SQL or the filesystem; anything malformed or unknown gets the same uninformative 404.
Routes live on `router`; other features add their own routers in `create_app`.
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
from datetime import date
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
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from agent import db
from agent.config import Settings, get_settings
from agent.web.progress import router as progress_router

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
# Blobs are content-addressed: the bytes behind a document id never change in place.
_PDF_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}

_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ ()-]+")

CitationId = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9_-]{8,32}$")]
ProviderName = Annotated[str, PathParam(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
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



class StoredFile(BaseModel):
    external_id: str
    sha256: str | None = None
    filename: str | None = None


_CITATION_SQL = """
SELECT c.id, c.claim, c.quote, c.page,
       d.id AS document_id, d.provider, d.matter, d.doc_type, d.external_id, d.title AS doc_title,
       d.filed_on, d.page_count,
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


def _download_name(stored: StoredFile) -> str:
    name = _UNSAFE_FILENAME_CHARS.sub("_", stored.filename or "").strip(" ._")
    return name if name.lower().endswith(".pdf") else f"{stored.external_id}.pdf"


def _servable_blob(data_dir: str, sha256: str | None) -> Path | None:
    """Path of a stored PDF, or None. The path is built only from a well-formed hash."""
    if not sha256 or not _SHA256.fullmatch(sha256):
        return None
    path = Path(data_dir) / "blobs" / sha256[:2] / sha256
    try:
        with path.open("rb") as f:
            head = f.read(1024)
    except FileNotFoundError:
        log.error("web.blob_missing", sha256=sha256)
        return None
    # Excel/Word filings share the documents table; never label those application/pdf.
    return path if b"%PDF-" in head else None


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
        "download_name": f"{view.external_id}.pdf",
    }
    return templates.TemplateResponse(request, "citation.html", context)


@router.api_route("/files/{document_id}.pdf", methods=_READ)
async def document_pdf(
    document_id: uuid.UUID, settings: Annotated[Settings, Depends(get_settings)]
) -> FileResponse:
    stored = await load_stored_file(document_id)
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
async def health() -> dict[str, bool]:
    return {"ok": True, "db": await db_ok()}


# ---------------------------------------------------------------- app


async def security_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers.update(_COMMON_HEADERS)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.update(_HTML_HEADERS)
    return response


async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
    return _error_page(request, exc.status_code, exc.headers)


async def validation_error(request: Request, exc: Exception) -> Response:
    # Every input is a path parameter, so a malformed one names nothing: a plain 404, no details.
    return _error_page(request, 404)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    await db.create_pool()
    try:
        yield
    finally:
        await db.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Regulatory source viewer", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.middleware("http")(security_headers)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(router)
    app.include_router(progress_router)
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
