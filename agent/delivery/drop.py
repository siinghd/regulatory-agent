"""Client for drop (drop.hsingh.app): end-to-end-encrypted, expiring, download-capped links.

The wire format mirrors drop's own browser uploader (frontend/src/app.js) so links open in its
unmodified download page (/d/:id):
- the file is cut into 1 MiB plaintext chunks; chunk i is uploaded as iv(12) || ciphertext || tag(16),
  AES-256-GCM under one random key with a fresh random IV per chunk;
- the key travels only in the URL fragment as unpadded base64url (drop's buf2b64), which browsers
  never send to a server, so drop stores ciphertext it cannot read;
- the download page streams /api/file/:id (frames of u32-BE length + chunk) and decrypts each frame.
"""

import asyncio
import base64
import os
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

import httpx
import structlog
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.config import Settings, get_settings
from agent.delivery.package import safe_filename
from agent.models import AgentError

log = structlog.get_logger()

CHUNK_SIZE = 1024 * 1024
IV_BYTES = 12
TAG_BYTES = 16
MAX_ENCRYPTED_BYTES = 500 * 1024 * 1024  # drop's MAX_FILE_SIZE, checked against the encrypted size
MAX_EXPIRY_S = 7 * 24 * 3600  # drop silently clamps expiresIn to [60, 604800]
MAX_DOWNLOADS_CAP = 1000  # ... and maxDownloads to [0, 1000]

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{6,64}")  # drop ids are nanoid(12)


