"""Abuse limits end to end, against real Postgres and Redis (fake portal / SMTP / DNS / LLM).

Every layer: normalised per-sender, per-organizational-domain and global caps per hour and per
day with bounded "slow down" replies; the pre-authentication limiter in ingest; the global
inbound ceiling; the per-sender in-flight cap; the daily LLM, portal-visit and per-sender byte
budgets; the web token buckets. And: no Redis key ever names an address.
"""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
import structlog
from redis.asyncio import Redis

from agent import audit, blobs, db, limits, pipeline, store, worker
from agent.config import get_settings
from agent.mail.ingest import Ingestor, header_block
from agent.web import app as web
from agent.web.ratelimit import WebLimiter
from tests.integration import harness

from .harness import (
    MATTER,
    PUBLIC_BASE_URL,
    SUMMARY_TEXT,
    body_text,
    doc_id,
    llm_parse,
    make_email,
    outbound_id,
)

pytestmark = pytest.mark.integration

REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"
EXHIBITS = "Hi,\n\nCan you send me the Exhibits for M12205?\n\nThanks"
HARNESS_IP = b"203.0.113.7"  # the client IP in every harness email's trusted Received header


def from_ip(raw: bytes, ip: str) -> bytes:
    return raw.replace(HARNESS_IP, ip.encode())


async def served(h, body: str = REQUEST, *, from_addr: str = "alice@example.com", ip: str = "198.51.100.1"):
    rid = await h.ingest(from_ip(make_email(body, from_addr=from_addr), ip))
    await h.run_job(rid)
    return rid


def notice_emails(h, rid) -> list:
    return [m for m in h.smtp.sent if m["Message-ID"] == outbound_id(rid, "notice")]


async def redis_keys(h) -> list[str]:
    return [k.decode() for k in await h.redis.keys("*")]


# ======================================================================== normalised keys


async def test_tagged_and_dotted_gmail_spellings_are_one_sender(h):
    h.configure(rate_per_sender_hour=3)
    spellings = ["alice+1@gmail.com", "A.Lice+news@Gmail.com", "alice@googlemail.com", "a.l.i.c.e+x@gmail.com"]
    rids = [await served(h, from_addr=addr, ip=f"198.51.100.{i}") for i, addr in enumerate(spellings, 1)]

    assert [(await h.request(r))["state"] for r in rids] == ["done", "done", "done", "rejected"]
    assert (await h.request(rids[3]))["reject_reason"] == "rate_limited:sender"
    assert await h.redis.zcard(f"rl:sender:{limits.sender_key('alice@gmail.com')}") == 3
    keys = await redis_keys(h)
    assert keys and not any(s in k for k in keys for s in ("alice", "gmail", "198.51.100", "@"))


async def test_u_label_and_a_label_spellings_are_one_sender_and_hash_alike(h):
    h.configure(rate_per_sender_hour=1)
    u_label = make_email(REQUEST, from_addr="alice@BUCHER.example").replace(
        b"From: Alice Smith <alice@BUCHER.example>", "From: Alice Smith <alice@BÜCHER.example>".encode())
    first = await h.ingest(u_label)
    await h.run_job(first)
    second = await served(h, from_addr="alice@xn--bcher-kva.example", ip="198.51.100.2")

    assert (await h.request(first))["state"] == "done"
    assert (await h.request(second))["reject_reason"] == "rate_limited:sender"
    # The audit trail's from_h (the U-label From) and to_h (replies go to the A-label) agree.
    row = await h.request(first)
    sent = [e["data"] for e in await store.events(first) if e["kind"].startswith("sent:")]
    assert sent and all(d["to_h"] == row["from_h"] for d in sent)
    assert row["from_h"] == audit.subject_hash("alice@xn--bcher-kva.example") == (await h.request(second))["from_h"]


