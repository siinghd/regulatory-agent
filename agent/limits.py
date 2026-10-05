"""Redis-backed rate limits, daily budgets and locks (work across worker processes and hosts).

Redis keys never hold a raw address: a sender, domain or client IP appears only as
`audit.limit_key` (an HMAC) of its normalised form, see `sender_key`, `domain_key`, `ip_key`.
"""

import asyncio
import ipaddress
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from agent import audit, metrics
from agent.config import Settings, get_settings
from agent.mail.auth import organizational_domain, to_ascii_domain

log = structlog.get_logger()

# ---------------------------------------------------------------- normalised, pseudonymous keys

GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})  # one mailbox: dots in the local part are ignored


def normalise_sender(addr: str) -> str:
    """The mailbox behind an address, for rate limits: lowercase, without a +tag, Gmail's ignored
    dots removed (googlemail.com is gmail.com), the domain as its A-label. Alice+1@Gmail.com,
    a.l.i.c.e+2@gmail.com and alice@googlemail.com are one sender."""
    local, at, domain = addr.strip().rpartition("@")
    if not at:
        return addr.strip().lower()
    domain = to_ascii_domain(domain) or domain.lower()
    local = local.lower()
    base = local.split("+", 1)[0] or local  # "+tag@x" has no base: keep it whole
    if domain in GMAIL_DOMAINS:
        base = base.replace(".", "") or base
        domain = "gmail.com"
    return f"{base}@{domain}"


def org_domain(addr_or_domain: str) -> str:
    """Organizational domain (Public Suffix List, as DMARC uses) of an address's or a domain's A-label."""
    domain = addr_or_domain.strip().rpartition("@")[2]
    return organizational_domain(to_ascii_domain(domain) or domain.lower())


def ip_bucket(ip: str) -> str:
    """An IPv4 address, or the /64 an IPv6 address is in (one host can rotate through all of it)."""
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return ip.strip().lower()[:64]
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{addr}/64", strict=False))
    return str(addr)


def sender_key(addr: str) -> str:
    return audit.limit_key("sender", normalise_sender(addr))


def domain_key(addr_or_domain: str) -> str:
    return audit.limit_key("domain", org_domain(addr_or_domain))


def ip_key(ip: str) -> str:
    return audit.limit_key("ip", ip_bucket(ip))

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end
"""

_EXTEND = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end
"""

