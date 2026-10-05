"""Shared download helpers (agent.providers.files, agent.providers.http): content checks, the
file-type allowlist, the size cap, Retry-After and status classification."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from agent.models import DocumentRef, PortalUnavailable, ProviderRejected, ScrapeError, TooLarge
from agent.providers import http
from agent.providers.files import (
    FilePolicy,
    UnsupportedFileType,
    content_problem,
    error_to_raise,
    finalise_download,
    write_capped,
)

POLICY = FilePolicy.of(10_000, [".pdf", ".docx", ".doc", ".xls", ".txt", ".csv"])
OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def ref(file_ext: str = ".pdf") -> DocumentRef:
    return DocumentRef(provider="oeb", matter="EB-2024-0111", doc_type="x", external_id="D1", title="t",
                       file_ext=file_ext)


@pytest.mark.parametrize(
    ("head", "ext", "ok"),
    [
        (b"%PDF-1.7", ".pdf", True),
        (b"\xef\xbb\xbf\r\n  <!DOCTYPE html><html>", ".pdf", False),  # BOM and blank lines first
        (b"<HTML><body>Error</body></HTML>", ".txt", False),  # an error page under any name
        (b"<head><title>Login", ".csv", False),
        (b"PK\x03\x04", ".docx", True),
        (b"%PDF-", ".docx", False),
        (OLE, ".doc", True),
        (b"{\\rtf1\\ansi", ".doc", True),  # old Word files are sometimes RTF
        (OLE, ".xls", True),
        (b"PK\x03\x04", ".xls", False),
        (b"plain text", ".txt", True),
    ],
)
def test_content_checks(head, ext, ok):
    assert (content_problem(head, ext) is None) is ok


def write(tmp_path: Path, data: bytes) -> str:
    path = tmp_path / ".x.part"
    path.write_bytes(data)
    return str(path)


def test_a_file_type_outside_the_allowlist_is_rejected_and_removed(tmp_path):
    tmp = write(tmp_path, b"MZ\x90\x00")

    with pytest.raises(UnsupportedFileType) as info:
        finalise_download(tmp, ref(""), "setup.exe", str(tmp_path), POLICY)
    assert not info.value.retryable and not list(tmp_path.iterdir())


def test_an_unnamed_pdf_is_recognised_by_its_bytes(tmp_path):
    f = finalise_download(write(tmp_path, b"%PDF-1.4 x"), ref(""), "", str(tmp_path), POLICY)
    assert f.filename == "D1.pdf" and f.path.endswith(".pdf")


def test_a_finished_file_over_the_budget_is_rejected(tmp_path):
    with pytest.raises(TooLarge):
        finalise_download(write(tmp_path, b"%PDF-" + b"x" * 20_000), ref(), "a.pdf", str(tmp_path), POLICY)
    assert not list(tmp_path.iterdir())


def test_an_html_page_is_rejected_whatever_its_name(tmp_path):
    with pytest.raises(ScrapeError):
        finalise_download(write(tmp_path, b"<!doctype html><p>busy"), ref(".txt"), "a.txt", str(tmp_path), POLICY)


async def test_write_capped_removes_the_partial_file(tmp_path):
    async def chunks():
        for _ in range(5):
            yield b"x" * 400

    target = tmp_path / ".part"
    with pytest.raises(TooLarge):
        await write_capped(chunks(), str(target), max_bytes=1_000, what="x")
    assert not target.exists()
    assert await write_capped(chunks(), str(target), max_bytes=2_000, what="x") == 2_000


def test_error_to_raise_prefers_a_retryable_error():
    too_large, slow = TooLarge("a"), PortalUnavailable("b")
    assert error_to_raise([too_large, slow]) is slow
    assert error_to_raise([too_large]) is too_large
    assert isinstance(error_to_raise([KeyboardInterrupt()]), ScrapeError)


@pytest.mark.parametrize(
    ("value", "low", "high"),
    [("120", 120, 120), ("0", 0, 0), ("soon", None, None), ("", None, None), ("-5", None, None)],
)
def test_retry_after_seconds(value, low, high):
    got = http.retry_after_s(httpx.Headers({"Retry-After": value} if value else {}))
    assert got == low == high if low is None else low <= got <= high


def test_retry_after_dates():
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=300), usegmt=True)
    earlier = format_datetime(datetime.now(UTC) - timedelta(seconds=300), usegmt=True)
    assert 290 <= http.retry_after_s(httpx.Headers({"Retry-After": later})) <= 300
    assert http.retry_after_s(httpx.Headers({"Retry-After": earlier})) == 0


@pytest.mark.parametrize(
    ("status", "error", "retry_after"),
    [
        (500, PortalUnavailable, None),
        (502, PortalUnavailable, None),
        (503, PortalUnavailable, 30.0),
        (429, PortalUnavailable, 30.0),
        (400, ProviderRejected, None),
        (403, ProviderRejected, None),
        (401, ScrapeError, None),
        (404, ScrapeError, None),
        (302, ScrapeError, None),
    ],
)
def test_status_classification(status, error, retry_after):
    r = httpx.Response(status, headers={"Retry-After": "30", "Location": "https://x.example/"})
    with pytest.raises(error) as info:
        http.raise_for_status(r, "x")
    assert type(info.value) is error and info.value.retry_after == retry_after


def test_clients_do_not_follow_redirects():
    assert http.make_client("https://example.com/").follow_redirects is False
