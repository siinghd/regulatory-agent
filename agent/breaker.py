"""Circuit breakers per dependency (uarb, oeb, ferc, drop, smtp, openrouter:<model>), kept in Redis
so every worker process shares one view of what is down.

closed --`breaker_failures` consecutive availability failures--> open
open --interval elapses--> half-open: exactly one caller (the probe, SET NX EX 120) goes through
probe succeeds -> closed; probe fails -> open again, interval doubled up to `breaker_open_cap_s`

Intervals get +/-20% jitter so breakers opened together don't probe together. Only availability
failures count (timeouts, connection errors, 5xx, 429): "no such matter" or a page we couldn't
parse means the dependency answered. While a breaker is open, callers get `Open` immediately
and the pipeline parks the request without using one of its attempts.

If Redis itself is unreachable the breaker fails open (calls go through): it is an optimisation,
never a reason to stop work.
"""

import random
import secrets
import time
from contextlib import asynccontextmanager

import aiosmtplib
import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent.config import Settings
from agent.models import AgentError, DeliveryFailed, MatterNotFound, ProviderRejected, ScrapeError, TooLarge

log = structlog.get_logger()

PROBE_TTL_S = 120
STATE_TTL_S = 24 * 3600

# KEYS: state hash, probe key. ARGV: now, threshold, open_s, cap_s, jitter factor, is_probe ("1"/"0")
_FAILURE = """
local fails = redis.call('hincrby', KEYS[1], 'fails', 1)
redis.call('expire', KEYS[1], tonumber(ARGV[7]))
local open_until = tonumber(redis.call('hget', KEYS[1], 'open_until') or '0')
local interval = tonumber(redis.call('hget', KEYS[1], 'interval') or '0')
local probe = ARGV[6] == '1'
if probe or (open_until == 0 and fails >= tonumber(ARGV[2])) then
  if probe and interval > 0 then
    interval = math.min(interval * 2, tonumber(ARGV[4]))
  else
    interval = tonumber(ARGV[3])
  end
  redis.call('hset', KEYS[1], 'open_until', tonumber(ARGV[1]) + interval * tonumber(ARGV[5]), 'interval', interval)
  redis.call('del', KEYS[2])
  return 1
end
return 0
"""


class Open(Exception):
    """The dependency's breaker is open (or another caller holds the half-open probe)."""

    def __init__(self, dependency: str, retry_in_s: float):
        super().__init__(f"{dependency} circuit open, next probe in {retry_in_s:.0f}s")
        self.dependency = dependency
        self.retry_in_s = max(retry_in_s, 0.0)


def is_availability_failure(exc: BaseException) -> bool:
    """Did the dependency fail to answer (as opposed to answering 'no')?"""
    if isinstance(exc, (MatterNotFound, ScrapeError, ProviderRejected, TooLarge, DeliveryFailed)):
        return False
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        return status >= 500 or status in (408, 429)
    if isinstance(exc, aiosmtplib.SMTPResponseException):
        return 400 <= exc.code < 500  # 421/450/451: the server is busy; 5xx is about this message
    if isinstance(exc, aiosmtplib.SMTPException):
        return True  # disconnected, timed out, couldn't connect
    if isinstance(exc, AgentError):
        return exc.retryable
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, httpx.TransportError))


class Breaker:
    def __init__(self, redis: Redis, name: str, *, failures: int, open_s: float, open_cap_s: float):
        self.r = redis
        self.name = name
        self.failures = failures
        self.open_s = open_s
        self.open_cap_s = open_cap_s
        self._state = f"breaker:{name}"
        self._probe = f"breaker:{name}:probe"
        self._failure = redis.register_script(_FAILURE)

    async def before_call(self) -> bool:
        """Raise `Open` if calls must not go through now. Returns True for the half-open probe."""
        try:
            raw = await self.r.hget(self._state, "open_until")
            open_until = float(raw or 0)
            if not open_until:
                return False
            remaining = open_until - time.time()
            if remaining > 0:
                raise Open(self.name, remaining)
            if await self.r.set(self._probe, secrets.token_hex(8), nx=True, ex=PROBE_TTL_S):
                log.info("breaker.probe", dependency=self.name)
                return True
        except (RedisError, OSError) as e:
            log.warning("breaker.unavailable", dependency=self.name, error=f"{type(e).__name__}: {e}")
            return False
        raise Open(self.name, 0.0)  # half-open and someone else is probing

    async def record(self, exc: BaseException | None, *, probe: bool = False) -> None:
        try:
            if exc is not None and is_availability_failure(exc):
                opened = await self._failure(
                    keys=[self._state, self._probe],
                    args=[time.time(), self.failures, self.open_s, self.open_cap_s,
                          random.uniform(0.8, 1.2), "1" if probe else "0", STATE_TTL_S],
                )
                if opened:
                    log.error("breaker.open", dependency=self.name, probe=probe,
                              error=f"{type(exc).__name__}: {str(exc)[:200]}")
            else:
                # an answer (even "not found") means the dependency is up: close / reset the count
                if await self.r.delete(self._state, self._probe) and probe:
                    log.info("breaker.closed", dependency=self.name)
        except (RedisError, OSError) as e:
            log.warning("breaker.unavailable", dependency=self.name, error=f"{type(e).__name__}: {e}")

    async def release_probe(self) -> None:
        """The probe was cancelled before it learned anything: let another caller probe."""
        try:
            await self.r.delete(self._probe)
        except (RedisError, OSError):
            pass


class Breakers:
    """One Breaker per dependency name, configured from settings."""

    def __init__(self, redis: Redis, settings: Settings):
        self.r = redis
        self.s = settings
        self._by_name: dict[str, Breaker] = {}

    def get(self, name: str) -> Breaker:
        if name not in self._by_name:
            self._by_name[name] = Breaker(
                self.r, name, failures=self.s.breaker_failures,
                open_s=self.s.breaker_open_s, open_cap_s=self.s.breaker_open_cap_s,
            )
        return self._by_name[name]

    @asynccontextmanager
    async def call(self, name: str):
        """Guard one call to `name`: raises Open before calling while open, records the outcome.

        AgentErrors that escape get `.dependency = name` unless the raiser set one already.
        """
        b = self.get(name)
        probe = await b.before_call()
        try:
            yield
        except Exception as e:
            if isinstance(e, AgentError) and e.dependency is None:
                e.dependency = name
            await b.record(e, probe=probe)
            raise
        except BaseException:
            if probe:
                await b.release_probe()
            raise
        else:
            await b.record(None, probe=probe)


# ---------------------------------------------------------------- hook for other modules

_installed: Breakers | None = None


def install(breakers: Breakers | None) -> None:
    """Called at worker startup, so modules without a Deps (e.g. agent.llm, for one breaker per
    `openrouter:<model>`) can use the same breakers via `get()`."""
    global _installed
    _installed = breakers


def get(name: str) -> Breaker | None:
    return _installed.get(name) if _installed else None
