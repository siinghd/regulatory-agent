"""Retention and disposal (`ragent purge [--dry-run]`, and a daily worker cron where the database
role may delete). Each step works in batches (FOR UPDATE SKIP LOCKED, so live work is never
blocked or raced), is idempotent, and the run ends with one `purge` event holding the counts.

Schedule (settings):
- raw MIME of settled requests: deleted after raw_mime_retention_days (rejected mail: spoofed,
  automated, spam, rate-limited... after rejected_raw_retention_days); `raw_sha256` becomes
  'purged'. The rendered copies of mail we sent are wiped with it;
- requests older than request_pseudonymise_days: the address becomes 'h:' + its HMAC, the
  subject is cleared, the client IP leaves `auth`, unsent-reply text leaves `result`, the
  Message-IDs are hashed and the progress-page token re-randomised;
- requests older than request_delete_days: deleted (citations and outbox rows cascade; the
  audit events stay, holding only HMACs);
- events older than audit_retention_days: deleted through purge_events(), the only way out of
  the append-only table;
- stored files and page text no request used for blob_retention_days: deleted, and their
  document rows unlinked so a later request downloads afresh;
- raw files no request refers to (left by a crash between write and insert): deleted.
Before all of that, data subjects on the suppression list with `erase` set (DSAR) are erased.

Nothing here touches a request that is still in progress: thread follow-ups and clarifications
read the request row, which keeps its matter, provider and category for the full 400 days.
"""

import asyncio
import contextlib
import secrets
import time
from collections import Counter
from pathlib import Path

import asyncpg
import httpx
import structlog

from agent import audit, blobs, crypto, db
from agent.config import Settings, get_settings
from agent.store import SETTLED

log = structlog.get_logger()

BATCH = 500
ORPHAN_GRACE_S = 2 * 24 * 3600  # a file younger than this may still be getting its row
PURGED = "purged"


# ------------------------------------------------------------------ drop links


async def revoke_delivery(record: dict | None, *, http: httpx.AsyncClient | None = None) -> bool | None:
    """Delete a request's drop upload with its sealed delete token.

    True: gone (deleted now, or already expired); False: drop refused or was unreachable;
    None: nothing to revoke (no link delivery recorded).
    """
    if not record or record.get("kind") != "link" or not record.get("drop_id") or not record.get("delete_token"):
        return None
    drop_id = str(record["drop_id"])
    try:
        token = crypto.unseal_text(record["delete_token"], aad=f"{drop_id}:{record.get('files')}")
    except (crypto.SealError, ValueError) as e:
        log.warning("retention.revoke_unreadable", drop_id=drop_id, error=str(e))
        return False
    s = get_settings()
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), trust_env=False)
    try:
        resp = await client.delete(f"{s.drop_upload_url.rstrip('/')}/api/file/{drop_id}",
                                   headers={"X-Delete-Token": token})
    except httpx.HTTPError as e:
        log.warning("retention.revoke_failed", drop_id=drop_id, error=type(e).__name__)
        return False
    finally:
        if http is None:
            await client.aclose()
    if resp.status_code in (200, 204, 404):  # 404: expired or already deleted
        return True
    log.warning("retention.revoke_failed", drop_id=drop_id, status=resp.status_code)
    return False


# ------------------------------------------------------------------ files


def _raw_path(sha: str) -> Path:
    return Path(get_settings().data_dir) / "raw" / sha[:2] / sha


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


async def _delete_raw_files(conn: asyncpg.Connection, shas: set[str]) -> int:
    """Delete raw MIME files no remaining request refers to."""
    shas = {x for x in shas if x and x != PURGED and len(x) == 64}
    if not shas:
        return 0
    still = {r["raw_sha256"] for r in await conn.fetch(
        "SELECT DISTINCT raw_sha256 FROM requests WHERE raw_sha256 = ANY($1::text[])", list(shas))}
    removed = 0
    for sha in shas - still:
        removed += await asyncio.to_thread(_unlink, _raw_path(sha))
    return removed


# ------------------------------------------------------------------ erasure (DSAR)


def _subject_filter(first_param: int) -> str:
    n = first_param
    return f"(from_h = ${n} OR from_addr = 'h:' || ${n} OR (${n + 1}::text IS NOT NULL AND from_addr = ${n + 1}))"


