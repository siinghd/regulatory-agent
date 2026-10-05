"""Decide how the ZIP reaches the requester.

Policy, in order:
1. drop link: end-to-end encrypted, expiring and download-capped; keeps large ZIPs out of mailboxes.
2. drop failed (or isn't configured) and the ZIP fits in an email: attach it (never when the
   reply must be link-only, e.g. our MTA refused it as too large).
3. otherwise DeliveryDeferred (retryable), so the queue retries later instead of us sending a
   partial set; a non-retryable DropRejected for an oversized ZIP propagates as is, and with no
   drop configured at all an oversized ZIP is TooLarge (retrying can't help).

A link delivery is recorded once per request (`to_record`): the URL (whose fragment is the
decryption key) and the delete token are sealed, so a retried reply reuses the upload without
the database ever holding a working link in plaintext.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog

from agent import breaker, crypto
from agent.config import get_settings
from agent.delivery.drop import DropClient, DropError, DropLink, DropUnavailable
from agent.delivery.package import ZipResult, safe_filename
from agent.models import AgentError, DownloadedFile, TooLarge

log = structlog.get_logger()

DeliveryKind = Literal["link", "attachment"]
REUSE_MIN_VALIDITY = timedelta(hours=24)  # don't hand out a stored link that expires sooner


class DeliveryDeferred(AgentError):
    retryable = True
    dependency = "drop"
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

    def to_record(self, files_key: str) -> dict[str, Any]:
        """A storable record of a link delivery; `files_key` identifies the exact files packaged."""
        if self.link is None:
            raise ValueError("only link deliveries are recorded")
        aad = f"{self.link.id}:{files_key}"
        return {
            "kind": "link", "filename": self.filename, "size": self.size, "sha256": self.sha256,
            "file_count": self.file_count, "files": files_key, "drop_id": self.link.id,
            "expires_at": self.link.expires_at.isoformat(), "max_downloads": self.link.max_downloads,
            "url": crypto.seal_text(self.link.url, aad=aad),
            "delete_token": crypto.seal_text(self.link.delete_token, aad=aad),
        }

    @classmethod
    def from_record(cls, record: dict[str, Any] | None, files_key: str) -> "Delivery | None":
        """The recorded delivery if it packaged exactly these files and its link is still good."""
        if not record or record.get("kind") != "link" or record.get("files") != files_key:
            return None
        expires_at = datetime.fromisoformat(record["expires_at"])
        if expires_at - datetime.now(UTC) < REUSE_MIN_VALIDITY:
            return None
        aad = f"{record['drop_id']}:{files_key}"
        try:
            url = crypto.unseal_text(record["url"], aad=aad)
            token = crypto.unseal_text(record["delete_token"], aad=aad)
        except crypto.SealError:
            log.warning("delivery.record_unreadable", drop_id=record.get("drop_id"))
            return None
        link = DropLink(url=url, id=record["drop_id"], delete_token=token, expires_at=expires_at,
                        size=record["size"], max_downloads=record["max_downloads"])
        return cls(kind="link", filename=record["filename"], size=record["size"], sha256=record["sha256"],
                   file_count=record["file_count"], link=link)


async def deliver(
    zip_result: ZipResult,
    files: Sequence[DownloadedFile],
    *,
    drop: DropClient | None,
    attach_max_bytes: int | None = None,
    link_only: bool = False,
) -> Delivery:
    if zip_result.file_count != len(files):
        raise ValueError(f"ZIP holds {zip_result.file_count} documents but {len(files)} were passed")
    limit = get_settings().attach_inline_max_bytes if attach_max_bytes is None else attach_max_bytes
    filename = _display_name(files)

    try:
        link = await _upload(drop, zip_result.path, filename)
    except (DropError, breaker.Open) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if zip_result.size <= limit and not link_only:
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
        if isinstance(exc, breaker.Open):
            raise  # drop is known to be down: the pipeline parks the request until it's back
        if drop is None:
            raise TooLarge(f"ZIP is {zip_result.size} B, attach limit {limit} B, and drop is not configured") from exc
        if exc.retryable:
            deferred = DeliveryDeferred(f"{reason}; ZIP is {zip_result.size} B, attach limit {limit} B")
            raise deferred.with_retry_after(exc.retry_after) from exc
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
