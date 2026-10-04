"""The request state machine: gate -> fetch -> package -> deliver -> reply.

received --gate--> rejected | clarify | done (question) | accepted
accepted -> fetching -> packaging -> replying -> done      (any step can -> failed)

Each step is resumable: a retry re-enters at the request's current state, re-uses whatever is
already on disk (content-addressed blobs, cached listings, the reserved outbound Message-ID)
and moves on. Retries are driven by the queue; this module decides retryable vs final.
"""

import asyncio
import hashlib
import itertools
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog

from agent import blobs, store
from agent.citations.claims import summarize_with_citations
from agent.citations.extract import extract_pages_async
from agent.citations.ground import new_citation_id
from agent.config import Settings
from agent.db import pool
from agent.delivery.choose import deliver
from agent.delivery.drop import DropClient
from agent.delivery.package import build_zip
from agent.gate.classify import classify
from agent.limits import Limits
from agent.llm import LLMUnavailable
from agent.mail import auth as mail_auth
from agent.mail import loops, mime, outbound
from agent.models import (
    AgentError,
    AuthVerdict,
    DocType,
    DocumentRef,
    DownloadedFile,
    InboundEmail,
    Intent,
    MatterInfo,
    MatterNotFound,
    ParsedRequest,
    ScrapeError,
)
from agent.providers.base import Provider, provider_for_matter

log = structlog.get_logger()


class TransientError(AgentError):
    retryable = True


@dataclass
class Deps:
    settings: Settings
    providers: dict[str, Provider]
    limits: Limits
    drop: DropClient | None


# ======================================================================== entry point


async def process(deps: Deps, request_id: UUID, *, final_attempt: bool) -> None:
    row = await store.get(request_id)
    if row is None or row["state"] in store.TERMINAL or row["state"] == "clarify":
        return
    log.info("request.start", request_id=str(request_id), state=row["state"], attempt=row["attempts"])
    email = mime.parse_raw(blobs.read_raw(row["raw_sha256"]), row["received_at"])
    try:
        pending = (row["result"] or {}).get("pending_reply") if row["state"] == "replying" else None
        if pending:
            # a short reply was decided but not confirmed sent: resend the same message
            await _deliver_pending_reply(row, email, pending)
            return
        if row["state"] == "received":
            parsed = await _gate(deps, row, email, final_attempt=final_attempt)
            if parsed is None:
                return
            row = await store.get(request_id)
        await _fulfil(deps, row, email)
    except AgentError as e:
        if e.retryable and not final_attempt:
            await store.event(request_id, "retry", {"error": str(e)[:500]})
            raise
        await _finish_with_error(row, email, e)
    except Exception as e:  # a bug, not a user problem: retry, then apologise, never go silent
        log.exception("request.crash", request_id=str(request_id))
        if not final_attempt:
            await store.event(request_id, "retry", {"error": f"{type(e).__name__}: {str(e)[:400]}"})
            raise TransientError(str(e)) from e
        await _finish_with_error(row, email, e)


# ======================================================================== gate


