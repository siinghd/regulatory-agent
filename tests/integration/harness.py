"""Fakes and a driver for running the request pipeline against real Postgres and Redis.

Everything outside the process is faked: the regulator portal (FakeProvider), SMTP, DNS-based
sender verification, the LLM and the drop service. Postgres and Redis are real (a throwaway
database and Redis db 15), so state transitions, locks, rate limits and the queue behave as
they do in production.
"""

import asyncio
import hashlib
import io
import json
import os
import re
import zipfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any
from uuid import UUID

import aiosmtplib
import pymupdf
import pytest
from arq import Retry
from arq.connections import ArqRedis
from pydantic import BaseModel

from agent import db, store, worker
from agent.config import get_settings
from agent.delivery.drop import DropLink
from agent.limits import Limits
from agent.llm import LLMUnavailable
from agent.mail.ingest import ingest_raw
from agent.models import (
    AuthVerdict,
    DocumentRef,
    DownloadedFile,
    InboundEmail,
    MatterInfo,
    MatterNotFound,
    PortalUnavailable,
    SenderAuth,
)
from agent.pipeline import Deps
from agent.providers import base as providers_base
from agent.providers.base import Category
from agent.providers.uarb import CATEGORIES as UARB_CATEGORIES
from agent.providers.uarb import MENTION_RE as UARB_MENTION_RE
from agent.providers.uarb import UarbProvider

AGENT_ADDRESS = "agent@hsingh.app"
AGENT_DOMAIN = "hsingh.app"
PUBLIC_BASE_URL = "https://uarb.test"
MATTER = "M12205"
DEFAULT_COUNTS = {
    "Exhibits": 2,
    "Key Documents": 0,
    "Other Documents": 3,
    "Transcripts": 0,
    "Recordings": 0,
}
COUNTS_SENTENCE = "I found 2 Exhibits, 3 Other Documents, and no Key Documents, Transcripts or Recordings."
SUMMARY_TEXT = "The Board approved the projects described in these filings."


# ---------------------------------------------------------------- documents


def doc_id(matter: str, doc_type: str, n: int) -> str:
    return f"{matter}-{doc_type.replace(' ', '')}-{n}"


def pdf_lines(external_id: str) -> list[str]:
    """The text of every fake filing; lines 3 and 4 are what the fake LLM quotes."""
    return [
        "Nova Scotia Utility and Review Board",
        f"Filing {external_id}",
        f"The Board approves project {external_id} subject to the conditions below.",
        f"Filing {external_id} sets out the reasons for the decision in detail.",
    ]


