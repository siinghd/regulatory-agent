"""The request state machine: gate -> fetch -> package -> deliver -> reply.

received --gate--> rejected | replying -> clarify | done (question) | accepted
accepted -> fetching -> packaging -> replying -> done      (any step can -> replying -> failed)

Each step is resumable: a retry re-enters at the request's current state, re-uses whatever is
already stored (content-addressed blobs, cached listings, the recorded drop link, the rendered
reply in the outbox) and moves on. Retries are driven by the queue; this module decides
retryable vs final, and *parks* a request (no attempt used) while a dependency it needs is known
to be down or its work is locked by another job.

Emails are never sent from here directly: each is rendered and written to the outbox in the
same transaction as the state change that decides it, then delivered (agent.outbox).
"""

import asyncio
import contextlib
import dataclasses
import hashlib
import itertools
import random
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import aiosmtplib
import asyncpg
import structlog
from arq.connections import ArqRedis
from redis.exceptions import RedisError

from agent import admin, audit, blobs, breaker, db, metrics, outbox, queue, store
from agent.breaker import Breakers
from agent.citations.claims import summarize_with_citations
from agent.citations.extract import extract_pages_async, is_docx, needs_ocr
from agent.citations.ground import DropReason, new_citation_id, quote_context
from agent.config import Settings, get_settings
from agent.delivery.choose import Delivery, DeliveryDeferred, deliver
from agent.delivery.drop import DropClient, DropError, DropLink
from agent.delivery.package import build_zip
from agent.gate import jev as jev_gate
from agent.gate import rules
from agent.gate.classify import _degraded as classify_degraded
from agent.gate.classify import clarification_for, classify_with_meta, matter_examples
from agent.limits import DAY_S, Budgets, Limits, LockTimeout, domain_key, seconds_to_utc_midnight, sender_key
from agent.llm import LLMUnavailable
from agent.mail import auth as mail_auth
from agent.mail import loops, mime, outbound
from agent.models import (
    AgentError,
    AuthVerdict,
    DeliveryFailed,
    DocumentRef,
    DownloadedFile,
    InboundEmail,
    Intent,
    MatterInfo,
    MatterNotFound,
    ParsedRequest,
    PortalUnavailable,
    ProviderRejected,
    ScrapeError,
    SenderAuth,
    TooLarge,
)
from agent.providers.base import Provider, all_providers, find_category, provider_for_matter
from agent.typesafe import TypeSafeUnavailable

log = structlog.get_logger()

# Never retried, whatever the attempt: the answer won't change.
NEVER_RETRY = (MatterNotFound, ProviderRejected, TooLarge, DeliveryFailed)
SINGLE_FLIGHT_TTL_S = 120  # renewed while held
SINGLE_FLIGHT_WAIT_S = 300
LOCK_PARK_S = 30.0  # work locked by another job: look again after 30-60 s
PARK_JITTER_S = 60.0
INFLIGHT_WAIT_S = 30.0  # over the sender's in-flight cap: look again after 30-60 s
# Over a portal's daily visits: look again at most this long after (each look also refreshes
# updated_at, so the sweeper never mistakes a waiting request for a stuck one).
BUDGET_RECHECK_S = 600.0
INFLIGHT_LOCK_TTL_S = 30


class TransientError(AgentError):
    retryable = True


class SenderAuthTemperror(TransientError):
    """Sender verification couldn't finish (DNS temperror): retried, never answered meanwhile."""


class PipelineTimeout(AgentError):
    """One try ran past `pipeline_timeout_s` (a hung portal session, a stalled transfer...)."""

    retryable = True


class RetriesExhausted(AgentError):
    """The sweeper gave up on a request whose attempts or deadline ran out without an answer."""

    retryable = True


class DiskLow(AgentError):
    retryable = True
    dependency = "disk"


class Parked(Exception):
    """Not this request's failure: try again later without using one of its attempts."""

    def __init__(self, reason: str, defer_s: float):
        super().__init__(reason)
        self.reason = reason
        self.defer_s = max(defer_s, 1.0)


class WaitedTooLong(AgentError):
    """A request waiting for a limit to free up (its sender's in-flight cap, a portal's daily
    visits) reached its deadline first."""

    retryable = False

    def __init__(self, message: str, user_message: str):
        super().__init__(message)
        self.user_message = user_message


class _Wait(Exception):
    """Over a limit that frees up by itself: park without using an attempt, until the deadline."""

    def __init__(self, kind: str, defer_s: float, *, progress: str, provider: str | None = None):
        super().__init__(kind)
        self.kind = kind  # inflight | portal_budget
        self.defer_s = defer_s
        self.progress = progress  # what the progress page says meanwhile
        self.provider = provider


@dataclass
class Deps:
    settings: Settings
    providers: dict[str, Provider]
    limits: Limits
    drop: DropClient | None
    queue: ArqRedis | None = None  # for scheduling outbox retries
    breakers: Breakers | None = None
    budgets: Budgets | None = None

    def __post_init__(self) -> None:
        if self.breakers is None:
            self.breakers = Breakers(self.limits.r, self.settings)
        if self.budgets is None:
            self.budgets = Budgets(self.limits.r)


# ======================================================================== entry point


async def process(deps: Deps, request_id: UUID, *, final_attempt: bool, deadline: datetime | None = None) -> None:
    row = await store.get(request_id)
    if row is None or row["state"] in store.SETTLED:
        return
    if await admin.is_paused(deps.limits.r):  # kill switch (`ragent pause`): no attempt used
        raise Parked("paused by an operator", admin.PAUSED_RETRY_S + random.uniform(0, PARK_JITTER_S))
    log.info("request.start", request_id=str(request_id), state=row["state"], attempt=row["attempts"])
    email: InboundEmail | None = None
    try:
        async with asyncio.timeout(deps.settings.pipeline_timeout_s):
            raw = await asyncio.to_thread(blobs.read_raw, row["raw_sha256"])
            email = mime.parse_raw(raw, row["received_at"])
            await _run(deps, row, raw, email, final_attempt=final_attempt)
    except Parked:
        raise
    except _Wait as w:
        await _wait_or_give_up(deps, request_id, email, w, deadline)
    except LockTimeout as e:
        raise Parked(f"waiting for lock {e}", LOCK_PARK_S + random.uniform(0, LOCK_PARK_S)) from e
    except breaker.Open as e:
        if deadline is None or datetime.now(UTC) < deadline:
            await _event_soft(request_id, "parked", {"dependency": e.dependency, "retry_in_s": round(e.retry_in_s)})
            raise Parked(str(e), e.retry_in_s + random.uniform(0, PARK_JITTER_S)) from e
        await _fail(deps, request_id, email, _open_error(deps, e), final=True)
    except TimeoutError as e:  # the per-try bound (or a timeout nothing below classified)
        err = PipelineTimeout(f"try did not finish within {deps.settings.pipeline_timeout_s}s: {e!r}")
        err.dependency = await _likely_dependency(request_id)
        await _fail(deps, request_id, email, err, final=final_attempt)
    except AgentError as e:
        await _fail(deps, request_id, email, e, final=final_attempt)
    except Exception as e:  # a bug or an outage of our own: retry, then apologise, never go silent
        log.exception("request.crash", request_id=str(request_id))
        _alert_if_disk_full(e)
        await _fail(deps, request_id, email, e, final=final_attempt)


async def _run(deps: Deps, row, raw: bytes, email: InboundEmail, *, final_attempt: bool) -> None:
    if row["state"] == "replying" and await _resume_reply(deps, row, email):
        return
    if row["state"] == "received":
        parsed = await _gate(deps, row, raw, email, final_attempt=final_attempt)
        if parsed is None:
            return
        row = await store.get(row["id"])
    await _fulfil(deps, row, email)


async def _fail(deps: Deps, request_id: UUID, email: InboundEmail | None, e: BaseException, *, final: bool) -> None:
    never = isinstance(e, NEVER_RETRY) or (isinstance(e, AgentError) and not e.retryable)
    if not never and not final:
        await store.event(request_id, "retry", {"error": _describe(e), "dependency": getattr(e, "dependency", None)})
        metrics.observe_retry(retry_cause(e))
        if isinstance(e, AgentError):
            raise e
        raise TransientError(_describe(e)) from e
    await _finish_with_error(deps, request_id, email, e)


