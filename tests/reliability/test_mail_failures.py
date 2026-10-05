"""SMTP and IMAP-ingest failure handling."""

import asyncio
import os

import aiosmtplib
import arq
import asyncpg
import pytest
import redis.exceptions
from arq.connections import ArqRedis

from agent import db, store
from agent.config import get_settings
from agent.delivery.drop import DropUnavailable
from agent.mail import outbound
from agent.mail.ingest import Ingestor, ingest_raw
from tests.integration.harness import FakeDrop, attachments, body_text, make_email, message_id_of, outbound_id

pytestmark = pytest.mark.integration

REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"
POSTFIX_MESSAGE_SIZE_LIMIT = 10_240_000  # `postconf message_size_limit` on this host


def refusing_smtp(h, exc_for):
    """Replace SMTP with one that raises exc_for(msg) (or sends when it returns None)."""
    attempts: list[tuple[str, bool]] = []

    async def send(msg):
        attempts.append((msg["Message-ID"], any(True for _ in msg.iter_attachments())))
        exc = exc_for(msg)
        if exc is not None:
            raise exc
        await h.smtp.send(msg)

    h._mp.setattr(outbound, "send", send)
    return attempts


# ======================================================================== permanent 5xx


async def test_permanent_smtp_rejection_of_the_reply_is_not_retried(h):
    def exc_for(msg):
        if any(True for _ in msg.iter_attachments()):
            return aiosmtplib.SMTPDataError(552, "5.3.4 Message size exceeds fixed limit")
        return None

    attempts = refusing_smtp(h, exc_for)
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)

    with_attachment = [a for a in attempts if a[1]]
    assert len(with_attachment) == 1, f"{len(with_attachment)} attempts to send a permanently rejected reply"


async def test_reply_too_large_for_the_mta_is_resent_as_a_link(h):
    """552 on a reply carrying the ZIP (attached because drop was down at the time): no blind retry;
    the request goes back to packaging and the same reply (same Message-ID) goes out with a link."""

    class DropDownOnce(FakeDrop):
        async def upload(self, path, display_name):
            if not self.uploads and not getattr(self, "failed", False):
                self.failed = True
                raise DropUnavailable("drop restarting")
            return await super().upload(path, display_name)

    def exc_for(msg):
        if any(True for _ in msg.iter_attachments()):
            return aiosmtplib.SMTPDataError(552, "5.3.4 Message size exceeds fixed limit")
        return None

    h.drop = DropDownOnce()
    attempts = refusing_smtp(h, exc_for)
    rid = await h.ingest(make_email(REQUEST))
    defers = await h.run_job(rid)

    assert defers == [] and sum(1 for _, att in attempts if att) == 1
    row = await h.request(rid)
    assert row["state"] == "done" and row["result"]["delivery"] == "link"
    (reply,) = h.replies(rid)
    assert attachments(reply) == {} and "https://drop.test/d/abcdef123456" in body_text(reply)
    assert len(h.drop.uploads) == 1
    assert "reply_too_large" in await h.event_kinds(rid)


async def test_reply_too_large_without_drop_ends_with_one_apology(h):
    """No drop configured: the documents can't go as a link either, so one apology (no retries)."""
    def exc_for(msg):
        if any(True for _ in msg.iter_attachments()):
            return aiosmtplib.SMTPDataError(552, "5.3.4 Message size exceeds fixed limit")
        return None

    refusing_smtp(h, exc_for)
    rid = await h.ingest(make_email(REQUEST))
    defers = await h.run_job(rid)

    assert defers == []
    row = await h.request(rid)
    assert row["state"] == "failed" and row["error"].startswith("TooLarge")
    (apology,) = h.replies(rid)
    assert "larger than I can deliver" in body_text(apology) and not attachments(apology)


async def test_attachment_fallback_fits_the_mta_size_limit(h, tmp_path):
    path = tmp_path / "M12205_Other_Documents.zip"
    path.write_bytes(os.urandom(get_settings().attach_inline_max_bytes))
    draft = outbound.Draft("reply", "Re: x", "text", "<p>html</p>", attachments=(str(path),))
    msg = outbound.build_message(draft, request_id=__import__("uuid").uuid4(), to_addr="a@example.com",
                                 in_reply_to="<x@example.com>", references=())
    assert len(msg.as_bytes()) <= POSTFIX_MESSAGE_SIZE_LIMIT, len(msg.as_bytes())


