"""Outbox: durable delivery of the agent's emails.

A message is rendered and written to `outbound` in the same transaction as the state change that
decides to send it (`store.transition(..., outbound=...)`). Sending is separate from deciding:
- inline, right after that commit (`send_now`): the common case, no added latency;
- otherwise by the queue task `send_outbound`, with its own backoff for 4xx and connection errors
  for up to 48 h (the sweeper re-enqueues anything whose job was lost).

While a message is being sent its row is locked, and it is marked sent in the same database
transaction as the request's final state change: a crash in between resends the same Message-ID
(at-least-once, deduplicable by the recipient), never a different email.

SMTP 5xx for this message: undeliverable, alert, and the request ends failed (DeliveryFailed).
552 (too large) for a reply carrying an attachment: the request goes back to packaging and the
documents are re-sent as a link. A queued acknowledgement is sent before its request's reply, or
dropped once the reply has gone ("I'm collecting your documents" must never follow them); so is
a queued delay notice (kind "notice", sent at most once per request).
"""

import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import asyncpg
import structlog
from arq.connections import ArqRedis
from redis.exceptions import RedisError

from agent import admin, audit, breaker, crypto, db, metrics, queue, store
from agent.breaker import Breakers
from agent.mail import outbound

log = structlog.get_logger()

MAX_AGE = timedelta(hours=48)
RETRY_BASE_S = 60.0
RETRY_CAP_S = 3600.0
TOO_LARGE = 552
# outbound_messages_total's outcome for what `_failed` decided (a 552 re-render: this message was undeliverable)
_FAILED_OUTCOME = {"queued": "deferred", "undeliverable": "undeliverable", "rerender": "undeliverable"}


@dataclass(frozen=True)
class Outcome:
    # sent | queued (try again in retry_in_s) | undeliverable | superseded | rerender | busy | missing
    status: str
    request_id: UUID | None = None
    retry_in_s: float | None = None


def backoff_s(attempts: int) -> float:
    step = min(RETRY_CAP_S, RETRY_BASE_S * 2 ** max(attempts - 1, 0))
    return random.uniform(0.5, 1.0) * step


async def send_now(message_id: str, *, breakers: Breakers | None, queue_redis: ArqRedis | None) -> Outcome:
    """Try once now; if that fails, schedule the outbox job (best effort: the sweeper backs it up)."""
    outcome = await attempt(message_id, breakers=breakers)
    if queue_redis is not None:
        try:
            if outcome.status == "queued":
                await queue.enqueue_outbound(queue_redis, message_id, defer_s=outcome.retry_in_s)
            elif outcome.status == "rerender" and outcome.request_id:
                await queue.enqueue_request(queue_redis, outcome.request_id)
        except (RedisError, OSError) as e:
            log.warning("outbound.enqueue_failed", message_id=message_id, error=f"{type(e).__name__}: {e}")
    return outcome


async def attempt(message_id: str, *, breakers: Breakers | None, flush_acks: bool = True) -> Outcome:
    """One delivery attempt, without scheduling the next one."""
    head = await db.fetchrow("SELECT request_id, kind, status FROM outbound WHERE message_id = $1", message_id)
    if head is None:
        return Outcome("missing")
    if head["status"] != "queued":
        return Outcome(head["status"], head["request_id"])
    if breakers is not None and await admin.is_paused(breakers.r):  # kill switch: send nothing
        return Outcome("queued", head["request_id"], admin.PAUSED_RETRY_S)
    if head["kind"] == "reply" and flush_acks:
        for row in await store.outbound_rows(head["request_id"]):
            if row["kind"] == "ack" and row["status"] == "queued":
                await attempt(row["message_id"], breakers=breakers, flush_acks=False)  # keep the order
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT * FROM outbound WHERE message_id = $1 FOR UPDATE SKIP LOCKED", message_id
        )
        if row is None:
            return Outcome("busy", head["request_id"])  # another sender holds it right now
        if row["status"] != "queued":
            return Outcome(row["status"], row["request_id"])
        try:
            body = store.outbound_body(row)
            msg = outbound.parse(body)
        except crypto.SealError as e:
            await _give_up(conn, row, f"unreadable: {e}", row["attempts"])
            metrics.observe_outbound(row["kind"], "undeliverable")
            return Outcome("undeliverable", row["request_id"])
        try:
            if breakers is None:
                await outbound.send(msg)
            else:
                async with breakers.call("smtp"):
                    await outbound.send(msg)
        except breaker.Open as e:  # SMTP is known to be down: wait for it without using an attempt
            metrics.observe_outbound(row["kind"], "deferred")
            return Outcome("queued", row["request_id"], e.retry_in_s + random.uniform(0, 60))
        except Exception as e:  # noqa: BLE001 - every failure is classified below
            outcome = await _failed(conn, row, e, msg)
            metrics.observe_outbound(row["kind"], _FAILED_OUTCOME[outcome.status])
            return outcome
        await _sent(conn, row, msg, len(body))
        metrics.observe_outbound(row["kind"], "sent")
        return Outcome("sent", row["request_id"])