async def finish_exhausted(deps: Deps, request_id: UUID) -> None:
    """The sweeper's end for a request whose attempts or deadline ran out without a final try
    finishing (every try killed by the queue's timeout, a worker crash loop...)."""
    row = await store.get(request_id)
    if row is None or row["state"] in store.SETTLED:
        return
    if await admin.is_paused(deps.limits.r):  # never apologise for a pause: it resumes where it was
        return
    last = await store.last_retry(request_id) or {}
    err = RetriesExhausted(last.get("error") or f"no answer after {row['attempts']} attempts")
    err.dependency = last.get("dependency") or (row["provider"] if row["state"] in {"accepted", "fetching"} else None)
    email = None
    try:
        email = mime.parse_raw(await asyncio.to_thread(blobs.read_raw, row["raw_sha256"]), row["received_at"])
    except Exception as e:  # noqa: BLE001 - without the email there is just nobody to answer
        log.warning("request.unreadable_email", request_id=str(request_id), error=_describe(e))
    await _finish_with_error(deps, request_id, email, err)


# ======================================================================== gate


async def _gate(deps: Deps, row, raw: bytes, email: InboundEmail, *, final_attempt: bool) -> ParsedRequest | None:
    """Decide whether and how to answer. Returns the parsed request if we'll fetch documents."""
    s, rid = deps.settings, row["id"]
    if await admin.is_suppressed(email.from_addr):  # DSAR or operator block: no processing, no reply
        await store.transition(rid, {"received"}, "rejected", reject_reason="suppressed")
        return None
    await store.set_progress(rid, "Checking your email")

    reason = loops.classify_automation(
        email, own_address=s.agent_mail_address, return_path=mime.envelope_sender(mime.parse_headers(raw))
    )
    if reason:
        await store.transition(rid, {"received"}, "rejected", reject_reason=f"automated:{reason}")
        return None

    auth = await mail_auth.verify_sender(raw, email, trusted_mta=s.trusted_mta_hostname)
    auth_json = auth.model_dump(mode="json")
    if mail_auth.is_temperror(auth):
        if final_attempt:
            # Still unverifiable after every retry: drop silently. Writing back to an
            # unverified From is exactly what a spoofer wants.
            metrics.observe_auth(auth.verdict.value)  # counted once, when it is the verdict ("none")
            await store.transition(rid, {"received"}, "rejected", reject_reason=f"unauthenticated:{auth.reason}",
                                   auth=auth_json)
            return None
        raise SenderAuthTemperror(f"sender verification DNS temperror: {auth.reason}")
    metrics.observe_auth(auth.verdict.value)
    await store.set_auth(rid, auth_json)
    if s.sender_auth_mode != "off" and auth.verdict is not AuthVerdict.PASS:
        # No reply at all: answering an unverifiable From is how agents become spam reflectors.
        await store.transition(rid, {"received"}, "rejected", reject_reason=f"unauthenticated:{auth.reason}", auth=auth_json)
        return None
    if s.sender_auth_mode == "allowlist" and not _allowlisted(email.from_addr, s.sender_allowlist):
        await store.transition(rid, {"received"}, "rejected", reject_reason="not_allowlisted", auth=auth_json)
        return None
    if admin.asks_for_deletion(email):  # "DELETE MY DATA" from a verified sender: erasure (DSAR)
        await admin.request_erasure(email.from_addr, rid)
        await _reply_and_close(deps, row, email, "done", reject_reason="dsar_delete", common={"auth": auth_json},
                               paragraphs=[admin.ERASURE_CONFIRMATION.format(contact=s.privacy_contact)])
        return None

    if not await _within_limits(deps, row, email, auth_json):
        return None

    # The daily budget covers every model call, TypeSafe's (input tokens x list price) and OpenRouter's.
    llm_meta: dict | None = None
    follow_up = await _thread_follow_up(row, email, s.max_docs_per_request)
    if follow_up is not None:  # "Key Documents" answering our question: no model needed
        parsed, llm_meta = follow_up, None
    elif await deps.budgets.llm_exhausted():  # today's budget is spent: the rules alone decide
        parsed = _classify_rules_only(email, s.max_docs_per_request)
        if parsed.source != "rules":
            await _event_soft(rid, "rate_limited", {"key": "llm_budget", "action": "degraded"})
    else:
        classify = jev_gate.classify_with_meta if s.gate_classifier == "jev" else classify_with_meta
        parsed, llm_meta = await classify(email.subject, email.text, max_docs=s.max_docs_per_request)
        if parsed.source != "rules":  # the classifier sent the email to a model (answered or not)
            await audit.record_llm_call(rid, llm_meta or {"outcome": "unavailable"}, purpose="gate",
                                        data_class="email_body", input_chars=len(email.subject) + len(email.text))
    classifier = _CLASSIFIERS.get(parsed.source, "rules")
    parsed = await _inherit_from_thread(row, parsed)
    common = {"auth": auth_json, "parsed": parsed.model_dump(mode="json")}
    provider = provider_for_matter(parsed.matter) if parsed.matter else None
    metrics.observe_gate(classifier, escalated=bool((llm_meta or {}).get("escalated")),
                         outcome=_gate_outcome(parsed, provider))

    if parsed.intent is Intent.SPAM:
        await store.transition(rid, {"received"}, "rejected", reject_reason="spam", **common)
        return None
    if parsed.intent is Intent.INJECTION:
        await _reply_and_close(deps, row, email, "rejected", reject_reason="injection_attempt", common=common, paragraphs=[
            (
                "I can only fetch public regulatory documents and send them back to the address that asked. "
                f"Please send a plain request, for example: {_example_requests()}"
            ),
        ])
        return None
    if parsed.intent is Intent.UNRELATED:
        if loops.in_thread_with_us(email, own_address=s.agent_mail_address):
            prev = await store.previous_in_thread(row["thread_root"], rid, row["from_addr"])
            if prev is not None and prev["state"] == "clarify":
                # We asked a question and this reply didn't answer it in a way we understood.
                # Silence here would strand the sender, so ask once more (thread caps bound it).
                await _reply_and_close(deps, row, email, "clarify", common=common, paragraphs=[
                    "Sorry, I didn't catch which documents you want.",
                    clarification_for(prev["matter"], None),
                ])
                return None
            # "Thanks, that's all" in a thread with us: the conversation is over, nothing to answer
            await store.transition(rid, {"received"}, "rejected", reject_reason="unrelated_in_thread", **common)
            return None
        if await deps.limits.once(f"help:{sender_key(email.from_addr)}", DAY_S):
            await _reply_and_close(deps, row, email, "rejected", reject_reason="unrelated", common=common, paragraphs=[
                (
                    "I'm an automated assistant that fetches documents from utility regulators' public "
                    "databases. Send me a matter number and a document type, for example: "
                    f"{_example_requests()}"
                ),
                *(
                    f"{p.display_name} (matter numbers like {p.matter_example}): {_category_list(p)}."
                    for p in all_providers()
                ),
            ])
        else:
            await store.transition(rid, {"received"}, "rejected", reject_reason="unrelated_repeat", **common)
        return None

    if parsed.matter and provider is None:
        examples = matter_examples()
        await _reply_and_close(deps, row, email, "clarify", common=common, paragraphs=[
            f"{parsed.matter} isn't a matter number I can look up. Matter numbers look like {examples}.",
        ])
        return None

    if (
        provider is None
        or parsed.intent is Intent.QUESTION
        or parsed.needs_clarification
        or not parsed.doc_type
    ):
        await _answer_without_documents(deps, row, email, parsed, provider, common)
        return None

    parsed = await _split(deps, row, email, parsed, auth_json)
    common["parsed"] = parsed.model_dump(mode="json")
    # The acknowledgement is queued with the decision; a failure to send it never holds up the fetch.
    accepted = await _queue(
        deps, row, email, _ack_draft(deps, row, email, parsed, provider), from_states={"received"},
        to_state="accepted", provider=provider.name, matter=parsed.matter, doc_type=parsed.doc_type, **common,
    )
    return parsed if accepted else None  # not accepted: another job already took it past the gate


