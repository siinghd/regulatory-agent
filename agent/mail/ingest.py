"""IMAP IDLE ingest for agent@: durable hand-off from the mailbox to the job queue.

Order of operations per message is what makes it lossless and duplicate-free:
  1. fetch raw bytes with BODY.PEEK (doesn't set \\Seen)
  2. store raw MIME (content-addressed) and INSERT the request (unique on Message-ID)
  3. enqueue the job (job id = request id, so re-enqueueing is a no-op)
  4. only then set \\Seen
A crash before 4 re-delivers the message on the next sweep; step 2's ON CONFLICT makes that
harmless, and a redelivered message whose request never got past `received` is enqueued again
(its first enqueue may be what failed). Nothing is ever marked read before it is safely in
Postgres, and nothing new is stored while the disk is nearly full (it stays unread instead).

Abuse limits come first, before raw MIME is stored or any DNS is asked (agent.limits, HMAC keys):
- a global ceiling (inbound_per_minute): over it, the rest of the mailbox stays unseen and the
  next sweep (within DEFERRED_IDLE_S) picks it up. Deferred, never dropped;
- per connecting client IP (from our own MTA's Received header; IPv6 per /64) and per claimed
  From organizational domain, per hour: over either, only the headers are stored, the request is
  created already rejected (preauth_rate_limited), the message is marked read, nothing is
  enqueued and nobody is answered.

The connection is watched: IDLE is re-issued at least every 5 minutes, and a dropped socket is
noticed at once (not when IDLE would have timed out). Messages are expunged from the mailbox
7 days after their request settled; the raw MIME stays in our own store under its retention.
"""

import asyncio
import hashlib
import random
import re
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

import aioimaplib
import structlog
from arq.connections import ArqRedis
from redis.exceptions import RedisError

from agent import audit, blobs, metrics, queue, store
from agent.config import Settings, get_settings
from agent.limits import Limits, domain_key, ip_key
from agent.mail import auth as mail_auth
from agent.mail import loops, mime
from agent.models import InboundEmail

log = structlog.get_logger()

_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY (\d+)")
_SIZE_RE = re.compile(rb"RFC822\.SIZE (\d+)")
IDLE_SECONDS = 5 * 60  # re-issue IDLE (and sweep) at least this often
EXPUNGE_AFTER = timedelta(days=7)
EXPUNGE_EVERY_S = 3600
MAX_BACKOFF_S = 60.0
HEARTBEAT_KEY = "ingest:heartbeat"  # unix time; `ragent healthcheck ingest` wants it < 600 s old
HEARTBEAT_EVERY_S = 60.0
HEARTBEAT_TTL_S = 900
DEFERRED_IDLE_S = 60  # after the inbound ceiling deferred mail, sweep again this soon
PREAUTH_REJECT = "preauth_rate_limited"
T = TypeVar("T")


async def enqueue(redis: ArqRedis, request_id) -> None:
    await queue.enqueue_request(redis, request_id)


@dataclass(frozen=True)
class Preauth:
    """Which pre-authentication limit a message is over (None: neither)."""

    limited: str | None = None  # ip | domain
    client_ip: str | None = None


async def inbound_slot(limits: Limits, settings: Settings, member: str) -> bool:
    """One message's place under the global inbound ceiling (inbound_per_minute). False: leave it
    unseen for a later sweep. `member` is stable per message, so a re-sweep never counts twice."""
    ok = await limits.decide("inbound_minute", "inbound:minute", limit=settings.inbound_per_minute, window_s=60,
                             member=member)
    if not ok:
        metrics.observe_limiter("inbound_minute", "deferred")
    return ok


