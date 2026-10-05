"""Request state machine persistence. Every transition is one committed statement plus an event row.

Transitions are compare-and-set on the current state, so a duplicate job (redelivery, a sweeper
re-enqueue racing a slow worker) can't move a request backwards or process it twice. An email
the transition decides to send is written to the outbox in the same transaction.
"""

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg
import structlog

from agent import audit, crypto, db, limits, metrics
from agent.models import MatterInfo

log = structlog.get_logger()

TERMINAL = {"rejected", "done", "failed"}
SETTLED = TERMINAL | {"clarify"}  # nothing more to do until the requester writes again


async def create_request(
    *,
    message_id: str,
    raw_sha256: str,
    from_addr: str,
    subject: str,
    thread_root: str,
    imap_uid: int | None = None,
    imap_uidvalidity: int | None = None,
    reject_reason: str | None = None,
) -> UUID | None:
    """Insert once per Message-ID. Returns the id, or None if this message was already seen.

    The sender is also recorded as `from_h` (its HMAC): the audit trail refers to people only
    by that, and it outlives the address when the request is pseudonymised. `sender_h` is the
    rate-limit key of the normalised sender (limits.sender_key), for the per-sender in-flight cap.
    With `reject_reason` the request is created already rejected (one statement: a crash can't
    leave it `received` for a worker to pick up).
    """
    from_h = audit.subject_hash(from_addr)
    sender_h = limits.sender_key(from_addr) if from_addr else None
    row = await db.fetchrow(
        """
        INSERT INTO requests (message_id, track_token, raw_sha256, from_addr, from_h, subject, thread_root,
                              imap_uid, imap_uidvalidity, sender_h, state, reject_reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, coalesce($11, 'received'), $12)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING id
        """,
        message_id,
        secrets.token_urlsafe(12),
        raw_sha256,
        from_addr,
        from_h,
        subject[:500],
        thread_root,
        imap_uid,
        imap_uidvalidity,
        sender_h,
        "rejected" if reject_reason else None,
        reject_reason,
    )
    if row:
        await event(row["id"], "received", {"from_h": from_h})
        if reject_reason:
            await event(row["id"], "state:rejected", {"reject_reason": reject_reason})
            metrics.observe_transition(None, "rejected", None, final=True)
    return row["id"] if row else None


SPLIT_PREFIX = "split:"  # message_id of a request split off another email's: split:<matter>:<its message_id>


def split_message_id(parent_message_id: str, matter: str) -> str:
    return f"{SPLIT_PREFIX}{matter}:{parent_message_id}"


async def create_split(parent, *, matter: str, doc_type: str, provider: str, parsed: dict, auth: dict) -> UUID | None:
    """A further matter of `parent`'s email as a request of its own, created `accepted` (it
    shares the parent's verified sender, raw MIME and receipt time; it never sees the gate).
    Once per (email, matter): returns None if it exists already."""
    row = await db.fetchrow(
        """
        INSERT INTO requests (message_id, track_token, raw_sha256, from_addr, from_h, subject, thread_root,
                              sender_h, received_at, state, auth, parsed, provider, matter, doc_type)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'accepted', $10, $11, $12, $13, $14)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING id
        """,
        split_message_id(parent["message_id"], matter),
        secrets.token_urlsafe(12),
        parent["raw_sha256"],
        parent["from_addr"],
        parent["from_h"],
        parent["subject"],
        parent["thread_root"],
        parent["sender_h"],
        parent["received_at"],
        auth,
        parsed,
        provider,
        matter,
        doc_type,
    )
    if row:
        await event(row["id"], "received", {"from_h": parent["from_h"], "split_from": str(parent["id"])})
        await event(row["id"], "state:accepted", {"split_from": str(parent["id"]), "matter": matter,
                                                  "doc_type": doc_type})
        metrics.observe_transition(None, "accepted", None, final=False, provider=provider)
    return row["id"] if row else None


async def get(request_id: UUID) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM requests WHERE id = $1", request_id)


# The progress page's view of a request: columns the web role may read (deploy/sql/grants.sql
# grants agent_web these on requests, and /status's aggregate columns, agent.web.status.STATUS_COLUMNS).
PROGRESS_COLUMNS = ("id", "track_token", "state", "progress", "result", "matter", "doc_type", "from_addr",
                    "reject_reason", "received_at", "updated_at")


async def get_by_token(token: str) -> asyncpg.Record | None:
    return await db.fetchrow(f"SELECT {', '.join(PROGRESS_COLUMNS)} FROM requests WHERE track_token = $1", token)