async def erase_subject(subject_h: str, *, addr: str | None = None, immediate: bool = False,
                        dry_run: bool = False) -> Counter:
    """Erase everything held about one person: their requests (outbox and citations cascade),
    raw MIME and drop uploads. Audit events stay: they hold only the HMAC.

    `immediate` (an operator's `ragent dsar delete`) includes requests still in progress; the
    daily purge leaves those, and any whose reply is still queued (the confirmation of a
    "DELETE MY DATA" email), for its next run.
    """
    counts: Counter = Counter()
    addr = audit.normalise_address(addr) if addr else None
    waiting = "" if immediate else (
        " AND state = ANY($3::text[]) AND NOT EXISTS "
        "(SELECT 1 FROM outbound o WHERE o.request_id = requests.id AND o.status = 'queued')"
    )
    args: list = [subject_h, addr] + ([] if immediate else [list(SETTLED)])
    rows = await db.fetch(f"SELECT id, delivery FROM requests WHERE {_subject_filter(1)}{waiting}", *args)
    if dry_run:
        counts["requests_erased"] = len(rows)
        return counts
    for r in rows:
        revoked = await revoke_delivery(r["delivery"])
        if revoked is not None:
            counts["links_revoked" if revoked else "links_not_revoked"] += 1
    async with db.acquire() as conn, conn.transaction():
        deleted = await conn.fetch(
            f"""DELETE FROM requests WHERE id IN (
                  SELECT id FROM requests WHERE {_subject_filter(1)}{waiting} FOR UPDATE SKIP LOCKED)
                RETURNING id, raw_sha256""",
            *args,
        )
        counts["requests_erased"] += len(deleted)
        counts["raw_deleted"] += await _delete_raw_files(conn, {r["raw_sha256"] for r in deleted})
        await conn.execute("UPDATE suppression SET erased_at = now() WHERE value_h = $1 AND erase", subject_h)
    return counts


# ------------------------------------------------------------------ purge


async def purge(*, dry_run: bool = False, settings: Settings | None = None, batch: int = BATCH) -> dict[str, int]:
    s = settings or get_settings()
    counts: Counter = Counter()
    # Deletion first, so nothing is pseudonymised or stripped only to be deleted a moment later.
    steps = (_backfill_from_h, _erase_suppressed, _delete_requests, _purge_events, _pseudonymise, _purge_raw,
             _purge_blobs, _purge_orphan_blobs, _purge_orphan_raw)
    for step in steps:
        counts.update(await step(s, dry_run=dry_run, batch=batch))
    result = {k: v for k, v in sorted(counts.items()) if v}
    if dry_run:
        log.info("purge.dry_run", **result)
    else:
        await audit.write(None, "purge", result)
        log.info("purge.done", **result)
    return result


async def _count(sql: str, *args) -> int:
    return int(await db.fetchval(f"SELECT count(*) FROM ({sql}) x", *args))


async def _batches(work) -> int:
    """Run `work(conn)` in its own transaction until it reports 0 rows."""
    total = 0
    while True:
        async with db.acquire() as conn, conn.transaction():
            n = await work(conn)
        total += n
        if not n:
            return total


