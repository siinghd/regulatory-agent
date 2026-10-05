"""Shared plumbing for the plain-HTTP providers (OEB, FERC): one client policy, one status-code
classification, and bounded file downloads.

Status codes: 5xx and 429 are the portal being unavailable (retried, honouring Retry-After); 400
and 403 are the portal refusing the request (ProviderRejected: retrying won't change the answer);
redirects are never followed (a moved API or a login page must not be read as data); anything
else unexpected is a ScrapeError (retried).
"""

import asyncio
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from agent.models import PortalUnavailable, ProviderRejected, ScrapeError
from agent.providers.files import FilePolicy, write_capped

USER_AGENT = "regulatory-agent/0.1 (+https://uarb.hsingh.app/bot)"
DOWNLOAD_TIMEOUT_S = 600.0  # one file, start to finish (per-read stalls are bounded by the client)
_REJECTED = frozenset({400, 403})


def make_client(base_url: str, proxy: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        proxy=proxy,
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(60, connect=10),
        follow_redirects=False,
    )


def retry_after_s(headers: httpx.Headers) -> float | None:
    """Seconds asked for by a Retry-After header (delta-seconds or an HTTP date), if any."""
    value = (headers.get("retry-after") or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def raise_for_status(r: httpx.Response, what: str) -> None:
    status = r.status_code
    if status == 200:
        return
    if status >= 500 or status == 429:
        hint = retry_after_s(r.headers) if status in (429, 503) else None
        raise PortalUnavailable(f"{what}: HTTP {status}").with_retry_after(hint)
    if status in _REJECTED:
        raise ProviderRejected(f"{what}: HTTP {status}")
    if r.is_redirect:
        raise ScrapeError(f"{what}: HTTP {status} redirect to {r.headers.get('location')!r} (not followed)")
    raise ScrapeError(f"{what}: HTTP {status}")


def served_filename(headers: httpx.Headers) -> str:
    msg = Message()
    msg["content-disposition"] = headers.get("content-disposition", "")
    return msg.get_filename() or ""


def content_length(headers: httpx.Headers) -> int | None:
    value = headers.get("content-length", "")
    return int(value) if value.isdigit() else None


async def download_to(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    tmp: str,
    *,
    what: str,
    policy: FilePolicy,
    deadline_s: float = DOWNLOAD_TIMEOUT_S,
    error_types: tuple[str, ...] = ("text/html",),
    **kwargs: Any,
) -> httpx.Headers:
    """Stream one file into `tmp` within `deadline_s` and the policy's size cap; return the headers.

    A declared Content-Length over the cap stops before the body is read; an undeclared or
    understated one is caught while streaming. Responses whose Content-Type starts with one of
    `error_types` are error pages, not files. No partial file is left behind on failure.
    """
    try:
        async with asyncio.timeout(deadline_s):
            async with client.stream(method, url, **kwargs) as r:
                raise_for_status(r, what)
                content_type = r.headers.get("content-type", "")
                if content_type.startswith(error_types):
                    raise ScrapeError(f"{what}: got {content_type} instead of the file")
                policy.check_size(content_length(r.headers), what)
                await write_capped(r.aiter_bytes(), tmp, max_bytes=policy.max_bytes, what=what)
                return r.headers
    except TimeoutError as e:
        raise PortalUnavailable(f"{what}: not finished within {deadline_s:.0f} s") from e
    except httpx.RequestError as e:
        raise PortalUnavailable(f"{what}: {e!r}") from e
