"""One email, several matters: each further matter the rules can pair with a category becomes a
request of its own, fetched and answered in its own reply in the same thread."""

import pytest

from agent import db, store
from agent.config import get_settings

from .harness import (
    MATTER,
    SecondFakeProvider,
    assert_threaded,
    body_text,
    llm_parse,
    make_email,
    message_id_of,
    outbound_id,
    zip_members,
)

pytestmark = pytest.mark.integration

TWO_REGULATORS = "Hi,\n\nPlease send the rulings for FK-1234 and the Other Documents for M12205.\n\nThanks,\nAlice"


@pytest.fixture
def other(h):
    provider = SecondFakeProvider()
    provider.add_matter("FK-1234", {"Rulings": 2, "Filings": 0})
    h.add_provider(provider)
    return provider


async def splits_of(rid) -> list:
    parent = await store.get(rid)
    return await db.pool().fetch(
        "SELECT * FROM requests WHERE message_id LIKE $1 ORDER BY matter", f"split:%:{parent['message_id']}"
    )


async def test_second_matter_is_fetched_and_answered_in_its_own_reply(h, other):
    h.llm.parse = llm_parse(matter="FK-1234", doc_type="Rulings", other_matters=["M12205"])
    raw = make_email(TWO_REGULATORS)
    orig = message_id_of(raw)
    rid = await h.ingest(raw)
    await h.run_job(rid)

    parent = await h.request(rid)
    assert (parent["state"], parent["matter"], parent["doc_type"]) == ("done", "FK-1234", "Rulings")
    assert parent["parsed"]["extra_matters"] == []
    assert parent["parsed"]["split"] == [["M12205", "Other Documents"]]
    (child,) = await splits_of(rid)
    assert str(child["id"]) in await h.queued_request_ids()
    assert (child["state"], child["provider"], child["matter"], child["doc_type"]) == (
        "accepted", "uarb", MATTER, "Other Documents")
    # the email's verified sender, raw MIME, thread and receipt time; never a gate of its own
    for col in ("from_addr", "raw_sha256", "thread_root", "received_at", "auth", "sender_h"):
        assert child[col] == parent[col], col
    assert child["parsed"]["source"] == "split" and child["imap_uid"] is None

    (ack,) = h.acks(rid)
    assert "I'm also collecting the Other Documents for M12205; it comes in a separate email." in body_text(ack)
    assert "the Other Documents for M12205; it comes in a separate email" in body_text(h.reply(rid))

    await h.run_job(child["id"])
    child = await h.request(child["id"])
    assert child["state"] == "done" and child["result"]["files"] == 3
    assert h.acks(child["id"]) == []  # one acknowledgement for the email
    reply = h.reply(child["id"])
    assert_threaded(reply, to="alice@example.com", in_reply_to=orig, references=orig)
    assert "I downloaded all 3 Other Documents" in body_text(reply)
    assert len([n for n in zip_members(reply) if n.endswith(".pdf")]) == 3
    assert [m["Message-ID"] for m in h.smtp.sent] == [
        outbound_id(rid, "ack"), outbound_id(rid, "reply"), outbound_id(child["id"], "reply")]
    assert other.calls["list"] == 1 and h.provider.calls["list"] == 1


async def test_a_matter_without_one_clear_category_is_offered_not_guessed(h):
    h.llm.parse = llm_parse(matter=MATTER, doc_type="Exhibits", other_matters=["M12383"])
    rid = await h.ingest(make_email("Can you send the Exhibits and Key Documents for M12205 and M12383?"))
    await h.run_job(rid)

    assert await splits_of(rid) == []
    row = await h.request(rid)
    assert row["parsed"]["extra_matters"] == ["M12383"] and row["parsed"]["split"] == []
    assert "You also mentioned M12383, but I didn't fetch it." in body_text(h.reply(rid))


async def test_matters_over_the_cap_are_offered(h, other):
    h.configure(max_matters_per_email=2)
    other.add_matter("FK-5678", {"Rulings": 1, "Filings": 0})
    h.llm.parse = llm_parse(matter=MATTER, doc_type="Other Documents", other_matters=["FK-1234", "FK-5678"])
    rid = await h.ingest(make_email(
        "Please send the Other Documents for M12205, the rulings for FK-1234 and the rulings for FK-5678."))
    await h.run_job(rid)

    assert [r["matter"] for r in await splits_of(rid)] == ["FK-1234"]
    row = await h.request(rid)
    assert row["parsed"]["extra_matters"] == ["FK-5678"]
    text = body_text(h.reply(rid))
    assert "I'm also collecting the Rulings for FK-1234" in text
    assert "You also mentioned FK-5678, but I didn't fetch it." in text


async def test_split_requests_count_against_the_senders_limits(h, other):
    h.configure(rate_per_sender_hour=1)
    h.llm.parse = llm_parse(matter="FK-1234", doc_type="Rulings", other_matters=["M12205"])
    rid = await h.ingest(make_email(TWO_REGULATORS))
    await h.run_job(rid)

    assert await splits_of(rid) == []
    assert (await h.request(rid))["parsed"]["extra_matters"] == ["M12205"]


async def test_a_split_is_created_once_per_email_and_matter(h, other):
    h.llm.parse = llm_parse(matter="FK-1234", doc_type="Rulings", other_matters=["M12205"])
    rid = await h.ingest(make_email(TWO_REGULATORS))
    await h.run_job(rid)
    parent = await h.request(rid)
    again = await store.create_split(parent, matter=MATTER, doc_type="Other Documents", provider="uarb",
                                     parsed={}, auth=parent["auth"])
    assert again is None and len(await splits_of(rid)) == 1


async def test_follow_up_in_a_split_thread_asks_which_matter(h, other):
    h.llm.parse = llm_parse(matter="FK-1234", doc_type="Rulings", other_matters=["M12205"])
    first = make_email(TWO_REGULATORS)
    rid = await h.ingest(first)
    await h.run_job(rid)
    (child,) = await splits_of(rid)
    await h.run_job(child["id"])
    assert get_settings().max_requests_per_thread >= 2  # the split isn't an email of the thread

    our_reply = outbound_id(rid, "reply")
    h.llm.parse = llm_parse(doc_type="Exhibits", clarification="Which matter number would you like?")
    r2 = await h.ingest(make_email("Exhibits please", subject="Re: Document request", in_reply_to=our_reply,
                                   references=[message_id_of(first), our_reply]))
    await h.run_job(r2)

    row = await h.request(r2)
    assert (row["state"], row["matter"]) == ("clarify", None)
    assert "Which matter number would you like?" in body_text(h.reply(r2))
