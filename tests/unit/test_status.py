"""The public /status page: aggregates only, computed once a minute, honest when Postgres or Redis
is down, and readable by the web role's grants (deploy/sql/grants.sql)."""

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent import health
from agent.web import app as web
from agent.web import status

REPO = Path(__file__).resolve().parents[2]
DISCLAIMER = "MVP for evaluation. Aggregate numbers only; no personal data. Not a service-level agreement."


def _window(label: str, **counts) -> dict:
    base = {"received": 0, "done": 0, "clarify": 0, "rejected": 0, "failed": 0, "replies": 0, "within_target": 0,
            "p50_s": None, "p95_s": None}
    w = {"label": label, **base, **counts}
    w["open"] = w["received"] - w["done"] - w["clarify"] - w["rejected"] - w["failed"]
    return w


class Fake:
    def __init__(self) -> None:
        self.calls = 0
        self.db_error: Exception | None = None
        self.redis = {"ok": True, "ingest_heartbeat_age_s": 12.0, "worker_reporting": True, "breakers_open": []}

    async def window(self, label, span):
        self.calls += 1
        if self.db_error:
            raise self.db_error
        if span.days == 7:
            return _window(label, received=40, done=30, clarify=3, rejected=4, failed=2, replies=35, within_target=33,
                           p50_s=48.2, p95_s=150.0)
        return _window(label, received=10, done=8, rejected=1, failed=0, replies=8, within_target=7, p50_s=40.0,
                       p95_s=200.0)

    async def volume(self):
        return [{"provider": "uarb", "day": 6, "week": 25}, {"provider": "oeb", "day": 2, "week": 9},
                {"provider": None, "day": 2, "week": 6}]

    async def citations(self):
        return {"kept": 46, "dropped": 4}

    async def jev(self):
        return {"gate": {"calls": 20, "escalated": 3}, "support_check": {"calls": 10, "escalated": 0}}

    async def component_facts(self, s, dependencies):
        assert list(dependencies) == ["uarb", "oeb", "ferc"]
        return self.redis


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    f = Fake()
    monkeypatch.setattr(status, "_window", f.window)
    monkeypatch.setattr(status, "_volume", f.volume)
    monkeypatch.setattr(status, "_citations", f.citations)
    monkeypatch.setattr(status, "_jev", f.jev)
    monkeypatch.setattr(health, "component_facts", f.component_facts)
    status.clear_cache()
    yield f
    status.clear_cache()


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_the_page_shows_aggregates_with_the_disclaimer_first(fake):
    r = TestClient(web.app).get("/status")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"
    html, text = r.text, _text(r.text)
    assert DISCLAIMER in text
    assert html.index("MVP for evaluation") < html.index("<h1>")  # at the top, before anything else
    assert "single Canadian egress server" in text
    assert "Answered 8 30" in text and "Failed 0 2" in text and "Still in progress 1 1" in text
    assert "Success rate (answered of answered + failed) 100.0 % 93.8 %" in text
    assert "Median (p50) 40 s 48 s" in text and "95th percentile (p95) 3 min 20 s 2 min 30 s" in text
    assert "Within 3 min 87.5 % 94.3 %" in text
    assert "p95 against the 3 min target Over Within" in text
    assert "Nova Scotia Utility and Review Board 6 25" in text and "No regulator identified 2 6" in text
    assert "Federal Energy Regulatory Commission (US) 0 0" in text
    assert "Citation support rate 46 of 50" in text and "92.0 %" in text
    assert "Jev escalation rate (request gate)" in text and "15.0 %" in text
    assert "Email intake Operational Mailbox checked 12 s ago." in text
    assert "Request worker Operational" in text and "Database Operational" in text


def test_the_page_follows_the_sites_rules(fake):
    r = TestClient(web.app).get("/status")
    assert r.headers["content-security-policy"] == web.CSP and r.headers["x-frame-options"] == "DENY"
    assert "<script" not in r.text  # nothing to run, from anywhere
    assert "@" not in r.text  # no addresses (not even ours)
    assert 'href="/status"' in TestClient(web.app).get("/privacy").text  # linked from every footer
    assert TestClient(web.app).head("/status").status_code == 200


def test_numbers_are_computed_at_most_once_a_minute(fake, monkeypatch):
    client = TestClient(web.app)
    client.get("/status")
    client.get("/status")
    assert fake.calls == 2  # one computation: two windows
    monkeypatch.setattr(status, "CACHE_S", 0.0)
    client.get("/status")
    assert fake.calls == 4


async def test_concurrent_requests_share_one_computation(fake):
    from agent.config import get_settings

    results = await asyncio.gather(*(status.snapshot(get_settings()) for _ in range(5)))
    assert fake.calls == 2 and all(r is results[0] for r in results)


