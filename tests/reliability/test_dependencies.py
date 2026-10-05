"""Per-dependency fault injection: drop, OEB API, UARB portal, Redis locks, Postgres pool, Redis restart."""

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import date

import arq
import asyncpg
import httpx
import pytest
import redis.exceptions
from arq.connections import RedisSettings

from agent import db, store, worker
from agent.config import get_settings
from agent.delivery.drop import DropClient, DropRejected, DropUnavailable
from agent.limits import Limits
from agent.models import (
    AgentError,
    DocumentRef,
    MatterNotFound,
    PortalUnavailable,
    ProviderRejected,
    ScrapeError,
)
from agent.providers.browser import BrowserPool
from agent.providers.oeb import API_URL, OebProvider
from agent.providers.uarb import UarbProvider
from tests.integration.harness import MATTER, body_text, make_email

pytestmark = pytest.mark.integration

REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"


# ======================================================================== drop


def drop_client(handler, monkeypatch, **kw) -> tuple[DropClient, list[float], list[tuple[str, str]]]:
    """DropClient over a mock transport. Backoff sleeps are recorded (virtual time), not slept."""
    sleeps: list[float] = []
    seen: list[tuple[str, str]] = []
    orig = DropClient._backoff_s

    def backoff(self, attempt):
        v = orig(self, attempt)
        sleeps.append(v)
        return 0

    monkeypatch.setattr(DropClient, "_backoff_s", backoff)

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return handler(request, sum(sleeps))

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    client = DropClient(upload_url="http://drop.test", public_url="https://drop.test", expiry_s=3600,
                        max_downloads=5, http=http, **kw)
    return client, sleeps, seen


INIT_OK = {"id": "abcdef123456", "deleteToken": "t", "expiresAt": 2_000_000_000}


async def test_drop_rate_limit_honours_retry_after(h, monkeypatch, tmp_path):
    def handler(request, virtual_now):
        if virtual_now < 60:
            return httpx.Response(429, headers={"Retry-After": "60"}, text="Too many requests")
        if request.url.path == "/api/upload/init":
            return httpx.Response(200, json=INIT_OK)
        return httpx.Response(200, json={"ok": True})

    client, _sleeps, _ = drop_client(handler, monkeypatch)
    f = tmp_path / "a.zip"
    f.write_bytes(b"x" * 100)
    try:
        await client.upload(str(f), "a.zip")
    finally:
        await client.aclose()


async def test_drop_partial_chunk_failure_is_retried_per_chunk(h, monkeypatch, tmp_path):
    """Evidence (correct): a chunk that 503s twice is resent; the upload completes."""
    fails = {"n": 2}

    def handler(request, _):
        if request.url.path == "/api/upload/init":
            return httpx.Response(200, json=INIT_OK)
        if request.url.path.endswith("/1") and fails["n"] > 0:
            fails["n"] -= 1
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client, sleeps, seen = drop_client(handler, monkeypatch)
    f = tmp_path / "a.zip"
    f.write_bytes(os.urandom(3 * 1024 * 1024 + 5))
    link = await client.upload(str(f), "a.zip")
    await client.aclose()
    assert link.id == "abcdef123456"
    assert sum(1 for m, p in seen if p.endswith("/1")) == 3 and len(sleeps) == 2
    assert ("POST", "/api/upload/abcdef123456/complete") in seen


async def test_drop_restart_mid_upload_discards_and_reports_unavailable(h, monkeypatch, tmp_path):
    """Evidence (correct): 404 on a chunk (drop restarted, session lost) -> DELETE the half upload,
    DropUnavailable (retryable) -> pipeline falls back to attachment or defers."""
    def handler(request, _):
        if request.url.path == "/api/upload/init":
            return httpx.Response(200, json=INIT_OK)
        if request.method == "PUT":
            return httpx.Response(404, text="upload not found")
        return httpx.Response(200)

    client, _, seen = drop_client(handler, monkeypatch)
    f = tmp_path / "a.zip"
    f.write_bytes(b"x" * 1000)
    with pytest.raises(DropUnavailable):
        await client.upload(str(f), "a.zip")
    await client.aclose()
    assert ("DELETE", "/api/file/abcdef123456") in seen


