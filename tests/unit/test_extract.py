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


# ---------------------------------------------------------------- DOCX

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _docx(path: Path, body: str, *, doctype: str = "") -> str:
    """A minimal Word package around `body` (the inside of w:body)."""
    import zipfile

    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>{doctype}'
        f'<w:document xmlns:w="{_W_NS}" xmlns:mc="{_MC_NS}"><w:body>{body}</w:body></w:document>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return str(path)


def _p(*runs: str) -> str:
    return "<w:p>" + "".join(f"<w:r>{r}</w:r>" for r in runs) + "</w:p>"


def _t(text: str) -> str:
    return f'<w:t xml:space="preserve">{text}</w:t>'


def test_docx_pages_follow_words_page_breaks(tmp_path: Path):
    body = (
        _p(_t("ORDER ON REHEARING")) + _p(_t("The Commission "), _t("denies rehearing."))
        + _p("<w:lastRenderedPageBreak/>" + _t("Page two starts here."))
        + _p('<w:br w:type="page"/>') + _p("<w:lastRenderedPageBreak/>" + _t("Page three, after a manual break."))
    )
    pages = extract_pages(_docx(tmp_path / "order.docx", body))
    assert pages == [
        "ORDER ON REHEARING\nThe Commission denies rehearing.",
        "Page two starts here.",
        "Page three, after a manual break.",
    ]


def test_docx_tables_deletions_fields_and_fallbacks(tmp_path: Path):
    row = "<w:tr>" + "".join(f"<w:tc>{_p(_t(cell))}</w:tc>" for cell in ("Party", "$1,000")) + "</w:tr>"
    body = (
        f"<w:tbl>{row}</w:tbl>"
        + _p(_t("Kept"), "<w:delText>deleted</w:delText>", "<w:instrText>PAGE</w:instrText>", "<w:tab/>", _t("text"))
        + f"<mc:AlternateContent><mc:Choice>{_p(_t('Once'))}</mc:Choice><mc:Fallback>{_p(_t('Twice'))}</mc:Fallback>"
        "</mc:AlternateContent>"
    )
    (page,) = extract_pages(_docx(tmp_path / "t.docx", body))
    assert page == "Party $1,000\nKept text\nOnce"


def test_long_docx_without_page_markers_is_cut_at_paragraph_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(extract, "DOCX_PAGE_CHARS", 100)
    monkeypatch.setattr(extract, "DOCX_MAX_PAGE_CHARS", 200)
    body = "".join(_p(_t(f"Paragraph {i} " + "x" * 40)) for i in range(10))
    pages = extract_pages(_docx(tmp_path / "long.docx", body))
    assert len(pages) > 1 and all(len(p) <= 200 for p in pages)
    assert "\n".join(pages).count("Paragraph") == 10  # nothing lost, nothing split mid-paragraph


def test_docx_is_recognised_by_content_not_name(tmp_path: Path):
    docx = _docx(tmp_path / "order.pdf", _p(_t("Named .pdf but a Word file.")))
    assert extract.is_docx(docx) and extract_pages(docx) == ["Named .pdf but a Word file."]
    assert not extract.is_docx(ORDER_PDF)


@pytest.mark.parametrize("doctype", ['<!DOCTYPE w [<!ENTITY a "aaaaaaaaaa">]>'])
def test_docx_with_a_dtd_is_refused(tmp_path: Path, doctype: str):
    path = _docx(tmp_path / "bomb.docx", _p(_t("&a;")), doctype=doctype)
    with capture_logs() as logs:
        assert extract_pages(path) == []
    assert [e["event"] for e in logs] == ["docx.unreadable"]


def test_broken_docx_yields_no_pages(tmp_path: Path):
    import zipfile

    path = tmp_path / "broken.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", "<w:document><unclosed>")
    with capture_logs() as logs:
        assert extract_pages(str(path)) == []
    assert [e["event"] for e in logs] == ["docx.unreadable"]
