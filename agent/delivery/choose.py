"""Decide how the ZIP reaches the requester.

Policy, in order:
1. drop link: end-to-end encrypted, expiring and download-capped; keeps large ZIPs out of mailboxes.
2. drop failed (or isn't configured) and the ZIP fits in an email: attach it.
3. otherwise DeliveryDeferred (retryable), so the queue retries later instead of us sending a
   partial set; a non-retryable DropRejected for an oversized ZIP propagates as is.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import structlog

from agent.config import get_settings
from agent.delivery.drop import DropClient, DropError, DropLink, DropUnavailable
from agent.delivery.package import ZipResult, safe_filename
from agent.models import AgentError, DownloadedFile

log = structlog.get_logger()

DeliveryKind = Literal["link", "attachment"]


class DeliveryDeferred(AgentError):
    retryable = True
    user_message = (
        "Your documents are ready, but they're too large to email and our secure file-sharing "
        "service is unavailable right now. I'll send the download link as soon as it's back."
    )


@dataclass(frozen=True, slots=True)
class Delivery:
    """What the reply composer needs. kind="link" carries `link`; kind="attachment" carries `path`."""

    kind: DeliveryKind
    filename: str  # what the recipient sees: the attachment name or the downloaded file's name
    size: int  # bytes of the ZIP
    sha256: str
    file_count: int
    link: DropLink | None = None  # url, expires_at, max_downloads, delete_token
    path: str | None = None
    fallback_reason: str | None = None  # why drop wasn't used, for logs and ops

    def __post_init__(self) -> None:
        is_link = self.link is not None and self.path is None
        is_attachment = self.link is None and bool(self.path)
        if not (is_link if self.kind == "link" else is_attachment):
            raise ValueError(f"inconsistent Delivery: kind={self.kind} link={self.link} path={self.path}")


async def deliver(
    zip_result: ZipResult,
    files: Sequence[DownloadedFile],
    *,
    drop: DropClient | None,
    attach_max_bytes: int | None = None,
) -> Delivery:
    if zip_result.file_count != len(files):
        raise ValueError(f"ZIP holds {zip_result.file_count} documents but {len(files)} were passed")
    limit = get_settings().attach_inline_max_bytes if attach_max_bytes is None else attach_max_bytes
    filename = _display_name(files)

    try:
        link = await _upload(drop, zip_result.path, filename)
    except DropError as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if zip_result.size <= limit:
            log.warning("delivery.attachment_fallback", reason=reason, size=zip_result.size)
            return Delivery(
                kind="attachment",
                filename=filename,
                size=zip_result.size,
                sha256=zip_result.sha256,
                file_count=zip_result.file_count,
                path=zip_result.path,
                fallback_reason=reason,
            )
        if exc.retryable:
            raise DeliveryDeferred(f"{reason}; ZIP is {zip_result.size} B, attach limit {limit} B") from exc
        raise
    return Delivery(
        kind="link",
        filename=filename,
        size=zip_result.size,
        sha256=zip_result.sha256,
        file_count=zip_result.file_count,
        link=link,
    )


async def _upload(drop: DropClient | None, path: str, filename: str) -> DropLink:
    if drop is None:
        raise DropUnavailable("drop is not configured")
    return await drop.upload(path, filename)


def _display_name(files: Sequence[DownloadedFile]) -> str:
    """Name the package after what was asked for, e.g. "M12205 Other Documents.zip"."""
    scopes = {(f.ref.matter, f.ref.doc_type) for f in files}
    if len(scopes) == 1:
        matter, doc_type = scopes.pop()
        return safe_filename(f"{matter} {doc_type}", ext=".zip")
    return "documents.zip"
