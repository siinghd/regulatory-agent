"""Operator controls for incidents and data-subject requests.

  ragent pause | resume       kill switch (Redis `agent:paused`): requests park without using an
                              attempt and the outbox sends nothing; ingest keeps storing mail
  ragent revoke <request_id>  delete the request's drop upload (a leaked or mis-sent link)
  ragent block <addr|@domain> add to the suppression list: the gate drops their mail unanswered
  ragent dsar export <email>  everything held about an address, as one JSON bundle
  ragent dsar delete <email>  erase it now, revoke its links, suppress the address

Every action writes an `admin.*` (or `dsar.*`) event naming the operator (SUDO_USER, else USER).
Addresses are never stored here in the clear: the suppression list and the events hold HMACs.
"""

import base64
import json
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent import audit, blobs, db, retention
from agent.models import InboundEmail

log = structlog.get_logger()

PAUSE_KEY = "agent:paused"
PAUSED_RETRY_S = 60.0  # how soon parked work and held mail look again
_DELETION = re.compile(r"\bdelete\s+my\s+data\b", re.IGNORECASE)
_DELETION_LINE = re.compile(r"^\W*delete\s+my\s+data\W*$", re.IGNORECASE)

ERASURE_CONFIRMATION = (
    "I've received your request to delete your data. Everything we hold about this address (your "
    "emails, their history and any download links) will be deleted within 30 days, usually within "
    "a day, and I won't process any more email from this address. This is the last email you'll get "
    "from me. If you didn't mean to ask for this, write to {contact}."
)


# ------------------------------------------------------------------ kill switch


async def is_paused(redis: Redis) -> bool:
    """Fails open: if Redis can't answer, the queue that feeds the work is down anyway."""
    try:
        return bool(await redis.exists(PAUSE_KEY))
    except (RedisError, OSError) as e:
        log.warning("admin.pause_unknown", error=f"{type(e).__name__}: {e}")
        return False


async def pause(redis: Redis, *, reason: str = "") -> None:
    value = json.dumps({"operator": audit.operator(), "at": datetime.now(UTC).isoformat(), "reason": reason})
    await redis.set(PAUSE_KEY, value)
    await audit.admin_event("pause", {"reason": reason})
    log.error("admin.paused", operator=audit.operator(), reason=reason, alert=True)


async def resume(redis: Redis) -> bool:
    was = bool(await redis.delete(PAUSE_KEY))
    await audit.admin_event("resume", {"was_paused": was})
    log.warning("admin.resumed", operator=audit.operator(), was_paused=was)
    return was


# ------------------------------------------------------------------ suppression


def _suppression_target(value: str) -> tuple[str, str]:
    """(kind, normalised value) for an address or '@domain'."""
    v = audit.normalise_address(value)
    if v.startswith("@") and "." in v and "@" not in v[1:]:
        return "domain", v
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
        return "address", v
    raise ValueError(f"not an address or @domain: {value!r}")


async def is_suppressed(addr: str) -> bool:
    """Is this sender (or their domain) on the suppression list? Checked first by the gate."""
    if not addr or "@" not in addr:
        return False
    domain = "@" + audit.normalise_address(addr).rsplit("@", 1)[1]
    hashes = [audit.subject_hash(addr), audit.subject_hash(domain)]
    return bool(await db.fetchval("SELECT EXISTS (SELECT 1 FROM suppression WHERE value_h = ANY($1::text[]))", hashes))


async def suppress(value: str, *, reason: str, erase: bool = False, note: str | None = None) -> str:
    """Add an address or '@domain' to the suppression list (idempotent). Returns its HMAC."""
    kind, v = _suppression_target(value)
    h = audit.subject_hash(v)
    await db.execute(
        """INSERT INTO suppression (value_h, kind, reason, erase, note) VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (value_h) DO UPDATE SET erase = suppression.erase OR EXCLUDED.erase,
             note = coalesce(EXCLUDED.note, suppression.note)""",
        h, kind, reason, erase, note,
    )
    return h


async def block(value: str, *, reason: str = "") -> str:
    h = await suppress(value, reason="blocked", note=reason or None)
    kind, _ = _suppression_target(value)
    await audit.admin_event("block", {"kind": kind, "value_h": h, "reason": reason},
                            subject_h=h if kind == "address" else None)
    return h


def asks_for_deletion(email: InboundEmail) -> bool:
    """ "DELETE MY DATA" in the subject, or as a line of its own in the body."""
    if _DELETION.search(email.subject or ""):
        return True
    return any(_DELETION_LINE.match(line) for line in (email.text or "").splitlines()[:40])


