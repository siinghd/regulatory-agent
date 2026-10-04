"""Redis-backed rate limits and locks (work across worker processes and hosts)."""

import asyncio
import secrets
import time
from contextlib import asynccontextmanager

from redis.asyncio import Redis

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""

# Sliding-window counter: one sorted set per key, members are unique hits scored by time.
_HIT = """
redis.call('zremrangebyscore', KEYS[1], 0, ARGV[1] - ARGV[2])
local n = redis.call('zcard', KEYS[1])
if n >= tonumber(ARGV[3]) then return n + 1 end
redis.call('zadd', KEYS[1], ARGV[1], ARGV[4])
redis.call('pexpire', KEYS[1], ARGV[2])
return n + 1
"""


class LockTimeout(Exception):
    pass


class Limits:
    def __init__(self, redis: Redis):
        self.r = redis
        self._hit = redis.register_script(_HIT)
        self._release = redis.register_script(_RELEASE)

    async def hit(self, key: str, *, limit: int, window_s: int) -> tuple[bool, int]:
        """Record one hit. Returns (allowed, count including this hit)."""
        now_ms = int(time.time() * 1000)
        n = int(await self._hit(keys=[f"rl:{key}"], args=[now_ms, window_s * 1000, limit, f"{now_ms}:{secrets.token_hex(4)}"]))
        return n <= limit, n

    @asynccontextmanager
    async def lock(self, name: str, *, ttl_s: int = 120, wait_s: float = 300, poll_s: float = 0.2):
        """Mutex with a TTL so a crashed holder can't wedge everyone. Raises LockTimeout."""
        key, token = f"lock:{name}", secrets.token_hex(16)
        deadline = time.monotonic() + wait_s
        while not await self.r.set(key, token, nx=True, ex=ttl_s):
            if time.monotonic() > deadline:
                raise LockTimeout(name)
            await asyncio.sleep(poll_s)
        try:
            yield
        finally:
            await self._release(keys=[key], args=[token])

    async def once(self, key: str, ttl_s: int) -> bool:
        """True the first time `key` is seen within ttl (e.g. 'sent a rate-limit notice')."""
        return bool(await self.r.set(f"once:{key}", 1, nx=True, ex=ttl_s))