async def get_by_message_id(message_id: str) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT id, state FROM requests WHERE message_id = $1", message_id)


# ------------------------------------------------------------------ transitions


@dataclass(frozen=True)
class OutboundMsg:
    """A rendered email to write to the outbox together with a state change."""

    kind: str  # ack | reply | notice (a request waiting on a daily limit: "this is delayed")
    message_id: str
    body: bytes  # RFC 5322 bytes; sealed before it is stored
    final_state: str | None = None  # the request's state once this has been sent
    has_attachment: bool = False


_TRANSITION_FIELDS = {"reject_reason", "auth", "parsed", "provider", "matter", "doc_type", "result", "error"}


async def transition(
    request_id: UUID, from_states: set[str], to_state: str, *, outbound: OutboundMsg | None = None, **fields: Any
) -> bool:
    """CAS state change. Returns False (and changes nothing) if the request isn't in from_states."""
    async with db.acquire() as conn, conn.transaction():
        return await transition_in(conn, request_id, from_states, to_state, outbound=outbound, **fields)


async def transition_in(
    conn: asyncpg.Connection,
    request_id: UUID,
    from_states: set[str],
    to_state: str,
    *,
    outbound: OutboundMsg | None = None,
    **fields: Any,
) -> bool:
    """`transition` inside the caller's transaction."""
    bad = set(fields) - _TRANSITION_FIELDS
    if bad:
        raise ValueError(f"unknown fields {bad}")
    cols = list(fields)
    sets = ", ".join(f"{c} = ${i + 4}" for i, c in enumerate(cols))
    # `old` is the row as the statement found it: the state being left, and how long ago it was
    # entered (its state event, or the request's arrival), for the stage-duration metric.
    row = await conn.fetchrow(
        f"""
        UPDATE requests r SET state = $3, updated_at = now(){', ' + sets if sets else ''}
        FROM (SELECT q.state AS from_state,
                     extract(epoch FROM now() - coalesce(
                         (SELECT max(e.at) FROM events e WHERE e.request_id = q.id AND e.kind = 'state:' || q.state),
                         q.received_at)) AS stage_s
              FROM requests q WHERE q.id = $1) old
        WHERE r.id = $1 AND r.state = ANY($2::text[])
        RETURNING r.id, r.provider, old.from_state, old.stage_s
        """,
        request_id,
        list(from_states),
        to_state,
        *[fields[c] for c in cols],
    )
    if row is None:
        return False
    await audit.write_in(
        conn, request_id, f"state:{to_state}",
        {k: v for k, v in fields.items() if k in {"reject_reason", "error", "matter", "doc_type"}},
    )
    metrics.observe_transition(row["from_state"], to_state, float(row["stage_s"] or 0), final=to_state in SETTLED,
                               provider=row["provider"])
    if to_state == "failed":
        await dead_letter_in(conn, request_id, fields.get("error"))
    if outbound is not None:
        await _queue_outbound(conn, request_id, outbound)
    return True


async def dead_letter_in(conn: asyncpg.Connection, request_id: UUID, error: str | None) -> None:
    """Every request that ends `failed` is recorded and alerted on, whatever path got it there."""
    await audit.write_in(conn, request_id, "dead_letter", {"error": (error or "")[:500]})
    log.error("request.dead_letter", request_id=str(request_id), error=(error or "")[:300], alert=True)


async def set_auth(request_id: UUID, auth: dict) -> None:
    """Record the sender verification verdict as soon as it is known: only a request whose stored
    verdict is "pass" may ever be answered, including with an apology."""
    await db.execute("UPDATE requests SET auth = $2 WHERE id = $1 AND state = 'received'", request_id, auth)


async def set_progress(request_id: UUID, step: str, *, done: int | None = None, total: int | None = None) -> None:
    progress = {"step": step, "done": done, "total": total, "at": datetime.now(UTC).isoformat()}
    await db.execute(
        "UPDATE requests SET progress = $2, updated_at = now() WHERE id = $1", request_id, progress
    )


async def begin_attempt(request_id: UUID) -> tuple[int, datetime] | None:
    """Count one attempt (attempts live here, not in the queue, which resets its own counter).

    Returns (attempts so far including this one, received_at), or None if nothing is left to do.
    """
    row = await db.fetchrow(
        """
        UPDATE requests SET attempts = attempts + 1
        WHERE id = $1 AND state <> ALL($2::text[])
        RETURNING attempts, received_at
        """,
        request_id,
        list(SETTLED),
    )
    return (row["attempts"], row["received_at"]) if row else None