async def test_drop_malformed_init_is_rejected_not_retried(h, monkeypatch, tmp_path):
    client, _, seen = drop_client(lambda r, _: httpx.Response(200, text="<html>oops</html>"), monkeypatch)
    f = tmp_path / "a.zip"
    f.write_bytes(b"x")
    with pytest.raises(DropRejected):
        await client.upload(str(f), "a.zip")
    await client.aclose()
    assert len(seen) == 1


# ======================================================================== OEB API


def oeb(handler) -> OebProvider:
    return OebProvider(httpx.AsyncClient(base_url=API_URL, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(("name", "response", "error"), [
    ("503", lambda: httpx.Response(503), PortalUnavailable),
    ("429", lambda: httpx.Response(429, headers={"Retry-After": "120"}), PortalUnavailable),
    ("html-error-page-200", lambda: httpx.Response(200, html="<html><body>Server Error</body></html>"), ScrapeError),
    ("truncated-json", lambda: httpx.Response(200, text='{"Results": [{"Uri": 1, "Record'), ScrapeError),
    ("403", lambda: httpx.Response(403), ProviderRejected),
    ("400", lambda: httpx.Response(400), ProviderRejected),
])
async def test_oeb_failure_classification(h, name, response, error):
    """Evidence: no failure shape is MatterNotFound; transient ones are retryable, while 403/400
    (the portal refusing the request) are ProviderRejected and not retried."""
    p = oeb(lambda r: response())
    with pytest.raises(error) as exc:
        await p.list_matter_and_documents("EB-2024-0111", "Decisions and Orders", 10)
    assert exc.value.retryable is (error is not ProviderRejected)


async def test_oeb_connection_reset_mid_body_is_unavailable(h):
    def handler(request):
        raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    with pytest.raises(PortalUnavailable):
        await oeb(handler).list_matter_and_documents("EB-2024-0111", "Decisions and Orders", 10)


async def test_oeb_429_retry_after_drives_the_job_defer(h):
    h.add_provider(oeb(lambda r: httpx.Response(429, headers={"Retry-After": "600"})))
    rid = await h.ingest(make_email("Could you send me the latest decisions for EB-2024-0111?"))
    with pytest.raises(arq.Retry) as exc:
        await h.process(rid, 1)
    assert (await h.request(rid))["provider"] == "oeb"
    assert exc.value.defer_score >= 600_000, exc.value.defer_score


# ======================================================================== UARB portal


class _NoBrowser:
    @asynccontextmanager
    async def session(self):
        yield object()


def _refs(n=3):
    return [DocumentRef(provider="uarb", matter=MATTER, doc_type="Other Documents", external_id=str(102670 + i),
                        title=f"Doc {i}", filed_on=date(2026, 1, i + 1)) for i in range(n)]


async def test_uarb_download_session_not_found_is_retryable(h, tmp_path, monkeypatch):
    p = UarbProvider(_NoBrowser())

    async def not_found(page, matter):
        raise MatterNotFound(matter)

    monkeypatch.setattr(p, "_open_matter", not_found)
    with pytest.raises(AgentError) as exc:
        async for _ in p.download(MATTER, _refs(), str(tmp_path)):
            pass
    assert exc.value.retryable, f"{type(exc.value).__name__} is final"


async def test_uarb_download_not_found_race_never_tells_the_user_the_matter_is_missing(h, monkeypatch):
    p = UarbProvider(_NoBrowser())
    refs = h.provider.refs("Other Documents")

    async def list_once(matter, doc_type, limit):
        return h.provider.info(), refs[:limit]

    async def not_found(page, matter):
        raise MatterNotFound(matter)

    monkeypatch.setattr(p, "_list_once", list_once)
    monkeypatch.setattr(p, "_open_matter", not_found)
    h.providers["uarb"] = p
    rid = await h.ingest(make_email(REQUEST))
    try:
        await h.run_job(rid)
    finally:
        h.providers["uarb"] = h.provider
    assert "couldn't find matter" not in body_text(h.reply(rid))


async def _stalling_portal():
    """A page with a GO GET IT row whose download sends headers + 5 bytes and then stalls."""
    writers = []
    page = (b"<html><body><table><tr class='v-grid-row-has-data'><td><div class='text'>102674</div></td>"
            b"<td><button onclick=\"document.getElementById('dlg').style.display='block'\">GO GET IT</button>"
            b"</td></tr></table><div id='dlg' class='v-window' style='display:none'>Download Files "
            b"<a class='fm-download-button' href='/file'>102674.pdf</a></div></body></html>")

    async def handle(reader, writer):
        writers.append(writer)
        line = await reader.readline()
        await reader.readuntil(b"\r\n\r\n")
        if b"/file" in line:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: 500000\r\n"
                         b"Content-Disposition: attachment; filename=\"102674.pdf\"\r\n\r\n%PDF-")
            await writer.drain()
            await asyncio.Event().wait()  # never finishes
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n" % len(page) + page)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], writers


async def test_uarb_stalled_download_is_bounded(h, tmp_path):
    server, port, writers = await _stalling_portal()
    pool = BrowserPool(proxy=None, max_sessions=1, nav_timeout_ms=3_000)
    p = UarbProvider(pool)
    ref = _refs(1)[0].model_copy(update={"external_id": "102674"})
    outcome = "hung"
    try:
        async with pool.session() as page:
            await page.goto(f"http://127.0.0.1:{port}/")
            go = page.locator("button", has_text="GO GET IT")
            button = page.locator(".v-window .fm-download-button")
            try:
                await asyncio.wait_for(p._click_and_save(page, ref, go, button, str(tmp_path)), timeout=20)
                outcome = "returned"
            except TimeoutError:
                outcome = "hung"
            except (PortalUnavailable, ScrapeError):
                outcome = "bounded"
    finally:
        for w in writers:
            w.close()
        server.close()
        await pool.close()
    assert outcome == "bounded", f"download {outcome} (20 s, context default timeout 3 s)"


# ======================================================================== Redis locks


async def test_lock_is_not_lost_while_the_holder_is_still_running(h):
    limits = Limits(h.redis)
    inside: list[str] = []
    overlap = []

    async def holder(name, hold_s):
        async with limits.lock("uarb:download", ttl_s=1, wait_s=10, poll_s=0.05):
            if inside:
                overlap.append((inside[:], name))
            inside.append(name)
            await asyncio.sleep(hold_s)
            inside.remove(name)

    first = asyncio.create_task(holder("slow-download", 2.5))
    await asyncio.sleep(0.1)
    await holder("next-download", 0.1)
    await first
    assert overlap == []


async def test_stale_holder_cannot_release_the_new_holders_lock(h):
    """Evidence (correct): release is token-checked, so the expired holder's exit is a no-op."""
    limits = Limits(h.redis)
    async with limits.lock("x", ttl_s=1, wait_s=1):
        await asyncio.sleep(1.2)
        await h.redis.set("lock:x", "someone-else", ex=30)
    assert await h.redis.get("lock:x") == b"someone-else"


# ======================================================================== Postgres pool


async def test_exhausted_pool_fails_fast(h):
    rid = await h.ingest(make_email(REQUEST))
    pool = db.pool()
    held = [await pool.acquire() for _ in range(pool.get_max_size())]
    t0 = time.monotonic()
    try:
        with contextlib.suppress(TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError):
            await asyncio.wait_for(store.get(rid), timeout=12)
    finally:
        for c in held:
            await pool.release(c)
    assert time.monotonic() - t0 < 11, "store.get() waited for a connection until the test gave up (12 s)"


# ======================================================================== Redis restart (throwaway container)


@pytest.fixture
def throwaway_redis():
    if not shutil.which("docker"):
        pytest.skip("docker not available")
    containers = []

    def start(*args: str) -> tuple[str, int]:
        name = f"reliability-redis-{secrets.token_hex(4)}"
        port = 16300 + secrets.randbelow(600)
        subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:6379",
                        "redis:7-alpine", "redis-server", *args], check=True, capture_output=True)
        containers.append(name)
        return name, port

    yield start
    for name in containers:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


