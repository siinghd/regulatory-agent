"""SOC 2 application controls against real Postgres and Redis: the append-only audit trail and
its daily hash-chained export, retention and disposal, data subject requests, incident hooks
(pause, block, revoke), reconciliation, health checks and metrics."""

import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosmtplib
import asyncpg
import httpx
import pytest
import respx
from prometheus_client import REGISTRY

import agent.mail.outbound
from agent import admin, audit, blobs, db, health, reconcile, retention, store
from agent.config import get_settings
from agent.delivery.choose import Delivery
from agent.mail.ingest import HEARTBEAT_KEY, Ingestor
from agent.web import app as web
from agent.web import progress

from .harness import MATTER, FakeDrop, body_text, doc_id, llm_parse, make_email, message_id_of, outbound_id

pytestmark = pytest.mark.integration

REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"
ALICE = "alice@example.com"
OTHER_DOCS = [doc_id(MATTER, "Other Documents", i) for i in (1, 2, 3)]


async def all_events() -> list:
    return await db.pool().fetch("SELECT * FROM events ORDER BY id")


async def served(h, body: str = REQUEST, **kw):
    rid = await h.ingest(make_email(body, **kw))
    await h.run_job(rid)
    return rid


async def sql(query: str, *args):
    return await db.pool().execute(query, *args)


# ======================================================================== audit trail


async def test_events_are_append_only_and_outlive_their_request(h):
    rid = await served(h)
    n = len(await store.events(rid))
    assert n > 3
    for statement in ("UPDATE events SET kind = 'x'", "DELETE FROM events"):
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="append-only"):
            await sql(statement)
    await sql("DELETE FROM requests WHERE id = $1", rid)  # no cascade, no FK
    assert len(await store.events(rid)) == n


async def test_only_purge_events_removes_events_and_never_younger_than_400_days(h):
    await sql("INSERT INTO events (kind, data, at) VALUES ('old', '{}', now() - interval '401 days')")
    await sql("INSERT INTO events (kind, data, at) VALUES ('recent', '{}', now() - interval '399 days')")
    with pytest.raises(asyncpg.RaiseError, match="at least 400 days"):
        await db.pool().fetchval("SELECT purge_events(interval '30 days')")
    assert await db.pool().fetchval("SELECT purge_events(interval '400 days')") == 1
    assert [e["kind"] for e in await all_events()] == ["recent"]


async def test_the_audit_trail_names_people_only_by_hmac(h):
    with audit.component_scope("ingest"):
        rid = await h.ingest(make_email(REQUEST))
    with audit.component_scope("worker"):
        await h.run_job(rid)

    row = await h.request(rid)
    h_alice = audit.subject_hash(ALICE)
    assert row["state"] == "done" and row["from_h"] == h_alice
    events = await all_events()
    assert ALICE not in json.dumps([dict(e["data"] or {}) for e in events])
    role = await db.pool().fetchval("SELECT current_user")
    assert all(e["actor"] == role and e["subject_h"] == h_alice for e in events)
    assert all(e["app_version"] == get_settings().app_version for e in events)
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["received"]["data"] == {"from_h": h_alice} and by_kind["received"]["component"] == "ingest"
    assert by_kind["state:done"]["component"] == "worker"

    for kind in ("ack", "reply"):
        sent = by_kind[f"sent:{kind}"]["data"]
        assert sent["message_id"] == outbound_id(rid, kind) and sent["kind"] == kind
        assert sent["to_h"] == h_alice and sent["smtp_code"] == 250 and sent["size"] > 500
    assert by_kind["sent:reply"]["data"]["attachments"] == ["M12205_Other_Documents.zip"]
    assert by_kind["sent:ack"]["data"]["attachments"] == [] and by_kind["sent:reply"]["data"]["drop_id"] is None

    llm = [e["data"] for e in events if e["kind"] == "llm.call"]
    assert llm and llm[0]["data_class"] == "public_document" and llm[0]["purpose"] == "summary"
    assert llm[0]["input_chars"] > 0 and llm[0]["zdr"] is True and llm[0]["model"] == "fake/llm"


