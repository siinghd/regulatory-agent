"""Metrics where the facts happen, and the public /status page, against real Postgres and Redis
(fakes for the portal, SMTP, sender verification and the LLM: harness.py). Every label value these
requests record (real-looking addresses, IPs, matters) is also checked at session end
(tests/conftest.py, tests/metrics_privacy.py)."""

import re
import secrets
from pathlib import Path

import asyncpg
import httpx
import pytest
from prometheus_client import REGISTRY

from agent import audit, db
from agent.config import get_settings
from agent.models import PortalUnavailable
from agent.web import app as web
from agent.web import status

from .harness import MATTER, PUBLIC_BASE_URL, make_email

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"
ALICE = "alice@example.com"


def total(name: str, **labels: str) -> float:
    """Sum of every sample of `name` whose labels include `labels`."""
    return sum(
        s.value for family in REGISTRY.collect() for s in family.samples
        if s.name == name and all(s.labels.get(k) == v for k, v in labels.items())
    )


def snapshot(*keys: tuple) -> dict:
    return {k: total(k[0], **dict(k[1:])) for k in keys}


def delta(before: dict) -> dict:
    return {k: total(k[0], **dict(k[1:])) - v for k, v in before.items()}


async def served(h, body: str = REQUEST, **kw):
    rid = await h.ingest(make_email(body, **kw))
    await h.run_job(rid)
    return rid


# ======================================================================== metrics


async def test_a_served_request_is_counted_once_everywhere(h):
    keys = (
        ("request_e2e_seconds_count", ("outcome", "done")),
        ("requests_total", ("final_state", "done")),
        ("provider_requests_total", ("provider", "uarb"), ("state", "done")),
        ("provider_fetch_seconds_count", ("provider", "uarb"), ("outcome", "ok")),
        ("provider_visits_total", ("provider", "uarb")),
        ("gate_decisions_total", ("outcome", "accept"), ("escalated", "false")),
        ("auth_verdicts_total", ("verdict", "pass")),
        ("outbound_messages_total", ("kind", "ack"), ("outcome", "sent")),
        ("outbound_messages_total", ("kind", "reply"), ("outcome", "sent")),
        ("deliveries_total", ("kind", "attachment"), ("outcome", "ok")),
        ("citations_total", ("outcome", "kept")),
    )
    before = snapshot(*keys)
    rid = await served(h)
    assert (await h.request(rid))["state"] == "done"
    d = delta(before)
    fetches = d.pop(keys[3])
    visits = d.pop(keys[4])
    kept = d.pop(keys[10])
    assert set(d.values()) == {1}, d
    assert fetches == 1 + 3 and visits == 2  # calls: the listing and 3 files; visits: it and one download batch
    assert kept == len(await h.citations(rid))
    # the reply went out well inside the objective, and is in the le=180 bucket
    assert total("request_e2e_seconds_bucket", outcome="done", le="180.0") >= 1


async def test_a_portal_failure_is_a_retry_with_a_fixed_cause_and_an_error_fetch(h):
    before = snapshot(("retries_total", ("cause", "portal_unavailable")),
                      ("provider_fetch_seconds_count", ("provider", "uarb"), ("outcome", "error")))
    h.provider.portal_errors.append(PortalUnavailable(f"portal said no to {ALICE} about {MATTER}"))
    rid = await served(h)
    assert (await h.request(rid))["state"] == "done"
    assert set(delta(before).values()) == {1}
    assert total("retries_total", cause=f"portal said no to {ALICE} about {MATTER}") == 0


async def test_an_unverified_sender_is_one_verdict_and_no_gate_decision(h):
    h.auth.verdict = "fail"
    before = snapshot(("auth_verdicts_total", ("verdict", "fail")), ("gate_decisions_total",),
                      ("provider_requests_total", ("provider", "none"), ("state", "rejected")),
                      ("request_e2e_seconds_count",))
    rid = await served(h)
    assert (await h.request(rid))["state"] == "rejected"
    assert delta(before) == {k: v for k, v in zip(before, (1, 0, 1, 0), strict=True)}


async def test_a_dns_temperror_is_one_verdict_however_many_tries(h):
    h.auth.verdict = "temperror"
    before = snapshot(("auth_verdicts_total", ("verdict", "none")), ("retries_total", ("cause", "auth_temperror")))
    rid = await served(h)
    row = await h.request(rid)
    assert row["state"] == "rejected" and row["attempts"] == get_settings().max_attempts
    assert list(delta(before).values()) == [1, get_settings().max_attempts - 1]


