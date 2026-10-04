"""Round trip against the real drop on this host: upload, download as the browser would, delete.

Run: .venv/bin/pytest -m live tests/live/test_drop_live.py -q
"""

import base64
import hashlib
import os
import re
import struct
from urllib.parse import urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent.delivery.drop import CHUNK_SIZE, DropClient

pytestmark = pytest.mark.live

DROP = os.environ.get("DROP_UPLOAD_URL", "http://127.0.0.1:3060")
PUBLIC = "https://drop.hsingh.app"


def browser_decrypt(stream: bytes, fragment: str, total_chunks: int) -> bytes:
    """Port of downloadChunked() + b642buf() from drop's download.html (see test_drop_crypto.py)."""
    padded = fragment.replace("-", "+").replace("_", "/") + "=" * ((4 - len(fragment) % 4) % 4)
    key = AESGCM(base64.b64decode(padded, validate=True))
    parts, pos = [], 0
    while len(stream) - pos >= 4:
        (frame_len,) = struct.unpack(">I", stream[pos : pos + 4])
        if len(stream) - pos < 4 + frame_len:
            break
        payload = stream[pos + 4 : pos + 4 + frame_len]
        pos += 4 + frame_len
        parts.append(key.decrypt(payload[:12], payload[12:], None))
    assert len(parts) == total_chunks
    return b"".join(parts)


@pytest.fixture
async def http():
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        yield client


@pytest.fixture
async def drop(http: httpx.AsyncClient) -> DropClient:
    # max_downloads=2: GET /api/file counts as a download and drop auto-deletes 5 s after the last
    # allowed one; keeping one spare lets this test exercise the explicit DELETE path.
    client = DropClient(upload_url=DROP, public_url=PUBLIC, expiry_s=300, max_downloads=2, http=http)
    if not await client.health():
        pytest.skip(f"drop not reachable at {DROP}")
    return client


async def test_upload_download_decrypt_delete(drop: DropClient, http: httpx.AsyncClient, tmp_path) -> None:
    data = os.urandom(int(2.5 * CHUNK_SIZE))
    src = tmp_path / "payload.zip"
    src.write_bytes(data)

    link = await drop.upload(str(src), "drop live test.zip")
    try:
        url = urlsplit(link.url)
        # What download.html reads: id = last path segment, key = location.hash.slice(1).
        assert f"{url.scheme}://{url.netloc}" == PUBLIC and url.path == f"/d/{link.id}"
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", url.fragment)

        info = (await http.get(f"{DROP}/api/info/{link.id}")).json()
        assert info["name"] == "drop live test.zip.enc"
        assert info["encrypted"] is True and info["password_protected"] is False
        assert info["upload_complete"] is True and info["chunks_received"] == 3
        assert info["total_chunks"] == 3 and info["chunk_size"] == CHUNK_SIZE
        assert info["size"] == len(data) + 3 * 28
        assert info["max_downloads"] == 2 and info["download_count"] == 0

        page = await http.get(f"{DROP}/d/{link.id}")
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]

        resp = await http.get(f"{DROP}/api/file/{link.id}")
        assert resp.status_code == 200 and resp.headers["x-total-chunks"] == "3"
        plain = browser_decrypt(resp.content, url.fragment, info["total_chunks"])
        assert hashlib.sha256(plain).hexdigest() == hashlib.sha256(data).hexdigest()
        assert (await http.get(f"{DROP}/api/info/{link.id}")).json()["download_count"] == 1
    finally:
        deleted = await http.delete(
            f"{DROP}/api/file/{link.id}", headers={"X-Delete-Token": link.delete_token}
        )

    assert deleted.status_code == 200 and deleted.json() == {"success": True}
    assert (await http.get(f"{DROP}/api/info/{link.id}")).status_code == 404


async def test_download_page_still_speaks_our_format(drop: DropClient, http: httpx.AsyncClient) -> None:
    """Canary for drop frontend changes: the served bundle must still read the key from the
    fragment, base64url-decode it, and split each frame as iv[0:12] || ciphertext."""
    page = (await http.get(f"{DROP}/d/anything")).text
    script = re.search(r'src="(/assets/download-[^"]+\.js)"', page)
    assert script, "download page no longer loads a download-*.js bundle"
    bundle = (await http.get(f"{DROP}{script.group(1)}")).text
    for needle in (
        "location.hash.slice(1)",
        'replace(/-/g,"+")',
        'replace(/_/g,"/")',
        "slice(0,12)",
        "slice(12)",
    ):
        assert needle in bundle, needle