async def _split(deps: Deps, row, email: InboundEmail, parsed: ParsedRequest, auth_json: dict) -> ParsedRequest:
    """Each further matter of the email, with the one category the rules pair it with, becomes a
    request of its own (fetched in parallel, answered in its own reply in the thread), up to
    max_matters_per_email in all. The model only proposes the matters; a matter the rules can't
    pair, over that cap or over the sender's limits stays in `extra_matters`, offered in the reply.

    Created before this request is accepted, once per (email, matter): a retry of the gate finds
    them and creates nothing twice. Queued now; the sweeper picks up any the queue missed.
    """
    s = deps.settings
    split: list[tuple[str, str]] = []
    offered: list[str] = []
    for matter in dict.fromkeys(m for m in parsed.extra_matters if m != parsed.matter):
        target = provider_for_matter(matter)
        text = email.text if matter in rules.find_matters(email.text) else f"{email.subject}\n{email.text}"
        pair = rules.category_for(text, matter, parsed.doc_type) if target else None
        if (pair is None or len(split) + 1 >= s.max_matters_per_email
                or not await _split_within_limits(deps, email, store.split_message_id(row["message_id"], matter))):
            offered.append(matter)
            continue
        doc_type, count = pair
        max_docs = min(count, s.max_docs_per_request) if count else (
            parsed.max_docs if doc_type == parsed.doc_type else s.max_docs_per_request)
        child = ParsedRequest(intent=Intent.DOCUMENT_REQUEST, matter=matter, doc_type=doc_type, max_docs=max_docs,
                              source="split", confidence=parsed.confidence)
        child_id = await store.create_split(row, matter=matter, doc_type=doc_type, provider=target.name,
                                            parsed=child.model_dump(mode="json"), auth=auth_json)
        if child_id is not None and deps.queue is not None:
            await queue.enqueue_request(deps.queue, child_id)
        split.append((matter, doc_type))
    if split:
        log.info("gate.split", request_id=str(row["id"]), matters=1 + len(split), offered=len(offered))
    return parsed.model_copy(update={"extra_matters": tuple(offered), "split": tuple(split)})


async def _split_within_limits(deps: Deps, email: InboundEmail, member: str) -> bool:
    """A split request counts against the sender's, the domain's and the global limits like an
    email of its own (no "slow down" notice: the email's own request is answered anyway)."""
    for who, window, key, limit in _rate_checks(deps.settings, email):
        if not await deps.limits.decide(f"{who}_{window}", key, limit=limit, window_s=_WINDOW_S[window], member=member):
            return False
    return True


def _ack_draft(
    deps: Deps, row, email: InboundEmail, parsed: ParsedRequest, provider: Provider
) -> outbound.Draft:
    return outbound.ack(
        name=_display_name(email), subject=email.subject, matter=parsed.matter,
        doc_type=parsed.doc_type, provider=provider, track_url=_track_url(deps, row), split=parsed.split,
    )


def _category_list(provider: Provider) -> str:
    return ", ".join(c.name for c in provider.categories)


def _example_requests() -> str:
    return " or ".join(
        f'"Can you send me the {p.categories[0].name} for {p.matter_example}?"' for p in all_providers()
    )


def _allowlisted(addr: str, allowlist: list[str]) -> bool:
    domain = "@" + addr.rsplit("@", 1)[-1]
    return any(a.lower() in {addr, domain} for a in allowlist)


_CLASSIFIERS = {"rules": "rules", "rules_degraded": "rules", "thread": "rules", "jev": "jev", "llm": "llm"}  # ParsedRequest.source


def _gate_outcome(parsed: ParsedRequest, provider: Provider | None) -> str:
    """gate_decisions_total's outcome: what `_gate`'s branches do with this parse (accept: documents
    are fetched or a question is answered)."""
    if parsed.intent in {Intent.SPAM, Intent.INJECTION, Intent.UNRELATED}:
        return "reject"
    if provider is None or parsed.needs_clarification or not parsed.doc_type:
        return "clarify"
    return "accept"


def _classify_rules_only(email: InboundEmail, max_docs: int) -> ParsedRequest:
    """The gate without an LLM call: the rules' answer, else the classifier's conservative
    fallback for when no model answers (a question rather than a guess)."""
    rule = rules.parse(email.subject, email.text, max_docs=max_docs)
    return rule.parsed or classify_degraded(rule, f"{email.subject}\n{email.text}", max_docs)


_WINDOW_S = {"hour": 3600, "day": DAY_S}
_SLOW_DOWN = {
    "hour": "You've sent a lot of requests in the last hour, so I'm pausing for a while. Please try again later.",
    "day": "You've sent a lot of requests today, so I'm pausing until tomorrow. Please try again then.",
}


async def _within_limits(deps: Deps, row, email: InboundEmail, auth_json: dict) -> bool:
    """Sliding-window caps per normalised sender, per organizational domain and globally, each
    per hour and per day. Keys are HMACs (agent.limits): no address is ever in Redis."""
    s, rid = deps.settings, row["id"]
    sender = sender_key(email.from_addr)
    for who, window, key, limit in _rate_checks(s, email):
        if await deps.limits.decide(f"{who}_{window}", key, limit=limit, window_s=_WINDOW_S[window], member=str(rid)):
            continue
        reason = f"rate_limited:{who}" + ("_day" if window == "day" else "")
        notify = who != "global" and await _may_slow_down(deps, sender, window)
        await _event_soft(rid, "rate_limited", {"key": who, "window": window, "action": "rejected", "notice": notify})
        if notify:
            notice = outbound.simple_reply(name=_display_name(email), subject=email.subject,
                                           paragraphs=[_SLOW_DOWN[window]])
            await _queue(deps, row, email, notice, from_states={"received"}, to_state="rejected",
                         reject_reason=reason, auth=auth_json)
        else:
            await store.transition(rid, {"received"}, "rejected", reject_reason=reason, auth=auth_json)
        return False
    if await store.thread_size(row["thread_root"], email.from_addr) > s.max_requests_per_thread:
        await store.transition(rid, {"received"}, "rejected", reject_reason="thread_cap", auth=auth_json)
        return False
    return True


def _rate_checks(s: Settings, email: InboundEmail) -> list[tuple[str, str, str, int]]:
    """(who, window, Redis key, limit) of every sliding-window cap an email counts against."""
    sender, domain = sender_key(email.from_addr), domain_key(email.from_addr)
    return [
        ("sender", "hour", f"sender:{sender}", s.rate_per_sender_hour),
        ("sender", "day", f"sender_day:{sender}", s.rate_per_sender_day),
        ("domain", "hour", f"domain:{domain}", s.rate_per_domain_hour),
        ("domain", "day", f"domain_day:{domain}", s.rate_per_domain_day),
        ("global", "hour", "global", s.rate_global_hour),
        ("global", "day", "global_day", s.rate_global_day),
    ]


async def _may_slow_down(deps: Deps, sender: str, window: str) -> bool:
    """May this rate-limited sender get a "slow down" reply? At most one an hour (one a day for
    the daily caps) and rate_notices_per_day in all, so a flood never becomes a flood of replies."""
    if not await deps.limits.once(f"rl-notice:{window}:{sender}", _WINDOW_S[window]):
        return False
    return await deps.limits.decide("slow_down_notice", f"notices:{sender}", limit=deps.settings.rate_notices_per_day,
                                    window_s=DAY_S)


async def _thread_follow_up(row, email: InboundEmail, max_docs: int) -> ParsedRequest | None:
    """A reply in our thread that names one category of the thread's matter ("Key Documents",
    "Exhibits please") is a request for that matter.

    Decided before any model: a classifier sees two words without the conversation and can't
    tell they answer the question we asked (live: Jev read "Key Documents" as unrelated). Only
    for the same verified sender (previous_in_thread), only when the body names no other matter,
    exactly one category of that regulator and no negation; anything else takes the normal path.
    """
    if not loops.in_thread_with_us(email, own_address=get_settings().agent_mail_address):
        return None
    body = email.text or ""
    if not body.strip() or len(body) > 1_000:
        return None
    prev = await store.previous_in_thread(row["thread_root"], row["id"], row["from_addr"])
    if prev is None:
        return None
    matter = prev["matter"]
    provider = provider_for_matter(matter)
    if provider is None:
        return None
    if any(m != matter for m in rules.find_matters(body)) or rules.negated(body):
        return None
    doc_types = rules.find_doc_types(body, provider.categories)
    if len(doc_types) != 1:
        return None
    return ParsedRequest(
        intent=Intent.DOCUMENT_REQUEST,
        matter=matter,
        doc_type=doc_types[0],
        max_docs=rules.find_count(body, max_docs, provider.categories),
        source="thread",
        confidence=1.0,
    )


