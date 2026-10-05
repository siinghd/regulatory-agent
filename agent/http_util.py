"""Small HTTP helpers shared by the model clients (agent.llm, agent.typesafe)."""

import time
from email.utils import parsedate_to_datetime

import httpx


def retry_after_s(r: httpx.Response) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), None if absent or unreadable."""
    raw = r.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None or when.tzinfo is None:
        return None
    return max(0.0, when.timestamp() - time.time())