async def test_subdomains_share_their_organizational_domains_limit(h):
    h.configure(rate_per_domain_hour=2)
    addrs = ["bob@mail.example.com", "carol@example.com", "dave@lists.example.com"]
    rids = [await served(h, from_addr=a, ip=f"198.51.100.{i}") for i, a in enumerate(addrs, 1)]
    assert [(await h.request(r))["reject_reason"] for r in rids] == [None, None, "rate_limited:domain"]


# ======================================================================== daily caps and notices


async def test_the_daily_cap_rejects_with_one_notice_a_day(h):
    h.configure(rate_per_sender_hour=100, rate_per_sender_day=2)
    rids = [await served(h, from_addr=f"alice+{i}@example.com", ip=f"198.51.100.{i}") for i in range(1, 6)]

    rows = [await h.request(r) for r in rids]
    assert [r["state"] for r in rows] == ["done", "done", "rejected", "rejected", "rejected"]
    assert {r["reject_reason"] for r in rows[2:]} == {"rate_limited:sender_day"}
    assert "pausing until tomorrow" in body_text(h.reply(rids[2]))
    assert h.emails(rids[3]) == [] and h.emails(rids[4]) == []
    event = [e["data"] for e in await store.events(rids[2]) if e["kind"] == "rate_limited"]
    assert event == [{"key": "sender", "window": "day", "action": "rejected", "notice": True}]


async def test_slow_down_replies_are_at_most_one_an_hour_and_three_a_day(h):
    h.configure(rate_per_sender_hour=1)
    sender = limits.sender_key("alice@example.com")
    await served(h, ip="198.51.100.1")  # uses the hour's one request
    noticed = []
    for i in range(2, 10):
        rid = await served(h, ip=f"198.51.100.{i}")
        assert (await h.request(rid))["reject_reason"] == "rate_limited:sender"
        noticed.append(bool(h.emails(rid)))
        if i % 2:  # every other email, an hour "passes" for the notice (not for the rate window)
            await h.redis.delete(f"once:rl-notice:hour:{sender}")
    assert noticed == [True, False, True, False, True, False, False, False]


async def test_the_global_daily_cap_rejects_silently(h):
    h.configure(rate_global_day=2)
    rids = [await served(h, from_addr=f"user{i}@org{i}.example", ip=f"198.51.100.{i}") for i in range(1, 4)]
    assert (await h.request(rids[2]))["reject_reason"] == "rate_limited:global_day"
    assert h.emails(rids[2]) == []


# ======================================================================== pre-authentication (ingest)


async def test_preauth_per_ip_stores_headers_only_and_answers_nobody(h):
    h.configure(preauth_per_ip_hour=2)
    raws = [from_ip(make_email(REQUEST, from_addr=f"user{i}@org{i}.example"), "192.0.2.50") for i in range(3)]
    rids = [await h.ingest(raw) for raw in raws]
    other_ip = await h.ingest(from_ip(make_email(REQUEST, from_addr="zed@else.example"), "192.0.2.51"))

    row = await h.request(rids[2])
    assert row["state"] == "rejected" and row["reject_reason"] == "preauth_rate_limited"
    stored = blobs.read_raw(row["raw_sha256"])
    assert stored == header_block(raws[2]) and b"Other Documents" not in stored
    assert sorted(await h.queued_request_ids()) == sorted(str(r) for r in (*rids[:2], other_ip))
    assert await h.event_kinds(rids[2]) == ["received", "state:rejected", "rate_limited"]
    limited = [e["data"] for e in await store.events(rids[2]) if e["kind"] == "rate_limited"]
    assert limited == [{"key": "ip", "window": "hour", "action": "rejected", "stage": "preauth"}]

    await h.run_job(rids[2])  # even if a job ran for it: nothing to do
    assert h.auth.calls == 0 and h.smtp.attempts == []
    assert await h.ingest(raws[2]) == rids[2]  # IMAP redelivery: the same row, still not enqueued
    assert str(rids[2]) not in await h.queued_request_ids()