async def _inherit_from_thread(row, parsed: ParsedRequest) -> ParsedRequest:
    """A follow-up like "Exhibits please" in an existing thread reuses that thread's matter.

    The classifier could only check the category against every regulator's; it must also be
    one of the inherited matter's.
    """
    if parsed.intent is not Intent.DOCUMENT_REQUEST or parsed.matter:
        return parsed
    prev = await store.previous_in_thread(row["thread_root"], row["id"], row["from_addr"])
    if not prev:
        return parsed
    matter = prev["matter"]
    provider = provider_for_matter(matter)
    categories = provider.categories if provider else ()
    category = find_category(categories, parsed.doc_type) if parsed.doc_type else None
    extra = (find_category(categories, t) for t in parsed.extra_doc_types)
    return parsed.model_copy(update={
        "matter": matter,
        "doc_type": category.name if category else None,
        "extra_doc_types": tuple(c.name for c in extra if c and c != category),
        "needs_clarification": None if category else clarification_for(matter, None),
    })


async def _answer_without_documents(
    deps: Deps, row, email, parsed: ParsedRequest, provider: Provider | None, common
) -> None:
    """Questions and incomplete requests: answer with what we know, ask for what's missing."""
    rid = row["id"]
    paragraphs: list[str] = []
    if provider is not None:
        await store.set_progress(rid, "Looking up the matter")
        try:
            info = await _matter_info(deps, provider.name, parsed.matter)
        except MatterNotFound as e:
            await _reply_and_close(deps, row, email, "done", common=common, paragraphs=[e.user_message])
            return
        paragraphs.append(outbound.matter_sentence(info, provider))
    if parsed.needs_clarification or not parsed.doc_type or provider is None:
        paragraphs.append(parsed.needs_clarification or clarification_for(parsed.matter, parsed.doc_type))
        state = "clarify"
    else:
        paragraphs.append(
            f"If you'd like the documents, reply with the type you want ({_category_list(provider)})."
        )
        state = "done"
    provider_name = provider.name if provider else None
    await _reply_and_close(deps, row, email, state, common={**common, "matter": parsed.matter, "provider": provider_name},
                           paragraphs=paragraphs)


# ======================================================================== fulfil


@dataclass(frozen=True)
class Fetched:
    info: MatterInfo
    refs: list[DocumentRef]  # the public documents listed, in portal order
    files: list[DownloadedFile]  # what goes in the package, in portal order
    failed: list[str]  # titles that couldn't be downloaded
    skipped: list[str]  # titles left out to stay within max_request_bytes
    confidential: int  # listed rows that are not public
    complete_listing: bool  # the listing covered every row of the category
    daily_allowance: int | None = None  # set when the sender's bytes_per_sender_day was the size budget


async def _fulfil(deps: Deps, row, email: InboundEmail) -> None:
    rid = row["id"]
    parsed = ParsedRequest.model_validate(row["parsed"])
    provider = deps.providers[row["provider"]]
    doc_type: str = row["doc_type"]
    await _start_fetching(deps, row)

    # What fits: the per-request cap, or what is left of the sender's daily allowance if that is less.
    sender = _sender_h(row)
    allowance = await deps.budgets.bytes_left(sender, rid)
    daily = allowance < deps.settings.max_request_bytes
    fetched = await _fetch(deps, rid, provider, parsed.matter, doc_type, parsed.max_docs,
                           budget=allowance if daily else deps.settings.max_request_bytes)
    if daily:
        fetched = dataclasses.replace(fetched, daily_allowance=deps.settings.bytes_per_sender_day)
        if fetched.skipped:
            await _event_soft(rid, "rate_limited", {"key": "bytes_budget", "action": "partial",
                                                    "skipped": len(fetched.skipped)})
    metrics.observe_limiter("bytes_budget", "limited" if daily and fetched.skipped else "allowed")
    info, files = fetched.info, fetched.files
    if not files:
        await _queue(
            deps, row, email, outbound.simple_reply(
                name=_display_name(email), subject=email.subject, track_url=_track_url(deps, row),
                paragraphs=[outbound.matter_sentence(info, provider), _nothing_to_send(parsed.matter, doc_type, fetched)],
                provider=provider,
            ),
            from_states={"fetching", "packaging", "replying"}, to_state="replying", final_state="done",
            result={"files": 0, "counts": _counts(info), "confidential": fetched.confidential,
                    "skipped": fetched.skipped},
        )
        return

    # 'replying' only for a reply queued before the outbox existed: rebuild it from the cache
    await store.transition(rid, {"fetching", "packaging", "replying"}, "packaging")
    link_only = bool(((await store.get(rid))["result"] or {}).get("link_only"))
    order = _order(fetched.refs)
    with tempfile.TemporaryDirectory(prefix=f"req-{rid}-") as tmp:
        await store.set_progress(rid, "Packaging the ZIP and writing a cited summary")
        # Independent work: the LLM summary (~20s) overlaps packaging and the encrypted upload.
        delivery, (summary, claims, basis) = await _together(
            _package_and_deliver(deps, rid, info, provider, doc_type, files, tmp, link_only=link_only),
            _summarise(deps, rid, info, provider, files),
        )
        await store.set_progress(rid, "Sending your documents")
        doc_ids = await store.documents_by_external_ids(provider.name, parsed.matter, [f.ref.external_id for f in files])
        draft = outbound.documents_reply(
            name=_display_name(email), subject=email.subject, info=info, provider=provider, doc_type=doc_type,
            docs=[outbound.DocLine(
                title=f.ref.title, filed=f.ref.filed_on.isoformat() if f.ref.filed_on else "undated",
                # the page viewer renders PDFs only; other files are in the ZIP (citations on a Word
                # file open a quote-only page with a download link)
                url=_pdf_url(deps, doc_ids[f.ref.external_id]["id"], f.sha256)
                if f.ref.external_id in doc_ids and f.filename.lower().endswith(".pdf") else None,
            ) for f in files],
            requested=parsed.max_docs, summary=summary, claims=claims,
            download_url=delivery.link.url if delivery.link else None,
            download_expires=delivery.link.expires_at if delivery.link else None,
            download_size=delivery.size, attachment_path=delivery.path,
            track_url=_track_url(deps, row), extra_doc_types=parsed.extra_doc_types,
            extra_matters=parsed.extra_matters, split=parsed.split, failed_titles=fetched.failed, confidential=fetched.confidential,
            order=order, skipped_titles=fetched.skipped,
            size_budget=fetched.daily_allowance or deps.settings.max_request_bytes,
            summary_basis=basis, daily_allowance=fetched.daily_allowance is not None,
        )
        outcome = await _queue(
            deps, row, email, draft, from_states={"packaging"}, to_state="replying", final_state="done",
            result={
                "files": len(files), "failed": fetched.failed, "skipped": fetched.skipped,
                "confidential": fetched.confidential, "counts": _counts(info), "zip_bytes": delivery.size,
                "delivery": delivery.kind, "citations": len(claims),
                "drop_id": delivery.link.id if delivery.link else None,
            },
        )
    if outcome is not None:  # counted once per request, whichever job queued the reply
        await deps.budgets.add_bytes(sender, rid, sum(f.size for f in files))
    if outcome is not None and outcome.status == "sent":
        await store.set_progress(rid, "Done", done=len(files), total=len(files))


def _sender_h(row) -> str:
    """The request's normalised-sender key (recorded at ingest; computed for older rows)."""
    return row["sender_h"] or sender_key(row["from_addr"])


async def _start_fetching(deps: Deps, row) -> None:
    """accepted -> fetching, unless the sender already has max_inflight_per_sender requests
    fetching or packaging: then this one waits its turn (parked, no attempt used)."""
    rid = row["id"]
    if row["state"] != "accepted":  # a retry: it holds its slot already (or is past fetching)
        await store.transition(rid, {"accepted", "fetching"}, "fetching")
        return
    sender = _sender_h(row)
    # Count and take the slot under one per-sender lock, so two waiting requests can't both take the last one.
    async with deps.limits.lock(f"inflight:{sender}", ttl_s=INFLIGHT_LOCK_TTL_S, wait_s=INFLIGHT_LOCK_TTL_S):
        if await store.inflight_count(sender, rid) >= deps.settings.max_inflight_per_sender:
            metrics.observe_limiter("inflight", "deferred")
            raise _Wait("inflight", INFLIGHT_WAIT_S, progress="Waiting for your earlier requests to finish")
        await store.transition(rid, {"accepted", "fetching"}, "fetching")
    metrics.observe_limiter("inflight", "allowed")