async def test_an_llm_parsed_email_is_audited_as_email_body(h):
    h.llm.parse = llm_parse(matter=MATTER, doc_type="Other Documents")
    rid = await served(h, "Could you send me stuff for M12205?")
    calls = [e["data"] for e in await store.events(rid) if e["kind"] == "llm.call"]
    gate = [c for c in calls if c["data_class"] == "email_body"]
    assert len(gate) == 1 and gate[0]["purpose"] == "gate" and gate[0]["input_chars"] > 20
    assert gate[0]["model"] == "fake/llm" and gate[0]["outcome"] == "ok"

    h.llm.parse = None  # every model down: the email was still offered to them
    rid = await served(h, "Could you send me stuff for M12205?", from_addr="bob@example.org")
    (gate,) = [e["data"] for e in await store.events(rid) if e["kind"] == "llm.call"]
    assert (gate["purpose"], gate["outcome"], gate["data_class"]) == ("gate", "unavailable", "email_body")


async def test_undeliverable_mail_is_audited_with_its_code_but_not_the_address(h, monkeypatch):
    async def refuse(msg):
        if msg["Message-ID"].startswith("<reply."):
            raise aiosmtplib.SMTPRecipientRefused(550, f"5.1.1 <{ALICE}>: Recipient address rejected", ALICE)
        return await h.smtp.send(msg)

    monkeypatch.setattr(agent.mail.outbound, "send", refuse)
    rid = await served(h)
    (event,) = [e for e in await store.events(rid) if e["kind"] == "undeliverable"]
    assert event["data"]["smtp_code"] == 550 and event["data"]["message_id"] == outbound_id(rid, "reply")
    assert ALICE not in json.dumps(event["data"]) and audit.pseudonym(ALICE) in event["data"]["error"]
    assert (await h.request(rid))["state"] == "failed"


async def test_daily_exports_form_a_hash_chain_that_exposes_tampering(h, tmp_path):
    today = datetime.now(UTC).date()
    for days, kind in ((3, "three"), (2, "two"), (2, "two-b"), (1, "one")):
        await sql("INSERT INTO events (kind, data, at) VALUES ($1, '{}', now() - make_interval(days => $2))", kind, days)
    directory = tmp_path / "audit"

    first = await audit.export_day(today - timedelta(days=3), directory)
    written = await audit.export_pending(today, directory)
    assert [p.name for p in written] == [f"{today - timedelta(days=d)}.jsonl" for d in (2, 1)]
    assert await audit.export_pending(today, directory) == []  # idempotent: never rewritten
    assert audit.verify_chain(directory) == []

    lines = [json.loads(x) for x in written[0].read_text().splitlines()]
    assert lines[0]["prev_file"] == first.name and lines[0]["events"] == 2
    assert [x["kind"] for x in lines[1:]] == ["two", "two-b"] and all(x["actor"] == "agent" for x in lines[1:])
    assert oct(written[0].stat().st_mode & 0o777) == "0o600"

    first.write_text(first.read_text().replace('"three"', '"edited"'))
    problem = f"{written[0].name}: does not link to {first.name} (records {first.name} {lines[0]['prev_sha256'][:12]})"
    assert audit.verify_chain(directory) == [problem]


# ======================================================================== retention


async def backdate(rid, *, received_days: float = 0, updated_days: float = 0) -> None:
    await sql("""UPDATE requests SET received_at = now() - make_interval(days => $2),
                 updated_at = now() - make_interval(days => $3) WHERE id = $1""", rid, received_days, updated_days)


def raw_file(sha: str) -> Path:
    return Path(get_settings().data_dir) / "raw" / sha[:2] / sha