async def test_preauth_per_claimed_organizational_domain(h):
    h.configure(preauth_per_domain_hour=2)
    addrs = ["a@one.example.org", "b@two.example.org", "c@example.org"]
    rids = [await h.ingest(from_ip(make_email(REQUEST, from_addr=a), f"192.0.2.{i}")) for i, a in enumerate(addrs, 1)]
    assert [(await h.request(r))["reject_reason"] for r in rids] == [None, None, "preauth_rate_limited"]
    limited = [e["data"]["key"] for e in await store.events(rids[2]) if e["kind"] == "rate_limited"]
    assert limited == ["domain"]


class FakeMailbox:
    """The IMAP commands Ingestor._sweep uses, over an in-memory mailbox."""

    def __init__(self, raws: list[bytes]):
        self.messages = dict(enumerate(raws, start=1))
        self.seen: set[int] = set()

    @staticmethod
    def _ok(*lines):
        return SimpleNamespace(result="OK", lines=[*lines, b"completed"])

    async def uid_search(self, *_):
        return self._ok(" ".join(str(u) for u in self.messages if u not in self.seen).encode())

    async def uid(self, command: str, uid: str, *args):
        u = int(uid)
        raw = self.messages[u]
        if command == "store":
            self.seen.add(u)
            return self._ok()
        if args[0] == "(RFC822.SIZE)":
            return self._ok(f"{u} FETCH (UID {u} RFC822.SIZE {len(raw)})".encode())
        return self._ok(f"{u} FETCH (UID {u} BODY[] {{{len(raw)}}}".encode(), bytearray(raw), b")")


async def test_the_inbound_ceiling_defers_the_rest_of_the_mailbox_unseen(h):
    h.configure(inbound_per_minute=3)
    box = FakeMailbox([from_ip(make_email(REQUEST, from_addr=f"u{i}@o{i}.example"), f"192.0.2.{i}") for i in range(5)])
    ing = Ingestor(get_settings(), h.redis)
    ing.uidvalidity = 1

    assert await ing._sweep(box) is True
    assert box.seen == {1, 2, 3} and len(await h.queued_request_ids()) == 3
    assert await ing._sweep(box) is True and box.seen == {1, 2, 3}  # same minute: still deferred, not lost

    await h.redis.delete("rl:inbound:minute")  # the minute passes
    assert await ing._sweep(box) is False
    assert box.seen == {1, 2, 3, 4, 5} and len(await h.queued_request_ids()) == 5
    deferred = await db.pool().fetch("SELECT data FROM events WHERE kind = 'rate_limited' AND request_id IS NULL")
    assert [r["data"] for r in deferred] == [{"key": "inbound", "window": "minute", "action": "deferred", "waiting": 2}]


# ======================================================================== in-flight cap


async def test_a_third_concurrent_request_waits_its_turn_without_using_an_attempt(h):
    h.provider.latency = 1.0  # the first request is still fetching when the third arrives
    rids = [await h.ingest(from_ip(make_email(REQUEST, from_addr=f"alice+{i}@example.com"), f"198.51.100.{i}"))
            for i in range(1, 4)]
    results = await asyncio.gather(*(h.process(r) for r in rids), return_exceptions=True)

    parked = [r for r, res in zip(rids, results, strict=True) if isinstance(res, worker.Park)]
    assert len(parked) == 1 and sum(res is None for res in results) == 2
    (waiting,) = parked
    row = await h.request(waiting)
    assert row["state"] == "accepted" and row["attempts"] == 0
    assert row["progress"]["step"] == "Waiting for your earlier requests to finish"
    assert {"key": "inflight", "action": "deferred"} in [e["data"] for e in await store.events(waiting)]

    await h.run_job(waiting)  # the others are done: its turn
    assert (await h.request(waiting))["state"] == "done" and len(h.acks(waiting)) == 1