async def test_an_ack_overtaken_by_the_reply_is_suppressed(h):
    h.smtp.fail["ack"] = 10
    before = snapshot(("outbound_messages_total", ("kind", "ack"), ("outcome", "suppressed")),
                      ("outbound_messages_total", ("kind", "ack"), ("outcome", "deferred")),
                      ("outbound_messages_total", ("kind", "ack"), ("outcome", "sent")))
    rid = await served(h)
    assert (await h.request(rid))["state"] == "done" and len(h.replies(rid)) == 1 and not h.acks(rid)
    suppressed, deferred, sent = delta(before).values()
    assert suppressed == 1 and deferred >= 1 and sent == 0


# ======================================================================== /status


def viewer() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=PUBLIC_BASE_URL)


async def _jev_events() -> None:
    """Two Jev gate calls (one handed to the LLM) and one citation check, as agent.audit records them."""
    for escalated in (True, False):
        await audit.record_llm_call(None, {"provider": "typesafe", "purpose": "gate", "escalated": escalated,
                                           "model": "jev-1.13.0"})
    await audit.record_llm_call(None, {"provider": "typesafe", "purpose": "support_check", "escalated": False})


async def test_the_status_page_shows_this_hours_requests_and_nothing_about_them(h):
    rids = [await served(h, subject=f"Exhibits please {i}", from_addr=f"user{i}@example.com") for i in range(2)]
    h.auth.verdict = "fail"
    await served(h, from_addr="mallory@example.net")
    await _jev_events()
    status.clear_cache()
    async with viewer() as c:
        r = await c.get("/status")
    status.clear_cache()
    assert r.status_code == 200
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
    assert "Emails received 3 3" in text and "Answered 2 2" in text and "Not processed" in text
    assert "Replies sent 2 2" in text and "Within 3 min 100.0 % 100.0 %" in text
    assert "Nova Scotia Utility and Review Board 2 2" in text and "No regulator identified 1 1" in text
    assert "Jev escalation rate (request gate) 1 of 2" in text and "50.0 %" in text
    assert "Citation support rate" in text and "Database Operational" in text
    rows = await db.pool().fetch("SELECT id, track_token, subject, from_addr FROM requests")
    for row in rows:
        for value in (str(row["id"]), row["track_token"], row["subject"], row["from_addr"]):
            assert value not in r.text
    assert MATTER not in r.text and "@" not in r.text and len(rids) == 2


async def test_the_status_numbers_need_no_more_than_the_web_roles_grants(h, database_url):
    await served(h)
    await _jev_events()
    grants = (REPO / "deploy" / "sql" / "grants.sql").read_text()
    m = re.search(r"'GRANT SELECT \((.*?)\) ON requests TO agent_web'", grants, re.DOTALL)
    assert m
    columns = re.sub(r"'\s*'", "", m.group(1))
    role = f"ragent_web_{secrets.token_hex(3)}"
    await db.pool().execute(f"CREATE ROLE {role} NOLOGIN")
    try:
        await db.pool().execute(f"GRANT SELECT ({columns}) ON requests TO {role}")
        await db.pool().execute(f"GRANT SELECT ON events TO {role}")
        pool, saved = await asyncpg.create_pool(database_url, min_size=1, max_size=2, init=db._init_conn,
                                                server_settings={"role": role}), db._pool
        db._pool = pool
        try:
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await db.fetchrow("SELECT subject FROM requests LIMIT 1")
            facts = await status.compute(get_settings())
        finally:
            db._pool = saved
            await pool.close()
    finally:
        await db.pool().execute(f"DROP OWNED BY {role}")
        await db.pool().execute(f"DROP ROLE {role}")
    assert facts["db_ok"] is True and facts["components"]["ok"] is True
    day, week = facts["windows"]
    assert (day["received"], day["done"], day["replies"], week["done"]) == (1, 1, 1, 1)
    assert 0 <= day["p50_s"] <= day["p95_s"] < 180 and day["within_target"] == 1
    assert {r["provider"]: r["week"] for r in facts["volume"]} == {"uarb": 1}
    assert facts["citations"]["kept"] >= 2 and facts["jev"] == {"gate": {"calls": 2, "escalated": 1},
                                                                 "support_check": {"calls": 1, "escalated": 0}}
