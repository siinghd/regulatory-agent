"""Public service status: /status.

Aggregates only, computed here from Postgres and Redis and kept for CACHE_S in this process, so a
busy page costs a handful of queries a minute. No address, subject, matter number, IP or request
id is read into it, let alone shown: of `requests` it reads STATUS_COLUMNS (granted to the web role
in deploy/sql/grants.sql), of `events` the counters of `summary` and TypeSafe `llm.call` events.
Redis (component health) and Postgres (the numbers) may each be unavailable; the page then says so
for that part and still renders.
"""

import asyncio
import functools
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import Response

from agent import db, health
from agent.config import Settings, get_settings

log = structlog.get_logger()
router = APIRouter()

CACHE_S = 60.0
TARGET_S = 180  # the reply-time objective: within 3 minutes of the email arriving
QUERY_TIMEOUT_S = 5.0
WINDOWS = (("Last 24 hours", timedelta(hours=24)), ("Last 7 days", timedelta(days=7)))
QUALITY_WINDOW = timedelta(days=7)
# The only columns of `requests` this page reads (the web role's grant must cover them).
STATUS_COLUMNS = ("state", "provider", "received_at", "reply_sent_at")

_WINDOW_SQL = """
SELECT count(*) AS received,
       count(*) FILTER (WHERE state = 'done') AS done,
       count(*) FILTER (WHERE state = 'clarify') AS clarify,
       count(*) FILTER (WHERE state = 'rejected') AS rejected,
       count(*) FILTER (WHERE state = 'failed') AS failed,
       count(reply_sent_at) AS replies,
       count(*) FILTER (WHERE reply_sent_at - received_at <= make_interval(secs => $2)) AS within_target,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM reply_sent_at - received_at)::float8) AS p50_s,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM reply_sent_at - received_at)::float8) AS p95_s
FROM requests WHERE received_at >= now() - $1::interval
"""
_VOLUME_SQL = """
SELECT provider, count(*) FILTER (WHERE received_at >= now() - $1::interval) AS day, count(*) AS week
FROM requests WHERE received_at >= now() - $2::interval
GROUP BY provider
"""
# Fresh summaries only: a cached one ({"cached": true}) proposed nothing new.
_CITATIONS_SQL = """
SELECT coalesce(sum((data->>'claims')::int), 0) AS kept, coalesce(sum((data->>'dropped')::int), 0) AS dropped
FROM events WHERE kind = 'summary' AND at >= now() - $1::interval AND data ? 'dropped'
"""
# Jev's own calls (agent.audit.record_llm_call): `escalated` says it handed the work to the LLM.
_JEV_SQL = """
SELECT data->>'purpose' AS purpose, count(*) AS calls,
       count(*) FILTER (WHERE data->>'escalated' = 'true') AS escalated
FROM events
WHERE kind = 'llm.call' AND at >= now() - $1::interval AND data->>'provider' = 'typesafe'
  AND data->>'purpose' IN ('gate', 'support_check')
GROUP BY 1
"""


@functools.cache
def regulators() -> dict[str, str]:
    """Name -> display name of each regulator the worker serves (agent.worker.startup)."""
    from agent.providers.ferc import FercProvider
    from agent.providers.oeb import OebProvider
    from agent.providers.uarb import UarbProvider

    return {p.name: p.display_name for p in (UarbProvider, OebProvider, FercProvider)}


# ---------------------------------------------------------------- facts (numbers only)


async def _window(label: str, span: timedelta) -> dict:
    row = await db.fetchrow(_WINDOW_SQL, span, TARGET_S)
    facts = {"label": label, **dict(row)}
    facts["open"] = facts["received"] - sum(facts[k] for k in ("done", "clarify", "rejected", "failed"))
    return facts


async def _volume() -> list[dict]:
    rows = await db.fetch(_VOLUME_SQL, WINDOWS[0][1], WINDOWS[1][1])
    return [dict(r) for r in rows]


async def _citations() -> dict:
    return dict(await db.fetchrow(_CITATIONS_SQL, QUALITY_WINDOW))


async def _jev() -> dict:
    rows = await db.fetch(_JEV_SQL, QUALITY_WINDOW)
    found = {r["purpose"]: {"calls": r["calls"], "escalated": r["escalated"]} for r in rows}
    return {p: found.get(p, {"calls": 0, "escalated": 0}) for p in ("gate", "support_check")}


async def compute(settings: Settings) -> dict:
    """Everything the page shows, as numbers. Never raises."""
    components = await health.component_facts(settings, list(regulators()))
    facts: dict = {"computed_at": datetime.now(UTC), "components": components, "db_ok": False}
    try:
        async with asyncio.timeout(QUERY_TIMEOUT_S):
            facts["windows"] = [await _window(label, span) for label, span in WINDOWS]
            facts["volume"] = await _volume()
            facts["citations"] = await _citations()
            facts["jev"] = await _jev()
        facts["db_ok"] = True
    except Exception as e:  # noqa: BLE001 - a public page: say the numbers are unavailable, never 500
        log.warning("web.status_unavailable", error=type(e).__name__)
    return facts


@dataclass
class _Cache:
    at: float = float("-inf")
    facts: dict | None = None
    lock: asyncio.Lock | None = None
    loop: asyncio.AbstractEventLoop | None = None


_cache = _Cache()


async def snapshot(settings: Settings) -> dict:
    """`compute`, at most once per CACHE_S in this process (concurrent requests share one run)."""
    if _cache.facts is not None and time.monotonic() - _cache.at < CACHE_S:
        return _cache.facts
    loop = asyncio.get_running_loop()
    if _cache.lock is None or _cache.loop is not loop:  # a lock belongs to one event loop
        _cache.lock, _cache.loop = asyncio.Lock(), loop
    async with _cache.lock:
        if _cache.facts is None or time.monotonic() - _cache.at >= CACHE_S:
            _cache.facts = await compute(settings)
            _cache.at = time.monotonic()
    return _cache.facts


