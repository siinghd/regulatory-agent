"""Abuse limits without Redis: normalised, pseudonymous keys; the pre-auth helpers; the web
limiter's routing, client address and 429s (with a fake limiter); IDN-consistent audit hashes.

The Redis-backed behaviour (windows, buckets, budgets, the pipeline) is in
tests/integration/test_limits.py."""

import json
from datetime import UTC, datetime
from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from agent import audit, limits
from agent.config import Settings, get_settings
from agent.mail.ingest import header_block
from agent.web import app as web
from agent.web import ratelimit
from agent.web.ratelimit import Bucket, WebLimiter, client_ip, route_class


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUDIT_HMAC_KEY", "test-audit-key")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


# ---------------------------------------------------------------- normalised senders and domains


@pytest.mark.parametrize("addr", [
    "alice@gmail.com", "Alice@Gmail.com", "alice+news@gmail.com", "a.l.i.c.e@gmail.com",
    "A.Lice+x+y@GMAIL.COM", "alice@googlemail.com", "al.ice+1@googlemail.com", " alice@gmail.com ",
])
def test_gmail_variants_are_one_sender(addr):
    assert limits.normalise_sender(addr) == "alice@gmail.com"


def test_dots_matter_outside_gmail_and_tags_never_do():
    assert limits.normalise_sender("first.last+tag@Example.com") == "first.last@example.com"
    assert limits.normalise_sender("firstlast@example.com") != limits.normalise_sender("first.last@example.com")


def test_idn_domains_become_a_labels():
    assert limits.normalise_sender("Alice+x@BÜCHER.example") == "alice@xn--bcher-kva.example"
    assert limits.normalise_sender("alice@xn--bcher-kva.example") == "alice@xn--bcher-kva.example"


def test_edge_addresses_are_kept_whole():
    assert limits.normalise_sender("+only@example.com") == "+only@example.com"
    assert limits.normalise_sender("no-at-sign") == "no-at-sign"
    assert limits.normalise_sender("x@not a domain") == "x@not a domain"  # lowercased, not an A-label


@pytest.mark.parametrize(("value", "org"), [
    ("alice@mail.example.com", "example.com"),
    ("example.com", "example.com"),
    ("bob@a.b.example.co.uk", "example.co.uk"),
    ("carol@Mail.BÜCHER.example", "xn--bcher-kva.example"),
    ("dave@tenant.onmicrosoft.com", "tenant.onmicrosoft.com"),  # shared tenant suffix: its own org
])
def test_domains_are_keyed_by_organizational_domain(value, org):
    assert limits.org_domain(value) == org


@pytest.mark.parametrize(("ip", "bucket"), [
    ("203.0.113.7", "203.0.113.7"),
    ("2001:db8:1:2:3:4:5:6", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff::1", "2001:db8:1:2::/64"),
    ("::ffff:203.0.113.7", "203.0.113.7"),
])
def test_ipv6_clients_share_a_bucket_per_64(ip, bucket):
    assert limits.ip_bucket(ip) == bucket


def test_keys_are_hmacs_that_never_contain_the_address(settings):
    keys = {limits.sender_key(a) for a in ("Alice+1@Gmail.com", "a.l.i.c.e@googlemail.com")}
    assert len(keys) == 1
    (key,) = keys
    assert len(key) == 32 and "alice" not in key and "gmail" not in key
    assert key != audit.subject_hash("alice@gmail.com")[:32]  # never equal to an audit subject id
    assert limits.domain_key("bob@mail.example.com") == limits.domain_key("example.com")
    assert limits.ip_key("2001:db8::1") == limits.ip_key("2001:db8::2")
    assert limits.ip_key("203.0.113.7") != limits.ip_key("203.0.113.8")


def test_utc_day_boundaries():
    now = datetime(2026, 10, 4, 23, 59, 30, tzinfo=UTC)
    assert limits.utc_day(now) == "2026-10-04"
    assert limits.seconds_to_utc_midnight(now) == 30


def test_default_limits():
    s = Settings(_env_file=None)
    assert (s.rate_per_sender_hour, s.rate_per_sender_day, s.rate_per_domain_day, s.rate_global_day) == (6, 20, 100, 1000)
    assert (s.preauth_per_ip_hour, s.preauth_per_domain_hour, s.inbound_per_minute) == (30, 60, 120)
    assert s.max_inflight_per_sender == 2 and s.rate_notices_per_day == 3
    assert s.llm_daily_budget_usd == 2.0 and s.bytes_per_sender_day == 1_500_000_000
    assert s.portal_daily_visits == {"uarb": 400, "oeb": 2000, "ferc": 2000}


# ---------------------------------------------------------------- IDN senders hash alike everywhere


def test_u_label_and_a_label_addresses_hash_alike(settings):
    assert audit.normalise_address("Alice@BÜCHER.example") == "alice@xn--bcher-kva.example"
    assert audit.normalise_address("@Bücher.example") == "@xn--bcher-kva.example"
    assert audit.subject_hash("alice@bücher.example") == audit.subject_hash("alice@xn--bcher-kva.example")
    assert audit.subject_hash("ALICE@Example.com") == audit.subject_hash("alice@example.com")


def test_an_idn_senders_to_h_matches_their_from_h(settings):
    msg = EmailMessage()
    msg["To"] = "alice@xn--bcher-kva.example"  # replies go to the A-label
    msg.set_content("hi")
    data = audit.outbound_data("<reply.1@hsingh.app>", "reply", msg, size=10, code=250)
    assert data["to_h"] == audit.subject_hash("alice@bücher.example")  # what ingest recorded as from_h
    assert "alice" not in json.dumps(data)


# ---------------------------------------------------------------- ingest helpers


@pytest.mark.parametrize(("raw", "head"), [
    (b"From: a@b.example\r\nSubject: x\r\n\r\nbody\r\n\r\nmore", b"From: a@b.example\r\nSubject: x\r\n\r\n"),
    (b"From: a@b.example\nSubject: x\n\nbody", b"From: a@b.example\nSubject: x\n\n"),
    (b"From: a@b.example\r\n", b"From: a@b.example\r\n"),
])
def test_header_block_keeps_only_the_headers(raw, head):
    assert header_block(raw) == head


# ---------------------------------------------------------------- web: routes, client address


@pytest.mark.parametrize(("path", "name", "buckets"), [
    ("/r/abcdefghijkl.json", "progress_json", (Bucket(60, 60),)),
    ("/r/abcdefghijkl", "progress", (Bucket(30, 60),)),
    ("/files/0d6e/abc.pdf", "files", (Bucket(30, 60), Bucket(200, 3600))),
    ("/c/AbCdEfGh12", "citation", (Bucket(60, 60),)),
    ("/privacy", "default", (Bucket(120, 60),)),
    ("/static/viewer.css", "default", (Bucket(120, 60),)),
])
def test_route_classes(path, name, buckets):
    assert route_class(path, Settings(_env_file=None)) == (name, buckets)


@pytest.mark.parametrize("path", ["/health", "/health/deep"])
def test_health_is_exempt(path):
    assert route_class(path, Settings(_env_file=None)) is None


def _request(peer: str | None, real: str | None = None):
    headers = {"x-real-ip": real} if real else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer) if peer else None, headers=headers)