# ======================================================================== SMTP outage


async def test_smtp_outage_never_leaves_a_terminal_request_with_an_unsent_reply(h):
    h.smtp.fail["ack"] = h.smtp.fail["reply"] = 10_000
    rid = await h.ingest(make_email(REQUEST))
    try:
        await h.run_job(rid)
    except aiosmtplib.SMTPException:
        pass  # what arq sees on the final try: a plain failure, no Retry

    row = await h.request(rid)
    silent = row["state"] in store.TERMINAL and row["reply_message_id"] and row["reply_sent_at"] is None
    assert not silent


async def test_smtp_outage_waits_in_the_outbox_without_blocking_the_fetch(h):
    """SMTP down from the start: the ack no longer gates the fetch; both emails wait in the outbox
    (the request stays 'replying', not terminal) and go out, ack first, once SMTP is back."""
    h.smtp.fail["ack"] = h.smtp.fail["reply"] = 10_000
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)

    row = await h.request(rid)
    assert row["state"] == "replying" and row["reply_sent_at"] is None and row["ack_sent_at"] is None
    assert h.provider.calls == {"list": 1, "download": 1}
    assert {r["kind"]: r["status"] for r in await store.outbound_rows(rid)} == {"ack": "queued", "reply": "queued"}
    assert await store.stuck_requests(__import__("datetime").timedelta(0)) == []  # the outbox owns it now

    h.smtp.fail.clear()  # SMTP is back
    await h.drain_outbound(rid)

    row = await h.request(rid)
    assert row["state"] == "done" and row["ack_sent_at"] is not None and row["reply_sent_at"] is not None
    assert [m["Message-ID"] for m in h.smtp.sent] == [outbound_id(rid, "ack"), outbound_id(rid, "reply")]


async def test_permanent_rejection_of_the_reply_fails_the_request_with_an_alert(h):
    """5xx for the recipient: undeliverable at once (no retries), the request ends failed + dead letter."""
    def exc_for(msg):
        if msg["Message-ID"].startswith("<reply."):
            return aiosmtplib.SMTPRecipientsRefused(
                [aiosmtplib.SMTPRecipientRefused(550, "5.1.1 mailbox unavailable", "alice@example.com")])
        return None

    attempts = refusing_smtp(h, exc_for)
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)

    assert sum(1 for mid, _ in attempts if mid.startswith("<reply.")) == 1
    row = await h.request(rid)
    assert row["state"] == "failed" and row["error"].startswith("DeliveryFailed")
    (reply,) = [r for r in await store.outbound_rows(rid) if r["kind"] == "reply"]
    assert reply["status"] == "undeliverable"
    kinds = await h.event_kinds(rid)
    assert "undeliverable" in kinds and "dead_letter" in kinds


async def test_ambiguous_smtp_timeout_after_data_resends_the_same_message_id(h):
    """Evidence (correct by design): a timeout after DATA may or may not have delivered; the retry
    reuses the reserved Message-ID so a duplicate is recognisable."""
    calls = {"n": 0}

    def exc_for(msg):
        if msg["Message-ID"].startswith("<reply.") and calls["n"] == 0:
            calls["n"] += 1
            return aiosmtplib.SMTPReadTimeoutError("Timed out waiting for server response")
        return None

    attempts = refusing_smtp(h, exc_for)
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)
    reply_ids = [mid for mid, _ in attempts if mid.startswith("<reply.")]
    assert reply_ids == [outbound_id(rid, "reply")] * 2


# ======================================================================== drop link + reply retry


async def test_reply_retry_reuses_the_drop_upload(h):
    h.drop = FakeDrop()
    h.smtp.fail["reply"] = 2
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)
    assert (await h.request(rid))["state"] == "done"
    assert len(h.drop.uploads) == 1, f"{len(h.drop.uploads)} uploads for one request"


# ======================================================================== ingest


