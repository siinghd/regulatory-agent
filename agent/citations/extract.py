"""Per-page text extraction (PyMuPDF).

The page list is the ground truth that citations are checked against and that `pages.text`
stores, so page numbering must never shift: a page we cannot read becomes "" rather than
disappearing. Bad input (encrypted, corrupt) is data, not a bug: it yields [] and a warning.
"""

import asyncio
import re
from collections.abc import Sequence

import pymupdf
import structlog

log = structlog.get_logger()

MAX_PAGES = 400
# Bounds memory and prompt-building work for pathological files (e.g. a 400-page data dump);
# ordinary decisions are ~3-5k chars/page.
MAX_TOTAL_CHARS = 2_000_000
# Below this many non-whitespace chars per page on average there is no usable text layer.
OCR_MIN_AVG_CHARS = 30

# Expand ligatures (ﬁ -> fi): the LLM quotes "fi" and PDF.js normalises them in its text layer.
_TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
# NUL and other C0 controls: Postgres text columns reject NUL, and none of them are visible text.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_HSPACE = re.compile(r"[^\S\n]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


def extract_pages(pdf_path: str) -> list[str]:
    """Text of each page (index 0 = page 1), whitespace normalised per line, line breaks kept.

    Returns [] for encrypted or unreadable PDFs. Stops at MAX_PAGES / MAX_TOTAL_CHARS.
    """
    try:
        doc = pymupdf.open(pdf_path, filetype="pdf")
    except pymupdf.FileDataError as e:  # includes EmptyFileError
        log.warning("pdf.unreadable", path=pdf_path, error=str(e))
        return []
    with doc:
        if doc.needs_pass:
            log.warning("pdf.encrypted", path=pdf_path)
            return []
        pages = _read_pages(doc, pdf_path)
    if needs_ocr(pages):
        log.warning("pdf.needs_ocr", path=pdf_path, pages=len(pages))
    return pages


async def extract_pages_async(pdf_path: str) -> list[str]:
    """`extract_pages` in a worker thread, so a 400-page PDF doesn't stall the event loop.

    Each page is a separate MuPDF call, so the loop gets the GIL back between pages; this is
    responsiveness, not parallelism (that would need a process pool).
    """
    return await asyncio.to_thread(extract_pages, pdf_path)


def needs_ocr(pages: Sequence[str]) -> bool:
    """True when the PDF has pages but (almost) no text layer, i.e. it is a scan."""
    if not pages:
        return False
    visible = sum(len("".join(p.split())) for p in pages)
    return visible / len(pages) < OCR_MIN_AVG_CHARS


def _read_pages(doc: pymupdf.Document, pdf_path: str) -> list[str]:
    if doc.page_count > MAX_PAGES:
        log.warning("pdf.page_cap", path=pdf_path, page_count=doc.page_count, cap=MAX_PAGES)
    pages: list[str] = []
    total = 0
    for index in range(min(doc.page_count, MAX_PAGES)):
        text = _page_text(doc, index, pdf_path)
        if total + len(text) > MAX_TOTAL_CHARS:
            log.warning("pdf.text_cap", path=pdf_path, pages_kept=len(pages), cap=MAX_TOTAL_CHARS)
            break
        pages.append(text)
        total += len(text)
    return pages


def _page_text(doc: pymupdf.Document, index: int, pdf_path: str) -> str:
    try:
        raw = doc[index].get_text("text", sort=True, flags=_TEXT_FLAGS)
    except (RuntimeError, pymupdf.mupdf.FzErrorBase) as e:
        # One damaged content stream must not renumber the pages after it.
        log.warning("pdf.page_unreadable", path=pdf_path, page=index + 1, error=str(e))
        return ""
    return _clean(raw)


def _clean(raw: str) -> str:
    lines = (_HSPACE.sub(" ", line).strip() for line in _CONTROL.sub("", raw).split("\n"))
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()