def make_pdf(external_id: str) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(pdf_lines(external_id)):
        page.insert_text((72, 72 + 16 * i), line, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------- portal


class _FakePortal:
    """The Provider protocol over in-memory matters; subclasses supply the regulator's identity."""

    name: str
    portal_url: str

    def __init__(self) -> None:
        self.matters: dict[str, tuple[MatterInfo, dict[str, list[DocumentRef]]]] = {}
        self.calls: Counter[str] = Counter()  # portal visits by method ("list", "fetch_matter", "download")
        self.list_limits: list[int] = []
        self.downloaded: list[str] = []  # one external id per file actually served
        self.latency = 0.0
        self.portal_errors: list[BaseException] = []  # raised by the next portal visits, in order
        self.portal_down: Callable[[], BaseException] | None = None  # raised by every portal visit
        self.hang_next = 0  # portal visits that never return (a worker killed mid-fetch)
        self.download_fail: set[str] = set()  # external ids whose download fails
        self._pdfs: dict[str, bytes] = {}

    def add_matter(self, matter: str, counts: Mapping[str, int], *, title: str | None = None) -> None:
        info = MatterInfo(
            provider=self.name,
            matter=matter,
            title=title or "Halifax Regional Water Commission - Windsor Street Exchange Redevelopment",
            status="Open",
            type="Capital Expenditure Approvals",
            category="Water",
            date_received=date(2025, 4, 7),
            counts=dict(counts),
            portal_url=self.portal_url,
            fetched_at=datetime.now(UTC),
        )
        docs = {
            t: [
                DocumentRef(
                    provider=self.name,
                    matter=matter,
                    doc_type=t,
                    external_id=doc_id(matter, t, i),
                    title=f"{t} {i} for {matter}",
                    filed_on=date(2026, 9, 30) - timedelta(days=7 * i),
                    row_index=i - 1,
                )
                for i in range(1, n + 1)
            ]
            for t, n in counts.items()
        }
        self.matters[matter] = (info, docs)

    def info(self, matter: str = MATTER) -> MatterInfo:
        return self.matters[matter][0]

    def refs(self, doc_type: str, matter: str = MATTER) -> list[DocumentRef]:
        return self.matters[matter][1][doc_type]

    def pdf(self, external_id: str) -> bytes:
        if external_id not in self._pdfs:  # stable bytes, so a re-download has the same sha256
            self._pdfs[external_id] = make_pdf(external_id)
        return self._pdfs[external_id]

    def visits(self) -> int:
        return sum(self.calls.values())

    async def _visit(self, what: str, matter: str) -> tuple[MatterInfo, dict[str, list[DocumentRef]]]:
        self.calls[what] += 1
        if self.hang_next:
            self.hang_next -= 1
            await asyncio.Event().wait()
        if self.latency:
            await asyncio.sleep(self.latency)
        if self.portal_down:
            raise self.portal_down()
        if self.portal_errors:
            raise self.portal_errors.pop(0)
        if matter not in self.matters:
            raise MatterNotFound(matter)
        info, docs = self.matters[matter]
        return info.model_copy(update={"fetched_at": datetime.now(UTC)}), docs

    async def fetch_matter(self, matter: str) -> MatterInfo:
        info, _ = await self._visit("fetch_matter", matter)
        return info

    async def list_documents(self, matter: str, doc_type: str, limit: int) -> list[DocumentRef]:
        _, docs = await self._visit("list_documents", matter)
        return docs.get(doc_type, [])[:limit]

    async def list_matter_and_documents(
        self, matter: str, doc_type: str, limit: int
    ) -> tuple[MatterInfo, list[DocumentRef]]:
        self.list_limits.append(limit)
        info, docs = await self._visit("list", matter)
        return info, docs.get(doc_type, [])[:limit]

    async def download(self, matter: str, refs: list[DocumentRef], dest_dir: str):
        self.calls["download"] += 1
        failed: list[str] = []
        for ref in refs:
            if self.latency:
                await asyncio.sleep(self.latency / 4)
            if ref.external_id in self.download_fail:
                failed.append(ref.external_id)
                continue
            data = self.pdf(ref.external_id)
            sha = hashlib.sha256(data).hexdigest()
            path = os.path.join(dest_dir, f"{sha}.pdf")
            await asyncio.to_thread(Path(path).write_bytes, data)
            self.downloaded.append(ref.external_id)
            yield DownloadedFile(ref=ref, path=path, sha256=sha, size=len(data), filename=f"{ref.external_id}.pdf")
        if failed and len(failed) == len(refs):  # like UarbProvider: partial failures are only logged
            raise PortalUnavailable(f"every download failed: {failed}")


class FakeProvider(_FakePortal):
    """The UARB as the gate and the pipeline see it: its matter format and its tabs."""

    name = "uarb"
    display_name = "Nova Scotia Utility and Review Board (fake)"
    portal_url = "https://uarb.example/fmi/webd/UARB15"
    matter_pattern = UarbProvider.matter_pattern
    mention_pattern = UARB_MENTION_RE
    matter_example = UarbProvider.matter_example
    categories = UARB_CATEGORIES
    normalise = staticmethod(UarbProvider.normalise)


class SecondFakeProvider(_FakePortal):
    """Another regulator: different matter format and categories, and the default normaliser."""

    name = "fakereg"
    display_name = "Fake Energy Regulator"
    portal_url = "https://regulator.example/documents"
    matter_pattern = re.compile(r"FK-\d{4}")
    mention_pattern = re.compile(r"(?<![A-Za-z0-9])FK-\d{4}(?!\d)", re.IGNORECASE)
    matter_example = "FK-1234"
    categories = (
        Category("Rulings", aliases=("rulings", "ruling"), description="Decisions of the regulator"),
        Category("Filings", aliases=("filings", "filing"), description="Everything parties filed"),
    )


# ---------------------------------------------------------------- LLM, sender auth, SMTP, drop

_META = {"model": "fake/llm", "latency_ms": 1, "cost": 0.0, "attempts": 1}
_DOC_LABEL = re.compile(r"<<<DOC (\S+) PAGE (\d+)")


def llm_parse(
    intent: str = "document_request",
    matter: str | None = None,
    doc_type: str | None = None,
    *,
    clarification: str | None = None,
    max_docs: int = 10,
    confidence: float = 0.9,
) -> dict[str, Any]:
    """A classifier answer in the shape of agent.gate.classify._LLMParse."""
    return {
        "intent": intent,
        "matter": matter,
        "other_matters": [],
        "doc_type": doc_type,
        "other_doc_types": [],
        "max_docs": max_docs,
        "clarification": clarification,
        "confidence": confidence,
    }


class FakeLLM:
    """Stands in for agent.llm.structured everywhere it is called (classifier and summary)."""

    def __init__(self) -> None:
        self.parse: dict[str, Any] | None = None  # classifier answer; None = model unavailable
        self.summary_down = False
        self.calls: Counter[str] = Counter()  # by schema name
        self.users: list[str] = []  # the user message of every call

    async def structured(self, *, system: str, user: str, schema: type[BaseModel], **_: Any):
        name = schema.__name__
        self.calls[name] += 1
        self.users.append(user)
        if name == "_LLMParse":
            if self.parse is None:
                raise LLMUnavailable("fake: classifier not configured for this test")
            return schema.model_validate(self.parse), dict(_META)
        if name == "_SummaryOut":
            if self.summary_down:
                raise LLMUnavailable("fake: every summary model is down")
            return schema.model_validate(self._summary(user)), dict(_META)
        raise LLMUnavailable(f"fake: unexpected schema {name}")

    @staticmethod
    def _summary(user: str) -> dict[str, Any]:
        """Two verbatim quotes from each of the first two documents the prompt shows."""
        pages: dict[str, int] = {}
        for external_id, page in _DOC_LABEL.findall(user):
            pages.setdefault(external_id, int(page))
        claims = []
        for external_id, page in list(pages.items())[:2]:
            lines = pdf_lines(external_id)
            claims += [
                {"claim": f"The Board approved project {external_id}.", "doc_external_id": external_id,
                 "page": page, "quote": lines[2]},
                {"claim": f"Filing {external_id} explains the decision.", "doc_external_id": external_id,
                 "page": page, "quote": lines[3]},
            ]
        return {"summary": SUMMARY_TEXT, "claims": claims}


class FakeAuth:
    def __init__(self) -> None:
        self.verdict = "pass"  # pass | fail | temperror
        self.calls = 0

    async def verify(self, raw: bytes, email: InboundEmail, *, trusted_mta: str, **_: Any) -> SenderAuth:
        self.calls += 1
        domain = email.from_addr.rpartition("@")[2]
        if self.verdict == "pass":
            return SenderAuth(verdict=AuthVerdict.PASS, from_domain=domain, spf="pass", spf_domain=domain,
                              aligned_via="spf", reason=f"SPF pass for {domain} aligned with {domain}")
        if self.verdict == "fail":
            return SenderAuth(verdict=AuthVerdict.FAIL, from_domain=domain, spf="fail", spf_domain=domain,
                              reason=f"DMARC p=reject at {domain}, nothing aligned")
        return SenderAuth(verdict=AuthVerdict.NONE, from_domain=domain, spf="none",
                          reason="temperror: DMARC lookup failed: timed out")


class FakeSMTP:
    """Captures what would have gone to the MTA, round-tripped through bytes like the real thing."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []
        self.attempts: list[str] = []  # Message-ID of every send attempt, failed or not
        self.fail: Counter[str] = Counter()  # kind ("ack" / "reply") -> sends to fail before succeeding
        self.latency = 0.0

    async def send(self, msg: EmailMessage) -> None:
        mid = msg["Message-ID"]
        self.attempts.append(mid)
        if self.latency:
            await asyncio.sleep(self.latency)
        kind = mid[1:].split(".", 1)[0]
        if self.fail[kind] > 0:
            self.fail[kind] -= 1
            raise aiosmtplib.SMTPServerDisconnected("fake SMTP: connection lost")
        self.sent.append(message_from_bytes(msg.as_bytes(), policy=policy.default))


class FakeDrop:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []  # (display name, ZIP bytes)

    async def upload(self, path: str, display_name: str) -> DropLink:
        data = await asyncio.to_thread(Path(path).read_bytes)
        self.uploads.append((display_name, data))
        return DropLink(
            url="https://drop.test/d/abcdef123456#k=not-a-real-key",
            id="abcdef123456",
            delete_token="delete-token",
            expires_at=datetime.now(UTC) + timedelta(days=7),
            size=len(data),
            max_downloads=25,
        )

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------- inbound mail


def make_email(
    body: str,
    *,
    subject: str = "Document request",
    from_addr: str = "alice@example.com",
    from_name: str = "Alice Smith",
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: Sequence[str] = (),
    reply_to: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> bytes:
    """Raw RFC 5322 bytes as our Postfix + Dovecot would deliver them."""
    msg = EmailMessage()
    msg["Return-Path"] = f"<{from_addr}>"
    msg["Received"] = (
        "from mx.example.com (mx.example.com [203.0.113.7]) by mail.hsingh.app (Postfix) "
        "with ESMTPS id 4Zq1; Sat, 04 Oct 2026 14:05:00 +0000"
    )
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"] = formataddr(("UARB Agent", AGENT_ADDRESS))
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = "Sat, 04 Oct 2026 14:04:58 +0000"
    msg["Message-ID"] = message_id or make_msgid(domain=from_addr.rpartition("@")[2])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    for name, value in (headers or {}).items():
        msg[name] = value
    msg.set_content(body)
    return msg.as_bytes(policy=policy.SMTP)


def message_id_of(raw: bytes) -> str:
    return message_from_bytes(raw)["Message-ID"].strip()


# ---------------------------------------------------------------- outbound mail


def body_text(msg: EmailMessage) -> str:
    return msg.get_body(preferencelist=("plain",)).get_content()


def attachments(msg: EmailMessage) -> dict[str, bytes]:
    return {part.get_filename(): part.get_content() for part in msg.iter_attachments()}


def zip_members(msg: EmailMessage) -> dict[str, bytes]:
    (data,) = [v for k, v in attachments(msg).items() if k.endswith(".zip")]
    return unzip(data)


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def outbound_id(rid: UUID, kind: str) -> str:
    return f"<{kind}.{rid}@{AGENT_DOMAIN}>"


def assert_threaded(msg: EmailMessage, *, to: str, in_reply_to: str, references: str) -> None:
    """Headers every message we send must carry."""
    assert msg["To"] == to
    assert msg["From"] == f"Regulatory Document Agent <{AGENT_ADDRESS}>"
    assert msg["In-Reply-To"] == in_reply_to
    assert msg["References"] == references
    assert msg["Auto-Submitted"] == "auto-replied"
    assert msg["X-Auto-Response-Suppress"] == "All"
    assert msg["X-Regulatory-Agent"] == "1"
    assert msg["Subject"].startswith("Re: ")
    assert not msg.get_all("Cc") and not msg.get_all("Bcc")


# ---------------------------------------------------------------- driver


class Harness:
    def __init__(
        self,
        *,
        redis: ArqRedis,
        provider: FakeProvider,
        llm: FakeLLM,
        auth: FakeAuth,
        smtp: FakeSMTP,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self.redis = redis
        self.provider = provider
        self.providers: dict[str, _FakePortal] = {provider.name: provider}
        self.llm = llm
        self.auth = auth
        self.smtp = smtp
        self.drop: FakeDrop | None = None
        self.limits = Limits(redis)
        self._mp = monkeypatch

    def configure(self, **settings: Any) -> None:
        """Override settings for this test (env var + cache clear, as in production)."""
        for key, value in settings.items():
            self._mp.setenv(key.upper(), value if isinstance(value, str) else json.dumps(value))
        get_settings.cache_clear()

    def add_provider(self, provider: _FakePortal) -> None:
        """Serve another regulator next to the fake UARB, in the registry and in the pipeline."""
        self.providers[provider.name] = provider
        self._mp.setattr(providers_base, "_REGISTRY", {n: (lambda p=p: p) for n, p in self.providers.items()})

    @property
    def deps(self) -> Deps:
        return Deps(settings=get_settings(), providers=dict(self.providers), limits=self.limits, drop=self.drop)

    def ctx(self, job_try: int = 1) -> dict[str, Any]:
        return {"redis": self.redis, "deps": self.deps, "job_try": job_try}

    async def ingest(self, raw: bytes) -> UUID:
        await ingest_raw(raw, self.redis)
        rid = await db.pool().fetchval("SELECT id FROM requests WHERE message_id = $1", message_id_of(raw))
        assert rid is not None
        return rid

    async def process(self, rid: UUID, job_try: int = 1) -> None:
        """One arq job try, exactly as the worker runs it."""
        await worker.process_request(self.ctx(job_try), str(rid))

    async def run_job(self, rid: UUID, *, first_try: int = 1) -> list[float]:
        """Drive a job the way arq does: re-run on Retry until it returns. Returns the defers (s)."""
        defers: list[float] = []
        for job_try in range(first_try, worker.MAX_TRIES + 1):
            try:
                await self.process(rid, job_try)
                return defers
            except Retry as r:
                defers.append((r.defer_score or 0) / 1000)
        raise AssertionError(f"request {rid} raised Retry on its final try")

    async def queued_request_ids(self) -> list[str]:
        jobs = await self.redis.queued_jobs()
        assert all(j.function == "process_request" and j.job_id == f"req:{j.args[0]}" for j in jobs)
        return [j.args[0] for j in jobs]

    async def request(self, rid: UUID):
        return await store.get(rid)

    async def event_kinds(self, rid: UUID) -> list[str]:
        return [e["kind"] for e in await store.events(rid)]

    async def backdate(self, rid: UUID, *, minutes: int) -> None:
        await db.pool().execute(
            "UPDATE requests SET updated_at = now() - make_interval(mins => $2) WHERE id = $1", rid, minutes
        )

    def emails(self, rid: UUID) -> list[EmailMessage]:
        ours = {outbound_id(rid, "ack"), outbound_id(rid, "reply")}
        return [m for m in self.smtp.sent if m["Message-ID"] in ours]

    def acks(self, rid: UUID) -> list[EmailMessage]:
        return [m for m in self.smtp.sent if m["Message-ID"] == outbound_id(rid, "ack")]

    def replies(self, rid: UUID) -> list[EmailMessage]:
        return [m for m in self.smtp.sent if m["Message-ID"] == outbound_id(rid, "reply")]

    def reply(self, rid: UUID) -> EmailMessage:
        (msg,) = self.replies(rid)
        return msg

    async def citations(self, rid: UUID) -> list:
        return await db.pool().fetch(
            """SELECT c.*, d.sha256 AS doc_sha256, d.external_id, p.text AS page_text
               FROM citations c
               JOIN documents d ON d.id = c.document_id
               LEFT JOIN pages p ON p.sha256 = c.sha256 AND p.page = c.page
               WHERE c.request_id = $1 ORDER BY c.created_at, c.id""",
            rid,
        )