async def refund_attempt(request_id: UUID) -> None:
    """A parked try (dependency down, work locked) didn't really use the request's attempt."""
    await db.execute("UPDATE requests SET attempts = greatest(attempts - 1, 0) WHERE id = $1", request_id)


async def event(request_id: UUID | None, kind: str, data: dict | None = None, *, subject_h: str | None = None) -> None:
    """Append to the audit trail (agent.audit: attributed, scrubbed of addresses)."""
    await audit.write(request_id, kind, data, subject_h=subject_h)


async def event_in(conn: asyncpg.Connection, request_id: UUID | None, kind: str, data: dict | None = None) -> None:
    """`event` inside the caller's transaction."""
    await audit.write_in(conn, request_id, kind, data)


async def events(request_id: UUID) -> list[asyncpg.Record]:
    return await db.fetch(
        "SELECT kind, data, at FROM events WHERE request_id = $1 ORDER BY id", request_id
    )


async def last_retry(request_id: UUID) -> dict | None:
    row = await db.fetchrow(
        "SELECT data FROM events WHERE request_id = $1 AND kind = 'retry' ORDER BY id DESC LIMIT 1", request_id
    )
    return row["data"] if row else None


# ------------------------------------------------------------------ outbox


async def _queue_outbound(conn: asyncpg.Connection, request_id: UUID, msg: OutboundMsg) -> None:
    # Same Message-ID again (a re-rendered reply, an apology replacing an unsent reply): replace the
    # unsent body. A message that has gone out is never replaced.
    await conn.execute(
        """
        INSERT INTO outbound (request_id, kind, message_id, body, has_attachment, final_state)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (message_id) DO UPDATE SET
          body = EXCLUDED.body, has_attachment = EXCLUDED.has_attachment, final_state = EXCLUDED.final_state,
          status = 'queued', attempts = 0, next_attempt_at = now(), last_error = NULL
        WHERE outbound.status <> 'sent'
        """,
        request_id,
        msg.kind,
        msg.message_id,
        crypto.seal(msg.body, aad=msg.message_id.encode()),
        msg.has_attachment,
        msg.final_state,
    )
    col = {"ack": "ack_message_id", "reply": "reply_message_id"}.get(msg.kind)
    if col:
        await conn.execute(f"UPDATE requests SET {col} = $2 WHERE id = $1", request_id, msg.message_id)


async def queue_notice(request_id: UUID, msg: OutboundMsg) -> bool:
    """Write a notice (no state change goes with it) to the outbox, once per Message-ID. False if
    it is there already, whatever its status."""
    async with db.acquire() as conn, conn.transaction():
        if await conn.fetchval("SELECT 1 FROM outbound WHERE message_id = $1", msg.message_id):
            return False
        await _queue_outbound(conn, request_id, msg)
    return True


def outbound_body(row: asyncpg.Record) -> bytes:
    return crypto.unseal(row["body"], aad=row["message_id"].encode())


async def outbound_rows(request_id: UUID) -> list[asyncpg.Record]:
    return await db.fetch(
        "SELECT id, kind, message_id, status, attempts, has_attachment, final_state, next_attempt_at "
        "FROM outbound WHERE request_id = $1 ORDER BY id",
        request_id,
    )


async def due_outbound(overdue: timedelta, limit: int = 200) -> list[str]:
    """Queued messages well past their next attempt: their queue job was lost (Redis flushed...)."""
    rows = await db.fetch(
        """
        SELECT message_id FROM outbound
        WHERE status = 'queued' AND next_attempt_at < now() - $1::interval
        ORDER BY next_attempt_at LIMIT $2
        """,
        overdue,
        limit,
    )
    return [r["message_id"] for r in rows]


# ------------------------------------------------------------------ delivery record


async def save_delivery(request_id: UUID, record: dict) -> None:
    await db.execute("UPDATE requests SET delivery = $2 WHERE id = $1", request_id, record)


async def load_delivery(request_id: UUID) -> dict | None:
    return await db.fetchval("SELECT delivery FROM requests WHERE id = $1", request_id)


# ------------------------------------------------------------------ threads & limits