# Sliding-window counter: one sorted set per key, members are unique hits scored by time.
_HIT = """
redis.call('zremrangebyscore', KEYS[1], 0, ARGV[1] - ARGV[2])
if redis.call('zscore', KEYS[1], ARGV[4]) then
  -- already counted (retry of the same request): report the current position, don't add
  return redis.call('zcount', KEYS[1], 0, redis.call('zscore', KEYS[1], ARGV[4]))
end
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
        self._extend = redis.register_script(_EXTEND)

    async def hit(self, key: str, *, limit: int, window_s: int, member: str | None = None) -> tuple[bool, int]:
        """Record one hit. Returns (allowed, count including this hit).

        Pass a stable `member` (e.g. the request id) so a retried job doesn't count twice.
        """
        now_ms = int(time.time() * 1000)
        member = member or f"{now_ms}:{secrets.token_hex(4)}"
        n = int(await self._hit(keys=[f"rl:{key}"], args=[now_ms, window_s * 1000, limit, member]))
        return n <= limit, n

    async def decide(self, limiter: str, key: str, *, limit: int, window_s: int, member: str | None = None) -> bool:
        """`hit`, counted in the limiter_decisions metric as `limiter` allowed/limited."""
        ok, _ = await self.hit(key, limit=limit, window_s=window_s, member=member)
        metrics.observe_limiter(limiter, "allowed" if ok else "limited")
        return ok

    @asynccontextmanager
    async def lock(self, name: str, *, ttl_s: float = 120, wait_s: float = 300, poll_s: float = 0.2):
        """Mutex. Raises LockTimeout after `wait_s`.

        The TTL only bounds how long a *crashed* holder can wedge everyone: while the holder runs,
        a background task extends it every ttl/3, so a slow holder never silently loses it.
        """
        key, token = f"lock:{name}", secrets.token_hex(16)
        ttl_ms = max(int(ttl_s * 1000), 1)
        deadline = time.monotonic() + wait_s
        while not await self.r.set(key, token, nx=True, px=ttl_ms):
            if time.monotonic() > deadline:
                raise LockTimeout(name)
            await asyncio.sleep(poll_s)
        renewer = asyncio.create_task(self._keep(key, token, ttl_ms))
        try:
            yield
        finally:
            renewer.cancel()  # not awaited: a cancelled renewal can only be a no-op extend
            await self._release(keys=[key], args=[token])

    async def _keep(self, key: str, token: str, ttl_ms: int) -> None:
        interval = max(ttl_ms / 3000, 0.05)
        while True:
            await asyncio.sleep(interval)
            try:
                held = await self._extend(keys=[key], args=[token, ttl_ms])
            except (RedisError, OSError) as e:  # keep trying: the TTL still has 2/3 to run
                log.warning("lock.renew_failed", lock=key, error=f"{type(e).__name__}: {e}")
                continue
            if not held:
                log.error("lock.lost", lock=key)  # expired while Redis was unreachable
                return

    async def once(self, key: str, ttl_s: int) -> bool:
        """True the first time `key` is seen within ttl (e.g. 'sent a rate-limit notice')."""
        return bool(await self.r.set(f"once:{key}", 1, nx=True, ex=ttl_s))


# ---------------------------------------------------------------- daily budgets

DAY_S = 24 * 3600
BUDGET_TTL_S = 2 * DAY_S  # a day's counters outlive it a little, for late readers

# KEYS: the day's counter, this request's marker. ARGV: amount, ttl. Adds `amount` once per marker.
_ADD_ONCE = """
if redis.call('get', KEYS[2]) then return tonumber(redis.call('get', KEYS[1]) or '0') end
redis.call('set', KEYS[2], ARGV[1], 'EX', ARGV[2])
local n = redis.call('incrby', KEYS[1], ARGV[1])
redis.call('expire', KEYS[1], ARGV[2])
return n
"""


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def seconds_to_utc_midnight(now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


class Budgets:
    """Daily (UTC) budgets: LLM spend, portal visits per provider, delivered bytes per sender.

    One counter per budget and day, so each resets at midnight UTC by itself. Settings are read
    at each call unless given (so a changed limit applies at once).
    """

    def __init__(self, redis: Redis, settings: Settings | None = None, *, prefix: str = "budget"):
        self.r = redis
        self._settings = settings
        self.prefix = prefix
        self._add_once = None  # registered on first use: constructing this never touches Redis

    @property
    def s(self) -> Settings:
        return self._settings or get_settings()

    def _key(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts, utc_day()))

    async def _exhausted(self, budget: str, **facts) -> None:
        """Once per budget and day: an alerting ERROR, the metric and an audit event."""
        day = utc_day()
        if not await self.r.set(f"once:{self.prefix}-exhausted:{budget}:{day}", 1, nx=True, ex=BUDGET_TTL_S):
            return
        log.error("budget.exhausted", alert=True, budget=budget, day=day, **facts)
        metrics.BUDGET_EXHAUSTED.labels(budget=budget).inc()
        try:
            await audit.write(None, "budget_exhausted", {"budget": budget, "day": day, **facts})
        except Exception as e:  # noqa: BLE001 - the alert is in the log either way
            log.warning("budget.event_lost", budget=budget, error=f"{type(e).__name__}: {e}")

    # -------------------------------------------------------- LLM spend (US dollars)

    async def add_llm_cost(self, usd: float) -> float:
        """Add one LLM call's cost (as OpenRouter reports it); returns today's total."""
        key = self._key("llm")
        total = await self.r.incrby(key, round(usd * 1_000_000))  # micro-dollars: an integer counter
        await self.r.expire(key, BUDGET_TTL_S)
        spent = total / 1_000_000
        metrics.BUDGET_USED.labels(budget="llm_usd").set(spent)
        if spent >= self.s.llm_daily_budget_usd:
            await self._exhausted("llm_usd", spent_usd=round(spent, 4), limit_usd=self.s.llm_daily_budget_usd)
        return spent

    async def llm_spent(self) -> float:
        return int(await self.r.get(self._key("llm")) or 0) / 1_000_000

    async def llm_exhausted(self) -> bool:
        """Today's LLM spend has reached llm_daily_budget_usd: no more LLM calls until midnight UTC."""
        spent, limit = await self.llm_spent(), self.s.llm_daily_budget_usd
        if spent < limit:
            metrics.observe_limiter("llm_budget", "allowed")
            return False
        metrics.observe_limiter("llm_budget", "limited")
        await self._exhausted("llm_usd", spent_usd=round(spent, 4), limit_usd=limit)
        return True

    # -------------------------------------------------------- portal visits

    async def portal_visits(self, provider: str) -> int:
        return int(await self.r.get(self._key("portal", provider)) or 0)

    async def portal_open(self, provider: str) -> bool:
        """Is a visit to `provider`'s portal left today? A check only: `count_portal_visit` spends one."""
        limit = self.s.portal_daily_visits.get(provider)
        if limit is None:
            return True
        visits = await self.portal_visits(provider)
        if visits < limit:
            return True
        metrics.observe_limiter(f"portal_budget:{provider}", "deferred")
        await self._exhausted(f"portal:{provider}", visits=visits, limit=limit)
        return False

    async def count_portal_visit(self, provider: str) -> int:
        key = self._key("portal", provider)
        n = await self.r.incr(key)
        await self.r.expire(key, BUDGET_TTL_S)
        metrics.observe_limiter(f"portal_budget:{provider}", "allowed")
        metrics.observe_portal_visit(provider)
        return int(n)

    # -------------------------------------------------------- delivered bytes per sender

    async def bytes_left(self, sender: str, request_id: UUID) -> int:
        """Bytes `sender` (a `sender_key`) may still be sent today. What this request already
        counted (a retry, a re-render as a link) is still its own to use."""
        used = int(await self.r.get(self._key("bytes", sender)) or 0)
        own = int(await self.r.get(self._key("bytes", sender, str(request_id))) or 0)
        return max(self.s.bytes_per_sender_day - used + own, 0)

    async def add_bytes(self, sender: str, request_id: UUID, size: int) -> int:
        """Count what a request delivered against its sender's day, once per request. Returns the day's total."""
        if self._add_once is None:
            self._add_once = self.r.register_script(_ADD_ONCE)
        keys = [self._key("bytes", sender), self._key("bytes", sender, str(request_id))]
        return int(await self._add_once(keys=keys, args=[max(int(size), 0), BUDGET_TTL_S]))

    # -------------------------------------------------------- metrics

    async def snapshot(self) -> dict[str, tuple[float, float]]:
        """{budget: (used today, limit)} for the budget gauges."""
        out = {"llm_usd": (await self.llm_spent(), float(self.s.llm_daily_budget_usd))}
        for provider, limit in self.s.portal_daily_visits.items():
            out[f"portal:{provider}"] = (float(await self.portal_visits(provider)), float(limit))
        return out


# ---------------------------------------------------------------- hook for agent.audit

_installed: Budgets | None = None


def install(budgets: Budgets | None) -> None:
    """Called at worker startup, so every LLM call agent.audit records counts against the day's budget."""
    global _installed
    _installed = budgets


async def record_llm_cost(usd: float) -> None:
    """Never raises: counting a call's cost must not fail the request it belongs to."""
    if _installed is None or not usd > 0:
        return
    try:
        await _installed.add_llm_cost(usd)
    except (RedisError, OSError) as e:
        log.warning("budget.llm_cost_lost", usd=usd, error=f"{type(e).__name__}: {e}")
