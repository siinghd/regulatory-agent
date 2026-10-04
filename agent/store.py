"""Request state machine persistence. Every transition is one committed statement plus an event row.

Transitions are compare-and-set on the current state, so a duplicate job (redelivery, a sweeper
re-enqueue racing a slow worker) can't move a request backwards or process it twice.
"""

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import asyncpg

from agent import db
from agent.models import MatterInfo

TERMINAL = {"rejected", "done", "failed"}


async def create_request(
    *,
    message_id: str,
    raw_sha256: str,
    from_addr: str,
    subject: str,
    thread_root: str,
    imap_uid: int | None = None,
    imap_uidvalidity: int | None = None,
) -> UUID | None:
    """Insert once per Message-ID. Returns the id, or None if this message was already seen."""
    row = await db.fetchrow(
        """
        INSERT INTO requests (message_id, track_token, raw_sha256, from_addr, subject, thread_root,
                              imap_uid, imap_uidvalidity)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (message_id) DO NOTHING
        RETURNING id
        """,
        message_id,
        secrets.token_urlsafe(12),
        raw_sha256,
        from_addr,
        subject[:500],
        thread_root,
        imap_uid,
        imap_uidvalidity,
    )
    if row:
        await event(row["id"], "received", {"from": from_addr})
    return row["id"] if row else None


async def get(request_id: UUID) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM requests WHERE id = $1", request_id)


async def get_by_token(token: str) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM requests WHERE track_token = $1", token)


async def transition(request_id: UUID, from_states: set[str], to_state: str, **fields: Any) -> bool:
    """CAS state change. Returns False (and changes nothing) if the request isn't in from_states."""
    allowed = {"reject_reason", "auth", "parsed", "provider", "matter", "doc_type", "result", "error"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown fields {bad}")
    cols = list(fields)
    sets = ", ".join(f"{c} = ${i + 4}" for i, c in enumerate(cols))
    async with db.pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            f"""
            UPDATE requests SET state = $3, updated_at = now(){', ' + sets if sets else ''}
            WHERE id = $1 AND state = ANY($2::text[])
            RETURNING id
            """,
            request_id,
            list(from_states),
            to_state,
            *[fields[c] for c in cols],
        )
        if row:
            await conn.execute(
                "INSERT INTO events (request_id, kind, data) VALUES ($1, $2, $3)",
                request_id,
                f"state:{to_state}",
                {k: v for k, v in fields.items() if k in {"reject_reason", "error", "matter", "doc_type"}},
            )
    return row is not None


async def set_progress(request_id: UUID, step: str, *, done: int | None = None, total: int | None = None) -> None:
    progress = {"step": step, "done": done, "total": total, "at": datetime.now(UTC).isoformat()}
    await db.execute(
        "UPDATE requests SET progress = $2, updated_at = now() WHERE id = $1", request_id, progress
    )


async def bump_attempts(request_id: UUID) -> int:
    row = await db.fetchrow(
        "UPDATE requests SET attempts = attempts + 1 WHERE id = $1 RETURNING attempts", request_id
    )
    return row["attempts"]


async def event(request_id: UUID, kind: str, data: dict | None = None) -> None:
    await db.execute(
        "INSERT INTO events (request_id, kind, data) VALUES ($1, $2, $3)", request_id, kind, data or {}
    )


async def events(request_id: UUID) -> list[asyncpg.Record]:
    return await db.fetch(
        "SELECT kind, data, at FROM events WHERE request_id = $1 ORDER BY id", request_id
    )


# ------------------------------------------------------------------ outbound idempotency


async def reserve_outbound(request_id: UUID, kind: str, message_id: str) -> bool:
    """Record the Message-ID we're about to send. False if this mail was already sent.

    Send happens after this commit; `mark_sent` after the SMTP OK. A crash in between resends
    the same Message-ID on retry (at-least-once, deduplicable by the recipient's client),
    never a second, different email.
    """
    col_id, col_at = {"ack": ("ack_message_id", "ack_sent_at"), "reply": ("reply_message_id", "reply_sent_at")}[kind]
    row = await db.fetchrow(
        f"""
        UPDATE requests SET {col_id} = COALESCE({col_id}, $2)
        WHERE id = $1 AND {col_at} IS NULL
        RETURNING {col_id}
        """,
        request_id,
        message_id,
    )
    return row is not None


