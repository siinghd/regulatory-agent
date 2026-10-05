"""Per-page text extraction: PDFs with PyMuPDF, Word (DOCX) files from their XML.

The page list is the ground truth that citations are checked against and that `pages.text`
stores, so page numbering must never shift: a page we cannot read becomes "" rather than
disappearing. Bad input (encrypted, corrupt) is data, not a bug: it yields [] and a warning.

A DOCX has no fixed pages. Its pages here are where Word last broke them (w:lastRenderedPageBreak,
which Word saves with every document it lays out) plus manual page breaks; a document without
those markers, or a "page" that runs far longer than a printed one, is cut into pieces of about
DOCX_PAGE_CHARS at paragraph ends. So a DOCX page number is Word's, or close to it. FERC issues its
orders as DOCX, which is why this exists.
"""

import asyncio
import io
import re
import zipfile
from collections.abc import Sequence
from xml.etree import ElementTree

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
# A printed page of a FERC order holds ~2,500-3,500 characters.
DOCX_PAGE_CHARS = 3_000
DOCX_MAX_PAGE_CHARS = 2 * DOCX_PAGE_CHARS  # a longer marker-delimited page is cut at paragraph ends
DOCX_MAX_XML_BYTES = 64_000_000  # uncompressed document.xml; the largest FERC order seen is ~9 MB

# NUL and other C0 controls: Postgres text columns reject NUL, and none of them are visible text.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_HSPACE = re.compile(r"[^\S\n]+")
_BLANK_RUNS = re.compile(r"\n{3,}")


def extract_pages(pdf_path: str) -> list[str]:
    """Text of each page (index 0 = page 1), whitespace normalised per line, line breaks kept.

    Reads PDFs, and Word documents (DOCX, recognised by content, not by name). Returns [] for
    encrypted or unreadable files. Stops at MAX_PAGES / MAX_TOTAL_CHARS.
    """
    if is_docx(pdf_path):
        return extract_docx_pages(pdf_path)
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
    except (RuntimeError, IndexError, ValueError, pymupdf.mupdf.FzErrorBase) as e:
        # IndexError: a page tree whose /Count promises more pages than it has.
        # One damaged content stream must not renumber the pages after it.
        log.warning("pdf.page_unreadable", path=pdf_path, page=index + 1, error=str(e))
        return ""
    return _clean(raw)


def _clean(raw: str) -> str:
    lines = (_HSPACE.sub(" ", line).strip() for line in _CONTROL.sub("", raw).split("\n"))
    return _BLANK_RUNS.sub("\n\n", "\n".join(lines)).strip()


# ---------------------------------------------------------------- DOCX

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_PAGE_BREAK = "\f"


def is_docx(path: str) -> bool:
    """A zip holding a Word main document (by content: a stored file's name can't be trusted)."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"PK\x03\x04":
                return False
        with zipfile.ZipFile(path) as z:
            return _main_part(z) is not None
    except (OSError, zipfile.BadZipFile, KeyError, ValueError):
        return False


def extract_docx_pages(path: str) -> list[str]:
    """Pages of a Word document (see the module docstring for what a DOCX page is)."""
    try:
        with zipfile.ZipFile(path) as z:
            part = _main_part(z)
            if part is None:
                raise ValueError("no word/document.xml")
            if z.getinfo(part).file_size > DOCX_MAX_XML_BYTES:
                raise ValueError(f"{part} is over {DOCX_MAX_XML_BYTES} bytes uncompressed")
            xml = z.read(part)
        if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:  # Word never writes them; entity expansion is a bomb
            raise ValueError("DTD in document.xml")
        text = _docx_text(xml)
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, ElementTree.ParseError, RuntimeError) as e:
        log.warning("docx.unreadable", path=path, error=f"{type(e).__name__}: {e}"[:300])
        return []
    pages: list[str] = []
    total = 0
    for raw in _docx_pages(text):
        page = _clean(raw)
        if len(pages) >= MAX_PAGES:
            log.warning("docx.page_cap", path=path, cap=MAX_PAGES)
            break
        if total + len(page) > MAX_TOTAL_CHARS:
            log.warning("docx.text_cap", path=path, pages_kept=len(pages), cap=MAX_TOTAL_CHARS)
            break
        pages.append(page)
        total += len(page)
    return pages


def _main_part(z: zipfile.ZipFile) -> str | None:
    """The main document part: from the package relationships, else the usual name."""
    names = set(z.namelist())
    if "_rels/.rels" in names:
        rels = ElementTree.fromstring(z.read("_rels/.rels")[:1_000_000])
        for rel in rels:
            target = (rel.get("Target") or "").lstrip("/")
            if rel.get("Type") == _OFFICE_DOCUMENT and target in names:
                return target
    return "word/document.xml" if "word/document.xml" in names else None


def _docx_text(xml: bytes) -> str:
    """Visible text of document.xml: paragraphs as lines, table rows as lines with their cells
    side by side, page breaks as form feeds. Deleted text, field codes and the fallback copies
    of alternate content are left out."""
    out: list[str] = []
    stack: list[str] = []
    skip = 0  # depth inside an mc:Fallback
    cells = 0  # depth inside table cells
    for event, el in ElementTree.iterparse(io.BytesIO(xml), events=("start", "end")):
        tag = el.tag
        if event == "start":
            if tag == _MC_FALLBACK:
                skip += 1
            elif tag == f"{_W}tc":
                cells += 1
            elif not skip and stack and stack[-1] == f"{_W}r":
                if tag == f"{_W}tab":
                    out.append("\t")
                elif tag in (f"{_W}br", f"{_W}cr"):
                    out.append(_PAGE_BREAK if el.get(f"{_W}type") == "page" else "\n")
                elif tag == f"{_W}lastRenderedPageBreak":
                    out.append(_PAGE_BREAK)
            stack.append(tag)
            continue
        stack.pop()
        if tag == _MC_FALLBACK:
            skip -= 1
        elif skip:
            pass
        elif tag == f"{_W}t":
            out.append(el.text or "")
        elif tag == f"{_W}p":
            out.append(" " if cells else "\n")
        elif tag == f"{_W}tc":
            cells -= 1
        elif tag == f"{_W}tr":
            out.append("\n")
        if tag in (f"{_W}p", f"{_W}tbl"):
            el.clear()  # bound memory on 1,000-page documents
    return "".join(out)


def _docx_pages(text: str) -> list[str]:
    """Split at page breaks (consecutive breaks count once), then cut overlong pages."""
    pages: list[str] = []
    for chunk in text.split(_PAGE_BREAK):
        if not chunk.strip():
            continue  # a manual break followed by Word's rendered one: one boundary
        pages.extend(_cut(chunk))
    return pages


def _cut(page: str) -> list[str]:
    """`page` as is, or in pieces of about DOCX_PAGE_CHARS ending at paragraph (line) ends."""
    if len(page) <= DOCX_MAX_PAGE_CHARS:
        return [page]
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for line in page.split("\n"):
        if size and size + len(line) > DOCX_PAGE_CHARS:
            pieces.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if any(s.strip() for s in current):
        pieces.append("\n".join(current))
    return pieces