async def _portal_open(deps: Deps, provider_name: str) -> None:
    """Raise _Wait if today's visits to this provider's portal are used up (portal_daily_visits)."""
    if await deps.budgets.portal_open(provider_name):
        return
    provider = deps.providers.get(provider_name)
    name = provider.display_name if provider else provider_name
    raise _Wait("portal_budget", min(BUDGET_RECHECK_S, seconds_to_utc_midnight() + 1), provider=provider_name,
                progress=f"Delayed: today's limit on visits to the {name} database was reached")


def _nothing_to_send(matter: str, doc_type: str, fetched: Fetched) -> str:
    total = fetched.info.counts.get(doc_type, 0)
    one = outbound.singular(doc_type)
    if total == 0:
        return f"{matter} has no {doc_type}."
    if fetched.skipped and fetched.daily_allowance is not None:
        return (f"You've reached today's download allowance ({outbound.human_size(fetched.daily_allowance)}), "
                f"so I didn't send any of the {doc_type}. Please ask again tomorrow.")
    if fetched.skipped:
        return (f"The {doc_type} are larger than I can send in one request, so I didn't send any: "
                + "; ".join(fetched.skipped) + ".")
    if fetched.confidential and fetched.confidential >= total:
        return (f"The only {one} for {matter} is marked confidential, so I can't send it." if total == 1
                else f"All {total} {doc_type} for {matter} are marked confidential, so I can't send them.")
    if fetched.confidential:
        return (f"The {outbound.count_of(fetched.confidential, doc_type)} I looked at for {matter} "
                f"{'is' if fetched.confidential == 1 else 'are'} marked confidential, so I can't send "
                f"{'it' if fetched.confidential == 1 else 'them'}.")
    return (f"I couldn't download the only {one} right now." if total == 1
            else f"I couldn't download any of the {total} {doc_type} right now.")


async def _matter_info(deps: Deps, provider_name: str, matter: str) -> MatterInfo:
    cached = await store.cached_matter(provider_name, matter, timedelta(seconds=deps.settings.matter_cache_ttl_s))
    if cached:
        return cached.info
    await _portal_open(deps, provider_name)
    async with deps.breakers.call(provider_name):
        await deps.budgets.count_portal_visit(provider_name)  # counted once the breaker lets the visit through
        with metrics.provider_call(provider_name):
            info = await deps.providers[provider_name].fetch_matter(matter)
    await store.save_matter(info)
    return info


async def _fetch(
    deps: Deps, rid: UUID, provider: Provider, matter: str, doc_type: str, limit: int, *, budget: int
) -> Fetched:
    """Listing + downloads, single-flighted per (provider, matter, category) and served from cache
    when possible: ten people asking for M12205 at once cost one portal visit. At most `budget`
    bytes of files go in the package; the rest are `skipped`."""
    async with deps.limits.lock(f"sf:{provider.name}:{matter}:{doc_type}", ttl_s=SINGLE_FLIGHT_TTL_S,
                                wait_s=SINGLE_FLIGHT_WAIT_S):
        return await _fetch_locked(deps, rid, provider, matter, doc_type, limit, budget)


async def _fetch_locked(
    deps: Deps, rid: UUID, provider: Provider, matter: str, doc_type: str, limit: int, budget: int
) -> Fetched:
    s = deps.settings
    ttl = timedelta(seconds=s.matter_cache_ttl_s)
    await store.set_progress(rid, f"Searching the {provider.display_name} database")
    cached = await store.cached_matter(provider.name, matter, ttl)
    listing = cached.listing(doc_type, ttl) if cached else None
    if cached and listing and listing.serves(limit, cached.info.counts.get(doc_type, 0)):
        info = cached.info
        ids = listing.ids[:limit]
        known = await store.documents_by_external_ids(provider.name, matter, ids)
        refs = [_ref_from_row(known[x], i) for i, x in enumerate(ids) if x in known]
        confidential, complete = listing.confidential, listing.complete
    else:
        await _portal_open(deps, provider.name)
        async with deps.breakers.call(provider.name):
            await deps.budgets.count_portal_visit(provider.name)
            with metrics.provider_call(provider.name):
                info, listed = await provider.list_matter_and_documents(matter, doc_type, limit)
        count = info.counts.get(doc_type, 0)
        if count > 0 and not listed:
            # The portal says there are documents but we read none: a scraper problem, not
            # an answer. Retry rather than tell the user there's nothing.
            raise ScrapeError(f"{matter}/{doc_type}: count {count} but empty listing")
        public = [r for r in listed if r.access == "Public"]
        refs = public[:limit]
        confidential = len(listed) - len(public)
        complete = len(listed) >= count
        for r in refs:
            await store.upsert_document(r)
        await store.save_matter(info, doc_type, store.Listing(
            ids=[r.external_id for r in refs], confidential=confidential, rows=len(listed), limit=limit, count=count,
        ))
        if confidential:
            await store.event(rid, "confidential_skipped", {"count": confidential})
    await store.set_progress(rid, "Downloading documents", done=0, total=len(refs))

    have = await store.documents_by_external_ids(provider.name, matter, [r.external_id for r in refs])
    ready: dict[str, DownloadedFile] = {}
    missing: list[DocumentRef] = []
    for r in refs:
        row = have.get(r.external_id)
        if row and row["sha256"] and blobs.has_blob(row["sha256"]):
            ready[r.external_id] = DownloadedFile(
                ref=r, path=str(blobs.blob_path(row["sha256"])), sha256=row["sha256"],
                size=row["size_bytes"], filename=row["filename"] or f"{r.external_id}{r.file_ext}",
            )
        else:
            missing.append(r)

    used = sum(f.size for f in ready.values())
    over_budget = bool(missing) and used >= budget
    if missing and not over_budget:
        _check_disk(s)
        await _portal_open(deps, provider.name)
        with tempfile.TemporaryDirectory(prefix="dl-", dir=s.data_dir) as tmp:
            async with contextlib.aclosing(provider.download(matter, missing, tmp)) as stream:
                counted = False  # one download batch is one portal visit
                while True:
                    try:
                        async with deps.breakers.call(provider.name):
                            if not counted:
                                await deps.budgets.count_portal_visit(provider.name)
                                counted = True
                            with metrics.provider_call(provider.name) as call:  # one file
                                f = await anext(stream, None)
                                call.skip = f is None
                    except MatterNotFound as e:
                        # We just listed this matter: "not found" now is the portal's search race.
                        raise ScrapeError(f"{matter} reported missing while downloading files it listed") from e
                    if f is None:
                        break
                    dest = blobs.put_file(f.path, f.sha256)
                    await store.upsert_document(f.ref, sha256=f.sha256, size=f.size, filename=f.filename)
                    ready[f.ref.external_id] = f.model_copy(update={"path": str(dest)})
                    used += f.size
                    await store.set_progress(rid, "Downloading documents", done=len(ready), total=len(refs))
                    if used >= budget:
                        over_budget = True  # stop pulling more: nothing else would fit
                        break

    files: list[DownloadedFile] = []
    failed: list[str] = []
    skipped: list[str] = []
    total = 0
    for r in refs:
        f = ready.get(r.external_id)
        if f is None:
            (skipped if over_budget else failed).append(r.title)
        elif total + f.size > budget:
            skipped.append(r.title)
        else:
            total += f.size
            files.append(f)
    if skipped:
        log.warning("fetch.over_budget", matter=matter, doc_type=doc_type, skipped=len(skipped), budget=budget)
        await store.event(rid, "size_budget", {"skipped": len(skipped), "budget": budget})
    if failed:
        log.warning("fetch.partial", matter=matter, doc_type=doc_type, got=len(files), wanted=len(refs))
    if failed and not files:
        raise TransientError(f"no downloads succeeded for {matter}/{doc_type}")
    return Fetched(info=info, refs=refs, files=files, failed=failed, skipped=skipped,
                   confidential=confidential, complete_listing=complete)


def _check_disk(s: Settings) -> None:
    free = blobs.disk_free()
    if free < s.disk_min_free_bytes:
        log.error("disk.low", alert=True, free_bytes=free, min_free_bytes=s.disk_min_free_bytes)
        raise DiskLow(f"only {free} bytes free under {s.data_dir} (minimum {s.disk_min_free_bytes})")


def _ref_from_row(row, index: int) -> DocumentRef:
    return DocumentRef(
        provider=row["provider"], matter=row["matter"], doc_type=row["doc_type"],
        external_id=row["external_id"], title=row["title"], filed_on=row["filed_on"],
        file_ext="." + (row["filename"] or ".pdf").rsplit(".", 1)[-1], row_index=index,
        source_type=row["source_type"],
    )