async def _gate(deps: Deps, row, email: InboundEmail, *, final_attempt: bool) -> ParsedRequest | None:
    """Decide whether and how to answer. Returns the parsed request if we'll fetch documents."""
    s, rid = deps.settings, row["id"]
    await store.set_progress(rid, "Checking your email")

    raw = blobs.read_raw(row["raw_sha256"])
    reason = loops.classify_automation(
        email, own_address=s.agent_mail_address, return_path=mime.envelope_sender(mime.parse_headers(raw))
    )
    if reason:
        await store.transition(rid, {"received"}, "rejected", reject_reason=f"automated:{reason}")
        return None

    auth = await mail_auth.verify_sender(raw, email, trusted_mta=s.trusted_mta_hostname)
    auth_json = auth.model_dump(mode="json")
    if mail_auth.is_temperror(auth):
        if final_attempt:
            # Still unverifiable after every retry: drop silently. Writing back to an
            # unverified From is exactly what a spoofer wants.
            await store.transition(rid, {"received"}, "rejected", reject_reason=f"unauthenticated:{auth.reason}",
                                   auth=auth_json)
            return None
        raise TransientError(f"sender verification DNS temperror: {auth.reason}")
    if s.sender_auth_mode != "off" and auth.verdict is not AuthVerdict.PASS:
        # No reply at all: answering an unverifiable From is how agents become spam reflectors.
        await store.transition(rid, {"received"}, "rejected", reject_reason=f"unauthenticated:{auth.reason}", auth=auth_json)
        return None
    if s.sender_auth_mode == "allowlist" and not _allowlisted(email.from_addr, s.sender_allowlist):
        await store.transition(rid, {"received"}, "rejected", reject_reason="not_allowlisted", auth=auth_json)
        return None

    if not await _within_limits(deps, row, email):
        return None

    parsed = await classify(email.subject, email.text, max_docs=s.max_docs_per_request)
    parsed = await _inherit_from_thread(row, parsed)
    common = {"auth": auth_json, "parsed": parsed.model_dump(mode="json")}

    if parsed.intent is Intent.SPAM:
        await store.transition(rid, {"received"}, "rejected", reject_reason="spam", **common)
        return None
    if parsed.intent is Intent.INJECTION:
        await _reply_and_close(rid, row, email, "rejected", reject_reason="injection_attempt", common=common, paragraphs=[
            (
                "I can only fetch public UARB documents and send them back to the address that asked. "
                "Please send a plain request, for example: \"Can you send me the Other Documents for M12205?\""
            ),
        ])
        return None
    if parsed.intent is Intent.UNRELATED:
        if await deps.limits.once(f"help:{email.from_addr}", 24 * 3600):
            await _reply_and_close(rid, row, email, "rejected", reject_reason="unrelated", common=common, paragraphs=[
                (
                    "I'm an automated assistant that fetches documents from the Nova Scotia Utility and Review "
                    "Board's public database. Send me a matter number and a document type, for example: "
                    "\"Can you send me the Other Documents for M12205?\""
                ),
                f"Document types: {', '.join(t.value for t in DocType)}.",
            ])
        else:
            await store.transition(rid, {"received"}, "rejected", reject_reason="unrelated_repeat", **common)
        return None

    provider_name = provider_for_matter(parsed.matter) if parsed.matter else None
    if parsed.matter and provider_name is None:
        await _reply_and_close(rid, row, email, "clarify", common=common, paragraphs=[
            f"{parsed.matter} doesn't look like a UARB matter number. They look like M12205 (M followed by 5 digits).",
        ])
        return None

    if parsed.intent is Intent.QUESTION or parsed.needs_clarification or not parsed.doc_type or not parsed.matter:
        await _answer_without_documents(deps, row, email, parsed, provider_name, common)
        return None

    accepted = await store.transition(
        rid, {"received"}, "accepted",
        provider=provider_name, matter=parsed.matter, doc_type=parsed.doc_type.value, **common,
    )
    if not accepted:
        return None  # another job already took this request past the gate
    await _send(row, email, _ack_draft(deps, row, email, parsed))
    return parsed


def _ack_draft(deps: Deps, row, email: InboundEmail, parsed: ParsedRequest) -> outbound.Draft:
    return outbound.ack(
        name=_display_name(email), subject=email.subject, matter=parsed.matter,
        doc_type=parsed.doc_type, track_url=_track_url(deps, row),
    )


def _allowlisted(addr: str, allowlist: list[str]) -> bool:
    domain = "@" + addr.rsplit("@", 1)[-1]
    return any(a.lower() in {addr, domain} for a in allowlist)


async def _within_limits(deps: Deps, row, email: InboundEmail) -> bool:
    s, rid = deps.settings, row["id"]
    domain = email.from_addr.rsplit("@", 1)[-1]
    checks = [
        (f"sender:{email.from_addr}", s.rate_per_sender_hour),
        (f"domain:{domain}", s.rate_per_domain_hour),
        ("global", s.rate_global_hour),
    ]
    for key, limit in checks:
        ok, _ = await deps.limits.hit(key, limit=limit, window_s=3600, member=str(rid))
        if not ok:
            await store.transition(rid, {"received"}, "rejected", reject_reason=f"rate_limited:{key.split(':')[0]}")
            # one notice per sender per hour, so a flood can't turn into a flood of replies
            if key != "global" and await deps.limits.once(f"rl-notice:{email.from_addr}", 3600):
                await _send(row, email, outbound.simple_reply(
                    name=_display_name(email), subject=email.subject,
                    paragraphs=[("You've sent a lot of requests in the last hour, so I'm pausing for a while. "
                                 "Please try again later.")],
                ))
            return False
    if await store.thread_size(row["thread_root"], email.from_addr) > s.max_requests_per_thread:
        await store.transition(rid, {"received"}, "rejected", reject_reason="thread_cap")
        return False
    return True


