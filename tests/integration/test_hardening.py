"""Retry policy, circuit breakers, the outbox, sealed delivery records, size budgets and mailbox
housekeeping, against real Postgres and Redis (fakes for everything outside the process)."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from arq import Retry
from arq.connections import ArqRedis

from agent import db, pipeline, store, worker
from agent.breaker import Breaker, Breakers, Open
from agent.config import get_settings
from agent.delivery.drop import DropUnavailable
from agent.mail.ingest import Ingestor, ingest_raw
from agent.models import MatterNotFound, PortalUnavailable, ProviderRejected

from .harness import MATTER, FakeDrop, attachments, body_text, doc_id, make_email, outbound_id

pytestmark = pytest.mark.integration

REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"
OTHER_DOCS = [doc_id(MATTER, "Other Documents", i) for i in (1, 2, 3)]


async def age(rid, hours: float) -> None:
    await db.pool().execute(
        "UPDATE requests SET received_at = now() - make_interval(secs => $2) WHERE id = $1", rid, hours * 3600
    )


# ======================================================================== breaker mechanics


async def test_breaker_counts_only_consecutive_availability_failures(h):
    b = Breaker(h.redis, "test", failures=3, open_s=30, open_cap_s=60)
    for _ in range(2):
        await b.record(PortalUnavailable("timeout"))
    await b.record(MatterNotFound("M1"))  # the portal answered: the run of failures is over
    for _ in range(2):
        await b.record(PortalUnavailable("timeout"))
    assert await b.before_call() is False  # still closed

    await b.record(PortalUnavailable("timeout"))  # third in a row

    with pytest.raises(Open) as exc:
        await b.before_call()
    assert 24 <= exc.value.retry_in_s <= 36  # 30 s +/- 20%


async def test_half_open_lets_exactly_one_probe_through_and_backs_off_on_failure(h):
    b = Breaker(h.redis, "test", failures=1, open_s=0.2, open_cap_s=10)
    await b.record(PortalUnavailable("timeout"))
    await asyncio.sleep(0.3)

    assert await b.before_call() is True  # the probe
    with pytest.raises(Open):
        await b.before_call()  # everyone else waits for its verdict
    await b.record(PortalUnavailable("still down"), probe=True)
    with pytest.raises(Open) as exc:
        await b.before_call()
    assert 0.25 <= exc.value.retry_in_s <= 0.48  # the interval doubled (0.4 s +/- 20%)

    await asyncio.sleep(0.5)
    assert await b.before_call() is True
    await b.record(None, probe=True)  # recovered: closed again
    assert await b.before_call() is False


async def test_breaker_fails_open_when_redis_is_unreachable(h):
    dead = ArqRedis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    b = Breaker(dead, "test", failures=1, open_s=60, open_cap_s=60)
    assert await b.before_call() is False
    await b.record(PortalUnavailable("timeout"))  # logged, not raised
    await dead.aclose()


# ======================================================================== parking, deadline, never-retried


async def test_an_open_breaker_parks_the_request_until_its_deadline_then_apologises(h):
    h.configure(breaker_failures=1)
    h.provider.portal_down = lambda: PortalUnavailable("portal timed out")
    rid = await h.ingest(make_email(REQUEST))

    defers = await h.run_job(rid)  # try 1 fails and opens the breaker; try 2 is parked

    assert len(defers) == 2 and defers[1] >= 0.8 * get_settings().breaker_open_s
    row = await h.request(rid)
    assert row["state"] == "fetching" and row["attempts"] == 1 and h.provider.calls["list"] == 1

    await age(rid, 3)  # past request_deadline_s: no more parking
    await h.process(rid, 3)

    row = await h.request(rid)
    assert row["state"] == "failed" and h.provider.calls["list"] == 1
    assert "regulator's website kept failing" in body_text(h.reply(rid))


async def test_a_retryable_failure_past_the_deadline_is_final(h):
    h.provider.portal_down = lambda: PortalUnavailable("portal timed out")
    rid = await h.ingest(make_email(REQUEST))
    await age(rid, 3)

    assert await h.run_job(rid) == []

    row = await h.request(rid)
    assert row["state"] == "failed" and row["attempts"] == 1
    assert len(h.replies(rid)) == 1


async def test_a_provider_rejection_is_never_retried(h):
    h.provider.portal_errors.append(ProviderRejected("HTTP 403 from the search API"))
    rid = await h.ingest(make_email(REQUEST))

    assert await h.run_job(rid) == []

    row = await h.request(rid)
    assert row["state"] == "failed" and row["error"].startswith("ProviderRejected")
    assert ProviderRejected.user_message in body_text(h.reply(rid))
    assert h.provider.calls["list"] == 1


async def test_a_try_that_overruns_the_pipeline_timeout_is_retried(h):
    h.configure(pipeline_timeout_s=1)
    h.provider.hang_next = 1  # the first portal visit never returns
    rid = await h.ingest(make_email(REQUEST))

    defers = await h.run_job(rid)

    assert len(defers) == 1
    row = await h.request(rid)
    assert row["state"] == "done" and row["attempts"] == 2
    retry = await store.last_retry(rid)
    assert retry["error"].startswith("PipelineTimeout") and retry["dependency"] == "uarb"


async def test_single_flight_contention_parks_without_using_an_attempt(h, monkeypatch):
    monkeypatch.setattr(pipeline, "SINGLE_FLIGHT_WAIT_S", 0.2)
    sf = f"lock:sf:uarb:{MATTER}:Other Documents"
    await h.redis.set(sf, "another-worker", ex=60)
    rid = await h.ingest(make_email(REQUEST))

    with pytest.raises(worker.Park):
        await h.process(rid, 1)
    row = await h.request(rid)
    assert row["state"] == "fetching" and row["attempts"] == 0 and len(h.acks(rid)) == 1

    await h.redis.delete(sf)
    await h.run_job(rid, first_try=2)
    assert (await h.request(rid))["state"] == "done"


# ======================================================================== drop behind its breaker


async def open_breaker(h, name: str) -> None:
    await Breakers(h.redis, get_settings()).get(name).record(DropUnavailable("down", status=503))


async def test_with_drop_down_a_small_zip_is_attached_without_trying_drop(h):
    h.configure(breaker_failures=1)
    h.drop = FakeDrop()
    await open_breaker(h, "drop")
    rid = await h.ingest(make_email(REQUEST))

    assert await h.run_job(rid) == []

    row = await h.request(rid)
    assert row["state"] == "done" and row["result"]["delivery"] == "attachment"
    assert h.drop.uploads == [] and any(n.endswith(".zip") for n in attachments(h.reply(rid)))


async def test_with_drop_down_a_zip_too_large_to_attach_parks_until_drop_is_back(h):
    h.configure(breaker_failures=1, attach_inline_max_bytes=100)
    h.drop = FakeDrop()
    await open_breaker(h, "drop")
    rid = await h.ingest(make_email(REQUEST))

    await h.run_job(rid)
    row = await h.request(rid)
    assert row["state"] == "packaging" and row["attempts"] == 0 and h.replies(rid) == []

    await h.redis.delete("breaker:drop")
    await h.run_job(rid, first_try=2)
    row = await h.request(rid)
    assert row["state"] == "done" and row["result"]["delivery"] == "link" and len(h.drop.uploads) == 1


# ======================================================================== outbox


async def test_an_smtp_breaker_holds_the_outbox_without_using_its_attempts(h):
    h.configure(breaker_failures=1)
    h.smtp.fail["ack"] = 1  # opens the smtp breaker
    rid = await h.ingest(make_email(REQUEST))

    await h.process(rid, 1)

    rows = {r["kind"]: r for r in await store.outbound_rows(rid)}
    assert {k: r["status"] for k, r in rows.items()} == {"ack": "queued", "reply": "queued"}
    assert rows["ack"]["attempts"] == 1 and rows["reply"]["attempts"] == 0
    assert (await h.request(rid))["state"] == "replying" and h.provider.calls == {"list": 1, "download": 1}

    await h.redis.delete("breaker:smtp")
    await h.drain_outbound(rid)
    assert (await h.request(rid))["state"] == "done"
    assert [m["Message-ID"] for m in h.smtp.sent] == [outbound_id(rid, "ack"), outbound_id(rid, "reply")]


async def test_mail_still_undeliverable_after_48_hours_is_given_up_with_an_alert(h):
    h.smtp.fail["reply"] = 10_000
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid, outbound_rounds=1)
    assert (await h.request(rid))["state"] == "replying"

    await db.pool().execute("UPDATE outbound SET created_at = now() - interval '49 hours' WHERE request_id = $1", rid)
    await h.drain_outbound(rid, rounds=1)

    (reply,) = [r for r in await store.outbound_rows(rid) if r["kind"] == "reply"]
    assert reply["status"] == "undeliverable"
    row = await h.request(rid)
    assert row["state"] == "failed" and row["error"].startswith("DeliveryFailed")
    assert "dead_letter" in await h.event_kinds(rid)


async def test_the_sweeper_requeues_outbox_mail_whose_job_was_lost(h):
    h.smtp.fail["reply"] = 1
    rid = await h.ingest(make_email(REQUEST))
    await h.process(rid, 1)
    await h.redis.flushdb()  # the deferred send_outbound job is gone
    await db.pool().execute(
        "UPDATE outbound SET next_attempt_at = now() - interval '10 minutes' WHERE request_id = $1", rid
    )
    await h.backdate(rid, minutes=20)

    await worker.sweep({"redis": h.redis})

    jobs = {j.job_id for j in await h.redis.queued_jobs()}
    assert jobs == {f"out:{outbound_id(rid, 'reply')}"}  # the request itself is the outbox's now


async def test_queued_mail_and_the_delivery_record_are_sealed_at_rest(h):
    h.drop = FakeDrop()
    rid = await h.ingest(make_email(REQUEST))
    await h.run_job(rid)

    for r in await db.pool().fetch("SELECT body FROM outbound WHERE request_id = $1", rid):
        body = bytes(r["body"])
        assert b"drop.test/d/" not in body and b"Subject:" not in body
    record = (await h.request(rid))["delivery"]
    flat = json.dumps(record)
    assert record["drop_id"] == "abcdef123456" and "#k=" not in flat and "delete-token" not in flat


async def test_the_sweeper_spreads_requeued_requests(h, monkeypatch):
    monkeypatch.setattr(worker, "SWEEP_JITTER_S", 30.0)
    rid = await h.ingest(make_email(REQUEST))
    await h.redis.flushdb()
    await h.backdate(rid, minutes=20)
    now_ms = time.time() * 1000

    await worker.sweep({"redis": h.redis})

    (job,) = await h.redis.queued_jobs()
    assert job.job_id == f"req:{rid}" and now_ms - 1000 <= job.score <= now_ms + 31_000


# ======================================================================== disk and size budgets


async def test_low_disk_stops_downloads_and_is_retried(h):
    h.configure(disk_min_free_bytes=10**18)
    rid = await h.ingest(make_email(REQUEST))

    with pytest.raises(Retry):
        await h.process(rid, 1)

    assert h.provider.calls == {"list": 1}  # listed, but nothing downloaded
    assert (await store.last_retry(rid))["error"].startswith("DiskLow")


async def test_a_request_over_the_size_budget_leaves_documents_out_and_says_which(h):
    sizes = [len(h.provider.pdf(d)) for d in OTHER_DOCS]
    h.configure(max_request_bytes=sizes[0] + sizes[1] + 1)
    rid = await h.ingest(make_email(REQUEST))

    assert await h.run_job(rid) == []

    row = await h.request(rid)
    assert row["state"] == "done" and row["result"]["files"] == 2
    assert row["result"]["skipped"] == ["Other Documents 3 for M12205"]
    text = body_text(h.reply(rid))
    assert "I left out Other Documents 3 for M12205 because together the documents would be over" in text
    assert "most recent" not in text  # not every listed document was sent


# ======================================================================== mailbox


class FakeMailbox:
    def __init__(self) -> None:
        self.commands: list[tuple] = []

    async def uid(self, command, *args):
        self.commands.append((command, *args))
        return SimpleNamespace(result="OK", lines=[b"done"])

    async def uid_search(self, *args):
        raise AssertionError("the mailbox must not be read while the disk is low")


async def test_new_mail_stays_unread_while_the_disk_is_low(h):
    h.configure(disk_min_free_bytes=10**18)
    await Ingestor(get_settings(), h.redis)._sweep(FakeMailbox())


async def test_settled_requests_are_expunged_from_the_mailbox_after_seven_days(h):
    old, recent = make_email(REQUEST), make_email(REQUEST, from_addr="bob@example.com")
    await ingest_raw(old, h.redis, uid=41, uidvalidity=7)
    await ingest_raw(recent, h.redis, uid=42, uidvalidity=7)
    rids = [r["id"] for r in await db.pool().fetch("SELECT id FROM requests ORDER BY imap_uid")]
    for rid in rids:
        await h.run_job(rid)
    await db.pool().execute("UPDATE requests SET updated_at = now() - interval '8 days' WHERE id = $1", rids[0])
    ing = Ingestor(get_settings(), h.redis)
    ing.uidvalidity = 7
    box = FakeMailbox()

    await ing._expunge_settled(box)

    assert box.commands == [("store", "41", "+FLAGS", r"(\Deleted)"), ("expunge", "41")]
    expunged = {r["id"]: r["imap_expunged_at"] for r in await db.pool().fetch("SELECT id, imap_expunged_at FROM requests")}
    assert expunged[rids[0]] is not None and expunged[rids[1]] is None


# ======================================================================== queue format


async def test_jobs_are_stored_as_json_and_redis_reads_time_out(h):
    rid = await h.ingest(make_email(REQUEST))
    job = json.loads(await h.redis.get(f"arq:job:req:{rid}"))
    assert job["f"] == "process_request" and job["a"] == [str(rid)]
    assert h.redis.connection_pool.connection_kwargs["socket_timeout"] == 5.0