@pytest.mark.parametrize(("peer", "real", "ip"), [
    ("127.0.0.1", "198.51.100.9", "198.51.100.9"),  # Caddy on loopback
    ("198.51.100.9", "198.51.100.9", "198.51.100.9"),  # uvicorn already resolved X-Forwarded-For
    ("203.0.113.5", "198.51.100.9", "203.0.113.5"),  # a direct client can't pick its own bucket
    ("127.0.0.1", "not-an-ip", "127.0.0.1"),
    ("127.0.0.1", None, "127.0.0.1"),  # on the host, not behind Caddy
    (None, None, "unknown"),
])
def test_client_ip_trusts_x_real_ip_only_from_caddy(peer, real, ip):
    assert client_ip(_request(peer, real)) == ip


# ---------------------------------------------------------------- web: middleware with a fake limiter


class FakeLimiter:
    def __init__(self, wait: float | None = None):
        self.wait = wait
        self.calls: list[tuple[str, str]] = []

    async def check(self, name, buckets, ip):
        self.calls.append((name, ip))
        return self.wait


@pytest.fixture
def limited_client():
    def make(wait):
        web.app.state.rate_limiter = FakeLimiter(wait)
        return TestClient(web.app), web.app.state.rate_limiter

    yield make
    web.app.state.rate_limiter = None


def test_a_limited_json_poll_is_429_with_retry_after_and_no_store(limited_client):
    client, limiter = limited_client(7)
    r = client.get("/r/abcdefghijkl.json", headers={"X-Real-IP": "198.51.100.9"})
    assert r.status_code == 429 and r.json() == {"error": "rate limited"}
    assert r.headers["retry-after"] == "7" and r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    # TestClient's peer ("testclient") is neither loopback nor an address: X-Real-IP isn't trusted
    assert limiter.calls == [("progress_json", "unknown")]


def test_a_limited_page_is_a_429_page_with_security_headers(limited_client):
    client, _ = limited_client(3)
    r = client.get("/c/AbCdEfGh12")
    assert r.status_code == 429 and "Too many requests" in r.text
    assert r.headers["retry-after"] == "3" and "default-src 'none'" in r.headers["content-security-policy"]


def test_health_is_never_limited(limited_client, monkeypatch):
    client, limiter = limited_client(60)

    async def ok() -> bool:
        return True

    monkeypatch.setattr(web, "db_ok", ok)
    assert client.get("/health").status_code == 200
    assert limiter.calls == []


def test_without_a_limiter_every_request_goes_ahead():
    web.app.state.rate_limiter = None
    assert TestClient(web.app).get("/c/AbCdEfGh12!").status_code == 404  # reached the router


class _DeadScript:
    def __init__(self):
        self.calls = 0

    async def __call__(self, **_):
        self.calls += 1
        raise RedisConnectionError("Error 111 connecting to 127.0.0.1:6392. Connection refused.")


async def test_redis_down_fails_open_and_backs_off(monkeypatch):
    limiter = WebLimiter(None)
    limiter._script = dead = _DeadScript()
    assert await limiter.check("progress", (Bucket(30, 60),), "198.51.100.9") is None
    assert await limiter.check("progress", (Bucket(30, 60),), "198.51.100.9") is None
    assert dead.calls == 1  # Redis left alone for RETRY_REDIS_AFTER_S after a failure
    monkeypatch.setattr(ratelimit, "RETRY_REDIS_AFTER_S", 0.0)
    limiter._down_until = 0.0
    assert await limiter.check("progress", (Bucket(30, 60),), "198.51.100.9") is None
    assert dead.calls == 2


def test_web_client_keys_are_hmacs_under_a_per_process_key():
    a, b = WebLimiter(None), WebLimiter(None)
    assert a._client_key("198.51.100.9") != b._client_key("198.51.100.9")
    assert "198" not in a._client_key("198.51.100.9")
    assert a._client_key("2001:db8::1") == a._client_key("2001:db8::ffff")