class _GuardedDrop:
    """The drop client behind its circuit breaker (Open lets `deliver` fall back or park)."""

    def __init__(self, drop: DropClient, breakers: Breakers):
        self._drop = drop
        self._breakers = breakers

    async def upload(self, path: str, display_name: str) -> DropLink:
        async with self._breakers.call("drop"):
            return await self._drop.upload(path, display_name)


async def _package_and_deliver(
    deps: Deps, rid: UUID, info: MatterInfo, provider: Provider, doc_type: str, files: list[DownloadedFile],
    tmp: str, *, link_only: bool,
) -> Delivery:
    """Package and deliver once per request: a recorded link for exactly these files is reused."""
    key = _files_key(files)
    stored = Delivery.from_record(await store.load_delivery(rid), key)
    if stored is not None:
        log.info("delivery.reused", request_id=str(rid), drop_id=stored.link.id if stored.link else None)
        return stored
    zip_result = await asyncio.to_thread(
        build_zip, files, f"{tmp}/{info.matter}_{doc_type.replace(' ', '_')}.zip",
        readme_text=_readme(info, provider, doc_type, files),
    )
    drop = _GuardedDrop(deps.drop, deps.breakers) if deps.drop else None
    try:
        delivery = await deliver(zip_result, files, drop=drop, link_only=link_only)
    except breaker.Open:
        raise  # drop is known to be down: nothing was tried (the request is parked)
    except Exception:
        metrics.observe_delivery("drop", "error")  # no attachment fallback either
        raise
    metrics.observe_delivery("drop" if delivery.kind == "link" else "attachment", "ok")
    if delivery.link is not None:
        await store.save_delivery(rid, delivery.to_record(key))
    return delivery


def _files_key(files: list[DownloadedFile]) -> str:
    return hashlib.sha256("\n".join(f"{f.ref.external_id}:{f.sha256}" for f in files).encode()).hexdigest()[:32]


async def _together(first, second):
    """Run two coroutines; the first failure cancels the other and is raised as itself."""
    try:
        async with asyncio.TaskGroup() as tg:
            a = tg.create_task(first)
            b = tg.create_task(second)
    except BaseExceptionGroup as eg:
        raise _first_error(eg)  # the group stays attached as __context__
    return a.result(), b.result()


def _first_error(eg: BaseExceptionGroup) -> BaseException:
    for e in eg.exceptions:
        if isinstance(e, BaseExceptionGroup):
            return _first_error(e)
        if not isinstance(e, asyncio.CancelledError):
            return e
    return eg.exceptions[0]


# Bump when the summary prompt or grounding rules change. v3: Word (DOCX) files are read and
# unreadable files are passed in (for "based on N of M documents"); v4: no speculation about status.
SUMMARY_VERSION = "v4"
_SPREADSHEET_EXTS = frozenset({".xlsx", ".xls", ".xlsm", ".csv"})
_RECORDING_EXTS = frozenset({".mp3", ".mp4", ".wav", ".m4a"})

Summary = tuple[str | None, list[outbound.ClaimLine], outbound.SummaryBasis | None]
_SUPPORT_FAILED = frozenset({DropReason.NOT_ENTAILED, DropReason.SUPPORT_CHECK_FAILED})  # citations_total


async def _summarise(
    deps: Deps, rid: UUID, info: MatterInfo, provider: Provider, files: list[DownloadedFile]
) -> Summary:
    """Cited summary. Best effort: whatever goes wrong here (LLM down, a damaged PDF, a bug),
    the documents still go out, just without a summary."""
    try:
        return await _summarise_unguarded(deps, rid, info, provider, files)
    except Exception as e:  # fail soft by design
        log.warning("summary.failed", request_id=str(rid), error=_describe(e), exc_info=True)
        await _event_soft(rid, "summary_failed", {"error": _describe(e)})
        return None, [], None