def clear_cache() -> None:
    _cache.at, _cache.facts = float("-inf"), None


# ---------------------------------------------------------------- the page's wording


def pct(n: int | None, d: int | None) -> str:
    if not d:
        return "–"
    return f"{100 * (n or 0) / d:.1f} %"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    s = round(seconds)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        m, rest = divmod(s, 60)
        return f"{m} min {rest} s" if rest else f"{m} min"
    h, rest = divmod(s, 3600)
    return f"{h} h {rest // 60} min" if rest // 60 else f"{h} h"


def _ago(seconds: float) -> str:
    return "just now" if seconds < 1 else f"{duration(seconds)} ago"


def _components(facts: dict) -> list[dict]:
    redis = facts["components"]
    rows = []
    if not redis.get("ok"):
        rows.append({"name": "Email intake", "state": "unknown", "detail": "Status not available right now."})
        rows.append({"name": "Request worker", "state": "unknown", "detail": "Status not available right now."})
    else:
        age = redis.get("ingest_heartbeat_age_s")
        if age is None:
            rows.append({"name": "Email intake", "state": "down", "detail": "No heartbeat in the last 15 minutes."})
        else:
            fresh = age < health.INGEST_MAX_AGE_S
            rows.append({"name": "Email intake", "state": "ok" if fresh else "down",
                         "detail": f"Mailbox checked {_ago(age)}."})
        reporting = bool(redis.get("worker_reporting"))
        rows.append({"name": "Request worker", "state": "ok" if reporting else "down",
                     "detail": "Reporting." if reporting else "Not reporting: new requests wait in the queue."})
    rows.append({"name": "Database", "state": "ok" if facts["db_ok"] else "down",
                 "detail": "Reachable." if facts["db_ok"] else "The numbers below are not available right now."})
    for name, display in regulators().items():
        if not redis.get("ok"):
            state, detail = "unknown", "Status not available right now."
        elif name in redis.get("breakers_open", ()):
            state, detail = "down", "Failing repeatedly: requests for it wait and retry."
        else:
            state, detail = "ok", "No repeated failures."
        rows.append({"name": f"{display} portal", "state": state, "detail": detail})
    return rows


def view(facts: dict) -> dict:
    """The template's context: labels and formatted values only."""
    out: dict = {
        "computed_at": f"{facts['computed_at']:%Y-%m-%d %H:%M} UTC",
        "components": _components(facts),
        "db_ok": facts["db_ok"],
        "target": duration(TARGET_S),
    }
    if not facts["db_ok"]:
        return out
    windows = facts["windows"]
    out["windows"] = [w["label"] for w in windows]

    def row(label: str, fn) -> dict:
        return {"label": label, "values": [fn(w) for w in windows]}

    out["requests"] = [
        row("Emails received", lambda w: str(w["received"])),
        row("Answered", lambda w: str(w["done"])),
        row("Asked the sender a question", lambda w: str(w["clarify"])),
        row("Not processed (spam, automated mail, unverified sender, over a limit)", lambda w: str(w["rejected"])),
        row("Failed", lambda w: str(w["failed"])),
        row("Still in progress", lambda w: str(w["open"])),
        row("Success rate (answered of answered + failed)", lambda w: pct(w["done"], w["done"] + w["failed"])),
    ]
    out["latency"] = [
        row("Replies sent", lambda w: str(w["replies"])),
        row("Median (p50)", lambda w: duration(w["p50_s"])),
        row("95th percentile (p95)", lambda w: duration(w["p95_s"])),
        row(f"Within {duration(TARGET_S)}", lambda w: pct(w["within_target"], w["replies"])),
    ]
    out["p95_ok"] = [None if w["p95_s"] is None else w["p95_s"] <= TARGET_S for w in windows]
    names = regulators()
    counts = {r["provider"]: r for r in facts["volume"]}
    out["volume"] = [
        {"label": display, "values": [str(counts.get(name, {}).get(k, 0)) for k in ("day", "week")]}
        for name, display in names.items()
    ]
    unknown = [r for r in facts["volume"] if r["provider"] not in names]
    out["volume"].append({"label": "No regulator identified",
                          "values": [str(sum(r[k] for r in unknown)) for k in ("day", "week")]})
    cites, jev = facts["citations"], facts["jev"]
    proposed = cites["kept"] + cites["dropped"]
    out["quality"] = [
        {"label": "Citation support rate", "value": pct(cites["kept"], proposed),
         "detail": f"{cites['kept']} of {proposed} claims proposed for summaries were kept: the quote was found "
                   "on the cited page and supports the claim."},
        {"label": "Jev escalation rate (request gate)", "value": pct(jev["gate"]["escalated"], jev["gate"]["calls"]),
         "detail": f"{jev['gate']['escalated']} of {jev['gate']['calls']} emails Jev classified were handed to the "
                   "LLM classifier (Jev unsure or unavailable)."},
        {"label": "Jev escalation rate (citation check)",
         "value": pct(jev["support_check"]["escalated"], jev["support_check"]["calls"]),
         "detail": f"{jev['support_check']['escalated']} of {jev['support_check']['calls']} citation checks had "
                   "claims re-checked by the LLM."},
    ]
    return out


@router.api_route("/status", methods=["GET", "HEAD"])
async def status_page(request: Request) -> Response:
    from agent.web.app import templates  # shared renderer

    facts = await snapshot(get_settings())
    return templates.TemplateResponse(
        request, "status.html", {"s": view(facts)}, headers={"Cache-Control": f"public, max-age={int(CACHE_S)}"},
    )
