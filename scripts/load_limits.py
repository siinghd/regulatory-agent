"""Fire bursts at the abuse limits and print what was allowed and what was limited.

  .venv/bin/python scripts/load_limits.py
      Against the integration harness: the real pipeline, ingest and web app on the compose
      Postgres and Redis (credentials from .env), with the portal, SMTP, DNS and LLM faked. It
      creates a throwaway database (dropped at the end) and uses Redis db 13 (flushed before and
      after). No email is sent; no portal or LLM is reached. Limits are the defaults.

  .venv/bin/python scripts/load_limits.py --web-url http://127.0.0.1:8710 --token <track token>
      Progress-page polls against a running web process (what Caddy would forward: X-Real-IP).

Exits 1 if any burst got through further than its limit allows.
"""

import argparse
import asyncio
import secrets
import sys
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import asyncpg
import httpx
import pytest

import agent.citations.claims
import agent.citations.ground
import agent.llm
import agent.mail.auth
import agent.mail.outbound
from agent import db, limits, queue, worker
from agent.config import Settings, get_settings
from agent.mail.ingest import Ingestor
from agent.providers import base as providers_base
from agent.web import app as web
from agent.web.ratelimit import WebLimiter
from tests.integration.harness import (
    AGENT_ADDRESS,
    DEFAULT_COUNTS,
    MATTER,
    PUBLIC_BASE_URL,
    FakeAuth,
    FakeLLM,
    FakeProvider,
    FakeSMTP,
    Harness,
    make_email,
)
from tests.integration.test_limits import FakeMailbox, from_ip

REDIS_DB = 13
REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks"
rows: list[tuple[str, int, int, int, str, bool]] = []


def report(burst: str, sent: int, allowed: int, limited: int, note: str, holds: bool) -> None:
    rows.append((burst, sent, allowed, limited, note, holds))
    print(f"  {'ok  ' if holds else 'FAIL'} {burst:<52} {sent:>5} {allowed:>8} {limited:>8}   {note}", flush=True)


# ---------------------------------------------------------------- harness (as tests/integration/conftest.py)


