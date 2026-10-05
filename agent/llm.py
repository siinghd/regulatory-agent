"""Structured-output LLM calls through OpenRouter.

Rules every caller gets for free:
- the response must validate against a pydantic model (json_schema, strict) or we try the next model;
- untrusted text is passed inside a delimited data block, never concatenated into instructions;
- the caller validates *semantics* (e.g. "the matter number must appear in the email") itself:
  schema-valid is not the same as true.

Per attempt (one model):
- a hard deadline (asyncio.timeout) covers connect, upload, the wait and the whole body. httpx's
  timeout is per read, and OpenRouter keeps slow non-streaming calls alive with whitespace, so on
  its own it never fires;
- a 429 with a short Retry-After is waited out on the same model (within RATE_LIMIT_WAIT_BUDGET_S
  per call); a longer one moves on to the next model at once;
- each model has its own circuit breaker (`openrouter:<model>`, agent.breaker): a model whose
  breaker is open is skipped without a request;
- with `llm_zero_data_retention` the request may only be routed to endpoints that neither store
  nor train on prompts. A model without such an endpoint answers 404 before anything is sent to
  a provider; it is logged and skipped. At startup `check_zero_data_retention` leaves out every
  configured model OpenRouter lists no such endpoint for, and logs the models that remain. With
  none left every call raises LLMUnavailable at once: the gate answers from its rules alone and
  replies go out without a summary.
"""

import asyncio
import json
import time
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from agent import breaker, metrics
from agent.config import get_settings
from agent.http_util import retry_after_s

log = structlog.get_logger()

# A 429 is waited out on the same model only when its Retry-After is at most this long...
RATE_LIMIT_MAX_WAIT_S = 2.0
# ...and the waits of one structured() call add up to at most this.
RATE_LIMIT_WAIT_BUDGET_S = 4.0
_NO_ZDR_ENDPOINT = "data policy"  # in OpenRouter's 404 "No endpoints found matching your data policy"
ZDR_ENDPOINTS_PATH = "/endpoints/zdr"  # OpenRouter's list of zero-data-retention endpoints
ZDR_CHECK_TIMEOUT_S = 15.0