async def previous_in_thread(thread_root: str, exclude_id: UUID, from_addr: str) -> asyncpg.Record | None:
    """Most recent earlier request *by the same sender* in this thread that resolved a matter.

    Scoped to the sender: Message-IDs are not secrets, and a stranger replying into someone
    else's thread must not inherit (or exhaust) that conversation's context. An email split into
    several matters leaves no one matter to inherit: None.
    """
    rows = await db.fetch(
        """
        SELECT matter, doc_type, provider, state FROM requests r
        WHERE thread_root = $1 AND id <> $2 AND from_addr = $3 AND matter IS NOT NULL
          AND received_at = (SELECT max(received_at) FROM requests
                             WHERE thread_root = $1 AND id <> $2 AND from_addr = $3 AND matter IS NOT NULL)
        ORDER BY (message_id LIKE $4) LIMIT 2
        """,
        thread_root,
        exclude_id,
        from_addr,
        SPLIT_PREFIX + "%",
    )
    if len({r["matter"] for r in rows}) > 1:
        return None
    return rows[0] if rows else None


async def thread_size(thread_root: str, from_addr: str) -> int:
    row = await db.fetchrow(
        "SELECT count(*) AS n FROM requests WHERE thread_root = $1 AND from_addr = $2 AND message_id NOT LIKE $3",
        thread_root, from_addr, SPLIT_PREFIX + "%",
    )
    return row["n"]


INFLIGHT_STATES = ("fetching", "packaging")  # the expensive part: portal visits, downloads, packaging


async def inflight_count(sender_h: str, exclude_id: UUID) -> int:
    """The sender's other requests now fetching or packaging (the per-sender in-flight cap)."""
    return await db.fetchval(
        "SELECT count(*) FROM requests WHERE sender_h = $1 AND id <> $2 AND state = ANY($3::text[])",
        sender_h, exclude_id, list(INFLIGHT_STATES),
    )


async def stuck_requests(older_than: timedelta) -> list[asyncpg.Record]:
    """Unfinished requests nothing has touched for a while. A request whose reply is queued in
    the outbox is not stuck: the outbox owns it until the reply is sent or given up on."""
    return await db.fetch(
        """
        SELECT r.id, r.state, r.attempts, r.received_at FROM requests r
        WHERE r.state <> ALL($2::text[]) AND r.updated_at < now() - $1::interval
          AND NOT EXISTS (SELECT 1 FROM outbound o
                          WHERE o.request_id = r.id AND o.kind = 'reply' AND o.status = 'queued')
        ORDER BY r.updated_at LIMIT 100
        """,
        older_than,
        list(SETTLED),
    )


# ------------------------------------------------------------------ mailbox hygiene


async def expungeable(uidvalidity: int, older_than: timedelta, limit: int = 200) -> list[asyncpg.Record]:
    """Requests settled for `older_than` whose message is still in the agent's mailbox."""
    return await db.fetch(
        """
        SELECT id, imap_uid FROM requests
        WHERE imap_uid IS NOT NULL AND imap_expunged_at IS NULL AND imap_uidvalidity = $1
          AND state = ANY($2::text[]) AND updated_at < now() - $3::interval
        ORDER BY updated_at LIMIT $4
        """,
        uidvalidity,
        list(SETTLED),
        older_than,
        limit,
    )


async def mark_expunged(request_ids: list[UUID]) -> None:
    await db.execute("UPDATE requests SET imap_expunged_at = now() WHERE id = ANY($1::uuid[])", request_ids)


# ------------------------------------------------------------------ matter / document cache


@dataclass(frozen=True)
class Listing:
    """One category's listing as cached: public ids in portal order, and what the portal showed."""

    ids: list[str]  # public external ids, in portal order, at most `limit` of them
    confidential: int  # rows listed but not public (never sent)
    rows: int  # rows the portal listed: public + confidential
    limit: int  # the limit the listing was made with
    count: int  # the category's count when it was listed

    @property
    def complete(self) -> bool:
        """The listing saw every row of the category."""
        return self.rows >= self.count

    def serves(self, limit: int, count: int) -> bool:
        """Can a request for `limit` documents be answered from this listing, given today's count?"""
        return count == self.count and (self.complete or self.limit >= limit)


@dataclass(frozen=True)
class CachedMatter:
    info: MatterInfo
    fetched_at: datetime
    listings: dict[str, Any]

    def listing(self, doc_type: str, ttl: timedelta) -> Listing | None:
        """The category's listing if it is younger than `ttl`.

        Each listing records how much older it is than the row's `fetched_at` (`behind_s`, kept
        up to date whenever `fetched_at` moves), so refreshing the matter info or another
        category never makes an old listing look fresh.
        """
        raw = self.listings.get(doc_type)
        if not isinstance(raw, dict) or "ids" not in raw:  # absent, or the format before per-listing ages
            return None
        age = datetime.now(UTC) - self.fetched_at + timedelta(seconds=float(raw.get("behind_s", 0)))
        if age > ttl:
            return None
        return Listing(ids=list(raw["ids"]), confidential=int(raw.get("confidential", 0)),
                       rows=int(raw.get("rows", 0)), limit=int(raw.get("limit", 0)), count=int(raw.get("count", 0)))