class DropError(AgentError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class DropUnavailable(DropError):
    """Drop is down, busy, rate-limiting us, or lost the upload; a later attempt can succeed."""

    retryable = True
    user_message = "Our secure file-sharing service is temporarily unavailable. I'll retry shortly."


class DropRejected(DropError):
    """Drop refused the upload itself (too large, malformed); retrying won't change that."""

    retryable = False
    user_message = "The document package couldn't be uploaded to our secure file-sharing service."


@dataclass(frozen=True, slots=True)
class DropLink:
    url: str = field(repr=False)  # the fragment is the decryption key: keep it out of logs
    id: str
    delete_token: str = field(repr=False)
    expires_at: datetime
    size: int  # plaintext bytes the recipient downloads
    max_downloads: int  # 0 = unlimited


@dataclass(frozen=True, slots=True)
class _Session:
    id: str
    delete_token: str = field(repr=False)
    expires_at: datetime


def chunk_count(plain_size: int) -> int:
    return -(-plain_size // CHUNK_SIZE)


def encrypted_size(plain_size: int) -> int:
    return plain_size + chunk_count(plain_size) * (IV_BYTES + TAG_BYTES)


def encrypt_chunk(key: bytes, plaintext: bytes) -> bytes:
    iv = os.urandom(IV_BYTES)
    return iv + AESGCM(key).encrypt(iv, plaintext, None)


def key_fragment(key: bytes) -> str:
    """Unpadded base64url, byte-for-byte what drop's buf2b64() produces."""
    return base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")


def share_url(public_url: str, file_id: str, key: bytes) -> str:
    return f"{public_url.rstrip('/')}/d/{file_id}#{key_fragment(key)}"


class DropClient:
    """Uploads one file per call; safe to share across concurrent deliveries."""

    def __init__(
        self,
        *,
        upload_url: str,
        public_url: str,
        expiry_s: int,
        max_downloads: int,
        http: httpx.AsyncClient | None = None,
        concurrency: int = 3,
        max_attempts: int = 4,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 20.0,
        timeout_s: float = 300.0,
    ):
        # Validate instead of letting drop clamp silently: the email promises these numbers.
        if not 60 <= expiry_s <= MAX_EXPIRY_S:
            raise ValueError(f"expiry_s must be within [60, {MAX_EXPIRY_S}], got {expiry_s}")
        if not 0 <= max_downloads <= MAX_DOWNLOADS_CAP:
            raise ValueError(f"max_downloads must be within [0, {MAX_DOWNLOADS_CAP}], got {max_downloads}")
        if concurrency < 1 or max_attempts < 1:
            raise ValueError("concurrency and max_attempts must be >= 1")
        self._upload_url = upload_url.rstrip("/")
        self._public_url = public_url.rstrip("/")
        self._expiry_s = expiry_s
        self._max_downloads = max_downloads
        self._http = http
        self._owns_http = http is None
        self._concurrency = concurrency
        self._max_attempts = max_attempts
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s
        self._timeout_s = timeout_s

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, http: httpx.AsyncClient | None = None
    ) -> "DropClient":
        s = settings or get_settings()
        return cls(
            upload_url=s.drop_upload_url,
            public_url=s.drop_base_url,
            expiry_s=s.drop_expiry_s,
            max_downloads=s.drop_max_downloads,
            http=http,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def health(self) -> bool:
        try:
            resp = await self._client().get(f"{self._upload_url}/health", timeout=5.0)
            body = resp.json() if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError) as exc:
            log.info("drop.health_failed", error=type(exc).__name__)
            return False
        return isinstance(body, dict) and body.get("status") == "ok"

    async def upload(self, path: str, display_name: str) -> DropLink:
        """Encrypt and upload `path`. Raises DropUnavailable (retryable) or DropRejected."""
        plain_size = os.path.getsize(path)
        if plain_size == 0:
            raise DropRejected("refusing to upload an empty file (drop rejects fileSize 0)")
        if encrypted_size(plain_size) > MAX_ENCRYPTED_BYTES:
            raise DropRejected(f"{plain_size} bytes exceeds drop's 500 MiB limit", status=413)

        key = AESGCM.generate_key(bit_length=256)
        try:
            async with asyncio.timeout(self._timeout_s):
                session = await self._init(display_name, plain_size)
                await self._transfer(session, path, plain_size, key)
        except TimeoutError as exc:
            raise DropUnavailable(f"drop upload did not finish within {self._timeout_s:.0f}s") from exc

        log.info("drop.uploaded", file_id=session.id, size=plain_size, chunks=chunk_count(plain_size))
        return DropLink(
            url=share_url(self._public_url, session.id, key),
            id=session.id,
            delete_token=session.delete_token,
            expires_at=session.expires_at,
            size=plain_size,
            max_downloads=self._max_downloads,
        )

    async def _init(self, display_name: str, plain_size: int) -> _Session:
        payload = {
            "fileName": safe_filename(display_name) + ".enc",  # the download page strips ".enc"
            "fileSize": encrypted_size(plain_size),
            "totalChunks": chunk_count(plain_size),
            "chunkSize": CHUNK_SIZE,
            "expiresIn": self._expiry_s,
            "maxDownloads": self._max_downloads,
            "passwordProtected": False,
            "encrypted": True,
        }
        resp = await self._send("POST", "/api/upload/init", what="init", json=payload)
        try:
            body = resp.json()
            session = _Session(
                id=str(body["id"]),
                delete_token=str(body["deleteToken"]),
                expires_at=datetime.fromtimestamp(int(body["expiresAt"]), tz=UTC),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise DropRejected(f"drop init: unexpected response {resp.text[:200]!r}") from exc
        # The id is interpolated into request paths and the emailed URL.
        if not _FILE_ID.fullmatch(session.id):
            raise DropRejected(f"drop init: malformed file id {session.id[:80]!r}")
        return session

    async def _transfer(self, session: _Session, path: str, plain_size: int, key: bytes) -> None:
        # drop frees an upload's concurrency slot only on /complete or DELETE (its stale-upload
        # sweep never does), so any failure here, including timeout or cancellation, must delete.
        try:
            await self._send_chunks(session.id, path, plain_size, key)
            await self._send("POST", f"/api/upload/{session.id}/complete", what="complete")
        except BaseException:
            await self._discard(session)
            raise

    async def _send_chunks(self, file_id: str, path: str, plain_size: int, key: bytes) -> None:
        total = chunk_count(plain_size)
        indices = iter(range(total))  # shared by all workers: <= `concurrency` chunks in memory
        try:
            async with asyncio.TaskGroup() as tg:
                for _ in range(min(self._concurrency, total)):
                    tg.create_task(self._chunk_worker(file_id, path, plain_size, key, indices))
        except ExceptionGroup as eg:
            raise _primary_error(eg) from eg

    async def _chunk_worker(
        self, file_id: str, path: str, plain_size: int, key: bytes, indices: Iterator[int]
    ) -> None:
        for index in indices:
            body = await asyncio.to_thread(_read_encrypted_chunk, path, index, plain_size, key)
            # Retries resend these exact bytes; drop answers ok for a chunk it already stored.
            await self._send(
                "PUT",
                f"/api/upload/{file_id}/{index}",
                what=f"chunk {index}",
                content=body,
                headers={"Content-Type": "application/octet-stream"},
            )

    async def _send(self, method: str, path: str, *, what: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._upload_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client().request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self._max_attempts:
                    raise DropUnavailable(f"drop {what}: {type(exc).__name__}: {exc}") from exc
                reason = type(exc).__name__
            else:
                if resp.status_code not in _RETRY_STATUSES or attempt >= self._max_attempts:
                    _raise_for_status(resp, what)
                    return resp
                reason = f"HTTP {resp.status_code}"
            log.warning("drop.retry", what=what, attempt=attempt, reason=reason)
            await asyncio.sleep(self._backoff_s(attempt))

    def _backoff_s(self, attempt: int) -> float:
        # Equal jitter: always waits at least half the step (drop's rate-limit window is a fixed
        # 60 s, so near-zero sleeps just burn attempts) while desynchronising parallel workers.
        step = min(self._backoff_cap_s, self._backoff_base_s * 2 ** (attempt - 1))
        return step / 2 + random.uniform(0, step / 2)

    async def _discard(self, session: _Session) -> None:
        """Best-effort delete of a half-made upload; never masks the error being propagated."""
        try:
            resp = await self._client().delete(
                f"{self._upload_url}/api/file/{session.id}",
                headers={"X-Delete-Token": session.delete_token},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            log.warning("drop.discard_failed", file_id=session.id, error=type(exc).__name__)
            return
        if resp.status_code not in (200, 404):
            log.warning("drop.discard_failed", file_id=session.id, status=resp.status_code)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            # trust_env=False: drop is on this host; never route uploads via an env-configured proxy.
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0), trust_env=False)
        return self._http


def _read_encrypted_chunk(path: str, index: int, plain_size: int, key: bytes) -> bytes:
    offset = index * CHUNK_SIZE
    length = min(CHUNK_SIZE, plain_size - offset)
    with open(path, "rb") as fh:
        fh.seek(offset)
        plaintext = fh.read(length)
    if len(plaintext) != length:
        raise OSError(f"short read at chunk {index}: file changed during upload")
    return encrypt_chunk(key, plaintext)


def _raise_for_status(resp: httpx.Response, what: str) -> None:
    status = resp.status_code
    if status < 400:
        return
    detail = f"drop {what}: HTTP {status} {resp.text[:200]}"
    # 404 mid-upload means drop lost the session (restart, or its 10-minute stale sweep) and 507 is
    # a full disk: both can succeed later from scratch, unlike a request drop refuses outright.
    if status >= 500 or status in (404, 408, 429):
        raise DropUnavailable(detail, status=status)
    raise DropRejected(detail, status=status)


def _primary_error(eg: ExceptionGroup) -> Exception:
    """TaskGroup wraps worker failures, but callers expect the typed DropError itself.

    The first failure cancels the other workers, so any further members are near-simultaneous
    reports of the same problem; the whole group stays attached as __cause__.
    """
    return next((e for e in eg.exceptions if isinstance(e, DropError)), eg.exceptions[0])
