from pathlib import Path

import pymupdf
import pytest
from structlog.testing import capture_logs

from agent.citations import extract
from agent.citations.extract import extract_pages, extract_pages_async, needs_ocr

ORDER_PDF = str(Path(__file__).resolve().parents[1] / "fixtures" / "uarb_102674.pdf")  # M12205 Board Order


def _make_pdf(path: Path, pages: list[str], **save_kwargs) -> str:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(path, **save_kwargs)
    doc.close()
    return str(path)


def test_board_order_pages_are_extracted_in_order():
    pages = extract_pages(ORDER_PDF)
    assert len(pages) == 2
    assert pages[0].startswith("ORDER M12205")
    assert "for a cost of $64,769,000" in pages[0]
    assert "for a total project cost of $59,143,000, inclusive of net HST" in pages[1]
    assert "this 8th day of July 2026" in pages[1]
    assert not needs_ocr(pages)


def test_whitespace_is_normalised_per_line_and_line_breaks_kept():
    pages = extract_pages(ORDER_PDF)
    for text in pages:
        lines = text.split("\n")
        assert all(line == line.strip() for line in lines)
        assert all("  " not in line for line in lines)
        assert "\n\n\n" not in text
    # sort=True layout output padded these with long runs of spaces
    assert "applied to the Nova Scotia\nRegulatory and Appeals Board" in pages[0]


async def test_async_wrapper_matches_sync():
    assert await extract_pages_async(ORDER_PDF) == extract_pages(ORDER_PDF)


def test_encrypted_pdf_yields_no_pages_and_a_warning(tmp_path: Path):
    path = _make_pdf(
        tmp_path / "locked.pdf",
        ["confidential text"],
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="user",
        owner_pw="owner",
    )
    with capture_logs() as logs:
        assert extract_pages(path) == []
    assert [e["event"] for e in logs] == ["pdf.encrypted"]


@pytest.mark.parametrize("content", [b"", b"%PDF-1.7\nthis is not really a pdf", b"\x00\x01\x02" * 100])
def test_broken_pdf_yields_no_pages_and_a_warning(tmp_path: Path, content: bytes):
    path = tmp_path / "broken.pdf"
    path.write_bytes(content)
    with capture_logs() as logs:
        assert extract_pages(str(path)) == []
    assert [e["event"] for e in logs] == ["pdf.unreadable"]


def test_scan_without_text_layer_is_flagged(tmp_path: Path):
    path = _make_pdf(tmp_path / "scan.pdf", ["", "", "p. 3"])
    with capture_logs() as logs:
        pages = extract_pages(path)
    assert len(pages) == 3  # numbering is preserved even for empty pages
    assert needs_ocr(pages)
    assert "pdf.needs_ocr" in [e["event"] for e in logs]


def test_needs_ocr_ignores_whitespace_and_empty_documents():
    assert needs_ocr(["   \n\n  ", "\n"])
    assert not needs_ocr([])
    assert not needs_ocr(["x" * 40])


def test_page_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(extract, "MAX_PAGES", 3)
    path = _make_pdf(tmp_path / "long.pdf", [f"page {i} text" for i in range(1, 6)])
    pages = extract_pages(path)
    assert [p.split()[1] for p in pages] == ["1", "2", "3"]


def test_total_text_cap_stops_at_a_page_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(extract, "MAX_TOTAL_CHARS", 25)
    path = _make_pdf(tmp_path / "big.pdf", ["first page text", "second page text", "third page"])
    assert extract_pages(path) == ["first page text"]


def test_control_characters_are_removed():
    assert extract._clean("a\x00b\x07c \t d\n\n\n\n  e  ") == "abc d\n\ne"