async def cached_matter(provider: str, matter: str, max_age: timedelta) -> CachedMatter | None:
    row = await db.fetchrow(
        "SELECT info, listings, fetched_at FROM matters WHERE provider = $1 AND matter = $2 AND fetched_at > now() - $3::interval",
        provider,
        matter,
        max_age,
    )
    if not row:
        return None
    return CachedMatter(MatterInfo.model_validate(row["info"]), row["fetched_at"], row["listings"] or {})


async def save_matter(info: MatterInfo, doc_type: str | None = None, listing: Listing | None = None) -> None:
    info_json = json.loads(info.model_dump_json())
    async with db.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO matters (provider, matter, info, listings, fetched_at) VALUES ($1, $2, $3, '{}', $4)
               ON CONFLICT (provider, matter) DO NOTHING""",
            info.provider, info.matter, info_json, info.fetched_at,
        )
        row = await conn.fetchrow(
            "SELECT listings, fetched_at FROM matters WHERE provider = $1 AND matter = $2 FOR UPDATE",
            info.provider, info.matter,
        )
        old_at: datetime = row["fetched_at"]
        new_at = max(old_at, info.fetched_at)
        shift = (new_at - old_at).total_seconds()
        listings = {
            k: {**v, "behind_s": float(v.get("behind_s", 0)) + shift}
            for k, v in (row["listings"] or {}).items()
            if isinstance(v, dict) and "ids" in v  # drop entries in the old format
        }
        if doc_type and listing is not None:
            listings[doc_type] = {
                "ids": listing.ids, "confidential": listing.confidential, "rows": listing.rows,
                "limit": listing.limit, "count": listing.count,
                "behind_s": (new_at - info.fetched_at).total_seconds(),
            }
        newer = info.fetched_at >= old_at
        await conn.execute(
            """UPDATE matters SET listings = $3, fetched_at = $4, info = CASE WHEN $5 THEN $6::jsonb ELSE info END
               WHERE provider = $1 AND matter = $2""",
            info.provider, info.matter, listings, new_at, newer, info_json,
        )


async def upsert_document(ref, *, sha256: str | None = None, size: int | None = None, filename: str | None = None) -> UUID:
    """One row per (provider, matter, external id): UARB exhibit numbers repeat across matters."""
    row = await db.fetchrow(
        """
        INSERT INTO documents (provider, matter, doc_type, external_id, title, filed_on, sha256, size_bytes, filename,
                               downloaded_at, last_used_at, source_type)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CASE WHEN $7::text IS NULL THEN NULL ELSE now() END, now(), $10)
        ON CONFLICT (provider, matter, external_id) DO UPDATE SET
          title = EXCLUDED.title, filed_on = EXCLUDED.filed_on,
          sha256 = COALESCE(EXCLUDED.sha256, documents.sha256),
          size_bytes = COALESCE(EXCLUDED.size_bytes, documents.size_bytes),
          filename = COALESCE(EXCLUDED.filename, documents.filename),
          downloaded_at = COALESCE(EXCLUDED.downloaded_at, documents.downloaded_at),
          source_type = COALESCE(EXCLUDED.source_type, documents.source_type),
          last_used_at = now()
        RETURNING id
        """,
        ref.provider,
        ref.matter,
        ref.doc_type,
        ref.external_id,
        ref.title,
        ref.filed_on,
        sha256,
        size,
        filename,
        ref.source_type,
    )
    return row["id"]


async def documents_by_external_ids(provider: str, matter: str, ids: list[str]) -> dict[str, asyncpg.Record]:
    """The documents a request is using; marks them used (at most hourly), which keeps their
    stored files and page text from retention's disposal (blob_retention_days)."""
    async with db.acquire() as conn:
        await conn.execute(
            """UPDATE documents SET last_used_at = now()
               WHERE provider = $1 AND matter = $2 AND external_id = ANY($3::text[])
                 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')""",
            provider, matter, ids,
        )
        rows = await conn.fetch(
            "SELECT * FROM documents WHERE provider = $1 AND matter = $2 AND external_id = ANY($3::text[])",
            provider,
            matter,
            ids,
        )
    return {r["external_id"]: r for r in rows}