async def _connect(port: int) -> arq.ArqRedis:
    for _ in range(50):
        try:
            return await arq.create_pool(RedisSettings(host="127.0.0.1", port=port, conn_retries=0))
        except (OSError, redis.exceptions.RedisError):
            await asyncio.sleep(0.1)
    raise RuntimeError("throwaway redis did not come up")


@pytest.mark.parametrize(("args", "survives"), [
    (("--appendonly", "yes", "--maxmemory-policy", "noeviction"), True),  # production config
    (("--appendonly", "no", "--save", ""), False),
], ids=["aof-like-prod", "no-persistence"])
async def test_deferred_retry_across_a_redis_restart(h, throwaway_redis, args, survives):
    """Evidence: with the production config (AOF) a job deferred by Retry survives `docker restart`
    of Redis (its retry counter too); without persistence it is lost and only the sweeper (requests
    idle > 15 min, checked every 5 min) brings the request back."""
    name, port = throwaway_redis(*args)
    r = await _connect(port)
    job = await r.enqueue_job("process_request", "rid-1", _job_id="req:rid-1", _defer_by=600)
    assert job is not None
    await r.incr("arq:retry:req:rid-1")
    await r.aclose()
    await asyncio.to_thread(subprocess.run, ["docker", "restart", "-t", "5", name], check=True, capture_output=True)
    r = await _connect(port)
    try:
        queued = [j.job_id for j in await r.queued_jobs()]
        assert (queued == ["req:rid-1"]) is survives
        assert (await r.get("arq:retry:req:rid-1") == b"1") is survives
    finally:
        await r.aclose()