async def _summarise_unguarded(
    deps: Deps, rid: UUID, info: MatterInfo, provider: Provider, files: list[DownloadedFile]
) -> Summary:
    """Cached by the exact document versions (content hashes), so a repeat request for the same
    filings costs no LLM call, and any new or changed filing produces a fresh summary."""
    key = hashlib.sha256(
        (SUMMARY_VERSION + info.matter + "".join(sorted(f.sha256 for f in files))).encode()
    ).hexdigest()
    cached = await db.fetchrow("SELECT summary, claims, basis FROM summaries WHERE key = $1", key)
    if cached:
        summary, raw_claims = cached["summary"], cached["claims"]
        basis = _basis_from_record(cached["basis"])
        await store.event(rid, "summary", {"cached": True, "claims": len(raw_claims)})
    elif await deps.budgets.llm_exhausted():  # today's LLM budget is spent: the documents go without one
        log.warning("summary.skipped", request_id=str(rid), reason="llm_budget")
        await _event_soft(rid, "summary_skipped", {"reason": "llm_budget"})
        await _event_soft(rid, "rate_limited", {"key": "llm_budget", "action": "degraded"})
        return None, [], None
    else:
        # Every file goes in, unreadable ones with no pages, so the result says what the summary
        # is not based on.
        docs: list[tuple[DocumentRef, list[str]]] = []
        kinds: dict[str, str] = {}  # external id -> why it can't be read (outbound.UNREADABLE_KINDS)
        for f in files:
            try:
                pages, kind = await _document_text(f)
            except Exception as e:  # noqa: BLE001 - one damaged file must not cost the others their summary
                log.warning("summary.extract_failed", request_id=str(rid), document=f.ref.external_id,
                            error=_describe(e))
                pages, kind = [], "damaged"
            docs.append((f.ref, pages))
            if kind:
                kinds[f.ref.external_id] = kind
        if not any(pages for _, pages in docs):
            return None, [], None
        input_chars = sum(len(t) for _, pages in docs for t in pages)
        try:
            result = await summarize_with_citations(info, docs, max_claims=5, regulator=provider.display_name)
        except LLMUnavailable as e:
            log.warning("summary.unavailable", request_id=str(rid), error=str(e)[:300])
            await audit.record_llm_call(rid, {"purpose": "summary", "outcome": "unavailable"},
                                        data_class="public_document", input_chars=input_chars)
            return None, [], None
        await audit.record_llm_call(rid, result.llm, purpose="summary", data_class="public_document",
                                    input_chars=input_chars)
        if result.support_check:  # the claims' entailment check: quotes and claims to another call
            await audit.record_llm_call(rid, result.support_check, purpose="support_check",
                                        data_class="public_document")
        unsupported = sum(d.reason in _SUPPORT_FAILED for d in result.dropped)
        metrics.observe_citations(kept=len(result.claims), dropped=len(result.dropped) - unsupported,
                                  support_failed=unsupported)
        summary = result.summary or None
        raw_claims = [
            {"claim": c.claim, "doc_external_id": c.doc_external_id, "page": c.page,
             "quote": c.quote, "char_start": c.char_start, "char_end": c.char_end}
            for c in result.claims
        ]
        basis = outbound.SummaryBasis(
            used=len(result.context_docs), total=len(files), unreadable=len(result.unreadable_docs),
            unread=len(result.unread_docs),
            kinds=tuple(dict.fromkeys(kinds[x] for x in result.unreadable_docs if x in kinds)),
        )
        await db.execute(
            "INSERT INTO summaries (key, summary, claims, basis) VALUES ($1, $2, $3, $4) ON CONFLICT (key) DO NOTHING",
            key, summary, raw_claims, _basis_record(basis),
        )
        await store.event(rid, "summary", {"claims": len(raw_claims), "dropped": len(result.dropped),
                                           "based_on": _basis_record(basis), **result.llm})

    existing = await db.fetch("SELECT id, claim FROM citations WHERE request_id = $1 ORDER BY created_at, id", rid)
    if existing:  # a retry of this request: reuse its links rather than minting new ones
        return summary, [outbound.ClaimLine(text=r["claim"], url=f"{deps.settings.public_base_url}/c/{r['id']}")
                         for r in existing], basis
    ids = await store.documents_by_external_ids(info.provider, info.matter, [c["doc_external_id"] for c in raw_claims])
    claims: list[outbound.ClaimLine] = []
    async with db.acquire() as conn:
        for c in raw_claims:
            doc = ids.get(c["doc_external_id"])
            if not doc:
                continue
            before, after = await _quote_context(conn, doc["sha256"], c)
            cid = new_citation_id()  # per request, so one requester's links never collide with another's
            await conn.execute(
                """INSERT INTO citations (id, request_id, document_id, sha256, page, quote, char_start, char_end, claim,
                                          context_before, context_after)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                cid, rid, doc["id"], doc["sha256"], c["page"], c["quote"], c["char_start"], c["char_end"], c["claim"],
                before, after,
            )
            claims.append(outbound.ClaimLine(text=c["claim"], url=f"{deps.settings.public_base_url}/c/{cid}"))
    return summary, claims, basis


async def _document_text(f: DownloadedFile) -> tuple[list[str], str | None]:
    """(page texts, why it can't be summarised) for one file. PDFs and Word (DOCX) files are read,
    a DOCX recognised by content too; anything else has no pages. The reason is a key of
    outbound.UNREADABLE_KINDS, None for a readable file."""
    name = f.filename.lower()
    if not (name.endswith(".pdf") or await asyncio.to_thread(is_docx, f.path)):
        ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
        if ext in _SPREADSHEET_EXTS:
            return [], "spreadsheet"
        if ext in _RECORDING_EXTS:
            return [], "recording"
        return [], {".doc": "old_word", ".docx": "damaged"}.get(ext, "other")
    pages = await _pages(f.sha256, f.path)
    if not pages:
        return [], "damaged"  # encrypted or corrupt
    return pages, "scanned" if needs_ocr(pages) else None


def _basis_record(basis: outbound.SummaryBasis) -> dict:
    return {"used": basis.used, "total": basis.total, "unreadable": basis.unreadable, "unread": basis.unread,
            "kinds": list(basis.kinds)}


def _basis_from_record(record: dict | None) -> outbound.SummaryBasis | None:
    if not record:
        return None  # cached before the basis was recorded
    return outbound.SummaryBasis(**{**record, "kinds": tuple(record.get("kinds") or ())})


async def _quote_context(conn, sha256: str | None, c: dict) -> tuple[str | None, str | None]:
    """The sentences around a claim's quote on its page, for the viewer's quote-only page (Word
    files), which can't read the page text itself."""
    text = await conn.fetchval("SELECT text FROM pages WHERE sha256 = $1 AND page = $2", sha256, c["page"])
    if not text or text[c["char_start"] : c["char_end"]] != c["quote"]:
        return None, None
    before, after = quote_context(text, c["char_start"], c["char_end"])
    return before or None, after or None


async def _pages(sha256: str, path: str) -> list[str]:
    """Page texts, extracted once per content hash."""
    rows = await db.fetch("SELECT text FROM pages WHERE sha256 = $1 ORDER BY page", sha256)
    if rows:
        return [r["text"] for r in rows]
    pages = await extract_pages_async(path)
    async with db.acquire() as conn, conn.transaction():
        await conn.executemany(
            "INSERT INTO pages (sha256, page, text) VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
            [(sha256, i + 1, t) for i, t in enumerate(pages)],
        )
        await conn.execute("UPDATE documents SET page_count = $2 WHERE sha256 = $1", sha256, len(pages))
    return pages


# ======================================================================== waiting on limits


async def _wait_or_give_up(deps: Deps, rid: UUID, email: InboundEmail | None, w: _Wait, deadline: datetime | None) -> None:
    """Park a request that is over a limit that frees up by itself (no attempt used); at its
    deadline, give up with one apology. The first time, record why and, for a portal's daily
    budget, tell the sender it's delayed (once per request)."""
    now = datetime.now(UTC)
    if deadline is not None and now >= deadline:
        await _fail(deps, rid, email, _waited_too_long(deps, w), final=True)
        return
    if await deps.limits.once(f"waiting:{w.kind}:{rid}", deps.settings.request_deadline_s + 3600):
        await _event_soft(rid, "rate_limited", {"key": w.kind, "action": "deferred"})
        await _event_soft(rid, "waiting", {"reason": w.kind, "provider": w.provider})
        if w.kind == "portal_budget":
            await _delay_notice(deps, rid, email, w, deadline)
    await store.set_progress(rid, w.progress)  # also keeps the sweeper from taking it for stuck
    defer = w.defer_s if deadline is None else min(w.defer_s, (deadline - now).total_seconds() + 1)
    raise Parked(f"waiting: {w.kind}", defer + random.uniform(0, min(w.defer_s, PARK_JITTER_S)))


def _waited_too_long(deps: Deps, w: _Wait) -> WaitedTooLong:
    if w.kind == "portal_budget":
        provider = deps.providers.get(w.provider or "")
        name = provider.display_name if provider else "regulator's"
        return WaitedTooLong(
            f"{w.provider} portal visits used up for today until the request's deadline",
            f"I've reached today's limit on how often I query the {name} database, so I couldn't fetch this "
            "in time. Please send the request again after midnight UTC.",
        )
    return WaitedTooLong(
        "sender's in-flight cap until the request's deadline",
        "You had other requests running at the same time, and this one waited too long for its turn, "
        "so I've stopped. Please send it again.",
    )


async def _delay_notice(deps: Deps, rid: UUID, email: InboundEmail | None, w: _Wait, deadline: datetime | None) -> None:
    """The one email a request waiting on a portal's daily budget gets: delayed, not lost. Like
    every email, only to a verified sender, and through the outbox."""
    row = await store.get(rid)
    if row is None or email is None or not _verified(row):
        return
    provider = deps.providers.get(w.provider or "")
    name = provider.display_name if provider else "regulator's"
    what = (f"the {row['doc_type']} for {row['matter']}" if row["matter"] and row["doc_type"]
            else row["matter"] or "your request")
    until = f" until {deadline:%H:%M} UTC" if deadline is not None else ""
    draft = outbound.simple_reply(
        name=_display_name(email), subject=email.subject, track_url=_track_url(deps, row), provider=provider,
        paragraphs=[(
            f"I've reached today's limit on how often I query the {name} database, so {what} is delayed. "
            f"I'll keep trying{until} and email you either way; the limit resets at midnight UTC."
        )],
    )
    draft = dataclasses.replace(draft, kind="notice")
    msg = outbound.build_message(draft, request_id=rid, to_addr=_recipient(row, email), in_reply_to=email.message_id,
                                 references=email.references)
    out = store.OutboundMsg(kind="notice", message_id=msg["Message-ID"], body=outbound.render(msg))
    if await store.queue_notice(rid, out):
        await outbox.send_now(out.message_id, breakers=deps.breakers, queue_redis=deps.queue)


# ======================================================================== replies & failures


async def _queue(
    deps: Deps, row, email: InboundEmail, draft: outbound.Draft, *, from_states: set[str], to_state: str,
    final_state: str | None = None, **fields,
) -> outbox.Outcome | None:
    """Render `draft`, write it to the outbox with the state change, then try to send it now.

    Returns the send outcome, or None if the state change lost its compare-and-set.
    """
    rid = row["id"]
    msg = outbound.build_message(
        draft, request_id=rid, to_addr=_recipient(row, email, fields.get("auth")), in_reply_to=email.message_id,
        references=email.references,
    )
    out = store.OutboundMsg(kind=draft.kind, message_id=msg["Message-ID"], body=outbound.render(msg),
                            final_state=final_state, has_attachment=bool(draft.attachments))
    if not await store.transition(rid, from_states, to_state, outbound=out, **fields):
        return None
    return await outbox.send_now(out.message_id, breakers=deps.breakers, queue_redis=deps.queue)


async def _reply_and_close(
    deps: Deps, row, email, state: str, *, paragraphs, common: dict, reject_reason: str | None = None
) -> None:
    fields = {k: v for k, v in common.items() if v is not None}
    if reject_reason:
        fields["reject_reason"] = reject_reason
    draft = outbound.simple_reply(name=_display_name(email), subject=email.subject, paragraphs=paragraphs)
    await _queue(deps, row, email, draft, from_states={"received"}, to_state="replying", final_state=state, **fields)


async def _resume_reply(deps: Deps, row, email: InboundEmail) -> bool:
    """A request in 'replying' already has its reply in the outbox: (re)send it, never rebuild it."""
    rows = await store.outbound_rows(row["id"])
    reply = next((r for r in rows if r["kind"] == "reply"), None)
    if reply is not None and reply["status"] in {"queued", "sent"}:
        if reply["status"] == "queued":
            await outbox.send_now(reply["message_id"], breakers=deps.breakers, queue_redis=deps.queue)
        return True
    pending = (row["result"] or {}).get("pending_reply")  # decided before the outbox existed
    if reply is None and row["reply_sent_at"] is not None:  # sent before the outbox existed: just close
        await store.transition(row["id"], {"replying"}, (pending or {}).get("final_state", "done"))
        return True
    if pending:
        draft = outbound.simple_reply(name=_display_name(email), subject=email.subject,
                                      paragraphs=pending["paragraphs"])
        await _queue(deps, row, email, draft, from_states={"replying"}, to_state="replying",
                     final_state=pending["final_state"])
        return True
    return False


async def _finish_with_error(deps: Deps, rid: UUID, email: InboundEmail | None, e: BaseException) -> None:
    row = await store.get(rid)
    if row is None or row["state"] in store.SETTLED:
        return
    state = row["state"]
    final_state = "done" if isinstance(e, MatterNotFound) else "failed"
    failure = _failure_kind(deps, e)
    fields = {"error": f"{type(e).__name__}: {str(e)[:500]}", "result": {"failure": failure}}
    if any(r["kind"] == "reply" and r["status"] in {"queued", "sent"} for r in await store.outbound_rows(rid)):
        log.warning("request.error_after_reply", request_id=str(rid), error=fields["error"])
        return  # the outbox owns this request now; never swap its reply for an apology
    if email is None or not _verified(row):
        # Never write to an address we haven't verified, not even to apologise.
        log.warning("request.closed_silently", request_id=str(rid), error=fields["error"])
        await store.transition(rid, {state}, final_state, **fields)
        return
    draft = outbound.simple_reply(name=_display_name(email), subject=email.subject,
                                  paragraphs=[_failure_message(e, failure)])
    await _queue(deps, row, email, draft, from_states={state}, to_state="replying", final_state=final_state, **fields)


def _verified(row) -> bool:
    return (row["auth"] or {}).get("verdict") == AuthVerdict.PASS.value


def _recipient(row, email: InboundEmail, auth: dict | None = None) -> str:
    """Every reply goes to the From address at the domain sender verification checked (its A-label),
    never to Reply-To. `auth` is the verification about to be recorded, else the recorded one."""
    auth = auth or row["auth"]
    if not auth:  # nothing verified on record (no reply should reach here without it)
        return email.from_addr
    return mail_auth.reply_address(email, SenderAuth.model_validate(auth))


def _failure_kind(deps: Deps, e: BaseException) -> str:
    """Who failed, for the apology's wording and the progress page: never blame the regulator for
    our own (or drop's, or the mail server's) problems."""
    dependency = getattr(e, "dependency", None)
    if isinstance(e, MatterNotFound):
        return "not_found"
    if isinstance(e, WaitedTooLong):
        return "limit"
    if isinstance(e, TooLarge):
        return "too_large"
    regulators = set(deps.providers) | {p.name for p in all_providers()}
    if dependency in regulators or isinstance(e, (PortalUnavailable, ScrapeError, ProviderRejected)):
        return "regulator"
    if dependency == "drop" or isinstance(e, (DropError, DeliveryDeferred)):
        return "drop"
    if dependency == "smtp" or isinstance(e, DeliveryFailed):
        return "mail"
    return "internal"


def _failure_message(e: BaseException, failure: str) -> str:
    if isinstance(e, AgentError) and not e.retryable:
        return e.user_message
    if failure == "regulator":
        return ("The regulator's website kept failing while I worked on this, so I've stopped retrying. "
                "Please send the request again in a little while.")
    if failure == "drop":
        return ("Your documents were ready, but our secure file-sharing service kept failing, so I've stopped "
                "retrying. Please send the request again in a little while.")
    return ("I ran into an unexpected problem on my side and couldn't finish this request. It has been logged; "
            "please send it again in a little while.")


def _open_error(deps: Deps, e: breaker.Open) -> AgentError:
    """A breaker still open at the request's deadline, as the failure it stands for."""
    err: AgentError
    if e.dependency in deps.providers:
        err = PortalUnavailable(str(e))
    elif e.dependency == "drop":
        err = DeliveryDeferred(str(e))
    else:
        err = TransientError(str(e))
    err.dependency = e.dependency
    return err


async def _likely_dependency(rid: UUID) -> str | None:
    """A try that timed out mid-fetch was almost certainly waiting on the regulator."""
    with contextlib.suppress(Exception):
        row = await store.get(rid)
        if row and row["state"] in {"accepted", "fetching"}:
            return row["provider"]
    return None


async def _event_soft(rid: UUID, kind: str, data: dict) -> None:
    """An event that must not turn a handled situation into a new failure."""
    try:
        await store.event(rid, kind, data)
    except Exception as e:  # noqa: BLE001
        log.warning("event.lost", kind=kind, error=_describe(e))


_RETRY_CAUSES: tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...] = (
    (SenderAuthTemperror, "auth_temperror"),
    (ScrapeError, "scrape_error"),
    (PortalUnavailable, "portal_unavailable"),
    ((PipelineTimeout, TimeoutError), "timeout"),
    (DiskLow, "disk_low"),
    ((LLMUnavailable, TypeSafeUnavailable), "llm_unavailable"),
    ((DropError, DeliveryDeferred), "drop_unavailable"),
    (aiosmtplib.SMTPException, "smtp_temp"),
    ((asyncpg.PostgresError, asyncpg.InterfaceError), "db"),
    (RedisError, "redis"),
    (LockTimeout, "lock_contention"),
    (breaker.Open, "breaker_open"),
)
_DEPENDENCY_CAUSES = {"drop": "drop_unavailable", "smtp": "smtp_temp", "disk": "disk_low", "typesafe": "llm_unavailable"}


def retry_cause(e: BaseException) -> str:
    """retries_total's cause (metrics.RETRY_CAUSES): the kind of failure, never its message."""
    for kinds, cause in _RETRY_CAUSES:
        if isinstance(e, kinds):
            return cause
    dependency = getattr(e, "dependency", None)
    if not dependency:
        return "internal"
    if dependency.startswith("openrouter:"):
        return "llm_unavailable"
    return _DEPENDENCY_CAUSES.get(dependency, "portal_unavailable")  # otherwise a regulator's name


def _alert_if_disk_full(e: BaseException) -> None:
    import errno

    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        log.error("disk.full", alert=True, error=_describe(e))


def _describe(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:400]}"