async def test_redelivery_after_a_failed_enqueue_enqueues_the_request(h):
    raw = make_email(REQUEST)
    dead = ArqRedis(host="127.0.0.1", port=1, socket_connect_timeout=0.5)
    with pytest.raises(redis.exceptions.ConnectionError):
        await ingest_raw(raw, dead)
    await dead.aclose()
    rid = await db.pool().fetchval("SELECT id FROM requests WHERE message_id = $1", message_id_of(raw))
    assert rid is not None and await h.queued_request_ids() == []

    await ingest_raw(raw, h.redis)  # IMAP redelivers the still-unseen message

    assert await h.queued_request_ids() == [str(rid)]


@pytest.mark.parametrize("error", [
    redis.exceptions.ConnectionError("Error 111 connecting to 127.0.0.1:6392"),
    asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed in the middle of operation"),
], ids=["redis", "postgres"])
async def test_ingest_loop_survives_backend_errors(h, monkeypatch, error):
    async def session(self):
        raise error

    monkeypatch.setattr(Ingestor, "_session", session)
    ing = Ingestor(get_settings(), h.redis)
    with pytest.raises(TimeoutError):  # still reconnecting with backoff, not dead
        await asyncio.wait_for(ing.run_forever(), timeout=0.5)


async def test_seen_flag_failure_after_enqueue_is_harmless(h):
    """Evidence (correct): STORE \\Seen failing -> reconnect -> the message is ingested again ->
    ON CONFLICT no-op, one request, one job."""
    raw = make_email(REQUEST)
    await ingest_raw(raw, h.redis)
    await ingest_raw(raw, h.redis)
    assert len(await h.queued_request_ids()) == 1
    assert await db.pool().fetchval("SELECT count(*) FROM requests") == 1


_ = arq  # imported for type context in failure messages


# ======================================================================== IMAP IDLE


class FakeImap:
    """Plain-TCP IMAP server: answers the commands Ingestor uses, then drops the socket during IDLE."""

    def __init__(self, drop_after_s: float) -> None:
        self.drop_after_s = drop_after_s
        self.connections = 0
        self.commands: list[str] = []
        self.writers: list[asyncio.StreamWriter] = []

    async def handle(self, reader, writer):
        self.connections += 1
        self.writers.append(writer)
        writer.write(b"* OK [CAPABILITY IMAP4rev1 IDLE] ready\r\n")
        while line := await reader.readline():
            tag, _, rest = line.decode().strip().partition(" ")
            cmd = rest.split(" ")[0].upper()
            arg = rest.split(" ")[1].upper() if " " in rest else ""
            self.commands.append(f"{cmd} {arg}".strip())
            if cmd == "CAPABILITY":
                writer.write(f"* CAPABILITY IMAP4rev1 IDLE\r\n{tag} OK done\r\n".encode())
            elif cmd == "SELECT":
                writer.write(f"* 0 EXISTS\r\n* OK [UIDVALIDITY 7] ok\r\n{tag} OK [READ-WRITE] done\r\n".encode())
            elif cmd == "UID" and arg == "SEARCH":
                writer.write(f"* SEARCH\r\n{tag} OK done\r\n".encode())
            elif cmd == "IDLE":
                writer.write(b"+ idling\r\n")
                await writer.drain()
                await asyncio.sleep(self.drop_after_s)
                writer.close()  # e.g. Dovecot restart / NAT timeout with RST
                return
            else:
                writer.write(f"{tag} OK done\r\n".encode())
            await writer.drain()


async def test_ingest_reconnects_promptly_after_the_connection_drops_during_idle(h, monkeypatch):
    import aioimaplib

    from agent.mail import ingest

    fake = FakeImap(drop_after_s=0.3)
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(ingest.aioimaplib, "IMAP4_SSL", aioimaplib.IMAP4)  # plain TCP for the fake
    h.configure(imap_host="127.0.0.1", imap_port=port, agent_mail_password="not-the-real-one")
    task = asyncio.create_task(Ingestor(get_settings(), h.redis).run_forever())
    try:
        await asyncio.sleep(4)
    finally:
        for _ in range(10):  # the first cancel lands in logout() on the dead socket (60 s timeout)
            task.cancel()
            await asyncio.wait({task}, timeout=0.2)
            if task.done():
                break
        for w in fake.writers:
            w.close()
        server.close()
    assert "IDLE" in fake.commands, fake.commands
    assert fake.connections >= 2, f"still waiting on a dead IDLE after 3.7 s ({fake.commands})"
