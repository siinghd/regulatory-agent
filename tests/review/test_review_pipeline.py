"""Code-review findings in the request pipeline, as strict xfail integration tests.

Same harness as tests/integration (throwaway Postgres database, Redis db 15, fake portal / SMTP /
DNS / LLM). Run with: .venv/bin/pytest -m integration tests/review -q -rx
"""

import asyncio
import hashlib
import os
import types
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest

from agent import blobs, db, store
from agent.models import DocumentRef, DownloadedFile
from tests.integration.harness import MATTER, attachments, body_text, make_email, make_pdf, unzip

pytestmark = pytest.mark.integration

OTHER_REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"
SECOND_MATTER = "M12383"


def exhibit(matter: str, number: str, n: int, access: str = "Public") -> DocumentRef:
    return DocumentRef(provider="uarb", matter=matter, doc_type="Exhibits", external_id=number,
                       title=f"Exhibit {number} of {matter}", filed_on=datetime(2025, 4, n, tzinfo=UTC).date(),
                       access=access, row_index=n - 1)


def serve_per_matter_pdfs(provider) -> None:
    """Like FakeProvider.download, but a file's bytes depend on its matter as well as its id
    (as on the real portal, where every matter has its own exhibit H-1)."""

    async def download(self, matter, refs, dest_dir):
        self.calls["download"] += 1
        for ref in refs:
            data = make_pdf(f"{ref.matter} {ref.external_id}")
            sha = hashlib.sha256(data).hexdigest()
            path = os.path.join(dest_dir, f"{sha}.pdf")
            await asyncio.to_thread(Path(path).write_bytes, data)
            self.downloaded.append(f"{ref.matter}/{ref.external_id}")
            yield DownloadedFile(ref=ref, path=path, sha256=sha, size=len(data), filename=f"{ref.external_id}.pdf")

    provider.download = types.MethodType(download, provider)


def zip_of(msg) -> dict[str, bytes]:
    (data,) = [v for k, v in attachments(msg).items() if k.endswith(".zip")]
    return unzip(data)


# ---------------------------------------------------------------- Critical


async def test_exhibit_numbers_shared_by_two_matters_never_cross_over(h):
    h.provider.add_matter(SECOND_MATTER, {"Exhibits": 2, "Key Documents": 0, "Other Documents": 0,
                                          "Transcripts": 0, "Recordings": 0})
    for matter in (MATTER, SECOND_MATTER):
        h.provider.matters[matter][1]["Exhibits"] = [exhibit(matter, "H-1", 1), exhibit(matter, "H-2", 2)]
    serve_per_matter_pdfs(h.provider)

    r1 = await h.ingest(make_email("Please send the Exhibits for M12205"))
    await h.run_job(r1)
    r2 = await h.ingest(make_email("Please send the Exhibits for M12383", from_addr="bob@example.org"))
    await h.run_job(r2)

    members = zip_of(h.reply(r2))
    # (compared by content: make_pdf() embeds a random /ID, so equal documents differ in bytes)
    text = pymupdf.open(stream=members["H-1.pdf"], filetype="pdf")[0].get_text()
    assert f"{SECOND_MATTER} H-1" in text and MATTER not in text  # was: M12205's H-1
    first = pymupdf.open(stream=zip_of(h.reply(r1))["H-1.pdf"], filetype="pdf")[0].get_text()
    assert f"{MATTER} H-1" in first


# ---------------------------------------------------------------- High


async def test_one_damaged_pdf_does_not_block_delivery(h):
    doc = pymupdf.open()
    for i in range(2):
        doc.new_page().insert_text((72, 72), f"The Board approves page {i + 1}.")
    damaged = doc.tobytes(garbage=0, deflate=False, no_new_id=True).replace(b"/Count 2", b"/Count 3", 1)
    first = h.provider.refs("Other Documents")[0].external_id
    h.provider.pdf(first)
    h.provider._pdfs[first] = damaged

    rid = await h.ingest(make_email(OTHER_REQUEST))
    await h.run_job(rid)

    reply = h.reply(rid)
    assert any(name.endswith(".zip") for name in attachments(reply)), body_text(reply)


async def test_a_request_whose_raw_mime_is_gone_ends_in_a_terminal_state(h):
    rid = await h.ingest(make_email(OTHER_REQUEST))
    row = await h.request(rid)
    sha = row["raw_sha256"]
    (blobs._root("raw") / sha[:2] / sha).unlink()

    try:
        await h.run_job(rid)
    except Exception:  # noqa: BLE001, S110 - arq would log "failed" and drop the job
        pass

    assert (await h.request(rid))["state"] in store.TERMINAL


# ---------------------------------------------------------------- Medium


async def test_all_confidential_tab_says_so(h):
    h.provider.matters[MATTER][1]["Exhibits"] = [
        exhibit(MATTER, "H-1", 1, "Confidential"), exhibit(MATTER, "H-2", 2, "Confidential")
    ]

    rid = await h.ingest(make_email("Please send the Exhibits for M12205"))
    await h.run_job(rid)

    text = body_text(h.reply(rid))
    assert "confidential" in text.lower(), text


async def test_a_tab_with_a_confidential_row_is_served_from_cache(h):
    h.provider.matters[MATTER][1]["Exhibits"] = [exhibit(MATTER, "H-1", 1), exhibit(MATTER, "H-2", 2, "Confidential")]

    r1 = await h.ingest(make_email("Please send the Exhibits for M12205"))
    await h.run_job(r1)
    before = h.provider.calls["list"]
    r2 = await h.ingest(make_email("Please send the Exhibits for M12205", from_addr="bob@example.org"))
    await h.run_job(r2)

    assert h.provider.calls["list"] == before


async def test_a_listing_older_than_the_ttl_is_not_served(h):
    r1 = await h.ingest(make_email(OTHER_REQUEST))
    await h.run_job(r1)
    await db.pool().execute("UPDATE matters SET fetched_at = fetched_at - interval '5 hours'")

    r2 = await h.ingest(make_email("Please send the Exhibits for M12205", from_addr="bob@example.org"))
    await h.run_job(r2)  # visits the portal for Exhibits; the Other Documents listing rides along
    await db.pool().execute("UPDATE matters SET fetched_at = fetched_at - interval '5 hours'")
    before = h.provider.calls["list"]

    r3 = await h.ingest(make_email(OTHER_REQUEST, from_addr="carol@example.net"))
    await h.run_job(r3)

    assert h.provider.calls["list"] == before + 1  # the Other Documents listing is ~10 h old


async def test_a_crash_before_sender_verification_never_emails_the_sender(h):
    async def broken_verify(*_args, **_kwargs):
        raise RuntimeError("resolver misconfigured")

    h._mp.setattr("agent.mail.auth.verify_sender", broken_verify)
    rid = await h.ingest(make_email(OTHER_REQUEST, from_addr="victim@example.net"))

    await h.run_job(rid)

    assert h.smtp.sent == []