async def _inherit_from_thread(row, parsed: ParsedRequest) -> ParsedRequest:
    """A follow-up like "Exhibits please" in an existing thread reuses that thread's matter."""
    if parsed.intent is not Intent.DOCUMENT_REQUEST or parsed.matter:
        return parsed
    prev = await store.previous_in_thread(row["thread_root"], row["id"], row["from_addr"])
    if not prev:
        return parsed
    update: dict = {"matter": prev["matter"]}
    if parsed.doc_type:
        update["needs_clarification"] = None
    return parsed.model_copy(update=update)


async def _answer_without_documents(deps: Deps, row, email, parsed: ParsedRequest, provider_name, common) -> None:
    """Questions and incomplete requests: answer with what we know, ask for what's missing."""
    rid = row["id"]
    paragraphs: list[str] = []
    info = None
    if parsed.matter and provider_name:
        await store.set_progress(rid, "Looking up the matter")
        try:
            info = await _matter_info(deps, provider_name, parsed.matter)
        except MatterNotFound as e:
            await _reply_and_close(rid, row, email, "done", common=common, paragraphs=[e.user_message])
            return
        paragraphs.append(outbound.matter_sentence(info))
    if parsed.needs_clarification or not parsed.doc_type or not parsed.matter:
        paragraphs.append(parsed.needs_clarification or "Which matter and document type would you like?")
        state = "clarify"
    else:
        paragraphs.append(f"If you'd like the documents, reply with the type you want "
                          f"({', '.join(t.value for t in DocType)}).")
        state = "done"
    await _reply_and_close(rid, row, email, state, common={**common, "matter": parsed.matter, "provider": provider_name},
                           paragraphs=paragraphs)


# ======================================================================== fulfil


async def _fulfil(deps: Deps, row, email: InboundEmail) -> None:
    rid = row["id"]
    parsed = ParsedRequest.model_validate(row["parsed"])
    provider = deps.providers[row["provider"]]
    doc_type = DocType(row["doc_type"])
    if row["ack_message_id"] and row["ack_sent_at"] is None:
        await _send(row, email, _ack_draft(deps, row, email, parsed))  # same Message-ID as reserved
    await store.transition(rid, {"accepted", "fetching"}, "fetching")

    info, files, failed, confidential = await _fetch(deps, rid, provider, parsed.matter, doc_type, parsed.max_docs)
    if not files:
        await store.transition(rid, {"fetching"}, "replying")
        total = info.counts.get(doc_type, 0)
        msg = (f"{parsed.matter} has no {doc_type.value}." if total == 0
               else f"I couldn't download any of the {total} {doc_type.value} right now.")
        await _send(row, email, outbound.simple_reply(
            name=_display_name(email), subject=email.subject, track_url=_track_url(deps, row),
            paragraphs=[outbound.matter_sentence(info), msg],
        ))
        await store.transition(rid, {"replying"}, "done", result={"files": 0, "counts": _counts(info)})
        return

    await store.transition(rid, {"fetching", "packaging"}, "packaging")
    with tempfile.TemporaryDirectory(prefix=f"req-{rid}-") as tmp:
        await store.set_progress(rid, "Packaging the ZIP and writing a cited summary")

        async def package_and_deliver():
            zip_result = await asyncio.to_thread(
                build_zip, files, f"{tmp}/{parsed.matter}_{doc_type.value.replace(' ', '_')}.zip",
                readme_text=_readme(info, doc_type, files),
            )
            return await deliver(zip_result, files, drop=deps.drop)

        # Independent work: the LLM summary (~20s) overlaps packaging and the encrypted upload.
        delivery, (summary, claims) = await asyncio.gather(package_and_deliver(), _summarise(deps, rid, info, files))

        await store.transition(rid, {"packaging", "replying"}, "replying")
        await store.set_progress(rid, "Sending your documents")
        doc_ids = await store.documents_by_external_ids(provider.name, [f.ref.external_id for f in files])
        draft = outbound.documents_reply(
            name=_display_name(email), subject=email.subject, info=info, doc_type=doc_type,
            docs=[outbound.DocLine(
                title=f.ref.title, filed=f.ref.filed_on.isoformat() if f.ref.filed_on else "undated",
                url=f"{deps.settings.public_base_url}/files/{doc_ids[f.ref.external_id]['id']}.pdf" if f.ref.external_id in doc_ids else None,
            ) for f in files],
            requested=parsed.max_docs, summary=summary, claims=claims,
            download_url=delivery.link.url if delivery.link else None,
            download_expires=delivery.link.expires_at if delivery.link else None,
            download_size=delivery.size, attachment_path=delivery.path,
            track_url=_track_url(deps, row), extra_doc_types=parsed.extra_doc_types,
            failed_titles=failed, newest_first=_newest_first(files), confidential=confidential,
        )
        await _send(row, email, draft)
    await store.transition(rid, {"replying"}, "done", result={
        "files": len(files), "failed": failed, "counts": _counts(info), "zip_bytes": delivery.size,
        "delivery": delivery.kind, "citations": len(claims),
        "drop_id": delivery.link.id if delivery.link else None,
    })
    await store.set_progress(rid, "Done", done=len(files), total=len(files))