async def preauth(limits: Limits, settings: Settings, raw: bytes, email: InboundEmail | None, member: str) -> Preauth:
    """Per connecting client IP and per claimed From organizational domain, per hour, before
    anything is stored and before any DNS lookup. Fails open when Redis is unreachable: the
    enqueue that follows fails then too, and the message stays unread."""
    client = mail_auth.smtp_client(mime.received_headers(mime.parse_headers(raw)), settings.trusted_mta_hostname)
    ip = client.ip if client else None
    try:
        if ip and not await limits.decide("preauth_ip", f"preauth_ip:{ip_key(ip)}", limit=settings.preauth_per_ip_hour,
                                          window_s=3600, member=member):
            return Preauth("ip", ip)
        domain = email.from_addr.rpartition("@")[2] if email else ""
        if domain and not await limits.decide("preauth_domain", f"preauth_domain:{domain_key(domain)}",
                                              limit=settings.preauth_per_domain_hour, window_s=3600, member=member):
            return Preauth("domain", ip)
    except (RedisError, OSError) as e:
        log.warning("ingest.preauth_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}")
    return Preauth(None, ip)


def header_block(raw: bytes) -> bytes:
    """The header section of a raw message, blank line included (all of it if there is no body)."""
    ends = [i + len(sep) for sep in (b"\r\n\r\n", b"\n\n") if (i := raw.find(sep)) >= 0]
    return raw[: min(ends)] if ends else raw


async def ingest_raw(
    raw: bytes, redis: ArqRedis, *, uid: int | None = None, uidvalidity: int | None = None,
    limits: Limits | None = None,
) -> str:
    """Persist one raw message and enqueue it. Safe to call twice for the same message.

    Returns what happened: enqueued | requeued | duplicate | rejected (malformed, or over a
    pre-authentication limit: stored as headers only, never enqueued).
    """
    received_at = datetime.now(UTC)
    raw_sha = hashlib.sha256(raw).hexdigest()
    try:
        email = mime.parse_raw(raw, received_at)
        malformed = None
    except mime.MalformedEmail as e:
        email, malformed = None, e
    gate = await preauth(limits or Limits(redis), get_settings(), raw, email, member=raw_sha)
    if gate.limited:
        return await _reject_preauth(raw, email, gate, uid=uid, uidvalidity=uidvalidity)

    sha = blobs.put_raw(raw)
    if email is None:
        rid = await store.create_request(
            message_id=f"<malformed.{sha}@invalid>", raw_sha256=sha, from_addr="", subject="",
            thread_root=f"<malformed.{sha}@invalid>", imap_uid=uid, imap_uidvalidity=uidvalidity,
        )
        if rid:
            await store.transition(rid, {"received"}, "rejected", reject_reason=f"malformed:{malformed}")
        return "rejected"
    # Messages without a Message-ID get a stable synthetic one, so redelivery still dedupes.
    message_id = email.message_id or f"<sha.{hashlib.sha256(raw).hexdigest()[:32]}@missing>"
    rid = await store.create_request(
        message_id=message_id, raw_sha256=sha, from_addr=email.from_addr, subject=email.subject,
        thread_root=loops.thread_root(email), imap_uid=uid, imap_uidvalidity=uidvalidity,
    )
    if rid is None:
        existing = await store.get_by_message_id(message_id)
        if existing is not None and existing["state"] == "received":
            # Seen before but never picked up: the enqueue after the INSERT may be what failed.
            await enqueue(redis, existing["id"])
            log.info("ingest.requeued_duplicate", request_id=str(existing["id"]), message_id=message_id)
            return "requeued"
        log.info("ingest.duplicate", message_id=message_id)
        return "duplicate"
    await enqueue(redis, rid)
    log.info("ingest.enqueued", request_id=str(rid), from_h=audit.subject_hash(email.from_addr))
    return "enqueued"


async def _reject_preauth(
    raw: bytes, email: InboundEmail | None, gate: Preauth, *, uid: int | None, uidvalidity: int | None
) -> str:
    """Over a pre-authentication limit: keep the headers (for the audit and abuse reports), not
    the body; the request is created rejected in one statement and never enqueued."""
    sha = blobs.put_raw(header_block(raw))
    if email is not None:
        message_id = email.message_id or f"<sha.{hashlib.sha256(raw).hexdigest()[:32]}@missing>"
        from_addr, subject, thread_root = email.from_addr, email.subject, loops.thread_root(email)
    else:
        message_id = thread_root = f"<malformed.{hashlib.sha256(raw).hexdigest()}@invalid>"
        from_addr = subject = ""
    rid = await store.create_request(
        message_id=message_id, raw_sha256=sha, from_addr=from_addr, subject=subject, thread_root=thread_root,
        imap_uid=uid, imap_uidvalidity=uidvalidity, reject_reason=PREAUTH_REJECT,
    )
    if rid is None:
        log.info("ingest.duplicate", message_id=message_id)
        return "duplicate"
    await store.event(rid, "rate_limited", {"key": gate.limited, "window": "hour", "action": "rejected",
                                            "stage": "preauth"})
    log.warning("ingest.preauth_rate_limited", request_id=str(rid), limit=gate.limited)
    return "rejected"