async def test_purge_follows_the_schedule_once(h):
    fresh = await served(h)
    month = await served(h)
    rejected_rid = await h.ingest(make_email("Buy cheap watches", from_addr="spam@example.net"))
    h.auth.verdict = "fail"
    await h.run_job(rejected_rid)
    h.auth.verdict = "pass"
    old = await served(h)
    ancient = await served(h)
    raws = {rid: (await h.request(rid))["raw_sha256"] for rid in (fresh, month, rejected_rid, old, ancient)}
    await backdate(fresh, received_days=10, updated_days=10)
    await backdate(month, received_days=31, updated_days=31)
    await backdate(rejected_rid, received_days=8, updated_days=8)
    await backdate(old, received_days=91, updated_days=91)
    await backdate(ancient, received_days=401, updated_days=401)
    ancient_events = len(await store.events(ancient))
    await sql("INSERT INTO events (kind, data, at) VALUES ('ancient', '{}', now() - interval '401 days')")
    # one document no request has used for 13 months, one orphaned raw file
    stale = await db.pool().fetchrow(
        "UPDATE documents SET last_used_at = now() - interval '400 days', downloaded_at = now() - interval '400 days' "
        "WHERE external_id = $1 RETURNING sha256", OTHER_DOCS[0])
    orphan = raw_file(blobs.put_raw(b"Subject: never inserted\r\n\r\n"))
    os.utime(orphan, (time.time() - 3 * 86400,) * 2)

    planned = await retention.purge(dry_run=True)
    assert raw_file(raws[month]).exists() and (await h.request(old))["from_addr"] == ALICE  # nothing changed
    counts = await retention.purge()

    assert counts["raw_purged"] == planned["raw_purged"] == 3  # month (done, 31 d), rejected (8 d), old
    assert counts["pseudonymised"] == planned["pseudonymised"] == 1
    assert counts["requests_deleted"] == planned["requests_deleted"] == 1
    assert counts["events_deleted"] == planned["events_deleted"] == 1
    assert counts["blobs_deleted"] == planned["blobs_deleted"] == 1
    assert counts["orphan_raw_deleted"] == 1 and counts["outbound_bodies_wiped"] == 4  # month's and old's

    assert raw_file(raws[fresh]).exists() and (await h.request(fresh))["raw_sha256"] == raws[fresh]
    for rid in (month, rejected_rid, old):
        assert (await h.request(rid))["raw_sha256"] == "purged" and not raw_file(raws[rid]).exists()
    assert not orphan.exists()
    bodies = await db.pool().fetch("SELECT length(body) AS n FROM outbound WHERE request_id = $1", month)
    assert bodies and all(b["n"] == 0 for b in bodies)

    p = await h.request(old)
    assert p["from_addr"] == audit.pseudonym(ALICE) and p["from_h"] == audit.subject_hash(ALICE)
    assert p["subject"] == "" and p["message_id"].startswith("h:") and p["thread_root"].startswith("h:")
    assert "client_ip" not in (p["auth"] or {}) and p["auth"]["verdict"] == "pass"
    assert p["matter"] == MATTER and p["pseudonymised_at"] is not None  # the record itself stays useful

    assert await h.request(ancient) is None
    assert len(await store.events(ancient)) == ancient_events  # the audit trail outlives it
    doc = await db.pool().fetchrow("SELECT sha256 FROM documents WHERE external_id = $1", OTHER_DOCS[0])
    assert doc["sha256"] is None and not blobs.has_blob(stale["sha256"])
    assert await db.pool().fetchval("SELECT count(*) FROM pages WHERE sha256 = $1", stale["sha256"]) == 0

    purge_events = [e for e in await all_events() if e["kind"] == "purge"]
    assert len(purge_events) == 1 and purge_events[0]["data"] == counts
    assert await retention.purge() == {}  # idempotent


async def test_raw_purge_never_breaks_a_thread_follow_up(h):
    h.llm.parse = llm_parse(matter=MATTER)
    raw = make_email("Could you send me stuff for M12205?", subject="Question")
    rid = await h.ingest(raw)
    await h.run_job(rid)
    assert (await h.request(rid))["state"] == "clarify"
    await backdate(rid, received_days=31, updated_days=31)
    assert (await retention.purge())["raw_purged"] == 1

    h.llm.parse = llm_parse(doc_type="Other Documents")
    answer = make_email("Other Documents please", subject="Re: Question", in_reply_to=outbound_id(rid, "reply"),
                        references=[message_id_of(raw), outbound_id(rid, "reply")])
    r2 = await h.ingest(answer)
    await h.run_job(r2)
    row = await h.request(r2)
    assert (row["state"], row["matter"], row["doc_type"]) == ("done", MATTER, "Other Documents")


