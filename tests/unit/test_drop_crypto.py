"""Our encoder vs. an independent port of drop's own download path.

`browser_decrypt` is a line-by-line port of downloadChunked() + b642buf() in drop's
frontend/download.html, and `server_frames` of the chunked branch of GET /api/file/:id in
src/index.ts. If these round-trip, the unmodified drop download page can open our links.
"""

import base64
import hashlib
import json
import os
import re
import struct
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.delivery.drop import CHUNK_SIZE, DropClient, DropRejected, key_fragment

UPLOAD = "http://drop.internal"
PUBLIC = "https://drop.example"
FILE_ID = "abc123DEF_-9"


def b642buf(b64: str) -> bytes:
    """download.html: atob(b64.replace(/-/g,'+').replace(/_/g,'/') + '='.repeat((4-len%4)%4))."""
    padded = b64.replace("-", "+").replace("_", "/") + "=" * ((4 - len(b64) % 4) % 4)
    return base64.b64decode(padded, validate=True)


def buf2b64(buf: bytes) -> str:
    """app.js: btoa(...).replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'')."""
    return re.sub("=+$", "", base64.b64encode(buf).decode().replace("+", "-").replace("/", "_"))


def server_frames(chunks: dict[int, bytes], total_chunks: int) -> bytes:
    """index.ts: for i in 0..totalChunks: [uint32 BE length][chunk file bytes]."""
    return b"".join(struct.pack(">I", len(chunks[i])) + chunks[i] for i in range(total_chunks))


def browser_decrypt(stream: bytes, fragment: str, total_chunks: int) -> bytes:
    """downloadChunked(): frame = peekU32 (BE), iv = payload[0:12], ct = payload[12:]."""
    key = AESGCM(b642buf(fragment))  # importKey('raw', ..., {name:'AES-GCM'}) needs 16/24/32 bytes
    parts: list[bytes] = []
    pos = 0
    while len(stream) - pos >= 4:
        (frame_len,) = struct.unpack(">I", stream[pos : pos + 4])
        if len(stream) - pos < 4 + frame_len:
            break
        payload = stream[pos + 4 : pos + 4 + frame_len]
        pos += 4 + frame_len
        parts.append(key.decrypt(payload[:12], payload[12:], None))
    assert len(parts) == total_chunks, f"Incomplete download: got {len(parts)}/{total_chunks} chunks"
    return b"".join(parts)


class FakeDrop:
    """Records what the client sends, enforcing the same preconditions drop's handlers do."""

    def __init__(self, router: respx.MockRouter):
        self.init: dict = {}
        self.chunks: dict[int, bytes] = {}
        self.completed = False
        router.post(f"{UPLOAD}/api/upload/init").mock(side_effect=self._init)
        router.put(url__regex=rf"^{UPLOAD}/api/upload/{FILE_ID}/(?P<index>\d+)$").mock(side_effect=self._put)
        router.post(f"{UPLOAD}/api/upload/{FILE_ID}/complete").mock(side_effect=self._complete)

    def _init(self, request: httpx.Request) -> httpx.Response:
        self.init = json.loads(request.content)
        assert all(self.init[k] for k in ("fileName", "fileSize", "totalChunks", "chunkSize"))
        return httpx.Response(200, json={"id": FILE_ID, "deleteToken": "t" * 24, "expiresAt": 1_900_000_000})

    def _put(self, request: httpx.Request, index: str) -> httpx.Response:
        assert int(index) < self.init["totalChunks"]
        self.chunks.setdefault(int(index), request.content)
        return httpx.Response(200, json={"ok": True})

    def _complete(self, request: httpx.Request) -> httpx.Response:
        assert sorted(self.chunks) == list(range(self.init["totalChunks"]))
        self.completed = True
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def fake_drop():
    with respx.mock(assert_all_called=False) as router:
        yield FakeDrop(router)


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as http:
        yield DropClient(
            upload_url=UPLOAD, public_url=PUBLIC, expiry_s=3600, max_downloads=5, http=http, backoff_base_s=0
        )


@pytest.mark.parametrize("size", [1, CHUNK_SIZE, CHUNK_SIZE + 1, int(5.5 * CHUNK_SIZE)])
async def test_round_trip_through_drop_download_path(
    size: int, tmp_path: Path, fake_drop: FakeDrop, client: DropClient
) -> None:
    data = os.urandom(size)
    src = tmp_path / "package.zip"
    src.write_bytes(data)

    link = await client.upload(str(src), "M12205 Other Documents.zip")

    total = -(-size // CHUNK_SIZE)
    assert fake_drop.completed
    assert fake_drop.init == {
        "fileName": "M12205 Other Documents.zip.enc",
        "fileSize": size + total * 28,
        "totalChunks": total,
        "chunkSize": CHUNK_SIZE,
        "expiresIn": 3600,
        "maxDownloads": 5,
        "passwordProtected": False,
        "encrypted": True,
    }
    assert sum(len(c) for c in fake_drop.chunks.values()) == fake_drop.init["fileSize"]
    assert len({c[:12] for c in fake_drop.chunks.values()}) == total  # fresh IV per chunk

    assert re.fullmatch(rf"{PUBLIC}/d/{FILE_ID}#[A-Za-z0-9_-]{{43}}", link.url)
    fragment = link.url.split("#", 1)[1]
    plain = browser_decrypt(server_frames(fake_drop.chunks, total), fragment, total)
    assert hashlib.sha256(plain).digest() == hashlib.sha256(data).digest()
    assert link.size == size and link.id == FILE_ID and link.max_downloads == 5


async def test_tampered_chunk_fails_authentication(
    tmp_path: Path, fake_drop: FakeDrop, client: DropClient
) -> None:
    src = tmp_path / "package.zip"
    src.write_bytes(os.urandom(CHUNK_SIZE + 10))
    link = await client.upload(str(src), "p.zip")

    chunk = bytearray(fake_drop.chunks[1])
    chunk[-1] ^= 0x01
    fake_drop.chunks[1] = bytes(chunk)
    with pytest.raises(InvalidTag):
        browser_decrypt(server_frames(fake_drop.chunks, 2), link.url.split("#", 1)[1], 2)


async def test_refuses_empty_file_without_calling_drop(tmp_path: Path, client: DropClient) -> None:
    src = tmp_path / "empty.zip"
    src.touch()
    with respx.mock(assert_all_mocked=True) as router:
        with pytest.raises(DropRejected, match="empty"):
            await client.upload(str(src), "empty.zip")
        assert router.calls.call_count == 0


def test_key_fragment_matches_browser_encoding() -> None:
    for _ in range(50):
        key = AESGCM.generate_key(bit_length=256)
        fragment = key_fragment(key)
        assert fragment == buf2b64(key) and "=" not in fragment and len(fragment) == 43
        assert b642buf(fragment) == key
