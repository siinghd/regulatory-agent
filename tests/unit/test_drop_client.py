"""Retry, error classification and cleanup behaviour of DropClient against a mocked drop."""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from agent.delivery.drop import CHUNK_SIZE, DropClient, DropRejected, DropUnavailable

UPLOAD = "http://drop.internal"
FILE_ID = "abc123DEF_-9"
TOKEN = "delete-token-xxxxxxxxxxx"
INIT_OK = httpx.Response(200, json={"id": FILE_ID, "deleteToken": TOKEN, "expiresAt": 1_900_000_000})
OK = httpx.Response(200, json={"ok": True})


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


def make_client(http: httpx.AsyncClient, **overrides: float) -> DropClient:
    params: dict = {"max_attempts": 3, "backoff_base_s": 0, "timeout_s": 10} | overrides
    return DropClient(
        upload_url=UPLOAD,
        public_url="https://drop.example",
        expiry_s=3600,
        max_downloads=5,
        http=http,
        **params,
    )


@pytest.fixture
def src(tmp_path: Path) -> str:
    path = tmp_path / "package.zip"
    path.write_bytes(os.urandom(2 * CHUNK_SIZE + 7))
    return str(path)


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as r:
        r.post(f"{UPLOAD}/api/upload/init", name="init").mock(return_value=INIT_OK)
        r.put(url__regex=rf"^{UPLOAD}/api/upload/{FILE_ID}/\d+$", name="put").mock(return_value=OK)
        r.post(f"{UPLOAD}/api/upload/{FILE_ID}/complete", name="complete").mock(return_value=OK)
        r.delete(f"{UPLOAD}/api/file/{FILE_ID}", name="delete").mock(
            return_value=httpx.Response(200, json={"success": True})
        )
        r.get(f"{UPLOAD}/health", name="health").mock(
            return_value=httpx.Response(200, json={"status": "ok", "active_uploads": 0})
        )
        yield r


async def test_429_then_success(router: respx.MockRouter, http: httpx.AsyncClient, src: str) -> None:
    router["init"].side_effect = [httpx.Response(429, json={"error": "Too many uploads."}), INIT_OK]

    link = await make_client(http).upload(src, "p.zip")

    assert router["init"].call_count == 2
    assert router["put"].call_count == 3 and router["complete"].called
    assert link.expires_at == datetime.fromtimestamp(1_900_000_000, tz=UTC)
    assert not router["delete"].called


async def test_503_exhausts_retries(router: respx.MockRouter, http: httpx.AsyncClient, src: str) -> None:
    router["init"].mock(return_value=httpx.Response(503, json={"error": "Server busy."}))

    with pytest.raises(DropUnavailable) as exc_info:
        await make_client(http).upload(src, "p.zip")

    assert exc_info.value.retryable and exc_info.value.status == 503
    assert router["init"].call_count == 3
    assert not router["put"].called and not router["delete"].called


async def test_413_is_rejected_without_retry(
    router: respx.MockRouter, http: httpx.AsyncClient, src: str
) -> None:
    router["init"].mock(return_value=httpx.Response(413, json={"error": "File too large."}))

    with pytest.raises(DropRejected) as exc_info:
        await make_client(http).upload(src, "p.zip")

    assert not exc_info.value.retryable and exc_info.value.status == 413
    assert router["init"].call_count == 1


async def test_chunk_retry_resends_identical_bytes(
    router: respx.MockRouter, http: httpx.AsyncClient, src: str
) -> None:
    bodies: list[bytes] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/0"):
            bodies.append(request.content)
            if len(bodies) == 1:
                return httpx.Response(500, json={"error": "Chunk upload failed"})
        return OK

    router["put"].side_effect = flaky
    await make_client(http).upload(src, "p.zip")

    assert len(bodies) == 2 and bodies[0] == bodies[1]  # same IV and ciphertext: idempotent
    assert router["put"].call_count == 4


async def test_failed_chunk_deletes_the_upload(
    router: respx.MockRouter, http: httpx.AsyncClient, src: str
) -> None:
    router["put"].mock(return_value=httpx.Response(503, json={"error": "busy"}))

    with pytest.raises(DropUnavailable):
        await make_client(http).upload(src, "p.zip")

    assert router["delete"].call_count == 1
    assert router["delete"].calls.last.request.headers["X-Delete-Token"] == TOKEN
    assert not router["complete"].called


async def test_lost_session_is_retryable_but_not_retried_inline(
    router: respx.MockRouter, http: httpx.AsyncClient, src: str
) -> None:
    # drop answers 404 to chunk PUTs once its in-memory session is gone (restart / stale sweep).
    router["put"].mock(
        return_value=httpx.Response(404, json={"error": "Upload not found or already complete"})
    )

    with pytest.raises(DropUnavailable) as exc_info:
        await make_client(http, concurrency=1).upload(src, "p.zip")

    assert exc_info.value.status == 404
    assert router["put"].call_count == 1
    assert router["delete"].called


async def test_transport_error_is_retried(
    router: respx.MockRouter, http: httpx.AsyncClient, src: str
) -> None:
    router["complete"].side_effect = [httpx.ConnectError("refused"), OK]

    await make_client(http).upload(src, "p.zip")

    assert router["complete"].call_count == 2


async def test_overall_timeout_cleans_up(router: respx.MockRouter, http: httpx.AsyncClient, src: str) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return OK

    router["put"].side_effect = hang

    with pytest.raises(DropUnavailable, match="did not finish"):
        await make_client(http, timeout_s=0.2).upload(src, "p.zip")

    assert router["delete"].called


async def test_malformed_file_id_is_rejected(
    router: respx.MockRouter, http: httpx.AsyncClient, src: str
) -> None:
    router["init"].mock(
        return_value=httpx.Response(200, json={"id": "../x", "deleteToken": "t", "expiresAt": 1})
    )

    with pytest.raises(DropRejected, match="malformed file id"):
        await make_client(http).upload(src, "p.zip")


async def test_link_repr_hides_key_and_delete_token(
    router: respx.MockRouter, http: httpx.AsyncClient, src: str
) -> None:
    link = await make_client(http).upload(src, "p.zip")

    assert link.url.split("#", 1)[1] not in repr(link)
    assert TOKEN not in repr(link)


async def test_health(router: respx.MockRouter, http: httpx.AsyncClient) -> None:
    client = make_client(http)
    assert await client.health()

    router["health"].mock(return_value=httpx.Response(502, text="Bad Gateway"))
    assert not await client.health()

    router["health"].mock(side_effect=httpx.ConnectError("refused"))
    assert not await client.health()


@pytest.mark.parametrize(("expiry_s", "max_downloads"), [(59, 5), (604_801, 5), (3600, -1), (3600, 1001)])
def test_rejects_settings_drop_would_silently_clamp(expiry_s: int, max_downloads: int) -> None:
    with pytest.raises(ValueError):
        DropClient(upload_url=UPLOAD, public_url=UPLOAD, expiry_s=expiry_s, max_downloads=max_downloads)