class ConnectionLost(Exception):
    pass


class Ingestor:
    def __init__(self, settings: Settings, redis: ArqRedis):
        self.s = settings
        self.redis = redis
        self.uidvalidity: int | None = None
        self._lost = asyncio.Event()
        self._last_expunge = float("-inf")
        self._limits: Limits | None = None

    @property
    def limits(self) -> Limits:
        if self._limits is None:
            self._limits = Limits(self.redis)
        return self._limits

    async def run_forever(self) -> None:
        backoff = 1.0
        while True:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - IMAP, Postgres, Redis, disk: back off and reconnect
                delay = backoff * random.uniform(0.5, 1.0)
                log.warning("ingest.reconnect", error=f"{type(e).__name__}: {str(e)[:300]}", backoff_s=round(delay, 1))
                await asyncio.sleep(delay)
                backoff = min(backoff * 2, MAX_BACKOFF_S)

    async def _session(self) -> None:
        lost = self._lost = asyncio.Event()  # this connection's own: a late callback can't trip the next one
        client = aioimaplib.IMAP4_SSL(host=self.s.imap_host, port=self.s.imap_port, timeout=60)
        # aioimaplib never wakes a waiting command when the socket closes: watch for it ourselves
        client.protocol.conn_lost_cb = lambda _exc: lost.set()
        try:
            await self._call(client.wait_hello_from_server())
            _ok(await self._call(client.login(self.s.agent_mail_address, self.s.agent_mail_password.get_secret_value())),
                "login")
            sel = _ok(await self._call(client.select("INBOX")), "select")
            m = _UIDVALIDITY_RE.search(b" ".join(_bytes(x) for x in sel.lines))
            self.uidvalidity = int(m.group(1)) if m else None
            log.info("ingest.connected", uidvalidity=self.uidvalidity)
            while True:
                deferred = await self._sweep(client)
                await self._expunge_settled(client)
                await self._heartbeat()
                await self._idle(client, DEFERRED_IDLE_S if deferred else IDLE_SECONDS)
        finally:
            if not self._lost.is_set():
                try:
                    await asyncio.wait_for(client.logout(), timeout=5)
                except (OSError, TimeoutError, aioimaplib.Abort, aioimaplib.CommandTimeout) as e:
                    log.debug("ingest.logout_failed", error=str(e))  # socket likely already dead

    async def _heartbeat(self) -> None:
        """`ragent healthcheck ingest`: written by the IMAP loop itself, every iteration and every
        HEARTBEAT_EVERY_S of a quiet IDLE, so a stuck or disconnected loop stops writing it."""
        try:
            await self.redis.set(HEARTBEAT_KEY, f"{time.time():.0f}", ex=HEARTBEAT_TTL_S)
        except (RedisError, OSError) as e:
            log.warning("ingest.heartbeat_failed", error=f"{type(e).__name__}: {e}")

    async def _call(self, aw: Awaitable[T]) -> T:
        """Await an IMAP command, but give up the moment the connection drops."""
        task = asyncio.ensure_future(aw)
        lost = asyncio.ensure_future(self._lost.wait())
        try:
            await asyncio.wait({task, lost}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            lost.cancel()
        if not task.done():
            task.cancel()
            raise ConnectionLost("IMAP connection closed")
        return task.result()

    async def _idle(self, client: aioimaplib.IMAP4_SSL, seconds: float | None = None) -> None:
        seconds = IDLE_SECONDS if seconds is None else seconds
        idle = await self._call(client.idle_start(timeout=seconds))
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            try:
                await self._call(client.wait_server_push(timeout=min(HEARTBEAT_EVERY_S, left)))
                break  # the server pushed something (new mail): sweep now
            except TimeoutError:
                await self._heartbeat()  # quiet mailbox, live connection
        # then re-issue IDLE (and sweep, in case a push was missed)
        client.idle_done()
        await self._call(asyncio.wait_for(idle, timeout=30))

    async def _sweep(self, client: aioimaplib.IMAP4_SSL) -> bool:
        """Ingest every unseen message. True if the inbound ceiling left some for the next sweep."""
        free = await asyncio.to_thread(blobs.disk_free)
        if free < self.s.disk_min_free_bytes:
            # Leave new mail unread: it's picked up once there is room again.
            log.error("ingest.disk_low", alert=True, free_bytes=free, min_free_bytes=self.s.disk_min_free_bytes)
            return False
        res = _ok(await self._call(client.uid_search("UNSEEN")), "search")
        uids = [int(u) for line in res.lines[:1] for u in _bytes(line).split() if u.isdigit()]
        for i, uid in enumerate(uids):
            if not await self._one(client, uid):
                await self._deferred(len(uids) - i)
                return True
        return False

    async def _deferred(self, waiting: int) -> None:
        """The inbound ceiling stopped a sweep: logged, and audited at most once a minute."""
        log.warning("ingest.inbound_deferred", waiting=waiting, limit_per_minute=self.s.inbound_per_minute)
        try:
            if await self.limits.once("audit:inbound_deferred", 60):
                await audit.write(None, "rate_limited", {"key": "inbound", "window": "minute", "action": "deferred",
                                                         "waiting": waiting})
        except Exception as e:  # noqa: BLE001 - the log line above is the record either way
            log.warning("ingest.deferred_event_lost", error=f"{type(e).__name__}: {e}")

    async def _one(self, client: aioimaplib.IMAP4_SSL, uid: int) -> bool:
        """Ingest one message and mark it read. False (and unread) when the inbound ceiling is reached."""
        if not await inbound_slot(self.limits, self.s, f"{self.uidvalidity}:{uid}"):
            return False
        head = _ok(await self._call(client.uid("fetch", str(uid), "(RFC822.SIZE)")), "fetch size")
        m = _SIZE_RE.search(b" ".join(_bytes(x) for x in head.lines))
        size = int(m.group(1)) if m else 0
        if size > self.s.max_inbound_bytes:
            # Oversized mail never needs its body (requests are a sentence); keep headers for audit.
            res = _ok(await self._call(client.uid("fetch", str(uid), "(BODY.PEEK[HEADER])")), "fetch header")
            raw = _literal(res.lines) + b"\r\n"
        else:
            res = _ok(await self._call(client.uid("fetch", str(uid), "(BODY.PEEK[])")), "fetch body")
            raw = _literal(res.lines)
        if not raw:
            raise IngestError(f"empty fetch for uid {uid}")
        await ingest_raw(raw, self.redis, uid=uid, uidvalidity=self.uidvalidity, limits=self.limits)
        _ok(await self._call(client.uid("store", str(uid), "+FLAGS", r"(\Seen)")), "store seen")
        return True

    async def _expunge_settled(self, client: aioimaplib.IMAP4_SSL) -> None:
        """Remove messages whose request settled more than EXPUNGE_AFTER ago (at most hourly)."""
        if self.uidvalidity is None or time.monotonic() - self._last_expunge < EXPUNGE_EVERY_S:
            return
        self._last_expunge = time.monotonic()
        rows = await store.expungeable(self.uidvalidity, EXPUNGE_AFTER)
        if not rows:
            return
        uid_set = ",".join(str(r["imap_uid"]) for r in rows)
        _ok(await self._call(client.uid("store", uid_set, "+FLAGS", r"(\Deleted)")), "store deleted")
        try:
            _ok(await self._call(client.uid("expunge", uid_set)), "uid expunge")
        except aioimaplib.Abort:  # no UIDPLUS: plain EXPUNGE removes only what is flagged \Deleted
            _ok(await self._call(client.expunge()), "expunge")
        await store.mark_expunged([r["id"] for r in rows])
        log.info("ingest.expunged", count=len(rows))


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
