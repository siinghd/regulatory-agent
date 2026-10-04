"""IMAP IDLE ingest for agent@: durable hand-off from the mailbox to the job queue.

Order of operations per message is what makes it lossless and duplicate-free:
  1. fetch raw bytes with BODY.PEEK (doesn't set \\Seen)
  2. store raw MIME (content-addressed) and INSERT the request (unique on Message-ID)
  3. enqueue the job (job id = request id, so re-enqueueing is a no-op)
  4. only then set \\Seen
A crash before 4 re-delivers the message on the next sweep; step 2's ON CONFLICT makes that
harmless. Nothing is ever marked read before it is safely in Postgres.
"""

import asyncio
import hashlib
import re
from datetime import UTC, datetime

import aioimaplib
import structlog
from arq.connections import ArqRedis

from agent import blobs, store
from agent.config import Settings
from agent.mail import loops, mime

log = structlog.get_logger()

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY (\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
IDLE_SECONDS = 20 * 60  # RFC 2177: re-issue IDLE before the 29-minute server timeout


async def enqueue(redis: ArqRedis, request_id) -> None:
    await redis.enqueue_job("process_request", str(request_id), _job_id=f"req:{request_id}")


async def ingest_raw(raw: bytes, redis: ArqRedis, *, uid: int | None = None, uidvalidity: int | None = None) -> None:
    """Persist one raw message and enqueue it. Safe to call twice for the same message."""
    sha = blobs.put_raw(raw)
    received_at = datetime.now(UTC)
    try:
        email = mime.parse_raw(raw, received_at)
    except mime.MalformedEmail as e:
        rid = await store.create_request(
            message_id=f"<malformed.{sha}@invalid>", raw_sha256=sha, from_addr="", subject="",
            thread_root=f"<malformed.{sha}@invalid>", imap_uid=uid, imap_uidvalidity=uidvalidity,
        )
        if rid:
            await store.transition(rid, {"received"}, "rejected", reject_reason=f"malformed:{e}")
        return
    # Messages without a Message-ID get a stable synthetic one, so redelivery still dedupes.
    message_id = email.message_id or f"<sha.{hashlib.sha256(raw).hexdigest()[:32]}@missing>"
    rid = await store.create_request(
        message_id=message_id, raw_sha256=sha, from_addr=email.from_addr, subject=email.subject,
        thread_root=loops.thread_root(email), imap_uid=uid, imap_uidvalidity=uidvalidity,
    )
    if rid is None:
        log.info("ingest.duplicate", message_id=message_id)
        return
    await enqueue(redis, rid)
    log.info("ingest.enqueued", request_id=str(rid), from_addr=email.from_addr)


class Ingestor:
    def __init__(self, settings: Settings, redis: ArqRedis):
        self.s = settings
        self.redis = redis
        self.uidvalidity: int | None = None

    async def run_forever(self) -> None:
        backoff = 1
        while True:
            try:
                await self._session()
                backoff = 1
            except (OSError, TimeoutError, aioimaplib.Abort, aioimaplib.CommandTimeout, IngestError) as e:
                log.warning("ingest.reconnect", error=f"{type(e).__name__}: {e}", backoff_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _session(self) -> None:
        client = aioimaplib.IMAP4_SSL(host=self.s.imap_host, port=self.s.imap_port, timeout=60)
        await client.wait_hello_from_server()
        _ok(await client.login(self.s.agent_mail_address, self.s.agent_mail_password.get_secret_value()), "login")
        try:
            sel = _ok(await client.select("INBOX"), "select")
            m = _UIDVALIDITY_RE.search(b" ".join(_bytes(x) for x in sel.lines))
            self.uidvalidity = int(m.group(1)) if m else None
            log.info("ingest.connected", uidvalidity=self.uidvalidity)
            while True:
                await self._sweep(client)
                idle = await client.idle_start(timeout=IDLE_SECONDS)
                try:
                    await client.wait_server_push(timeout=IDLE_SECONDS)
                except TimeoutError:
                    pass  # quiet mailbox: re-issue IDLE (and sweep, in case a push was missed)
                client.idle_done()
                await asyncio.wait_for(idle, timeout=30)
        finally:
            try:
                await client.logout()
            except Exception:  # noqa: BLE001 - best-effort logout on a possibly dead socket
                pass

    async def _sweep(self, client: aioimaplib.IMAP4_SSL) -> None:
        res = _ok(await client.uid_search("UNSEEN"), "search")
        uids = [int(u) for line in res.lines[:1] for u in _bytes(line).split() if u.isdigit()]
        for uid in uids:
            await self._one(client, uid)

    async def _one(self, client: aioimaplib.IMAP4_SSL, uid: int) -> None:
        head = _ok(await client.uid("fetch", str(uid), "(RFC822.SIZE)"), "fetch size")
        m = _SIZE_RE.search(b" ".join(_bytes(x) for x in head.lines))
        size = int(m.group(1)) if m else 0
        if size > self.s.max_inbound_bytes:
            # Oversized mail never needs its body (requests are a sentence); keep headers for audit.
            res = _ok(await client.uid("fetch", str(uid), "(BODY.PEEK[HEADER])"), "fetch header")
            raw = _literal(res.lines) + b"\r\n"
        else:
            res = _ok(await client.uid("fetch", str(uid), "(BODY.PEEK[])"), "fetch body")
            raw = _literal(res.lines)
        if not raw:
            raise IngestError(f"empty fetch for uid {uid}")
        await ingest_raw(raw, self.redis, uid=uid, uidvalidity=self.uidvalidity)
        _ok(await client.uid("store", str(uid), "+FLAGS", r"(\Seen)"), "store seen")


class IngestError(Exception):
    pass


def _ok(resp, what: str):
    if resp.result != "OK":
        raise IngestError(f"IMAP {what} failed: {resp.result} {resp.lines[-1:]}")
    return resp


def _bytes(x) -> bytes:
    return bytes(x) if isinstance(x, (bytes, bytearray)) else str(x).encode()


def _literal(lines) -> bytes:
    """aioimaplib returns the message literal as the bytearray line of a FETCH response."""
    for line in lines:
        if isinstance(line, bytearray):
            return bytes(line)
    return b""
