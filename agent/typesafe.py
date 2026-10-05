"""TypeSafe System One (Jev) client: typed questions in, typed answers out.

One call = one POST to /v1/systemone with a `state` and a map of questions (Choice, Noul, Score).
Every answer comes back under its question id and is constrained to the options we sent, so
callers never parse prose. Questions in one call are evaluated in parallel against the same
state: ask everything the caller might need in one request (docs.typesafe.ai/primitives).

Rules every caller gets for free:
- a hard deadline (asyncio.timeout) covers connect, upload, the waits between retries and the body;
- 429, 529, 5xx and transport errors are retried, at most MAX_ATTEMPTS times: a Retry-After up to
  RETRY_MAX_WAIT_S is honoured, otherwise a short exponential backoff with jitter; the waits of one
  call add up to at most RETRY_WAIT_BUDGET_S and never run past the deadline;
- one circuit breaker ("typesafe", agent.breaker) per call: while it is open the call fails at once,
  without a request;
- the response is checked against the questions asked: a missing answer, an option we didn't offer
  or a probability outside [0, 1] is an error, not an answer.
Any failure raises TypeSafeUnavailable; callers fall back (another classifier, or fail closed).

The state is data, not instructions: Jev only ever answers the questions we wrote, but adversarial
text in the state can still move an answer (docs: jev-1.13 jaggedness), so callers keep their
own guards (closed candidate sets, code-side checks).

Settings (agent.config, from the environment): TYPESAFE_API_KEY, TYPESAFE_BASE_URL, TYPESAFE_MODEL
(pinned), TYPESAFE_DEADLINE_S, TYPESAFE_CONNECT_TIMEOUT_S. TypeSafe is not zero-retention on our plan
and is outside `llm_zero_data_retention` (OpenRouter only): an owner decision, disclosed in the
privacy notice; every meta says `zdr: False`.
"""

import asyncio
import math
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from agent import breaker, metrics
from agent.config import get_settings
from agent.http_util import retry_after_s

log = structlog.get_logger()

BREAKER = "typesafe"
# docs.typesafe.ai/models (jev-1.13): $0.042 per million input tokens; output tokens are free.
# The API returns token counts, not prices, so `cost` in the meta is computed from this.
PRICE_PER_MTOK_INPUT_USD = 0.042

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
MAX_ATTEMPTS = 3
# A Retry-After is waited out only when it is at most this long...
RETRY_MAX_WAIT_S = 2.0
# ...and the waits of one call add up to at most this.
RETRY_WAIT_BUDGET_S = 3.0
BACKOFF_BASE_S = 0.25

MAX_CHOICE_OPTIONS = 255  # API limits (docs.typesafe.ai/api)
MAX_SCORE_LEVELS = 10

Entry = str | Mapping[str, Any] | Sequence[Any]  # instructions and criteria accept JSON structure


# ---------------------------------------------------------------- questions


@dataclass(frozen=True)
class Choice:
    """Pick one option. `criteria` maps each option to its description (None: the name says it)."""

    instructions: Entry
    criteria: Mapping[str, Entry | None]

    def payload(self) -> dict[str, Any]:
        if not 2 <= len(self.criteria) <= MAX_CHOICE_OPTIONS:
            raise ValueError(f"a Choice needs 2-{MAX_CHOICE_OPTIONS} options, got {len(self.criteria)}")
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.criteria)}


@dataclass(frozen=True)
class Noul:
    """Probability that a yes/no question is answered yes. `true`/`false` optionally say what each means."""

    instructions: Entry
    true: Entry | None = None
    false: Entry | None = None

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "noul", "instructions": self.instructions}
        if self.true is not None or self.false is not None:
            out["criteria"] = {k: v for k, v in (("true", self.true), ("false", self.false)) if v is not None}
        return out


@dataclass(frozen=True)
class Score:
    """A position along ordered levels (lowest first)."""

    instructions: Entry
    levels: Sequence[Entry]

    def payload(self) -> dict[str, Any]:
        if not 2 <= len(self.levels) <= MAX_SCORE_LEVELS:
            raise ValueError(f"a Score needs 2-{MAX_SCORE_LEVELS} levels, got {len(self.levels)}")
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.levels)}


Question = Choice | Noul | Score


# ---------------------------------------------------------------- answers


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float  # TypeSafe's concentration measure: 0 = even spread, 1 = all on one option

    def p(self, option: str) -> float:
        return self.probabilities.get(option, 0.0)


@dataclass(frozen=True)
class NoulAnswer:
    noul: float  # P(yes)


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: Mapping[int, float]
    legend: Mapping[int, str]
    confidence: float