async def mark_sent(request_id: UUID, kind: str) -> None:
    col_at = {"ack": "ack_sent_at", "reply": "reply_sent_at"}[kind]
    await db.execute(f"UPDATE requests SET {col_at} = now() WHERE id = $1", request_id)
    await event(request_id, f"sent:{kind}")


# ------------------------------------------------------------------ threads & limits


async def previous_in_thread(thread_root: str, exclude_id: UUID, from_addr: str) -> asyncpg.Record | None:
    """Most recent earlier request *by the same sender* in this thread that resolved a matter.

    Scoped to the sender: Message-IDs are not secrets, and a stranger replying into someone
    else's thread must not inherit (or exhaust) that conversation's context.
    """
    return await db.fetchrow(
        """
        SELECT matter, doc_type, provider FROM requests
        WHERE thread_root = $1 AND id <> $2 AND from_addr = $3 AND matter IS NOT NULL
        ORDER BY received_at DESC LIMIT 1
        """,
        thread_root,
        exclude_id,
        from_addr,
    )


async def thread_size(thread_root: str, from_addr: str) -> int:
    row = await db.fetchrow(
        "SELECT count(*) AS n FROM requests WHERE thread_root = $1 AND from_addr = $2", thread_root, from_addr
    )
    return row["n"]


async def stuck_requests(older_than: timedelta) -> list[UUID]:
    rows = await db.fetch(
        """
        SELECT id FROM requests
        WHERE state NOT IN ('rejected', 'done', 'failed', 'clarify') AND updated_at < now() - $1::interval
        ORDER BY updated_at LIMIT 100
        """,
        older_than,
    )
    return [r["id"] for r in rows]


# ------------------------------------------------------------------ matter / document cache


async def cached_matter(provider: str, matter: str, max_age: timedelta) -> tuple[MatterInfo, dict] | None:
    row = await db.fetchrow(
        "SELECT info, listings, fetched_at FROM matters WHERE provider = $1 AND matter = $2 AND fetched_at > now() - $3::interval",
        provider,
        matter,
        max_age,
    )
    if not row:
        return None
    return MatterInfo.model_validate(row["info"]), row["listings"] or {}


async def save_matter(info: MatterInfo, doc_type: str | None = None, listing: list[str] | int | None = None) -> None:
    await db.execute(
        """
        INSERT INTO matters (provider, matter, info, listings, fetched_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (provider, matter) DO UPDATE
          SET info = EXCLUDED.info, fetched_at = EXCLUDED.fetched_at,
              listings = CASE WHEN matters.fetched_at > now() - interval '6 hours'
                              THEN matters.listings || EXCLUDED.listings ELSE EXCLUDED.listings END
        """,
        info.provider,
        info.matter,
        json.loads(info.model_dump_json()),
        {doc_type: listing} if doc_type and listing is not None else {},
        info.fetched_at,
    )


async def upsert_document(ref, *, sha256: str | None = None, size: int | None = None, filename: str | None = None) -> UUID:
    row = await db.fetchrow(
        """
        INSERT INTO documents (provider, matter, doc_type, external_id, title, filed_on, sha256, size_bytes, filename,
                               downloaded_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, CASE WHEN $7::text IS NULL THEN NULL ELSE now() END)
        ON CONFLICT (provider, external_id) DO UPDATE SET
          title = EXCLUDED.title, filed_on = EXCLUDED.filed_on,
          sha256 = COALESCE(EXCLUDED.sha256, documents.sha256),
          size_bytes = COALESCE(EXCLUDED.size_bytes, documents.size_bytes),
          filename = COALESCE(EXCLUDED.filename, documents.filename),
          downloaded_at = COALESCE(EXCLUDED.downloaded_at, documents.downloaded_at)
        RETURNING id
        """,
        ref.provider,
        ref.matter,
        ref.doc_type.value,
        ref.external_id,
        ref.title,
        ref.filed_on,
        sha256,
        size,
        filename,
    )
    return row["id"]


async def documents_by_external_ids(provider: str, ids: list[str]) -> dict[str, asyncpg.Record]:
    rows = await db.fetch(
        "SELECT * FROM documents WHERE provider = $1 AND external_id = ANY($2::text[])", provider, ids
    )
    return {r["external_id"]: r for r in rows}
