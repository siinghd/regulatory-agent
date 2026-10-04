"""Structured-output LLM calls through OpenRouter.

Rules every caller gets for free:
- the response must validate against a pydantic model (json_schema, strict) or we try the next model;
- untrusted text is passed inside a delimited data block, never concatenated into instructions;
- the caller validates *semantics* (e.g. "the matter number must appear in the email") itself:
  schema-valid is not the same as true.
"""

import json
import time
from typing import TypeVar

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from agent.config import get_settings

log = structlog.get_logger()
T = TypeVar("T", bound=BaseModel)

_client: httpx.AsyncClient | None = None


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


async def structured(
    *,
    system: str,
    user: str,
    schema: type[T],
    models: list[str] | None = None,
    max_tokens: int = 1500,
    temperature: float = 0.0,
) -> tuple[T, dict]:
    """Return (parsed, meta). meta = {model, latency_ms, cost, attempts}. Raises LLMUnavailable."""
    s = get_settings()
    errors: list[str] = []
    payload_base = {
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "strict": True, "schema": _strict_schema(schema)},
        },
        "provider": {"require_parameters": True},
        # Extraction needs no chain of thought; reasoning models otherwise burn the budget
        # thinking, or leak the thinking into `content` instead of the JSON.
        "reasoning": {"enabled": False},
        "usage": {"include": True},
    }
    for attempt, model in enumerate(models or s.llm_models, start=1):
        t0 = time.perf_counter()
        try:
            r = await _http().post("/chat/completions", json={**payload_base, "model": model})
            if r.status_code >= 400:
                raise LLMUnavailable(f"{model}: HTTP {r.status_code} {r.text[:200]}")
            body = r.json()
            choice = body["choices"][0]
            content = choice["message"].get("content") or ""
            if not content.strip():
                raise LLMUnavailable(f"{model}: empty content (finish_reason={choice.get('finish_reason')})")
            parsed = schema.model_validate(json.loads(_strip_fences(content)))
            meta = {
                "model": body.get("model", model),
                "latency_ms": round((time.perf_counter() - t0) * 1000),
                "cost": (body.get("usage") or {}).get("cost"),
                "attempts": attempt,
            }
            log.info("llm.ok", schema=schema.__name__, **meta)
            return parsed, meta
        except (httpx.HTTPError, LLMUnavailable, ValidationError, json.JSONDecodeError, KeyError, IndexError) as e:
            errors.append(f"{model}: {type(e).__name__}: {str(e)[:160]}")
            log.warning("llm.fallback", model=model, error=errors[-1])
    raise LLMUnavailable("; ".join(errors))


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