async def test_purge_leaves_requests_in_progress_and_their_raw_mail_alone(h):
    rid = await h.ingest(make_email(REQUEST))
    await backdate(rid, received_days=100, updated_days=100)  # never processed: not settled
    assert await retention.purge() == {}
    row = await h.request(rid)
    assert row["from_addr"] == ALICE and raw_file(row["raw_sha256"]).exists()


async def test_purge_removes_stored_files_nothing_refers_to(h):
    rid = await served(h)
    cited = (await h.citations(rid))[0]["sha256"]
    # the regulator replaced the cited file: no document row has that version any more
    await sql("UPDATE documents SET sha256 = NULL WHERE sha256 = $1", cited)
    orphan = blobs.blob_path("ab" * 32)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"%PDF-1.4 nobody's")
    for path in (orphan, blobs.blob_path(cited)):
        os.utime(path, (time.time() - 3 * 86400,) * 2)

    counts = await retention.purge()
    assert counts["orphan_blobs_deleted"] == 1 and not orphan.exists()
    assert blobs.has_blob(cited)  # a recent citation still shows that version


# ======================================================================== data subject requests


DROP_DELETE = "http://127.0.0.1:3060/api/file/abcdef123456"


async def test_dsar_export_then_delete_revokes_links_erases_and_suppresses(h):
    h.drop = FakeDrop()
    rid = await served(h)
    other = await served(h, from_addr="bob@example.org")

    bundle = await admin.dsar_export(ALICE.upper())
    assert bundle["subject_h"] == audit.subject_hash(ALICE)
    assert [r["id"] for r in bundle["requests"]] == [str(rid)]
    assert "url" not in bundle["requests"][0]["delivery"] and "delete_token" not in bundle["requests"][0]["delivery"]
    assert len(bundle["raw_emails"]) == 1 and "M12205" in bundle["raw_emails"][0]["content"]
    assert {e["kind"] for e in bundle["events"]} >= {"received", "sent:reply"}
    assert [x["kind"] for x in bundle["emails_sent"]] == ["ack", "reply"]
    raw_sha = (await h.request(rid))["raw_sha256"]

    with respx.mock(assert_all_called=True) as drop:
        route = drop.delete(DROP_DELETE).mock(return_value=httpx.Response(200))
        counts = await admin.dsar_delete(ALICE)
    assert route.calls.last.request.headers["X-Delete-Token"] == "delete-token"
    assert counts == {"links_revoked": 1, "requests_erased": 1, "raw_deleted": 1}
    assert await h.request(rid) is None and not raw_file(raw_sha).exists()
    assert await h.request(other) is not None
    sup = await db.pool().fetchrow("SELECT * FROM suppression WHERE value_h = $1", audit.subject_hash(ALICE))
    assert sup["reason"] == "dsar_delete" and sup["erase"] and sup["erased_at"] is not None

    sent = len(h.smtp.sent)
    again = await served(h)  # they write again: dropped unanswered
    assert (await h.request(again))["reject_reason"] == "suppressed" and len(h.smtp.sent) == sent
    kinds = [e["kind"] for e in await audit.timeline(subject_h=audit.subject_hash(ALICE))]
    assert {"received", "sent:reply", "admin.dsar_export", "admin.dsar_delete"} <= set(kinds)
    admin_events = [e for e in await all_events() if e["kind"].startswith("admin.")]
    assert all(e["data"]["operator"] == audit.operator() for e in admin_events)
    assert ALICE not in json.dumps([dict(e["data"] or {}) for e in await all_events()])