async def test_redis_down_at_job_start_is_a_retry(h):
    """Redis unreachable when the job takes its request lock -> Retry with backoff (was: a raw redis
    ConnectionError escaped process_request and arq failed the job for good)."""
    from arq.connections import ArqRedis

    dead = ArqRedis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    rid = await h.ingest(make_email(REQUEST))
    deps = h.deps
    deps.limits = Limits(dead)
    with pytest.raises(arq.Retry):
        await worker.process_request({"redis": h.redis, "deps": deps, "job_try": 1}, str(rid))
    row = await h.request(rid)
    assert row["state"] == "received" and row["attempts"] == 0
    await dead.aclose()


async def test_redis_failing_mid_job_is_retried_then_apologised(h):
    """Evidence: Redis failing inside the job (rate limits / single-flight lock) -> TransientError ->
    Retry; on the final try the user gets the generic apology for an internal outage."""

    class FlakyLimits(Limits):
        async def hit(self, *a, **kw):
            raise redis.exceptions.ConnectionError("Connection reset by peer")

    rid = await h.ingest(make_email(REQUEST))
    deps = h.deps
    deps.limits = FlakyLimits(h.redis)
    with pytest.raises(arq.Retry):
        await worker.process_request({"redis": h.redis, "deps": deps, "job_try": 1}, str(rid))
    await db.pool().execute("UPDATE requests SET attempts = $2 WHERE id = $1", rid, get_settings().max_attempts - 1)
    await worker.process_request({"redis": h.redis, "deps": deps, "job_try": 2}, str(rid))  # the final attempt
    row = await h.request(rid)
    assert row["state"] == "failed" and "unexpected problem" in body_text(h.reply(rid))


_ = json  # (kept for ad-hoc debugging of mock payloads)


# ======================================================================== disk full


async def test_disk_full_while_storing_a_download_is_retried_then_apologised(h, monkeypatch):
    """ENOSPC in the worker is retried like any internal failure, then a generic apology (never one
    blaming the regulator); the `disk.full` alert is logged on each try."""
    import errno

    from agent import blobs

    def full(src, sha):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(blobs, "put_file", full)
    rid = await h.ingest(make_email(REQUEST))
    defers = await h.run_job(rid)
    row = await h.request(rid)
    assert len(defers) == get_settings().max_attempts - 1 and row["state"] == "failed"
    assert row["error"].startswith("OSError") and "unexpected problem" in body_text(h.reply(rid))


async def test_disk_full_at_ingest_leaves_the_message_unread(h, monkeypatch):
    """Evidence (correct): put_raw fails before any DB write; ingest_raw raises OSError (caught by
    Ingestor.run_forever, so \\Seen is not set and the message is retried on reconnect)."""
    import errno

    from agent import blobs
    from agent.mail.ingest import ingest_raw

    def full(raw):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(blobs, "put_raw", full)
    with pytest.raises(OSError):
        await ingest_raw(make_email(REQUEST), h.redis)
    assert await db.pool().fetchval("SELECT count(*) FROM requests") == 0