async def _matter_info(deps: Deps, provider_name: str, matter: str) -> MatterInfo:
    cached = await store.cached_matter(provider_name, matter, timedelta(seconds=deps.settings.matter_cache_ttl_s))
    if cached:
        return cached[0]
    info = await deps.providers[provider_name].fetch_matter(matter)
    await store.save_matter(info)
    return info


async def _fetch(
    deps: Deps, rid: UUID, provider: Provider, matter: str, doc_type: DocType, limit: int
) -> tuple[MatterInfo, list[DownloadedFile], list[str], int]:
    """Listing + downloads, single-flighted per (provider, matter, tab) and served from cache
    when possible: ten people asking for M12205 at once cost one portal visit."""
    ttl = timedelta(seconds=deps.settings.matter_cache_ttl_s)
    async with deps.limits.lock(f"sf:{provider.name}:{matter}:{doc_type.value}", ttl_s=900, wait_s=900):
        await store.set_progress(rid, "Searching the UARB database")
        cached = await store.cached_matter(provider.name, matter, ttl)
        listing = (cached[1].get(doc_type.value) if cached else None)
        confidential = int((cached[1].get(f"{doc_type.value}#confidential") if cached else 0) or 0)
        if cached and listing is not None and len(listing) >= min(limit, cached[0].counts.get(doc_type, 0)):
            info = cached[0]
            known = await store.documents_by_external_ids(provider.name, listing[:limit])
            refs = [_ref_from_row(known[x], i) for i, x in enumerate(listing[:limit]) if x in known]
        else:
            info, listed = await provider.list_matter_and_documents(matter, doc_type, limit)
            if info.counts.get(doc_type, 0) > 0 and not listed:
                # The portal says there are documents but we read none: a scraper problem, not
                # an answer. Retry rather than tell the user there's nothing.
                raise ScrapeError(f"{matter}/{doc_type.value}: count {info.counts[doc_type]} but empty listing")
            refs = [r for r in listed if r.access == "Public"][:limit]
            await store.save_matter(info, doc_type.value, [r.external_id for r in refs])
            for r in refs:
                await store.upsert_document(r)
            confidential = sum(1 for r in listed if r.access != "Public")
            if confidential:
                await store.save_matter(info, f"{doc_type.value}#confidential", confidential)
                await store.event(rid, "confidential_skipped", {"count": confidential})
        await store.set_progress(rid, "Downloading documents", done=0, total=len(refs))

        have = await store.documents_by_external_ids(provider.name, [r.external_id for r in refs])
        ready: dict[str, DownloadedFile] = {}
        missing: list[DocumentRef] = []
        for r in refs:
            row = have.get(r.external_id)
            if row and row["sha256"] and blobs.has_blob(row["sha256"]):
                ready[r.external_id] = DownloadedFile(
                    ref=r, path=str(blobs.blob_path(row["sha256"])), sha256=row["sha256"],
                    size=row["size_bytes"], filename=row["filename"] or f"{r.external_id}{r.file_ext}",
                )
            else:
                missing.append(r)
        if missing:
            with tempfile.TemporaryDirectory(prefix="dl-", dir=deps.settings.data_dir) as tmp:
                async for f in provider.download(matter, missing, tmp):
                    dest = blobs.put_file(f.path, f.sha256)
                    await store.upsert_document(f.ref, sha256=f.sha256, size=f.size, filename=f.filename)
                    ready[f.ref.external_id] = f.model_copy(update={"path": str(dest)})
                    await store.set_progress(rid, "Downloading documents", done=len(ready), total=len(refs))
    files = [ready[r.external_id] for r in refs if r.external_id in ready]
    failed = [r.title for r in refs if r.external_id not in ready]
    if refs and len(files) < len(refs):
        log.warning("fetch.partial", matter=matter, doc_type=doc_type.value, got=len(files), wanted=len(refs))
    if refs and not files:
        raise TransientError(f"no downloads succeeded for {matter}/{doc_type.value}")
    return info, files, failed, confidential