def test_without_postgres_the_page_still_renders_and_says_so(fake):
    fake.db_error = OSError("connection refused")
    r = TestClient(web.app).get("/status")
    text = _text(r.text)
    assert r.status_code == 200 and "Database Down" in text
    assert "The request numbers are not available right now." in text and "Success rate" not in text


def test_without_redis_components_are_unknown(fake):
    fake.redis = {"ok": False}
    text = _text(TestClient(web.app).get("/status").text)
    assert "Email intake Unknown" in text and "Request worker Unknown" in text
    assert "Nova Scotia Utility and Review Board portal Unknown" in text
    assert "Answered 8 30" in text  # the numbers don't depend on Redis


def test_a_stale_heartbeat_a_missing_worker_and_an_open_breaker_are_down(fake):
    fake.redis = {"ok": True, "ingest_heartbeat_age_s": 900.0, "worker_reporting": False, "breakers_open": ["uarb"]}
    text = _text(TestClient(web.app).get("/status").text)
    assert "Email intake Down Mailbox checked 15 min ago." in text
    assert "Request worker Down Not reporting" in text
    assert "Nova Scotia Utility and Review Board portal Down Failing repeatedly" in text
    assert "Ontario Energy Board portal Operational" in text
    fake.redis = {"ok": True, "ingest_heartbeat_age_s": None, "worker_reporting": True, "breakers_open": []}
    status.clear_cache()
    assert "Email intake Down No heartbeat" in _text(TestClient(web.app).get("/status").text)


def test_empty_windows_show_dashes_not_zero_rates(fake, monkeypatch):
    async def empty(label, span):
        return _window(label)

    monkeypatch.setattr(status, "_window", empty)
    text = _text(TestClient(web.app).get("/status").text)
    assert "Success rate (answered of answered + failed) – –" in text and "Median (p50) – –" in text


@pytest.mark.parametrize(("seconds", "shown"), [
    (None, "–"), (0.4, "0 s"), (45, "45 s"), (60, "1 min"), (130, "2 min 10 s"), (3600, "1 h"), (3725, "1 h 2 min"),
])
def test_durations(seconds, shown):
    assert status.duration(seconds) == shown


def test_percentages():
    assert (status.pct(1, 0), status.pct(0, 4), status.pct(1, 3)) == ("–", "0.0 %", "33.3 %")


def test_the_page_reads_only_columns_the_web_role_is_granted():
    grants = (REPO / "deploy" / "sql" / "grants.sql").read_text()
    m = re.search(r"'GRANT SELECT \((.*?)\) ON requests TO agent_web'", grants, re.DOTALL)
    assert m
    granted = {c.strip() for c in re.sub(r"'\s*'", "", m.group(1)).split(",")}
    assert set(status.STATUS_COLUMNS) <= granted
    sql = f"{status._WINDOW_SQL} {status._VOLUME_SQL}"
    used = set(re.findall(r"\b(state|provider|received_at|reply_sent_at|from_addr|subject|matter|track_token)\b", sql))
    assert used == set(status.STATUS_COLUMNS)  # and nothing that names a person or a matter


class FakeRedis:
    def __init__(self, values: dict, *, fail: bool = False):
        self.values, self.fail, self.closed = values, fail, False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("down")
        return self.values.get(key)

    async def exists(self, key):
        return int(key in self.values)

    async def hget(self, key, field):
        return self.values.get(f"{key}/{field}")

    async def aclose(self):
        self.closed = True


async def test_component_facts_from_redis(monkeypatch):
    now = time.time()
    fake = FakeRedis({health.INGEST_HEARTBEAT_KEY: f"{now - 30:.0f}", health.WORKER_HEALTH_KEY: b"j_complete=1",
                      "breaker:uarb/open_until": str(now + 60), "breaker:oeb/open_until": str(now - 60)})
    monkeypatch.setattr(health, "_redis", lambda s: fake)
    facts = await health.component_facts(None, ["uarb", "oeb", "ferc"])
    assert facts["ok"] and facts["worker_reporting"] and facts["breakers_open"] == ["uarb"]
    assert 25 <= facts["ingest_heartbeat_age_s"] <= 35 and fake.closed

    down = FakeRedis({}, fail=True)
    monkeypatch.setattr(health, "_redis", lambda s: down)
    assert await health.component_facts(None, ["uarb"]) == {"ok": False} and down.closed


def test_view_carries_no_identifiers(fake):
    from agent.config import get_settings

    facts = asyncio.run(status.compute(get_settings()))
    assert isinstance(facts["computed_at"], datetime) and facts["computed_at"].tzinfo is UTC
    rendered = repr(status.view(facts))
    assert "@" not in rendered and not re.search(r"M\d{5}|EB-\d{4}-\d{4}", rendered)