async def _backfill_from_h(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    """Rows written before from_h existed: needed by erasure, DSAR and the audit trail."""
    where = "from_h IS NULL AND from_addr <> ''"
    if dry_run:
        return Counter(from_h_backfilled=await _count(f"SELECT 1 FROM requests WHERE {where}"))

    async def work(conn: asyncpg.Connection) -> int:
        rows = await conn.fetch(f"SELECT id, from_addr FROM requests WHERE {where} LIMIT $1 FOR UPDATE SKIP LOCKED",
                                batch)
        if rows:
            hashes = [r["from_addr"][2:] if r["from_addr"].startswith("h:") else audit.subject_hash(r["from_addr"])
                      for r in rows]
            await conn.execute(
                "UPDATE requests SET from_h = v.h FROM unnest($1::uuid[], $2::text[]) v(id, h) WHERE requests.id = v.id",
                [r["id"] for r in rows], hashes,
            )
        return len(rows)

    return Counter(from_h_backfilled=await _batches(work))


async def _erase_suppressed(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    counts: Counter = Counter()
    for r in await db.fetch("SELECT value_h FROM suppression WHERE erase AND kind = 'address'"):
        counts.update(await erase_subject(r["value_h"], dry_run=dry_run))
    return Counter({f"dsar_{k}": v for k, v in counts.items()})


_RAW_DUE = """
    state = ANY($1::text[]) AND raw_sha256 <> 'purged'
    AND updated_at < now() - make_interval(days => CASE WHEN state = 'rejected' THEN $2::int ELSE $3::int END)
    AND received_at >= now() - make_interval(days => $4::int)
"""


async def _purge_raw(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    args = (list(SETTLED), s.rejected_raw_retention_days, s.raw_mime_retention_days, s.request_delete_days)
    if dry_run:
        return Counter(raw_purged=await _count(f"SELECT 1 FROM requests WHERE {_RAW_DUE}", *args))
    counts: Counter = Counter()

    async def work(conn: asyncpg.Connection) -> int:
        rows = await conn.fetch(
            f"SELECT id, raw_sha256 FROM requests WHERE {_RAW_DUE} ORDER BY updated_at LIMIT $5 FOR UPDATE SKIP LOCKED",
            *args, batch,
        )
        if not rows:
            return 0
        ids = [r["id"] for r in rows]
        await conn.execute("UPDATE requests SET raw_sha256 = 'purged' WHERE id = ANY($1::uuid[])", ids)
        # The rendered emails we sent hold the same personal data (name, address, links).
        wiped = await conn.execute(
            "UPDATE outbound SET body = ''::bytea WHERE request_id = ANY($1::uuid[]) AND status <> 'queued' "
            "AND length(body) > 0", ids)
        counts["outbound_bodies_wiped"] += int(wiped.split()[-1])
        counts["raw_files_deleted"] += await _delete_raw_files(conn, {r["raw_sha256"] for r in rows})
        return len(rows)

    counts["raw_purged"] = await _batches(work)
    return counts


async def _pseudonymise(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    where = ("pseudonymised_at IS NULL AND received_at < now() - make_interval(days => $1::int) "
             "AND state = ANY($2::text[]) AND received_at >= now() - make_interval(days => $3::int)")
    args = (s.request_pseudonymise_days, list(SETTLED), s.request_delete_days)
    if dry_run:
        return Counter(pseudonymised=await _count(f"SELECT 1 FROM requests WHERE {where}", *args))
    counts: Counter = Counter()

    async def work(conn: asyncpg.Connection) -> int:
        rows = await conn.fetch(
            f"SELECT id, from_addr, from_h, message_id, thread_root FROM requests WHERE {where} "
            f"ORDER BY received_at LIMIT $4 FOR UPDATE SKIP LOCKED",
            *args, batch,
        )
        if not rows:
            return 0

        def ph(value: str | None) -> str | None:
            return value if not value or value.startswith("h:") else audit.pseudonym(value)

        await conn.execute(
            """
            UPDATE requests SET
              from_addr = v.from_addr, from_h = coalesce(requests.from_h, v.from_h), subject = '',
              message_id = v.message_id, thread_root = v.thread_root, track_token = v.track_token,
              auth = CASE WHEN auth IS NULL THEN NULL ELSE auth - 'client_ip' END,
              result = CASE WHEN result IS NULL THEN NULL ELSE result - 'pending_reply' END,
              delivery = CASE WHEN delivery IS NULL THEN NULL ELSE delivery - 'url' - 'delete_token' END,
              pseudonymised_at = now()
            FROM unnest($1::uuid[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[])
                 AS v(id, from_addr, from_h, message_id, thread_root, track_token)
            WHERE requests.id = v.id
            """,
            [r["id"] for r in rows],
            [ph(r["from_addr"]) or "" for r in rows],
            [r["from_h"] or audit.subject_hash(r["from_addr"]) for r in rows],
            [ph(r["message_id"]) for r in rows],
            [ph(r["thread_root"]) for r in rows],
            [secrets.token_urlsafe(12) for _ in rows],
        )
        wiped = await conn.execute("UPDATE outbound SET body = ''::bytea WHERE request_id = ANY($1::uuid[]) "
                                   "AND status <> 'queued' AND length(body) > 0", [r["id"] for r in rows])
        counts["outbound_bodies_wiped"] += int(wiped.split()[-1])
        return len(rows)

    counts["pseudonymised"] = await _batches(work)
    return counts


async def _delete_requests(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    where = "received_at < now() - make_interval(days => $1::int)"
    if dry_run:
        return Counter(requests_deleted=await _count(f"SELECT 1 FROM requests WHERE {where}", s.request_delete_days))
    counts: Counter = Counter()

    async def work(conn: asyncpg.Connection) -> int:
        rows = await conn.fetch(
            f"""DELETE FROM requests WHERE id IN (
                  SELECT id FROM requests WHERE {where} ORDER BY received_at LIMIT $2 FOR UPDATE SKIP LOCKED)
                RETURNING raw_sha256""",
            s.request_delete_days, batch,
        )
        counts["raw_files_deleted"] += await _delete_raw_files(conn, {r["raw_sha256"] for r in rows})
        return len(rows)

    counts["requests_deleted"] = await _batches(work)
    return counts


async def _purge_events(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    days = max(s.audit_retention_days, 400)
    if dry_run:
        return Counter(events_deleted=await _count(
            "SELECT 1 FROM events WHERE at < now() - make_interval(days => $1::int)", days))
    total = 0
    while True:
        n = await db.fetchval("SELECT purge_events(make_interval(days => $1::int), $2)", days, batch)
        total += n
        if n < batch:
            return Counter(events_deleted=total)


_STALE = "coalesce(last_used_at, downloaded_at, listed_at) < now() - make_interval(days => $1::int)"


async def _purge_blobs(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    """Files (and their page text) no request has used for blob_retention_days. A file shared by
    several document rows (the same PDF filed in two matters) goes only when all of them are stale."""
    days = s.blob_retention_days
    stale_shas = f"""
        SELECT sha256 FROM documents WHERE sha256 IS NOT NULL
        GROUP BY sha256 HAVING bool_and({_STALE})
    """
    if dry_run:
        return Counter(blobs_deleted=await _count(stale_shas, days))
    counts: Counter = Counter()
    skipped: set[str] = set()

    async def work(conn: asyncpg.Connection) -> int:
        shas = [r["sha256"] for r in await conn.fetch(
            f"{stale_shas} AND NOT sha256 = ANY($3::text[]) LIMIT $2", days, batch, list(skipped))]
        if not shas:
            return 0
        locked = await conn.fetch(
            f"SELECT id, sha256 FROM documents WHERE sha256 = ANY($2::text[]) AND {_STALE} FOR UPDATE SKIP LOCKED",
            days, shas,
        )
        rows = await conn.fetch("SELECT id, sha256 FROM documents WHERE sha256 = ANY($1::text[])", shas)
        mine = {r["id"] for r in locked}
        # in use right now (row locked by a request) or used since the candidates were chosen
        busy = {r["sha256"] for r in rows if r["id"] not in mine}
        skipped.update(busy)
        gone = [x for x in shas if x not in busy]
        for sha in gone:
            with contextlib.suppress(ValueError):
                counts["blob_files_deleted"] += await asyncio.to_thread(_unlink, blobs.blob_path(sha))
        await conn.execute(
            "UPDATE documents SET sha256 = NULL, size_bytes = NULL, downloaded_at = NULL, page_count = NULL "
            "WHERE sha256 = ANY($1::text[])", gone)
        pages = await conn.execute("DELETE FROM pages WHERE sha256 = ANY($1::text[])", gone)
        counts["pages_deleted"] += int(pages.split()[-1])
        counts["blobs_deleted"] += len(gone)
        return len(shas)

    await _batches(work)
    return counts


def _old_files(root: Path, grace_s: float) -> list[Path]:
    if not root.is_dir():
        return []
    cutoff = time.time() - grace_s
    out = []
    for path in root.glob("*/*"):
        with contextlib.suppress(FileNotFoundError):
            if path.is_file() and path.stat().st_mtime < cutoff:
                out.append(path)
    return out


async def _purge_orphan_blobs(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    """Stored files no document row has (a replaced version), unless a citation made within
    blob_retention_days still points at them."""
    files = await asyncio.to_thread(_old_files, Path(s.data_dir) / "blobs", ORPHAN_GRACE_S)
    counts: Counter = Counter()
    for i in range(0, len(files), 1000):
        chunk = {p.name: p for p in files[i : i + 1000]}
        used = {r["sha256"] for r in await db.fetch(
            """SELECT sha256 FROM documents WHERE sha256 = ANY($1::text[])
               UNION SELECT sha256 FROM citations WHERE sha256 = ANY($1::text[])
                 AND created_at > now() - make_interval(days => $2::int)""",
            list(chunk), s.blob_retention_days)}
        for name, path in chunk.items():
            if name in used:
                continue
            counts["orphan_blobs_deleted"] += 1
            if not dry_run:
                await asyncio.to_thread(_unlink, path)
                await db.execute("DELETE FROM pages WHERE sha256 = $1", name)
    return counts


async def _purge_orphan_raw(s: Settings, *, dry_run: bool, batch: int) -> Counter:
    """Raw MIME files no request refers to (and stale temporary files)."""
    files = await asyncio.to_thread(_old_files, Path(s.data_dir) / "raw", ORPHAN_GRACE_S)
    counts: Counter = Counter()
    for i in range(0, len(files), 1000):
        chunk = {p.name: p for p in files[i : i + 1000]}
        used = {r["raw_sha256"] for r in await db.fetch(
            "SELECT raw_sha256 FROM requests WHERE raw_sha256 = ANY($1::text[])", list(chunk))}
        for name, path in chunk.items():
            if name in used:
                continue
            counts["orphan_raw_deleted"] += 1
            if not dry_run:
                await asyncio.to_thread(_unlink, path)
    return counts


# ------------------------------------------------------------------ cron


async def can_purge() -> bool:
    """Only a role that may delete runs the purge (agent_retention, or a single-role dev setup);
    the worker's agent_app may not, and leaves it to the retention job."""
    return bool(await db.fetchval(
        "SELECT has_table_privilege(current_user, 'requests', 'DELETE') "
        "AND has_function_privilege(current_user, 'purge_events(interval, integer)', 'EXECUTE')"))


async def purge_job(ctx: dict) -> None:
    """arq cron: the daily purge, when this process's database role is allowed to run it."""
    with audit.component_scope("cron"):
        if not await can_purge():
            log.info("purge.skipped", reason="role cannot delete; the retention job (ragent purge) runs it")
            return
        await purge()