_client: httpx.AsyncClient | None = None
# Configured models the startup check found no zero-data-retention endpoint for: never asked in
# this process while llm_zero_data_retention is on.
_no_zdr: set[str] = set()


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = get_settings()
        _client = httpx.AsyncClient(
            base_url=s.openrouter_base_url,
            timeout=httpx.Timeout(s.llm_timeout_s, connect=10),
            headers={
                "Authorization": f"Bearer {s.openrouter_api_key.get_secret_value()}",
                "HTTP-Referer": s.public_base_url,
                "X-Title": "regulatory-agent",
            },
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _client


class LLMUnavailable(Exception):
    pass


class _HTTPStatus(LLMUnavailable):
    """An error status from OpenRouter; `status` lets the breaker tell busy (429/5xx) from bad request."""

    def __init__(self, message: str, status: int, retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


# One attempt's failures that mean "try the next model". TypeError/AttributeError/KeyError/IndexError
# cover response bodies of an unexpected shape ("choices": null, "message": "text", ...).
_ATTEMPT_ERRORS = (
    httpx.HTTPError, LLMUnavailable, ValidationError, ValueError, KeyError, IndexError, TypeError,
    AttributeError, TimeoutError,
)


def untrusted_block(label: str, text: str, max_chars: int = 12_000) -> str:
    """Wrap attacker-controllable text so instructions inside it read as data.

    Strips control characters and our own delimiter so the block can't be closed early.
    """
    cleaned = "".join(ch for ch in text if ch in "\n\t" or ch >= " ")
    cleaned = cleaned.replace("<<<", "‹‹‹").replace(">>>", "›››")[:max_chars]
    return f"<<<{label}\n{cleaned}\n{label}>>>"


def _strict_schema(model: type[BaseModel]) -> dict:
    schema = model.model_json_schema()

    def fix(node: dict) -> None:
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            node["required"] = list(node.get("properties", {}).keys())
        for v in node.get("properties", {}).values():
            fix(v)
        for key in ("items", "anyOf", "allOf"):
            sub = node.get(key)
            if isinstance(sub, dict):
                fix(sub)
            elif isinstance(sub, list):
                for s in sub:
                    fix(s)
        for d in node.get("$defs", {}).values():
            fix(d)

    fix(schema)
    return schema


def usable_models() -> list[str]:
    """The configured models in fallback order, less those known to have no zero-data-retention
    endpoint while llm_zero_data_retention is on."""
    s = get_settings()
    if not s.llm_zero_data_retention:
        return list(s.llm_models)
    return [m for m in s.llm_models if m not in _no_zdr]


def _exclude(model: str, reason: str) -> None:
    if model in _no_zdr:
        return
    _no_zdr.add(model)
    usable = usable_models()
    log.warning("llm.model_excluded", model=model, reason=reason, usable_models=usable)
    if not usable:
        log.error("llm.no_usable_models", alert=True, configured=get_settings().llm_models,
                  effect="the gate answers from its rules alone; replies go out without a summary")


async def check_zero_data_retention() -> list[str]:
    """Startup check: leave out every configured model OpenRouter lists no zero-data-retention
    endpoint for (GET /endpoints/zdr), log the models that remain, and return them.

    A failed lookup leaves the list as it is: each call still asks OpenRouter for ZDR routing only,
    and a model that has none is left out at its first 404.
    """
    s = get_settings()
    if not s.llm_zero_data_retention:
        log.info("llm.models_usable", usable_models=list(s.llm_models), zdr=False)
        return list(s.llm_models)
    try:
        r = await _http().get(ZDR_ENDPOINTS_PATH, timeout=ZDR_CHECK_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()["data"]
        zdr_models = {e["model_id"] for e in data if isinstance(e, dict) and isinstance(e.get("model_id"), str)}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning("llm.zdr_check_failed", error=f"{type(e).__name__}: {str(e)[:200]}", usable_models=usable_models())
        return usable_models()
    for model in s.llm_models:
        if model not in zdr_models:
            _exclude(model, "OpenRouter lists no zero-data-retention endpoint for it")
    usable = usable_models()
    if usable and _no_zdr:
        log.warning("llm.models_usable", usable_models=usable, excluded=sorted(_no_zdr), zdr=True)
    elif usable:
        log.info("llm.models_usable", usable_models=usable, zdr=True)
    return usable


def provider_preferences() -> dict[str, Any]:
    """OpenRouter `provider` routing object (field names per openrouter.ai/docs provider routing)."""
    prefs: dict[str, Any] = {"require_parameters": True}
    if get_settings().llm_zero_data_retention:
        prefs.update(data_collection="deny", zdr=True)
    return prefs


async def structured[T: BaseModel](
    *,
    system: str,
    user: str,
    schema: type[T],
    models: list[str] | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.0,
    timeout_s: float | None = None,
    purpose: str | None = None,
) -> tuple[T, dict]:
    """Return (parsed, meta). Raises LLMUnavailable when no model gives a valid answer.

    meta (for logs and the audit trail): model (as served), model_requested, provider (the
    upstream OpenRouter routed to), purpose, latency_ms, cost (this answer), cost_total (every
    attempt, failed ones included), attempts, prompt_tokens, completion_tokens, zdr.
    `timeout_s` is a hard deadline per attempt (default `llm_timeout_s`).
    """
    s = get_settings()
    purpose = purpose or schema.__name__
    deadline_s = timeout_s or s.llm_timeout_s
    errors: list[str] = []
    wait_budget = RATE_LIMIT_WAIT_BUDGET_S
    cost_total = 0.0
    payload_base = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "strict": True, "schema": _strict_schema(schema)},
        },
        "provider": provider_preferences(),
        # Extraction needs no chain of thought; reasoning models otherwise burn the budget
        # thinking, or leak the thinking into `content` instead of the JSON.
        "reasoning": {"enabled": False},
        "usage": {"include": True},
    }
    attempt = 0
    candidates = models or usable_models()
    if not candidates:
        raise LLMUnavailable("no usable models: none of the configured models has a zero-data-retention endpoint")
    for model in candidates:
        guard = breaker.get(f"openrouter:{model}")
        while True:  # once, or again after waiting out a short 429
            try:
                probe = await guard.before_call() if guard else False
            except breaker.Open as e:
                errors.append(f"{model}: {e}")
                log.warning("llm.skipped", model=model, purpose=purpose, reason="circuit_open")
                break
            attempt += 1
            t0 = time.perf_counter()
            try:
                try:
                    async with asyncio.timeout(deadline_s):
                        body = await _post({**payload_base, "model": model}, deadline_s)
                except TimeoutError as e:
                    raise TimeoutError(f"no complete answer within {deadline_s:g}s") from e
                cost_total += _cost(body)
                parsed = schema.model_validate(json.loads(_strip_fences(_content(model, body))))
            except _ATTEMPT_ERRORS as e:
                metrics.observe_model_call("llm", model, _outcome(e), time.perf_counter() - t0)
                if guard:
                    await guard.record(_availability(e), probe=probe)
                errors.append(f"{model}: {type(e).__name__}: {str(e)[:160]}")
                retry_after = getattr(e, "retry_after_s", None)
                if (
                    getattr(e, "status", None) == 429
                    and retry_after is not None
                    and retry_after <= min(RATE_LIMIT_MAX_WAIT_S, wait_budget)
                ):
                    wait_budget -= retry_after
                    log.info("llm.rate_limited_wait", model=model, purpose=purpose, wait_s=retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                if isinstance(e, _HTTPStatus) and e.status == 404 and _NO_ZDR_ENDPOINT in str(e):
                    log.warning("llm.no_zdr_endpoint", model=model, purpose=purpose)
                else:
                    log.warning("llm.fallback", model=model, purpose=purpose, error=errors[-1])
                break
            except BaseException:  # cancelled: the probe learned nothing, let another caller probe
                metrics.observe_model_call("llm", model, "error", time.perf_counter() - t0)
                if probe and guard:
                    await guard.release_probe()
                raise
            metrics.observe_model_call("llm", model, "ok", time.perf_counter() - t0)
            if guard:
                await guard.record(None, probe=probe)
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            meta = {
                "model": body.get("model") or model,
                "model_requested": model,
                "provider": body.get("provider"),
                "purpose": purpose,
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "cost": usage.get("cost"),
                "cost_total": round(cost_total, 8),
                "attempts": attempt,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "zdr": bool(payload_base["provider"].get("zdr")),
            }
            log.info("llm.ok", schema=schema.__name__, **meta)
            return parsed, meta
    raise LLMUnavailable("; ".join(errors) or "no models configured")


async def _post(payload: dict[str, Any], deadline_s: float) -> dict[str, Any]:
    r = await _http().post(
        "/chat/completions", json=payload, timeout=httpx.Timeout(deadline_s, connect=min(10.0, deadline_s))
    )
    model = payload["model"]
    if r.status_code >= 400:
        raise _HTTPStatus(f"{model}: HTTP {r.status_code} {r.text[:200]}", r.status_code, retry_after_s(r))
    body = r.json()
    if not isinstance(body, dict):
        raise LLMUnavailable(f"{model}: response is not a JSON object")
    error = body.get("error")
    if error and not body.get("choices"):
        # OpenRouter can answer 200 with an error object (an upstream failure after the headers).
        code = error.get("code") if isinstance(error, dict) else None
        status = code if isinstance(code, int) else 502
        raise _HTTPStatus(f"{model}: upstream error {str(error)[:200]}", status)
    return body


def _content(model: str, body: dict[str, Any]) -> str:
    choice = body["choices"][0]
    content = choice["message"].get("content") or ""
    if not isinstance(content, str) or not content.strip():
        raise LLMUnavailable(f"{model}: empty content (finish_reason={choice.get('finish_reason')})")
    return content


def _cost(body: dict[str, Any]) -> float:
    usage = body.get("usage")
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return float(cost) if isinstance(cost, (int, float)) else 0.0


def _availability(exc: Exception) -> Exception | None:
    """What the breaker should count: the failure if the model didn't answer, else None (an
    unparseable or schema-invalid answer still means the endpoint is up)."""
    if isinstance(exc, (TimeoutError, httpx.TransportError)):
        return exc
    if isinstance(exc, _HTTPStatus) and (exc.status >= 500 or exc.status in (408, 429)):
        return exc
    return None


def _outcome(exc: Exception) -> str:
    """model_calls_total's outcome for a failed attempt: timeout, refused (a 4xx: rate limited, no
    zero-retention endpoint...), else error (5xx, no answer, an unusable or schema-invalid one)."""
    if isinstance(exc, TimeoutError):
        return "timeout"
    return metrics.model_outcome(exc.status if isinstance(exc, _HTTPStatus) else None)


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
