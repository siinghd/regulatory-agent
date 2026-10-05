"""Word files through the whole pipeline (FERC issues its orders as DOCX), where replies go, and
what an in-thread "thanks" gets: real Postgres and Redis, fakes for everything else (harness.py)."""

import html
import re
import secrets

import asyncpg
import httpx
import pytest

from agent import db
from agent.web import app as web

from .harness import (
    AGENT_ADDRESS,
    MATTER,
    PUBLIC_BASE_URL,
    SUMMARY_TEXT,
    assert_threaded,
    body_text,
    doc_id,
    make_email,
    make_spreadsheet,
    message_id_of,
    outbound_id,
    pdf_lines,
    zip_members,
)

pytestmark = pytest.mark.integration

REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"
OTHER_DOCS = [doc_id(MATTER, "Other Documents", i) for i in (1, 2, 3)]
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
BASIS = "The summary is based on 2 of 3 documents; 1 couldn't be read (a spreadsheet)."


def viewer() -> httpx.AsyncClient:
    """The citation viewer in this event loop, on the harness's database pool and data dir."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=PUBLIC_BASE_URL)


# ======================================================================== Word files


async def test_word_files_are_summarised_cited_and_shown_quote_only(h):
    # two orders issued as Word files and an exhibit spreadsheet, as a FERC docket has them
    h.provider.serve_as("Other Documents", [".docx", ".docx", ".xlsx"])
    rid = await h.ingest(make_email(REQUEST))

    assert await h.run_job(rid) == []

    row = await h.request(rid)
    assert row["state"] == "done" and row["result"]["citations"] == 4
    assert h.llm.calls["_SummaryOut"] == 1
    # the model was shown the Word files' text, labelled like any PDF page
    (prompt,) = [u for u in h.llm.users if "<<<DOC " in u]
    assert f"<<<DOC {OTHER_DOCS[0]} PAGE 1" in prompt and f"<<<DOC {OTHER_DOCS[1]} PAGE 1" in prompt
    assert OTHER_DOCS[2] not in prompt.split("Document pages", 1)[1]  # the spreadsheet has no pages

    reply = h.reply(rid)
    text = body_text(reply)
    assert f"Summary\n{SUMMARY_TEXT}\n{BASIS}\n" in text
    assert html.escape(BASIS) in reply.get_body(preferencelist=("html",)).get_content()
    assert "Key points, each linked to the exact passage:" in text
    assert "/files/" not in text  # the page viewer is for PDFs; these files are in the ZIP
    members = zip_members(reply)
    assert {n for n in members if n.startswith(MATTER)} == {
        f"{OTHER_DOCS[0]}.docx", f"{OTHER_DOCS[1]}.docx", f"{OTHER_DOCS[2]}.xlsx",
    }
    assert members[f"{OTHER_DOCS[2]}.xlsx"] == make_spreadsheet(OTHER_DOCS[2])

    events = [e for e in await db.pool().fetch("SELECT kind, data FROM events WHERE request_id = $1", rid)]
    (summary,) = [e["data"] for e in events if e["kind"] == "summary"]
    assert summary["based_on"] == {"used": 2, "total": 3, "unreadable": 1, "unread": 0, "kinds": ["spreadsheet"]}
    llm_calls = [e["data"] for e in events if e["kind"] == "llm.call"]
    assert {c["purpose"] for c in llm_calls} >= {"summary"} and all(c["data_class"] for c in llm_calls)

    # every citation points into a Word file's extracted text and carries its neighbouring sentences
    cits = await h.citations(rid)
    assert len(cits) == 4
    for c in cits:
        assert c["external_id"] in OTHER_DOCS[:2]
        assert c["page_text"][c["char_start"]:c["char_end"]] == c["quote"]
        assert c["context_before"] or c["context_after"]
        assert f"{PUBLIC_BASE_URL}/c/{c['id']}" in text
    first = next(c for c in cits if c["quote"].endswith(pdf_lines(c["external_id"])[2]))
    assert first["context_after"] == pdf_lines(first["external_id"])[3]

    # the quote-only citation page and the download it links
    doc = await db.pool().fetchrow("SELECT id, sha256, filename FROM documents WHERE external_id = $1",
                                   first["external_id"])
    async with viewer() as client:
        page = await client.get(f"/c/{first['id']}")
        assert page.status_code == 200
        flat = " ".join(page.text.split())
        assert f"<mark>{html.escape(first['quote'])}</mark>".replace("\n", " ") in flat
        assert f'<span class="context">{html.escape(first["context_after"])}</span>' in flat
        assert "data-pdf-url" not in page.text and "Download Word file" in page.text
        link = re.search(r'href="(/files/[^"]+\.docx)" download="([^"]+)"', page.text)
        assert link and link.group(1) == f"/files/{doc['id']}/{doc['sha256']}.docx"
        assert link.group(2) == doc["filename"] == f"{first['external_id']}.docx"

        download = await client.get(link.group(1))
        assert download.status_code == 200
        assert download.content == h.provider.pdf(first["external_id"])  # the Word file as served
        assert download.headers["content-type"] == DOCX_TYPE
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        # a Word file is never served as a PDF
        assert (await client.get(f"/files/{doc['id']}.pdf")).status_code == 404

    # A repeat request is summarised from the cache, and still says what the summary is based on.
    r2 = await h.ingest(make_email(REQUEST))
    await h.run_job(r2)
    assert h.llm.calls["_SummaryOut"] == 1
    assert f"Summary\n{SUMMARY_TEXT}\n{BASIS}\n" in body_text(h.reply(r2))


async def test_the_quote_only_page_needs_no_more_than_the_web_roles_grants(h, database_url):
    """agent_web may read citations, documents and matters (deploy/sql/grants.sql), never pages."""
    h.provider.serve_as("Other Documents", [".docx", ".docx", ".docx"])
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)
    cid = (await h.citations(rid))[0]["id"]
    role = f"ragent_web_{secrets.token_hex(3)}"
    await db.pool().execute(f"CREATE ROLE {role} NOLOGIN")
    try:
        await db.pool().execute(f"GRANT SELECT ON citations, documents, matters TO {role}")
        pool, saved = await asyncpg.create_pool(database_url, min_size=1, max_size=2, init=db._init_conn,
                                                server_settings={"role": role}), db._pool
        db._pool = pool
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await db.fetchrow("SELECT text FROM pages LIMIT 1")
            view = await web.load_citation(cid)
            assert view is not None and not view.is_pdf and view.ext == "docx"
            assert view.context_before or view.context_after
        finally:
            db._pool = saved
            await pool.close()
    finally:
        await db.pool().execute(f"DROP OWNED BY {role}")
        await db.pool().execute(f"DROP ROLE {role}")


async def test_only_pdfs_read_means_no_basis_line(h):
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)
    assert "The summary is based on" not in body_text(h.reply(rid))  # every document was read


# ======================================================================== where replies go


async def test_replies_go_to_the_a_label_of_the_verified_domain(h):
    raw = make_email(REQUEST, from_addr="alice@BUCHER.example").replace(
        b"From: Alice Smith <alice@BUCHER.example>", "From: Alice Smith <alice@BÜCHER.example>".encode()
    )
    rid = await h.ingest(raw)
    await h.run_job(rid)

    row = await h.request(rid)
    assert row["state"] == "done" and row["auth"]["from_domain"] == "xn--bcher-kva.example"
    orig = message_id_of(raw)
    for msg in h.emails(rid):
        assert_threaded(msg, to="alice@xn--bcher-kva.example", in_reply_to=orig, references=orig)


# ======================================================================== acknowledgements


async def test_thanks_in_a_thread_with_us_gets_no_reply(h):
    first = make_email(REQUEST)
    r1 = await h.ingest(first)
    await h.run_job(r1)
    sent = len(h.smtp.sent)
    our_reply = outbound_id(r1, "reply")

    r2 = await h.ingest(make_email(
        "Thanks, that's all I needed!", subject="Re: Document request", message_id="<thanks@example.com>",
        in_reply_to=our_reply, references=[message_id_of(first), our_reply],
    ))
    await h.run_job(r2)

    row = await h.request(r2)
    assert (row["state"], row["reject_reason"]) == ("rejected", "unrelated_in_thread")
    assert len(h.smtp.sent) == sent and h.emails(r2) == []


async def test_thanks_as_a_first_contact_gets_the_help_reply_once_a_day(h):
    r1 = await h.ingest(make_email("Thanks, that's all I needed!", subject="Hello"))
    await h.run_job(r1)
    row = await h.request(r1)
    assert (row["state"], row["reject_reason"]) == ("rejected", "unrelated")
    help_text = body_text(h.reply(r1))
    assert "I'm an automated assistant that fetches documents" in help_text
    assert h.reply(r1)["X-Loop"] == AGENT_ADDRESS

    r2 = await h.ingest(make_email("Thanks again!", subject="Hello"))
    await h.run_job(r2)
    assert (await h.request(r2))["reject_reason"] == "unrelated_repeat" and h.emails(r2) == []