async def test_a_request_that_waits_past_its_deadline_gets_one_apology(h):
    busy = [await h.ingest(from_ip(make_email(REQUEST, from_addr=f"alice+{i}@example.com"), f"198.51.100.{i}"))
            for i in (1, 2)]
    await db.pool().execute("UPDATE requests SET state = 'fetching' WHERE id = ANY($1::uuid[])", busy)
    late = await h.ingest(from_ip(make_email(REQUEST, from_addr="alice+3@example.com"), "198.51.100.3"))
    await db.pool().execute("UPDATE requests SET received_at = now() - interval '3 hours' WHERE id = $1", late)

    await h.run_job(late)

    row = await h.request(late)
    assert row["state"] == "failed" and row["result"]["failure"] == "limit"
    assert "waited too long for its turn" in body_text(h.reply(late))


# ======================================================================== daily LLM budget


async def test_a_spent_llm_budget_means_rules_only_and_no_summaries(h, monkeypatch):
    h.configure(llm_daily_budget_usd=0.01)
    monkeypatch.setattr(harness, "_META", {**harness._META, "cost": 0.006})
    with structlog.testing.capture_logs() as logs:
        first = await served(h)  # summary + entailment check: $0.012, over the budget
        assert SUMMARY_TEXT in body_text(h.reply(first))
        assert await limits.Budgets(h.redis).llm_spent() == pytest.approx(0.012)

        h.llm.parse = llm_parse(matter=MATTER, doc_type="Exhibits")  # never asked: the budget is spent
        vague = await served(h, "Could you send me stuff for M12205?", ip="198.51.100.2")
        exhibits = await served(h, EXHIBITS, ip="198.51.100.3")

    assert h.llm.calls["_LLMParse"] == 0 and h.llm.calls["_SummaryOut"] == 1
    assert (await h.request(vague))["state"] == "clarify"  # the rules' cautious answer: a question
    reply = body_text(h.reply(exhibits))
    assert (await h.request(exhibits))["result"]["files"] == 2 and "Summary" not in reply
    assert "summary_skipped" in await h.event_kinds(exhibits)
    alerts = [e for e in logs if e["event"] == "budget.exhausted"]
    assert len(alerts) == 1 and alerts[0]["log_level"] == "error" and alerts[0]["budget"] == "llm_usd"
    exhausted = await db.pool().fetch("SELECT data FROM events WHERE kind = 'budget_exhausted'")
    assert [r["data"]["budget"] for r in exhausted] == ["llm_usd"]


# ======================================================================== daily portal visits


async def test_a_used_up_portal_budget_parks_with_one_delay_notice_then_apologises(h):
    h.configure(portal_daily_visits={"uarb": 2})
    first = await served(h)  # a listing and a download batch: both visits
    assert (await h.request(first))["state"] == "done"

    rid = await h.ingest(from_ip(make_email(EXHIBITS), "198.51.100.2"))
    defers = await h.run_job(rid)
    assert len(defers) == 1 and defers[0] > 60  # parked: it looks again later
    (notice,) = notice_emails(h, rid)
    assert "today's limit on how often I query" in body_text(notice) and notice["To"] == "alice@example.com"
    assert (await h.request(rid))["attempts"] == 0

    await h.run_job(rid)  # still over: parked again, no second notice
    assert len(notice_emails(h, rid)) == 1 and h.provider.calls["list"] == 1

    await db.pool().execute("UPDATE requests SET received_at = now() - interval '3 hours' WHERE id = $1", rid)
    await h.run_job(rid)
    row = await h.request(rid)
    assert row["state"] == "failed" and row["result"]["failure"] == "limit"
    assert "send the request again after midnight UTC" in body_text(h.reply(rid))
    assert [m["Message-ID"].split(".")[0] for m in h.smtp.sent if str(rid) in m["Message-ID"]] == [
        "<ack", "<notice", "<reply"]


async def test_the_delay_notice_is_never_sent_to_an_unverified_sender(h):
    h.configure(portal_daily_visits={"uarb": 0}, sender_auth_mode="off")
    h.auth.verdict = "fail"
    rid = await h.ingest(make_email(EXHIBITS))
    await h.run_job(rid)
    assert (await h.request(rid))["progress"]["step"].startswith("Delayed: today's limit")
    assert notice_emails(h, rid) == [] and h.provider.visits() == 0
    assert "waiting" in await h.event_kinds(rid)