@asynccontextmanager
async def harness():
    service = Settings(_env_file=REPO / ".env")
    base = urlsplit(service.database_url)
    name = f"agent_load_{secrets.token_hex(4)}"
    admin_url = urlunsplit(base._replace(path="/postgres"))
    redis_url = urlunsplit(urlsplit(service.redis_url)._replace(path=f"/{REDIS_DB}"))

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    await admin(f'CREATE DATABASE "{name}"')
    data_dir = Path(f"/tmp/{name}")
    try:
        with pytest.MonkeyPatch.context() as mp:
            env = {"DATABASE_URL": urlunsplit(base._replace(path=f"/{name}")), "REDIS_URL": redis_url,
                   "DATA_DIR": str(data_dir), "AGENT_MAIL_ADDRESS": AGENT_ADDRESS, "SENDER_AUTH_MODE": "dmarc",
                   "PUBLIC_BASE_URL": PUBLIC_BASE_URL, "SMTP_HOST": "127.0.0.1", "SMTP_PORT": "9",
                   "OPENROUTER_API_KEY": "not-used", "BREAKER_FAILURES": "1000", "DISK_MIN_FREE_BYTES": "1000000"}
            for k, v in env.items():
                mp.setenv(k, v)
            get_settings.cache_clear()
            mp.setattr(db, "_pool", None)
            pool = await db.create_pool(max_size=10)
            async with pool.acquire() as conn:
                await db.migrate(conn)
            redis = await queue.create_pool(redis_url)
            await redis.flushdb()
            limits.install(limits.Budgets(redis))
            provider = FakeProvider()
            provider.add_matter(MATTER, DEFAULT_COUNTS)
            mp.setattr(providers_base, "_REGISTRY", {"uarb": lambda: provider})
            llm = FakeLLM()
            for module in (agent.llm, agent.citations.claims, agent.citations.ground):
                mp.setattr(module, "structured", llm.structured)
            auth, smtp = FakeAuth(), FakeSMTP()
            mp.setattr(agent.mail.auth, "verify_sender", auth.verify)
            mp.setattr(agent.mail.outbound, "send", smtp.send)
            try:
                yield Harness(redis=redis, provider=provider, llm=llm, auth=auth, smtp=smtp, monkeypatch=mp)
            finally:
                limits.install(None)
                await redis.flushdb()
                await redis.aclose()
                await db.close_pool()
                get_settings.cache_clear()
    finally:
        await admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        for p in sorted(data_dir.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        if data_dir.exists():
            data_dir.rmdir()


# ---------------------------------------------------------------- bursts


async def one_sender_many_spellings(h) -> str:
    s = get_settings()
    spellings = [f"{'.'.join('alice') if i % 2 else 'Alice'}+{i}@{'googlemail' if i % 3 == 0 else 'gmail'}.com"
                 for i in range(50)]
    rids = []
    for i, addr in enumerate(spellings):
        rid = await h.ingest(from_ip(make_email(REQUEST, from_addr=addr), f"198.51.100.{i + 1}"))
        await h.run_job(rid)
        rids.append(rid)
    states = Counter([(await h.request(r))["reject_reason"] or (await h.request(r))["state"] for r in rids])
    notices = sum("You've sent a lot of requests" in str(m.get_body(("plain",)).get_content()) for m in h.smtp.sent)
    report("50 emails, one sender: +tags, dots, googlemail, 50 IPs", 50, states["done"],
           states["rate_limited:sender"], f"{notices} slow-down reply; sender cap {s.rate_per_sender_hour}/h",
           states["done"] == s.rate_per_sender_hour and notices == 1)
    return (await h.request(rids[0]))["track_token"]


async def one_ip_many_senders(h) -> None:
    s = get_settings()
    box = FakeMailbox([from_ip(make_email(REQUEST, from_addr=f"user{i}@org{i}.example"), "192.0.2.66")
                       for i in range(200)])
    ing = Ingestor(s, h.redis)
    ing.uidvalidity = 1
    before = len(await h.queued_request_ids())
    await ing._sweep(box)
    enqueued = len(await h.queued_request_ids()) - before
    preauth = await db.pool().fetchval("SELECT count(*) FROM requests WHERE reject_reason = 'preauth_rate_limited'")
    deferred = len(box.messages) - len(box.seen)
    report("200 emails, one client IP, 200 domains (one IMAP sweep)", 200, enqueued, preauth,
           f"{deferred} left unseen (ceiling {s.inbound_per_minute}/min); IP cap {s.preauth_per_ip_hour}/h, "
           "rejected ones stored as headers only",
           enqueued == s.preauth_per_ip_hour and enqueued + preauth == s.inbound_per_minute
           and deferred == 200 - s.inbound_per_minute)


async def concurrent_from_one_sender(h) -> None:
    s = get_settings()
    h.provider.latency = 0.3
    rids = [await h.ingest(from_ip(make_email(REQUEST, from_addr=f"bob+{i}@example.net"), f"198.51.100.{100 + i}"))
            for i in range(5)]
    sender = limits.sender_key("bob@example.net")
    peak = 0

    async def watch() -> None:  # the sender's requests fetching or packaging at once, sampled every 10 ms
        nonlocal peak
        while True:
            n = await db.pool().fetchval("SELECT count(*) FROM requests WHERE sender_h = $1 "
                                         "AND state IN ('fetching', 'packaging')", sender)
            peak = max(peak, n)
            await asyncio.sleep(0.01)

    watcher = asyncio.create_task(watch())
    results = await asyncio.gather(*(h.process(r) for r in rids), return_exceptions=True)
    waited = sum(isinstance(r, worker.Park) for r in results)
    h.provider.latency = 0.0
    for r in rids:
        await h.run_job(r)
    watcher.cancel()
    rows_ = [await h.request(r) for r in rids]
    done = [r["state"] for r in rows_].count("done")
    report("5 concurrent requests, one sender", 5, 5 - waited, waited,
           f"peak {peak} in flight (cap {s.max_inflight_per_sender}); waiting ones used no attempt; {done} done",
           peak == s.max_inflight_per_sender and waited >= 1 and done == 5)


async def polls(client_for, path: str, n: int, ips: list[str]) -> tuple[Counter, float]:
    """(status codes, seconds the burst took), round-robin over `ips`."""
    codes: Counter = Counter()
    clients = [client_for(ip) for ip in ips]
    started = time.monotonic()
    try:
        for i in range(n):
            codes[(await clients[i % len(clients)].get(path)).status_code] += 1
    finally:
        for c in clients:
            await c.aclose()
    return codes, time.monotonic() - started


def bucket_holds(allowed: int, per_ip: int, window_s: int, took: float, ips: int = 1) -> bool:
    """A token bucket allows its burst, plus what refills while the burst runs, and no more."""
    return ips * per_ip <= allowed <= ips * (per_ip + int(took * per_ip / window_s) + 1)


async def web_bursts(h, token: str) -> None:
    s = get_settings()
    web.app.state.rate_limiter = WebLimiter(h.redis)

    def client_for(ip: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=PUBLIC_BASE_URL,
                                 headers={"X-Real-IP": ip})

    try:
        per = s.web_rate_progress_json_per_min
        one, took = await polls(client_for, f"/r/{token}.json", 1000, ["203.0.113.40"])
        report("1,000 progress polls, one IP", 1000, one[200], one[429],
               f"bucket {per}/min: burst {per} + refill over {took:.1f}s", bucket_holds(one[200], per, 60, took))
        ten, took = await polls(client_for, f"/r/{token}.json", 1000, [f"203.0.113.{50 + i}" for i in range(10)])
        report("1,000 progress polls, 10 IPs", 1000, ten[200], ten[429],
               f"a bucket per IP: 10 x {per} + refill over {took:.1f}s", bucket_holds(ten[200], per, 60, took, 10))
        files, took = await polls(client_for, "/files/00000000-0000-0000-0000-000000000000.pdf", 250, ["203.0.113.70"])
        report("250 file downloads, one IP", 250, files[404], files[429],
               f"{s.web_rate_files_per_min}/min and {s.web_rate_files_per_hour}/h (an unknown file: 404 when allowed)",
               bucket_holds(files[404], s.web_rate_files_per_min, 60, took))
        health, _ = await polls(client_for, "/health", 300, ["203.0.113.80"])
        report("300 health checks, one IP", 300, health[200], health[429], "exempt", health[200] == 300)
    finally:
        web.app.state.rate_limiter = None


async def run_harness() -> int:
    print("Bursts against the integration harness (default limits):\n")
    print(f"       {'burst':<52} {'sent':>5} {'allowed':>8} {'limited':>8}   notes")
    async with harness() as h:
        token = await one_sender_many_spellings(h)
        await one_ip_many_senders(h)
        await concurrent_from_one_sender(h)
        await web_bursts(h, token)
    return 0 if all(r[5] for r in rows) else 1


async def run_web(url: str, token: str, n: int) -> int:
    async with httpx.AsyncClient(base_url=url, headers={"X-Real-IP": "203.0.113.99"}, timeout=10) as c:
        codes = Counter([(await c.get(f"/r/{token}.json")).status_code for _ in range(n)])
    limit = get_settings().web_rate_progress_json_per_min
    print(f"{n} polls of /r/<token>.json at {url}: {dict(codes)} (limit {limit}/min)")
    return 0 if codes[200] <= limit + 1 else 1  # +1: a token may refill during the burst


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--web-url", help="poll a running web process instead of using the harness")
    parser.add_argument("--token", help="a progress token (with --web-url)")
    parser.add_argument("--polls", type=int, default=1000)
    args = parser.parse_args()
    if args.web_url:
        if not args.token:
            parser.error("--web-url needs --token")
        return asyncio.run(run_web(args.web_url, args.token, args.polls))
    return asyncio.run(run_harness())


if __name__ == "__main__":
    sys.exit(main())