async def request_erasure(addr: str, request_id: UUID) -> str:
    """An authenticated sender emailed "DELETE MY DATA": suppress them now (nothing more is
    processed), and let the daily purge erase what we hold once their confirmation has gone."""
    h = await suppress(addr, reason="dsar_email", erase=True)
    await audit.write(request_id, "dsar.requested", {"channel": "email"}, subject_h=h)
    log.error("dsar.requested", request_id=str(request_id), channel="email", alert=True)
    return h


# ------------------------------------------------------------------ links


async def revoke(request_id: UUID) -> bool | None:
    """Delete the request's drop upload and make sure a retry can't reuse the link."""
    row = await db.fetchrow("SELECT delivery FROM requests WHERE id = $1", request_id)
    if row is None:
        raise LookupError(f"no request {request_id}")
    record = row["delivery"]
    revoked = await retention.revoke_delivery(record)
    if revoked:
        await db.execute(
            """UPDATE requests SET delivery = jsonb_build_object('kind', 'revoked', 'drop_id', delivery->>'drop_id',
                 'revoked_at', now()) WHERE id = $1""",
            request_id,
        )
    await audit.admin_event("revoke", {"drop_id": (record or {}).get("drop_id"), "revoked": revoked},
                            request_id=request_id)
    return revoked


# ------------------------------------------------------------------ data subject requests


_SUBJECT_ROWS = """
    SELECT * FROM requests
    WHERE from_h = $1 OR from_addr = 'h:' || $1 OR from_addr = $2
    ORDER BY received_at
"""


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode()
    return value


async def dsar_export(addr: str) -> dict:
    """Everything held about `addr`: request rows, the audit trail (by HMAC), the raw emails
    still retained, and what we sent (metadata). Sealed secrets are left out."""
    addr = audit.normalise_address(addr)
    h = audit.subject_hash(addr)
    rows = await db.fetch(_SUBJECT_ROWS, h, addr)
    requests, raw = [], []
    for r in rows:
        d = {k: _jsonable(v) for k, v in dict(r).items()}
        if isinstance(d.get("delivery"), dict):
            d["delivery"] = {k: v for k, v in d["delivery"].items() if k not in {"url", "delete_token"}}
        requests.append(d)
        sha = r["raw_sha256"]
        if sha and sha != retention.PURGED:
            try:
                data = blobs.read_raw(sha)
            except (FileNotFoundError, OSError):
                continue
            try:
                raw.append({"request_id": str(r["id"]), "sha256": sha, "encoding": "utf-8", "content": data.decode()})
            except UnicodeDecodeError:
                raw.append({"request_id": str(r["id"]), "sha256": sha, "encoding": "base64",
                            "content": base64.b64encode(data).decode()})
    ids = [r["id"] for r in rows]
    outbound = await db.fetch(
        "SELECT request_id, kind, message_id, status, attempts, created_at, sent_at FROM outbound "
        "WHERE request_id = ANY($1::uuid[]) ORDER BY id", ids)
    events = await db.fetch(
        "SELECT id, request_id, kind, data, at, actor, component FROM events "
        "WHERE subject_h = $1 OR request_id = ANY($2::uuid[]) ORDER BY id", h, ids)
    suppressed = await db.fetchrow("SELECT kind, reason, erase, created_at, erased_at FROM suppression WHERE value_h = $1", h)
    bundle = {
        "subject": addr, "subject_h": h, "generated_at": datetime.now(UTC).isoformat(),
        "requests": requests,
        "raw_emails": raw,
        "emails_sent": [{k: _jsonable(v) for k, v in dict(o).items()} for o in outbound],
        "events": [{k: _jsonable(v) for k, v in dict(e).items()} for e in events],
        "suppression": {k: _jsonable(v) for k, v in dict(suppressed).items()} if suppressed else None,
    }
    await audit.admin_event("dsar_export", {"requests": len(requests), "raw_emails": len(raw),
                                            "events": len(events)}, subject_h=h)
    return bundle


async def dsar_delete(addr: str) -> dict:
    """Erase `addr` now: revoke its drop links, delete its requests and raw mail, and suppress
    it (with `erase`, so anything that arrives later is erased by the daily purge too)."""
    addr = audit.normalise_address(addr)
    h = await suppress(addr, reason="dsar_delete", erase=True)
    counts = dict(await retention.erase_subject(h, addr=addr, immediate=True))
    await audit.admin_event("dsar_delete", counts, subject_h=h)
    log.warning("dsar.deleted", operator=audit.operator(), **counts)
    return counts
