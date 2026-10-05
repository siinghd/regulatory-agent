"""Per-client-IP token buckets for the viewer, kept in Redis.

The client is Caddy's X-Real-IP (Caddy overwrites whatever the client sent), trusted only when
the connection comes from loopback or uvicorn's proxy-header handling already resolved the same
address; otherwise the socket peer. IPv6 clients share a bucket per /64.

Route classes and their buckets (settings.web_rate_*): /r/{token}.json, /r/{token}, /files/*
(per minute and per hour), /c/*, everything else (/status, /privacy...). /health, /health/deep and
/metrics (loopback only: Prometheus) are exempt.

Keys are HMACs of the client's address under a random key made when the process starts: nothing
in Redis names an IP, and nothing ties one process's keys to the audit log's. Because the key is
per process, buckets are per web process (we run one); several processes would each allow the full
rate.

If Redis is unavailable the limiter fails open: pages keep being served, a warning is logged (at
most once a minute) and Redis is left alone for a few seconds before it is tried again.
"""

import hashlib
import hmac
import ipaddress
import math
import secrets
import time
from dataclasses import dataclass

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.requests import Request

from agent import metrics
from agent.config import Settings
from agent.limits import ip_bucket

log = structlog.get_logger()

EXEMPT = frozenset({"/health", "/health/deep", "/metrics"})
_LOOPBACK = {"127.0.0.1", "::1"}
REDIS_TIMEOUT_S = 0.25  # a slow Redis must not slow every page down
RETRY_REDIS_AFTER_S = 5.0  # after a failure, serve without limits this long before trying Redis again
WARN_EVERY_S = 60.0

# Token bucket, one string per (class, window, client): "<tokens>:<last refill ms>".
# KEYS[1] bucket. ARGV: now ms, capacity, refill per ms, ttl ms. Returns {allowed, wait ms}.
# Only GET/SET (the agent's Redis ACL allows both): no TIME, so the caller supplies the clock.
_BUCKET = """
local now, cap, rate = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local tokens, last = cap, now
local v = redis.call('get', KEYS[1])
if v then
  local sep = string.find(v, ':', 1, true)
  tokens, last = tonumber(string.sub(v, 1, sep - 1)), tonumber(string.sub(v, sep + 1))
end
tokens = math.min(cap, tokens + math.max(0, now - last) * rate)
local allowed, wait = 0, 0
if tokens >= 1 then
  tokens, allowed = tokens - 1, 1
else
  wait = math.ceil((1 - tokens) / rate)
end
redis.call('set', KEYS[1], string.format('%.6f:%d', tokens, now), 'PX', ARGV[4])
return {allowed, wait}
"""


@dataclass(frozen=True)
class Bucket:
    capacity: int  # requests allowed in a burst ...
    window_s: int  # ... and refilled evenly over this window


def route_class(path: str, s: Settings) -> tuple[str, tuple[Bucket, ...]] | None:
    """(class name, its buckets) for a request path; None if exempt."""
    if path in EXEMPT:
        return None
    if path.startswith("/r/"):
        if path.endswith(".json"):
            return "progress_json", (Bucket(s.web_rate_progress_json_per_min, 60),)
        return "progress", (Bucket(s.web_rate_progress_per_min, 60),)
    if path.startswith("/files/"):
        return "files", (Bucket(s.web_rate_files_per_min, 60), Bucket(s.web_rate_files_per_hour, 3600))
    if path.startswith("/c/"):
        return "citation", (Bucket(s.web_rate_citation_per_min, 60),)
    return "default", (Bucket(s.web_rate_default_per_min, 60),)


def _ip(value: str | None) -> str | None:
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """Caddy's X-Real-IP when the connection is Caddy's, else the socket peer."""
    peer = request.client.host if request.client else None
    real = _ip(request.headers.get("x-real-ip"))
    if real and (peer in _LOOPBACK or peer == real):
        return real
    return _ip(peer) or "unknown"


class WebLimiter:
    def __init__(self, redis: Redis | None, *, prefix: str = "web_rl"):
        self.r = redis
        self.prefix = prefix
        self._key = secrets.token_bytes(32)
        self._script = redis.register_script(_BUCKET) if redis is not None else None
        self._down_until = 0.0
        self._warned_at = float("-inf")

    @classmethod
    def from_settings(cls, s: Settings) -> "WebLimiter":
        return cls(Redis.from_url(s.redis_url, socket_timeout=REDIS_TIMEOUT_S, socket_connect_timeout=REDIS_TIMEOUT_S))

    async def aclose(self) -> None:
        if self.r is not None:
            await self.r.aclose()

    def _client_key(self, ip: str) -> str:
        return hmac.new(self._key, ip_bucket(ip).encode(), hashlib.sha256).hexdigest()[:32]

    async def check(self, name: str, buckets: tuple[Bucket, ...], ip: str) -> float | None:
        """None if the request may go ahead, else the seconds to wait (Retry-After)."""
        if self._script is None or time.monotonic() < self._down_until:
            return None
        client = self._client_key(ip)
        now_ms = int(time.time() * 1000)
        wait_ms = 0
        try:
            for b in buckets:
                if b.capacity <= 0:
                    continue  # 0 = no limit for this window
                allowed, wait = await self._script(
                    keys=[f"{self.prefix}:{name}:{b.window_s}:{client}"],
                    args=[now_ms, b.capacity, b.capacity / (b.window_s * 1000), b.window_s * 1000],
                )
                if not int(allowed):
                    wait_ms = max(wait_ms, int(wait))
        except (RedisError, OSError, TimeoutError) as e:
            self._down_until = time.monotonic() + RETRY_REDIS_AFTER_S
            if time.monotonic() - self._warned_at >= WARN_EVERY_S:
                self._warned_at = time.monotonic()
                log.warning("web.rate_limit_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}",
                            effect="serving without rate limits")
            metrics.observe_limiter(f"web_{name}", "unavailable")
            return None
        if wait_ms:
            metrics.observe_limiter(f"web_{name}", "limited")
            return max(math.ceil(wait_ms / 1000), 1)
        metrics.observe_limiter(f"web_{name}", "allowed")
        return None