async def test_delete_my_data_by_email_confirms_suppresses_and_is_erased_by_the_purge(h):
    earlier = await served(h)
    rid = await served(h, "Please remove everything you have about me.", subject="DELETE MY DATA")

    row = await h.request(rid)
    assert (row["state"], row["reject_reason"]) == ("done", "dsar_delete")
    assert "delete your data" in body_text(h.reply(rid)) and "privacy@hsingh.app" in body_text(h.reply(rid))
    assert h.provider.visits() == 2  # only the earlier request touched the portal
    assert "dsar.requested" in await h.event_kinds(rid)
    assert await admin.is_suppressed(ALICE)

    counts = await retention.purge()
    assert counts["dsar_requests_erased"] == 2
    assert await h.request(earlier) is None and await h.request(rid) is None
    assert "dsar.requested" in [e["kind"] for e in await audit.timeline(subject_h=audit.subject_hash(ALICE))]


async def test_erasure_by_the_purge_waits_for_the_confirmation_to_go_out(h):
    h.smtp.fail["reply"] = 100
    rid = await h.ingest(make_email("DELETE MY DATA"))
    await h.run_job(rid, outbound_rounds=1)
    assert await h.queued_outbound(rid) == [outbound_id(rid, "reply")]
    assert "dsar_requests_erased" not in await retention.purge()
    assert await h.request(rid) is not None


# ======================================================================== incident hooks


async def test_pause_parks_requests_and_holds_mail_until_resume(h):
    h.smtp.fail["reply"] = 2  # inline and first outbox try
    queued = await h.ingest(make_email(REQUEST))
    await h.run_job(queued, outbound_rounds=1)
    assert await h.queued_outbound(queued) == [outbound_id(queued, "reply")]

    await admin.pause(h.redis, reason="incident drill")
    rid = await h.ingest(make_email(REQUEST, from_addr="bob@example.org"))
    defers = await h.run_job(rid)
    row = await h.request(rid)
    assert len(defers) == 1 and defers[0] >= admin.PAUSED_RETRY_S
    assert row["state"] == "received" and row["attempts"] == 0  # parked: no attempt used
    attempts = len(h.smtp.attempts)
    await h.drain_outbound(queued)
    assert len(h.smtp.attempts) == attempts and await h.queued_outbound(queued)  # nothing sent

    assert await admin.resume(h.redis) is True
    await h.run_job(rid)
    await h.drain_outbound(queued)
    assert (await h.request(rid))["state"] == "done" and (await h.request(queued))["state"] == "done"
    kinds = [e["kind"] for e in await all_events()]
    assert "admin.pause" in kinds and "admin.resume" in kinds


async def test_a_blocked_domain_is_dropped_unanswered(h):
    await admin.block("@Example.com", reason="abuse report 17")
    rid = await served(h)
    assert (await h.request(rid))["reject_reason"] == "suppressed" and h.smtp.sent == []
    assert h.auth.calls == 0  # dropped before anything else is looked at
    (event,) = [e for e in await all_events() if e["kind"] == "admin.block"]
    assert event["data"]["kind"] == "domain" and "example.com" not in json.dumps(event["data"])


async def test_revoke_deletes_the_link_and_a_retry_cannot_reuse_it(h):
    h.drop = FakeDrop()
    rid = await served(h)
    record = (await h.request(rid))["delivery"]
    assert Delivery.from_record(record, record["files"]) is not None

    with respx.mock(assert_all_called=True) as drop:
        drop.delete(DROP_DELETE).mock(return_value=httpx.Response(404))  # already expired counts as gone
        assert await admin.revoke(rid) is True
    record = (await h.request(rid))["delivery"]
    assert record["kind"] == "revoked" and Delivery.from_record(record, "anything") is None
    (event,) = [e for e in await store.events(rid) if e["kind"] == "admin.revoke"]
    assert event["data"]["drop_id"] == "abcdef123456" and event["data"]["revoked"] is True

    with respx.mock(assert_all_called=False) as drop:
        route = drop.delete(DROP_DELETE).mock(return_value=httpx.Response(500))
        await sql("UPDATE requests SET delivery = $2 WHERE id = $1", rid, {**record, "kind": "link"})
        assert await admin.revoke(rid) is None and not route.called  # nothing sealed left to revoke with