# ======================================================================== daily bytes per sender


async def test_the_daily_byte_allowance_delivers_what_fits_and_says_so(h):
    sizes = [len(h.provider.pdf(doc_id(MATTER, "Other Documents", i))) for i in (1, 2, 3)]
    h.configure(bytes_per_sender_day=sizes[0] + sizes[1])
    first = await served(h)

    row = await h.request(first)
    assert row["state"] == "done" and row["result"]["files"] == 2 and len(row["result"]["skipped"]) == 1
    assert "go over today's download allowance" in body_text(h.reply(first))

    second = await served(h, EXHIBITS, from_addr="Alice+more@example.com", ip="198.51.100.2")
    assert (await h.request(second))["result"]["files"] == 0
    assert "You've reached today's download allowance" in body_text(h.reply(second))
    other = await served(h, EXHIBITS, from_addr="bob@example.com", ip="198.51.100.3")  # someone else's day
    assert (await h.request(other))["result"]["files"] == 2


# ======================================================================== cached listings keep source_type


async def test_source_type_is_stored_and_survives_a_cached_listing(h):
    docs = h.provider.matters[MATTER][1]["Other Documents"]
    docs[:] = [d.model_copy(update={"source_type": f"Decision and Order {i}"}) for i, d in enumerate(docs)]
    rid = await served(h)
    stored = await db.pool().fetch("SELECT source_type FROM documents WHERE doc_type = 'Other Documents' "
                                   "ORDER BY external_id")
    assert [r["source_type"] for r in stored] == [f"Decision and Order {i}" for i in range(3)]

    again = await pipeline._fetch(h.deps, rid, h.provider, MATTER, "Other Documents", 10, budget=10**9)
    assert h.provider.calls["list"] == 1  # served from the cache
    assert [r.source_type for r in again.refs] == [f"Decision and Order {i}" for i in range(3)]


# ======================================================================== web token buckets


@pytest.fixture
def web_client(h):
    web.app.state.rate_limiter = WebLimiter(h.redis)

    def client(ip: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=PUBLIC_BASE_URL,
                                 headers={"X-Real-IP": ip})

    yield client
    web.app.state.rate_limiter = None


async def test_progress_polls_are_limited_per_client_ip(h, web_client):
    rid = await served(h)
    token = (await h.request(rid))["track_token"]
    async with web_client("198.51.100.20") as c, web_client("198.51.100.21") as other:
        started = time.monotonic()
        codes = [(await c.get(f"/r/{token}.json")).status_code for _ in range(60)]
        limited = await c.get(f"/r/{token}.json")
        assert codes == [200] * 60 and time.monotonic() - started < 1  # the burst, before a token refills
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
        assert limited.headers["cache-control"] == "no-store"
        assert (await other.get(f"/r/{token}.json")).status_code == 200  # its own bucket
        assert (await c.get("/health")).status_code == 200  # exempt
        assert (await c.get(f"/r/{token}")).status_code == 200  # another route class
    assert not any("198.51.100" in k for k in await redis_keys(h))


async def test_file_downloads_have_a_per_minute_bucket(h, web_client):
    async with web_client("198.51.100.30") as c:
        started = time.monotonic()
        codes = [(await c.get("/files/00000000-0000-0000-0000-000000000000.pdf")).status_code for _ in range(31)]
    assert codes == [404] * 30 + [429] and time.monotonic() - started < 2  # 0.5 tokens/s refill


async def test_the_web_keeps_serving_when_redis_is_down(h):
    dead = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    web.app.state.rate_limiter = WebLimiter(dead)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=PUBLIC_BASE_URL) as c:
            assert [(await c.get("/privacy")).status_code for _ in range(3)] == [200, 200, 200]
    finally:
        web.app.state.rate_limiter = None
        await dead.aclose()