async def _sent(conn: asyncpg.Connection, row: asyncpg.Record, msg, size: int) -> None:
    rid, kind = row["request_id"], row["kind"]
    await conn.execute(
        "UPDATE outbound SET status = 'sent', sent_at = now(), attempts = attempts + 1, last_error = NULL WHERE id = $1",
        row["id"],
    )
    col = {"ack": "ack_sent_at", "reply": "reply_sent_at"}.get(kind)  # a notice has no column of its own
    if col:
        await conn.execute(f"UPDATE requests SET {col} = now() WHERE id = $1", rid)
    req = await conn.fetchrow(
        """SELECT result->>'drop_id' AS drop_id, state, extract(epoch FROM clock_timestamp() - received_at)::float8 AS e2e_s
           FROM requests WHERE id = $1""",
        rid,
    ) if kind == "reply" else None
    drop_id = req["drop_id"] if req else None
    await store.event_in(conn, rid, f"sent:{kind}",
                         audit.outbound_data(row["message_id"], kind, msg, size=size, code=250, drop_id=drop_id))
    if req:  # the latency objective: email received -> reply accepted (a slow-down reply closed it as queued)
        metrics.observe_reply_sent(row["final_state"] or req["state"], req["e2e_s"])
    if kind == "reply":
        superseded = await conn.fetch(
            """UPDATE outbound SET status = 'superseded', last_error = 'the reply was sent first'
               WHERE request_id = $1 AND kind IN ('ack', 'notice') AND status = 'queued' RETURNING kind""",
            rid,
        )
        for r in superseded:
            metrics.observe_outbound(r["kind"], "suppressed")
        if row["final_state"]:
            await store.transition_in(conn, rid, {"replying"}, row["final_state"])
    log.info("outbound.sent", request_id=str(rid), kind=kind, attempts=row["attempts"] + 1)


async def _failed(conn: asyncpg.Connection, row: asyncpg.Record, exc: Exception, msg=None) -> Outcome:
    rid = row["request_id"]
    permanent, code = outbound.smtp_failure(exc)
    error = f"{type(exc).__name__}: {str(exc)[:300]}"
    attempts = row["attempts"] + 1
    if permanent and code == TOO_LARGE and row["kind"] == "reply" and row["has_attachment"]:
        await conn.execute(
            "UPDATE outbound SET status = 'undeliverable', attempts = $2, last_error = $3 WHERE id = $1",
            row["id"], attempts, error,
        )
        reopened = await conn.fetchrow(
            """UPDATE requests SET state = 'packaging', updated_at = now(),
                      result = coalesce(result, '{}'::jsonb) || '{"link_only": true}'::jsonb
               WHERE id = $1 AND state = 'replying' RETURNING id""",
            rid,
        )
        if reopened:
            await store.event_in(conn, rid, "reply_too_large", {"code": code, "message_id": row["message_id"]})
            await store.event_in(conn, rid, "state:packaging", {})
            log.warning("outbound.too_large_relinking", request_id=str(rid), error=error)
            return Outcome("rerender", rid)
    if permanent:
        await _give_up(conn, row, error, attempts, code=code, msg=msg)
        return Outcome("undeliverable", rid)
    if datetime.now(UTC) - row["created_at"] > MAX_AGE:
        await _give_up(conn, row, f"still failing after {MAX_AGE}: {error}", attempts, code=code, msg=msg)
        return Outcome("undeliverable", rid)
    delay = backoff_s(attempts)
    await conn.execute(
        """UPDATE outbound SET attempts = $2, last_error = $3, next_attempt_at = now() + make_interval(secs => $4)
           WHERE id = $1""",
        row["id"], attempts, error, delay,
    )
    log.warning("outbound.retry", request_id=str(rid), kind=row["kind"], attempts=attempts, error=error,
                defer_s=round(delay))
    return Outcome("queued", rid, delay)


async def _give_up(
    conn: asyncpg.Connection, row: asyncpg.Record, error: str, attempts: int, *, code: int | None = None, msg=None
) -> None:
    rid = row["request_id"]
    await conn.execute(
        "UPDATE outbound SET status = 'undeliverable', attempts = $2, last_error = $3 WHERE id = $1",
        row["id"], attempts, error,
    )
    await store.event_in(conn, rid, "undeliverable",
                         audit.outbound_data(row["message_id"], row["kind"], msg, size=None, code=code, error=error))
    log.error("outbound.undeliverable", request_id=str(rid), kind=row["kind"], error=audit.scrub(error), alert=True)
    if row["kind"] == "reply" and row["final_state"]:
        await store.transition_in(conn, rid, {"replying"}, "failed", error=f"DeliveryFailed: {error[:450]}")