Answer = ChoiceAnswer | NoulAnswer | ScoreAnswer


@dataclass(frozen=True)
class Result:
    answers: Mapping[str, Answer]
    meta: dict[str, Any] = field(default_factory=dict)

    def choice(self, qid: str) -> ChoiceAnswer:
        answer = self.answers[qid]
        if not isinstance(answer, ChoiceAnswer):
            raise TypeError(f"{qid} is a {type(answer).__name__}, not a Choice answer")
        return answer

    def noul(self, qid: str) -> float:
        answer = self.answers[qid]
        if not isinstance(answer, NoulAnswer):
            raise TypeError(f"{qid} is a {type(answer).__name__}, not a Noul answer")
        return answer.noul

    def score(self, qid: str) -> ScoreAnswer:
        answer = self.answers[qid]
        if not isinstance(answer, ScoreAnswer):
            raise TypeError(f"{qid} is a {type(answer).__name__}, not a Score answer")
        return answer


# ---------------------------------------------------------------- errors


class TypeSafeUnavailable(Exception):
    """No usable answer (down, rate limited, timed out, circuit open, malformed reply): fall back."""


class _HTTPStatus(TypeSafeUnavailable):
    """An error status; `status` lets the breaker tell busy (429/5xx) from a bad request."""

    def __init__(self, message: str, status: int, retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


class _BadResponse(TypeSafeUnavailable):
    """The service answered, but not with answers to our questions (does not count against the breaker)."""


# ---------------------------------------------------------------- transport

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = httpx.AsyncClient(
            base_url=s.typesafe_base_url,
            timeout=httpx.Timeout(s.typesafe_deadline_s, connect=s.typesafe_connect_timeout_s),
            headers={"Authorization": f"Bearer {s.typesafe_api_key.get_secret_value()}"},
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def ask(
    state: Any,
    questions: Mapping[str, Question],
    *,
    purpose: str,
    model: str | None = None,
    deadline_s: float | None = None,
) -> Result:
    """Evaluate `questions` against `state` in one request. Raises TypeSafeUnavailable.

    meta (for logs and the audit trail): model (as served), model_requested, provider, purpose,
    latency_ms, input_tokens, output_tokens, cost and cost_total (USD, from the list price),
    attempts, questions, typesafe_request_id, zdr.
    """
    if not questions:
        raise ValueError("no questions")
    s = get_settings()
    model = model or s.typesafe_model
    deadline = deadline_s or s.typesafe_deadline_s
    payload = {"state": state, "model": model, "questions": {q: v.payload() for q, v in questions.items()}}

    guard = breaker.get(BREAKER)
    try:
        probe = await guard.before_call() if guard else False
    except breaker.Open as e:
        log.warning("typesafe.skipped", purpose=purpose, reason="circuit_open")
        raise TypeSafeUnavailable(str(e)) from e

    t0 = time.perf_counter()
    attempts = 0
    try:
        try:
            async with asyncio.timeout(deadline):
                body, response, attempts = await _post_with_retries(payload, purpose)
        except TimeoutError as e:
            raise TimeoutError(f"no complete answer within {deadline:g}s") from e
        answers = _parse_answers(body, questions)
    except (TypeSafeUnavailable, TimeoutError, httpx.HTTPError) as e:
        metrics.observe_model_call("jev", model, _outcome(e), time.perf_counter() - t0)
        if guard:
            await guard.record(e, probe=probe)
        log.warning("typesafe.failed", purpose=purpose, error=f"{type(e).__name__}: {str(e)[:200]}")
        if isinstance(e, TypeSafeUnavailable):
            raise
        raise TypeSafeUnavailable(f"{type(e).__name__}: {e}") from e
    except BaseException:  # cancelled: the probe learned nothing, let another caller probe
        metrics.observe_model_call("jev", model, "error", time.perf_counter() - t0)
        if probe and guard:
            await guard.release_probe()
        raise
    metrics.observe_model_call("jev", model, "ok", time.perf_counter() - t0)
    if guard:
        await guard.record(None, probe=probe)

    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    input_tokens = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None
    cost = round(input_tokens * PRICE_PER_MTOK_INPUT_USD / 1e6, 8) if input_tokens is not None else None
    meta = {
        "model": body.get("model") or model,
        "model_requested": model,
        "provider": "typesafe",
        "purpose": purpose,
        "latency_ms": round((time.perf_counter() - t0) * 1000),
        "input_tokens": input_tokens,
        "output_tokens": usage.get("output_tokens"),
        "cost": cost,
        "cost_total": cost,  # billed per input token; failed attempts are not billed answers
        "attempts": attempts,
        "questions": len(questions),
        # not "request_id": log lines carry ours under that name
        "typesafe_request_id": response.headers.get("x-typesafe-request-id"),
        # Not trained on customer data; zero data retention only on enterprise plans (docs: models).
        "zdr": False,
    }
    log.info("typesafe.ok", **meta)
    return Result(answers=answers, meta=meta)


async def _post_with_retries(payload: dict[str, Any], purpose: str) -> tuple[dict[str, Any], httpx.Response, int]:
    wait_budget = RETRY_WAIT_BUDGET_S
    attempt = 0
    while True:
        attempt += 1
        try:
            response = await _http().post("/v1/systemone", json=payload)
            if response.status_code >= 400:
                raise _HTTPStatus(
                    f"HTTP {response.status_code} {response.text[:200]}", response.status_code, retry_after_s(response)
                )
            try:
                body = response.json()
            except ValueError as e:
                raise _BadResponse(f"response is not JSON: {response.text[:200]}") from e
            if not isinstance(body, dict):
                raise _BadResponse("response is not a JSON object")
            return body, response, attempt
        except (_HTTPStatus, httpx.TransportError) as e:
            retryable = isinstance(e, httpx.TransportError) or e.status in RETRY_STATUSES
            if not retryable or attempt >= MAX_ATTEMPTS:
                raise
            retry_after = getattr(e, "retry_after_s", None)
            if retry_after is not None and retry_after > RETRY_MAX_WAIT_S:
                raise  # the service asks for longer than we will hold a request for
            wait = retry_after if retry_after is not None else random.uniform(0, BACKOFF_BASE_S * 2 ** (attempt - 1))
            if wait > wait_budget:
                raise
            wait_budget -= wait
            log.info("typesafe.retry", purpose=purpose, attempt=attempt, wait_s=round(wait, 3),
                     error=f"{type(e).__name__}: {str(e)[:120]}")
            await asyncio.sleep(wait)


def _outcome(exc: Exception) -> str:
    """model_calls_total's outcome for a failed call: timeout, refused (a 4xx such as 429), else error."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    return metrics.model_outcome(exc.status if isinstance(exc, _HTTPStatus) else None)


def _parse_answers(body: dict[str, Any], questions: Mapping[str, Question]) -> dict[str, Answer]:
    raw = body.get("answers")
    if not isinstance(raw, dict):
        raise _BadResponse("no answers in the response")
    out: dict[str, Answer] = {}
    for qid, question in questions.items():
        a = raw.get(qid)
        if not isinstance(a, dict):
            raise _BadResponse(f"no answer for {qid}")
        try:
            out[qid] = _answer(a, question)
        except (KeyError, TypeError, ValueError) as e:
            raise _BadResponse(f"answer for {qid} is malformed: {type(e).__name__}: {str(e)[:120]}") from e
    return out


def _prob(x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not -1e-6 <= x <= 1 + 1e-6:
        raise ValueError(f"not a probability: {x!r}")
    return min(max(float(x), 0.0), 1.0)


def _answer(a: dict[str, Any], question: Question) -> Answer:
    if isinstance(question, Noul):
        if a.get("type") != "noul":
            raise TypeError(f"expected a noul answer, got {a.get('type')!r}")
        return NoulAnswer(noul=_prob(a["noul"]))
    if isinstance(question, Choice):
        if a.get("type") != "choice":
            raise TypeError(f"expected a choice answer, got {a.get('type')!r}")
        options = set(question.criteria)
        probabilities = {str(k): _prob(v) for k, v in a["probabilities"].items()}
        choice = a["choice"]
        if choice not in options or not set(probabilities) <= options:
            raise ValueError(f"option outside the criteria: {choice!r}")
        return ChoiceAnswer(choice=choice, probabilities=probabilities, confidence=_prob(a["confidence"]))
    if a.get("type") != "score":
        raise TypeError(f"expected a score answer, got {a.get('type')!r}")
    levels = range(len(question.levels))
    probabilities = {int(k): _prob(v) for k, v in a["probabilities"].items()}
    if not set(probabilities) <= set(levels):
        raise ValueError("level outside the criteria")
    score = float(a["score"])
    if not 0 <= score <= len(question.levels) - 1 + 1e-6:
        raise ValueError(f"score out of range: {score}")
    legend = {int(k): str(v) for k, v in (a.get("legend") or {}).items()}
    return ScoreAnswer(score=score, probabilities=probabilities, legend=legend, confidence=_prob(a["confidence"]))