# ======================================================================== reconcile, health, metrics


async def test_reconcile_reports_anomalies_and_records_them(h):
    await served(h)
    clean = await reconcile.reconcile()
    assert clean["anomalies"] == [] and clean["rejections"] == {}

    stuck = await h.ingest(make_email(REQUEST, from_addr="bob@example.org"))
    await h.backdate(stuck, minutes=31)
    sha = await db.pool().fetchval("SELECT sha256 FROM documents WHERE external_id = $1", OTHER_DOCS[0])
    blobs.blob_path(sha).unlink()
    for _ in range(12):
        r = await h.ingest(make_email("hello", from_addr=f"x{secrets.token_hex(3)}@spam.example"))
        await store.transition(r, {"received"}, "rejected", reject_reason="unauthenticated:none")

    findings = await reconcile.reconcile(timedelta(hours=24))
    expected = {"stuck", "documents_blob_missing", "rejection_spike:unauthenticated"}
    assert expected <= set(findings["anomalies"]) <= expected | {"citations_blob_missing"}
    assert findings["stuck_sample"] == [f"{stuck}:received"]
    events = [e for e in await all_events() if e["kind"] == "reconcile"]
    assert len(events) == 2 and events[-1]["data"]["anomalies"] == findings["anomalies"]


async def test_health_checks_against_real_redis(h):
    assert (await health.healthcheck("ingest"))[0] is False
    await Ingestor(get_settings(), h.redis)._heartbeat()
    raw = await h.redis.get(HEARTBEAT_KEY)
    assert abs(float(raw) - time.time()) < 5
    assert (await health.healthcheck("ingest"))[0] is True
    assert (await health.healthcheck("worker"))[0] is False
    await h.redis.set(health.WORKER_HEALTH_KEY, b"Oct-04 j_complete=0", px=61_000)
    assert (await health.healthcheck("worker"))[0] is True


async def test_deep_health_from_the_host(h, tmp_path):
    await h.ingest(make_email(REQUEST))
    await admin.pause(h.redis)
    await Ingestor(get_settings(), h.redis)._heartbeat()
    facts = await web.deep_facts(get_settings().model_copy(update={"backup_log_path": str(tmp_path / "x")}))
    assert facts["ok"] is True and facts["db"]["oldest_open_request_age_s"] >= 0
    assert facts["redis"]["paused"] is True and facts["redis"]["ingest_heartbeat_age_s"] <= 5
    assert facts["redis"]["queue_depth"] >= 1 and facts["backup"] == {"readable": False, "error": "FileNotFoundError"}


async def test_progress_page_works_as_the_column_restricted_web_role(h, database_url):
    rid = await served(h)
    role = f"ragent_web_{secrets.token_hex(3)}"
    cols = ", ".join(store.PROGRESS_COLUMNS)
    await sql(f"CREATE ROLE {role} NOLOGIN")
    try:
        await sql(f"GRANT SELECT ({cols}) ON requests TO {role}")
        await sql(f"GRANT SELECT ON events TO {role}")
        pool, saved = await asyncpg.create_pool(database_url, min_size=1, max_size=2, init=db._init_conn,
                                                server_settings={"role": role}), db._pool
        db._pool = pool
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await db.fetchrow("SELECT * FROM requests LIMIT 1")
            token = (await saved.fetchrow("SELECT track_token FROM requests WHERE id = $1", rid))["track_token"]
            view = await progress._view(token)
            assert view["state"] == "done" and view["outcome"].startswith("Sent 3 documents")
        finally:
            db._pool = saved
            await pool.close()
    finally:
        await sql(f"DROP OWNED BY {role}")
        await sql(f"DROP ROLE {role}")


async def test_final_states_are_counted(h):
    def done() -> float:
        return REGISTRY.get_sample_value("requests_total", {"final_state": "done"}) or 0.0

    before = done()
    await served(h)
    assert done() == before + 1
    assert (REGISTRY.get_sample_value("stage_duration_seconds_count", {"stage": "replying"}) or 0) >= 1