# ======================================================================== helpers


def _display_name(email: InboundEmail) -> str:
    return email.from_name


def _track_url(deps: Deps, row) -> str:
    return f"{deps.settings.public_base_url}/r/{row['track_token']}"


def _pdf_url(deps: Deps, document_id: UUID, sha256: str) -> str:
    """Pinned to the version we sent, so the link shows exactly the file in the ZIP."""
    return f"{deps.settings.public_base_url}/files/{document_id}/{sha256}.pdf"


def _order(refs: list[DocumentRef]) -> str:
    """How the portal ordered this listing: "newest" or "oldest" first when every document is dated
    and the dates say so, else "portal" (we don't claim an order we can't see)."""
    dates = [r.filed_on for r in refs]
    if len(dates) < 2 or any(d is None for d in dates) or dates[0] == dates[-1]:
        return "portal"
    pairs = list(itertools.pairwise(dates))
    if all(a >= b for a, b in pairs):
        return "newest"
    if all(a <= b for a, b in pairs):
        return "oldest"
    return "portal"


def _counts(info: MatterInfo) -> dict[str, int]:
    return dict(info.counts)


def _readme(info: MatterInfo, provider: Provider, doc_type: str, files: list[DownloadedFile]) -> str:
    """The ZIP's README. Its ordering words describe the files in the ZIP (and MANIFEST.csv), in
    the order they were packaged: by their own dates, not the listing they came from."""
    order = _order([f.ref for f in files])
    arranged = {"newest": ", newest first", "oldest": ", oldest first"}.get(order, ", in the order the portal lists them")
    if len(files) == 1:
        arranged = ""
    total = info.counts.get(doc_type, 0)
    return (
        f"{info.matter}: {info.title}\n"
        f"Source: {provider.display_name}, public documents database\n"
        f"{info.portal_url}\n\n"
        f"Category: {doc_type} ({len(files)} of {total} {'document' if total == 1 else 'documents'}{arranged})\n"
        f"Retrieved: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC\n"
        f"MANIFEST.csv lists each file's portal id, title, filing date and SHA-256.\n"
    )
