"""Downloaded files, shared by every provider: size-capped streaming, content checks (no HTML error
pages, magic bytes), the file-type allowlist, and turning a verified temp file into a
content-addressed DownloadedFile."""

import contextlib
import hashlib
import os
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass

import anyio

from agent.config import get_settings
from agent.models import (
    AgentError,
    DocumentRef,
    DownloadedFile,
    PortalUnavailable,
    ProviderRejected,
    ScrapeError,
    TooLarge,
)

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # legacy Office (.doc, .xls)
_ZIP = b"PK\x03\x04"  # Office Open XML (.docx, .xlsx, .xlsm)
# What a file of each type must start with (any one of). Other types are only checked for HTML.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (_ZIP,),
    ".xlsx": (_ZIP,),
    ".xlsm": (_ZIP,),
    ".doc": (_OLE, b"{\\rtf"),  # old Word files are sometimes RTF under a .doc name
    ".xls": (_OLE,),
}
# An error or login page served in place of the file, whatever it claims to be.
_HTML = (b"<!doctype html", b"<html", b"<head", b"<body")
_HEAD_BYTES = 512


class UnsupportedFileType(ProviderRejected):
    """The portal serves this document as a file type we don't deliver (not in allowed_file_exts)."""

    user_message = "The regulator only offers these documents in file types I can't deliver."


@dataclass(frozen=True)
class FilePolicy:
    """Per-file budget and allowlist (Settings.max_file_bytes / allowed_file_exts)."""

    max_bytes: int
    allowed_exts: frozenset[str]

    @classmethod
    def of(cls, max_bytes: int, allowed_exts: Iterable[str]) -> "FilePolicy":
        return cls(max_bytes, frozenset(e.lower() for e in allowed_exts))

    @classmethod
    def from_settings(cls) -> "FilePolicy":
        s = get_settings()
        return cls.of(s.max_file_bytes, s.allowed_file_exts)

    def check_size(self, size: int | None, what: str) -> None:
        """Raise TooLarge for a known size over budget (None: unknown, checked while streaming)."""
        if size is not None and size > self.max_bytes:
            raise TooLarge(f"{what}: {size} bytes is over the {self.max_bytes}-byte limit per file")

    def allows(self, ext: str) -> bool:
        return ext.lower() in self.allowed_exts

    def check_ext(self, ext: str, what: str) -> None:
        if not self.allows(ext):
            raise UnsupportedFileType(f"{what}: file type {ext or '(none)'!r} is not delivered")


async def write_capped(chunks: AsyncIterable[bytes], tmp: str, *, max_bytes: int, what: str) -> int:
    """Stream `chunks` into `tmp`, aborting with TooLarge past `max_bytes`. Returns the size.

    The partial file is removed on any failure (including cancellation by a deadline)."""
    written = 0
    try:
        async with await anyio.open_file(tmp, "wb") as f:
            async for chunk in chunks:
                written += len(chunk)
                if written > max_bytes:
                    raise TooLarge(f"{what}: over the {max_bytes}-byte limit per file")
                await f.write(chunk)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        raise
    return written


def error_to_raise(errors: list[BaseException]) -> BaseException:
    """When every file of a download failed: a retryable error if there is one (a later try may
    get the files), else the first (e.g. TooLarge, which the user should hear about)."""
    retryable = next((e for e in errors if isinstance(e, AgentError) and e.retryable), None)
    if retryable is not None:
        return retryable
    first = errors[0]
    return first if isinstance(first, Exception) else ScrapeError(str(first))


def content_problem(head: bytes, ext: str) -> str | None:
    """Why the first bytes of a file can't be a real file of type `ext`, or None if they can."""
    sniff = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if sniff.startswith(_HTML):
        return "got an HTML page instead of the file"
    magic = _MAGIC.get(ext)
    if magic and not head.startswith(magic):
        return f"expected {ext} but got {head[:8]!r}"
    return None


def finalise_download(
    tmp: str, ref: DocumentRef, suggested: str, dest_dir: str, policy: FilePolicy | None = None
) -> DownloadedFile:
    """Check `tmp` is a real file of an allowed type and within budget, then move it to `<sha256><ext>`.

    `suggested` is the filename the portal served; its extension wins over the listing's. Every
    failure removes `tmp`.
    """
    policy = policy or FilePolicy.from_settings()
    try:
        size = os.path.getsize(tmp)
        if size == 0:
            raise PortalUnavailable(f"empty download for {ref.external_id}")
        policy.check_size(size, ref.external_id)
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            head = f.read(_HEAD_BYTES)
            h.update(head)
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        ext = (os.path.splitext(suggested)[1] or ref.file_ext).lower()
        if not ext and head.startswith(b"%PDF-"):
            ext = ".pdf"
        problem = content_problem(head, ext)
        if problem:
            raise ScrapeError(f"{ref.external_id}: {problem}")
        policy.check_ext(ext, ref.external_id)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp)
        raise
    sha = h.hexdigest()
    final = os.path.join(dest_dir, f"{sha}{ext}")
    os.replace(tmp, final)
    return DownloadedFile(
        ref=ref, path=final, sha256=sha, size=size, filename=f"{ref.external_id}{ext}"
    )