def _ref_from_row(row, index: int) -> DocumentRef:
    return DocumentRef(
        provider=row["provider"], matter=row["matter"], doc_type=DocType(row["doc_type"]),
        external_id=row["external_id"], title=row["title"], filed_on=row["filed_on"],
        file_ext="." + (row["filename"] or ".pdf").rsplit(".", 1)[-1], row_index=index,
    )


SUMMARY_VERSION = "v1"  # bump when the summary prompt or grounding rules change


async def _summarise(deps: Deps, rid: UUID, info: MatterInfo, files: list[DownloadedFile]):
    """Cited summary. Best effort: if the LLM is down, the documents still go out.

    Cached by the exact document versions (content hashes), so a repeat request for the same
    filings costs no LLM call, and any new or changed filing produces a fresh summary.
    """
    key = hashlib.sha256(
        (SUMMARY_VERSION + info.matter + "".join(sorted(f.sha256 for f in files))).encode()
    ).hexdigest()
    cached = await pool().fetchrow("SELECT summary, claims FROM summaries WHERE key = $1", key)
    if cached:
        summary, raw_claims = cached["summary"], cached["claims"]
        await store.event(rid, "summary", {"cached": True, "claims": len(raw_claims)})
    else:
        docs = []
        for f in files:
            if not f.filename.lower().endswith(".pdf"):
                continue
            pages = await _pages(f.sha256, f.path)
            if pages:
                docs.append((f.ref, pages))
        if not docs:
            return None, []
        try:
            result = await summarize_with_citations(info, docs, max_claims=5)
        except LLMUnavailable as e:
            log.warning("summary.unavailable", request_id=str(rid), error=str(e)[:300])
            return None, []
        summary = result.summary or None
        raw_claims = [
            {"claim": c.claim, "doc_external_id": c.doc_external_id, "page": c.page,
             "quote": c.quote, "char_start": c.char_start, "char_end": c.char_end}
            for c in result.claims
        ]
        await pool().execute(
            "INSERT INTO summaries (key, summary, claims) VALUES ($1, $2, $3) ON CONFLICT (key) DO NOTHING",
            key, summary, raw_claims,
        )
        await store.event(rid, "summary", {"claims": len(raw_claims), "dropped": len(result.dropped), **result.llm})

    existing = await pool().fetch("SELECT id, claim FROM citations WHERE request_id = $1 ORDER BY created_at, id", rid)
    if existing:  # a retry of this request: reuse its links rather than minting new ones
        return summary, [outbound.ClaimLine(text=r["claim"], url=f"{deps.settings.public_base_url}/c/{r['id']}")
                         for r in existing]
    ids = await store.documents_by_external_ids(info.provider, [c["doc_external_id"] for c in raw_claims])
    claims: list[outbound.ClaimLine] = []
    async with pool().acquire() as conn:
        for c in raw_claims:
            doc = ids.get(c["doc_external_id"])
            if not doc:
                continue
            cid = new_citation_id()  # per request, so one requester's links never collide with another's
            await conn.execute(
                """INSERT INTO citations (id, request_id, document_id, sha256, page, quote, char_start, char_end, claim)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                cid, rid, doc["id"], doc["sha256"], c["page"], c["quote"], c["char_start"], c["char_end"], c["claim"],
            )
            claims.append(outbound.ClaimLine(text=c["claim"], url=f"{deps.settings.public_base_url}/c/{cid}"))
    return summary, claims


async def _pages(sha256: str, path: str) -> list[str]:
    """Page texts, extracted once per content hash."""
    rows = await pool().fetch("SELECT text FROM pages WHERE sha256 = $1 ORDER BY page", sha256)
    if rows:
        return [r["text"] for r in rows]
    pages = await extract_pages_async(path)
    async with pool().acquire() as conn, conn.transaction():
        await conn.executemany(
            "INSERT INTO pages (sha256, page, text) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            [(sha256, i + 1, t) for i, t in enumerate(pages)],
        )
        await conn.execute("UPDATE documents SET page_count = $2 WHERE sha256 = $1", sha256, len(pages))
    return pages


# ======================================================================== replies & failures


async def _send(row, email: InboundEmail, draft: outbound.Draft) -> None:
    rid = row["id"]
    if draft.kind in {"ack", "reply"}:
        mid = outbound.message_id(rid, draft.kind)
        if not await store.reserve_outbound(rid, draft.kind, mid):
            log.info("send.already_sent", request_id=str(rid), kind=draft.kind)
            return
    msg = outbound.build_message(
        draft, request_id=rid, to_addr=email.from_addr,
        in_reply_to=email.message_id, references=email.references,
    )
    await outbound.send(msg)
    if draft.kind in {"ack", "reply"}:
        await store.mark_sent(rid, draft.kind)


async def _reply_and_close(rid, row, email, state: str, *, paragraphs, common: dict, reject_reason: str | None = None) -> None:
    fields = {k: v for k, v in common.items() if v is not None}
    if reject_reason:
        fields["reject_reason"] = reject_reason
    pending = {"paragraphs": list(paragraphs), "final_state": state}
    # Persist what we're about to say before saying it, so a crash or SMTP failure resumes by
    # resending exactly this message (same Message-ID) instead of re-deciding.
    await store.transition(rid, {"received"}, "replying", result={"pending_reply": pending}, **fields)
    await _deliver_pending_reply(row, email, pending)


async def _deliver_pending_reply(row, email: InboundEmail, pending: dict) -> None:
    await _send(row, email, outbound.simple_reply(
        name=_display_name(email), subject=email.subject, paragraphs=pending["paragraphs"]
    ))
    await store.transition(row["id"], {"replying"}, pending["final_state"])


async def _finish_with_error(row, email: InboundEmail, e: BaseException) -> None:
    rid = row["id"]
    user_msg = e.user_message if isinstance(e, AgentError) else (
        "I ran into an unexpected problem and couldn't finish this request. It has been logged."
    )
    if isinstance(e, AgentError) and e.retryable:
        user_msg = ("The regulator's website kept failing while I worked on this, so I've stopped retrying. "
                    "Please send the request again in a little while.")
    final_state = "done" if isinstance(e, MatterNotFound) else "failed"
    current = (await store.get(rid))["state"]
    pending = {"paragraphs": [user_msg], "final_state": final_state}
    await store.transition(rid, {current}, "replying", error=f"{type(e).__name__}: {str(e)[:500]}",
                           result={"pending_reply": pending})
    try:
        await _send(row, email, outbound.simple_reply(name=_display_name(email), subject=email.subject, paragraphs=[user_msg]))
    finally:
        # Even if this last email fails, the request must not loop forever: close it.
        await store.transition(rid, {"replying"}, final_state)


# ======================================================================== helpers


def _display_name(email: InboundEmail) -> str:
    return email.from_name


def _track_url(deps: Deps, row) -> str:
    return f"{deps.settings.public_base_url}/r/{row['track_token']}"


def _newest_first(files: list[DownloadedFile]) -> bool:
    """Portal tabs differ (Other Documents newest first, Exhibits oldest first): say which."""
    dates = [f.ref.filed_on for f in files if f.ref.filed_on]
    return all(a >= b for a, b in itertools.pairwise(dates))


def _counts(info: MatterInfo) -> dict[str, int]:
    return {t.value: n for t, n in info.counts.items()}


def _readme(info: MatterInfo, doc_type: DocType, files: list[DownloadedFile]) -> str:
    return (
        f"{info.matter}: {info.title}\n"
        f"Source: Nova Scotia Utility and Review Board, Public Documents Database\n"
        f"{info.portal_url}\n\n"
        f"Tab: {doc_type.value} ({len(files)} of {info.counts.get(doc_type, 0)} documents, newest first)\n"
        f"Retrieved: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC\n"
        f"MANIFEST.csv lists each file's portal id, title, filing date and SHA-256.\n"
    )
