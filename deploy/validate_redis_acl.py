#!/usr/bin/env python3
"""Exercise everything the agent does with Redis, as the agent's Redis user, and confirm that
what it must never do is refused.

  REDIS_URL=redis://agent:<password>@127.0.0.1:6392/0 python deploy/validate_redis_acl.py

Positive paths use the real libraries and the real agent code:
  arq      create_pool (INFO/DBSIZE), enqueue + duplicate job id, deferred jobs, a worker that
           runs jobs with keep_result 0 and >0, Retry, a cron job, aborting a running job,
           its health-check key, and `arq --check`'s async_check_health
  limits   agent.limits.Limits: sliding-window hit (EVALSHA, SCRIPT LOAD fallback), lock
           (SET NX PX + Lua renew/release, contention -> LockTimeout), once; agent.limits.Budgets
           (INCR/INCRBY/EXPIRE/GET, once-per-request Lua); agent.web.ratelimit token buckets
  breaker  the circuit breakers' hash + Lua commands (HINCRBY, HGET, HSET, EXPIRE, DEL)
  ingest   the heartbeat key (SET ... EX, GET)
Negative paths are probed inside MULTI and then DISCARDed: Redis checks ACLs when a command is
queued, so a wrongly permitted FLUSHALL would only be queued, never executed. Safe to run
against the live Redis: everything uses a private queue name and key prefix and is cleaned up.

Restart durability (the ACL must not make Redis drop AOF-replayed MULTI/EXEC or Lua writes):
  validate_redis_acl.py --persist-write    write marker keys via MULTI/EXEC and via Lua
  docker compose restart redis
  validate_redis_acl.py --persist-check    the markers must have survived the AOF reload

Prints PASS/FAIL lines; exits 1 on any failure. Needs arq + redis (the app image or .venv).
"""

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root: agent.limits

from arq import Retry, create_pool, cron
from arq.connections import RedisSettings
from arq.jobs import Job, JobStatus
from arq.worker import Worker, async_check_health
from redis import exceptions as rexc
from redis.asyncio import Redis

from agent.config import Settings
from agent.limits import Budgets, Limits, LockTimeout, utc_day
from agent.web.ratelimit import Bucket, WebLimiter

RUN = uuid.uuid4().hex[:8]
QUEUE = f"arq:acl-validate:{RUN}"
PREFIX = f"acl-validate:{RUN}"
FORBIDDEN = [
    ["FLUSHALL"], ["FLUSHDB"], ["CONFIG", "GET", "*"], ["CONFIG", "SET", "appendonly", "no"],
    ["DEBUG", "SLEEP", "0"], ["KEYS", "*"], ["SCAN", "0"], ["MONITOR"],
    ["ACL", "LIST"], ["ACL", "SETUSER", "agent", "+@all"], ["BGSAVE"], ["BGREWRITEAOF"],
    ["REPLICAOF", "127.0.0.1", "1"], ["SLAVEOF", "NO", "ONE"], ["MODULE", "LIST"],
    ["SCRIPT", "FLUSH"], ["SCRIPT", "KILL"], ["FUNCTION", "FLUSH"], ["CLIENT", "LIST"],
    ["CLIENT", "KILL", "ID", "1"], ["SWAPDB", "0", "1"], ["MIGRATE", "127.0.0.1", "1", "k", "0", "1"],
    ["OBJECT", "ENCODING", "k"], ["RENAME", "a", "b"], ["SORT", "k"], ["LATENCY", "RESET"],
    ["SLOWLOG", "GET"], ["MEMORY", "DOCTOR"], ["FAILOVER"], ["LASTSAVE"], ["PUBLISH", "c", "m"],
]
# Commands Redis refuses inside MULTI before it checks ACLs, probed directly instead, in a form
# that is harmless even if the ACL were wrong: SHUTDOWN with an unknown flag is a syntax error
# before it does anything, and a SAVE is only a snapshot.
FORBIDDEN_DIRECT = [["SHUTDOWN", "not-a-flag"], ["SAVE"]]

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(f"{'PASS' if ok else 'FAIL'} {what}", flush=True)


# ------------------------------------------------------------------------------- arq jobs
seen: dict[str, int] = {}


async def ok_task(ctx, x):
    return x * 2


async def retry_task(ctx):
    seen["retry"] = ctx["job_try"]
    if ctx["job_try"] < 2:
        raise Retry(defer=0.2)
    return "retried"


async def slow_task(ctx):
    await asyncio.sleep(30)


async def cron_task(ctx):
    seen["cron"] = seen.get("cron", 0) + 1


async def arq_checks(settings: RedisSettings) -> None:
    redis = await create_pool(settings, default_queue_name=QUEUE)  # INFO x3 + DBSIZE + PING
    check(True, "arq create_pool (PING, INFO, DBSIZE)")
    ids = {k: f"{PREFIX}:{k}" for k in ("ok", "keep", "retry", "slow")}
    job = await redis.enqueue_job("ok_task", 21, _job_id=ids["ok"], _queue_name=QUEUE)
    dup = await redis.enqueue_job("ok_task", 21, _job_id=ids["ok"], _queue_name=QUEUE)
    check(job is not None and dup is None, "arq enqueue_job with a job id; duplicate id refused (WATCH/MULTI/EXEC)")
    await redis.enqueue_job("ok_task", 1, _job_id=ids["keep"], _queue_name=QUEUE, _defer_by=0.3)
    await redis.enqueue_job("retry_task", _job_id=ids["retry"], _queue_name=QUEUE)
    check(await Job(ids["ok"], redis, _queue_name=QUEUE).status() == JobStatus.queued, "arq job status (EXISTS/ZSCORE)")

    def worker(**kw) -> Worker:
        return Worker(functions=[ok_task, retry_task, slow_task], redis_pool=redis, queue_name=QUEUE,
                      burst=True, poll_delay=0.05, handle_signals=False, allow_abort_jobs=True,
                      health_check_interval=1, max_tries=3, **kw)

    w1 = worker(keep_result=0, cron_jobs=[cron(cron_task, run_at_startup=True, job_id=f"{PREFIX}:cron")])
    await asyncio.wait_for(w1.main(), 20)
    check(w1.jobs_complete >= 3 and w1.jobs_failed == 0,
          f"arq worker ran jobs, keep_result=0 (complete={w1.jobs_complete} failed={w1.jobs_failed} retried={w1.jobs_retried})")
    check(seen.get("retry") == 2, "arq Retry(defer=...) re-queued and succeeded on try 2 (INCR/EXPIRE retry key)")
    check(seen.get("cron", 0) >= 1, "arq cron job enqueued and ran (run_at_startup)")
    check(await async_check_health(settings, None, QUEUE) == 0, "arq --check health key written and read")
    await w1.close()

    # keep_result > 0 stores the result (PSETEX) and Job.result reads it
    await redis.enqueue_job("ok_task", 5, _job_id=f"{ids['keep']}2", _queue_name=QUEUE)
    w2 = worker(keep_result=30)
    await asyncio.wait_for(w2.main(), 10)
    res = await Job(f"{ids['keep']}2", redis, _queue_name=QUEUE).result(timeout=5)
    check(res == 10, "arq result stored with keep_result>0 and read back")
    await w2.close()

    # abort a running job (allow_abort_jobs=True, as the app's WorkerSettings). keep_result>0
    # here only so Job.abort can read the CancelledError back and report True.
    await redis.enqueue_job("slow_task", _job_id=ids["slow"], _queue_name=QUEUE)
    w3 = worker(keep_result=30)
    runner = asyncio.create_task(w3.main())
    for _ in range(100):
        if await redis.exists(f"arq:in-progress:{ids['slow']}"):
            break
        await asyncio.sleep(0.05)
    aborted = await Job(ids["slow"], redis, _queue_name=QUEUE).abort(timeout=10)
    await asyncio.wait_for(runner, 15)
    check(aborted, "arq abort of a running job (ZADD arq:abort, cancellation)")
    await w3.close()

    # cleanup: only keys this run created
    keys = [QUEUE, f"{QUEUE}:health-check"]
    for jid in [*ids.values(), f"{ids['keep']}2", f"{PREFIX}:cron"]:
        keys += [f"arq:job:{jid}", f"arq:result:{jid}", f"arq:in-progress:{jid}", f"arq:retry:{jid}"]
    await redis.delete(*keys)
    await redis.zrem("arq:abort", *ids.values())
    await redis.aclose()


# ------------------------------------------------------------------------------- agent.limits
async def limits_checks(url: str) -> None:
    r = Redis.from_url(url)
    lim = Limits(r)
    a = await lim.hit(f"{PREFIX}:sender", limit=2, window_s=60, member="m1")
    b = await lim.hit(f"{PREFIX}:sender", limit=2, window_s=60, member="m1")
    c = await lim.hit(f"{PREFIX}:sender", limit=2, window_s=60, member="m2")
    d = await lim.hit(f"{PREFIX}:sender", limit=2, window_s=60, member="m3")
    check(a == (True, 1) and b == (True, 1) and c == (True, 2) and d == (False, 3),
          f"limits.hit sliding window via Lua (EVALSHA/SCRIPT LOAD; ZADD/ZSCORE/ZCOUNT/ZCARD/PEXPIRE): {a} {b} {c} {d}")
    async with lim.lock(f"{PREFIX}:lock", ttl_s=5, wait_s=1):
        try:
            async with lim.lock(f"{PREFIX}:lock", ttl_s=5, wait_s=0.3, poll_s=0.05):
                check(False, "limits.lock is exclusive")
        except LockTimeout:
            check(True, "limits.lock: SET NX EX, contention raises LockTimeout")
    check(await r.get(f"lock:{PREFIX}:lock") is None, "limits.lock released by token-checked Lua (GET/DEL)")
    check(await lim.once(f"{PREFIX}:once", 5) and not await lim.once(f"{PREFIX}:once", 5), "limits.once (SET NX EX)")

    # agent.breaker: the same commands its failure script and probe use, on a private key
    failure = r.register_script(
        "local f = redis.call('hincrby', KEYS[1], 'fails', 1) redis.call('expire', KEYS[1], 60) "
        "local o = tonumber(redis.call('hget', KEYS[1], 'open_until') or '0') "
        "if f >= 2 then redis.call('hset', KEYS[1], 'open_until', ARGV[1], 'interval', 1) redis.call('del', KEYS[2]) end "
        "return f")
    state, probe = f"breaker:{PREFIX}", f"breaker:{PREFIX}:probe"
    fails = [await failure(keys=[state, probe], args=[int(time.time()) + 60]) for _ in range(2)]
    opened = await r.hget(state, "open_until")
    got_probe = await r.set(probe, "x", nx=True, ex=5)
    await r.delete(state, probe)
    check(fails == [1, 2] and opened is not None and got_probe,
          "breaker: HINCRBY/EXPIRE/HGET/HSET/DEL in Lua, HGET, SET NX EX probe, DEL")

    hb = f"ingest:heartbeat:{PREFIX}"  # same commands as ingest:heartbeat, without faking it
    await r.set(hb, f"{time.time():.3f}", ex=900)
    check(time.time() - float(await r.get(hb)) < 5, "ingest heartbeat SET EX / GET")
    await r.delete(f"rl:{PREFIX}:sender", f"once:{PREFIX}:once", hb)

    # agent.limits.Budgets on a private prefix (limits high enough that nothing is "exhausted")
    budgets = Budgets(r, Settings(_env_file=None, llm_daily_budget_usd=1000, portal_daily_visits={"x": 10},
                                  bytes_per_sender_day=100), prefix=f"{PREFIX}:budget")
    spent = await budgets.add_llm_cost(0.25)
    visits = [await budgets.count_portal_visit("x") for _ in range(2)]
    opened = await budgets.portal_open("x")
    added = [await budgets.add_bytes("s1", "r1", 40) for _ in range(2)]  # the second is a no-op
    left = await budgets.bytes_left("s1", "r2")
    check(spent == 0.25 and await budgets.llm_spent() == 0.25 and visits == [1, 2] and opened
          and added == [40, 40] and left == 60,
          f"budgets: INCRBY/INCR/EXPIRE/GET and once-per-request Lua: {spent} {visits} {added} {left}")
    day = utc_day()
    await r.delete(*(f"{PREFIX}:budget:{k}:{day}" for k in ("llm", "portal:x", "bytes:s1", "bytes:s1:r1")))

    # agent.web.ratelimit: the token bucket Lua (GET / SET PX)
    web = WebLimiter(r, prefix=f"{PREFIX}:web")
    waits = [await web.check("x", (Bucket(2, 60),), "198.51.100.1") for _ in range(3)]
    check(waits[:2] == [None, None] and waits[2] is not None and waits[2] >= 1,
          f"web rate limiter token bucket via Lua (EVALSHA; GET/SET PX): {waits}")
    await r.delete(f"{PREFIX}:web:x:60:{web._client_key('198.51.100.1')}")
    await r.aclose()


# ------------------------------------------------------------------------------- forbidden
async def forbidden_checks(url: str) -> None:
    # A raw connection: replies are read without redis-py's per-command parsers ("QUEUED" is
    # not a SCAN reply).
    r = Redis.from_url(url)
    conn = r.connection_pool.make_connection()
    await conn.connect()
    for cmd in FORBIDDEN:
        await conn.send_command("MULTI")
        await conn.read_response()
        await conn.send_command(*cmd)
        try:
            reply = await conn.read_response()
            check(False, f"denied: {' '.join(cmd)} (was {reply!r}; discarded, never executed)")
        except rexc.NoPermissionError:
            check(True, f"denied: {' '.join(cmd)}")
        except rexc.ResponseError as e:
            # DEBUG is refused even earlier, by enable-debug-command (off by default)
            check(cmd[0] == "DEBUG" and "DEBUG command not allowed" in str(e),
                  f"denied: {' '.join(cmd)} ({str(e)[:60]})")
        finally:
            await conn.send_command("DISCARD")
            try:
                await conn.read_response()
            except rexc.ResponseError:
                pass
    for cmd in FORBIDDEN_DIRECT:
        await conn.send_command(*cmd)
        try:
            reply = await conn.read_response()
            check(False, f"denied: {' '.join(cmd)} (was {reply!r})")
        except rexc.NoPermissionError:
            check(True, f"denied: {' '.join(cmd)}")
        except rexc.ResponseError as e:
            check(False, f"denied: {' '.join(cmd)} (unexpected error: {e})")
    await conn.disconnect()
    await r.aclose()

    u = urlsplit(url)
    anon = Redis(host=u.hostname, port=u.port or 6379)
    try:
        await anon.ping()
        check(False, "unauthenticated connection is refused (default user off)")
    except rexc.AuthenticationError:
        check(True, "unauthenticated connection is refused (default user off)")
    try:
        await anon.execute_command("AUTH", "default", "")
        check(False, "AUTH as the default user fails")
    except (rexc.AuthenticationError, rexc.ResponseError):
        check(True, "AUTH as the default user fails")
    await anon.aclose()
    wrong = urlunsplit(u._replace(netloc=f"{u.username}:wrong-password@{u.hostname}:{u.port}"))
    try:
        await Redis.from_url(wrong).ping()
        check(False, "a wrong password is refused")
    except rexc.AuthenticationError:
        check(True, "a wrong password is refused")


MARKERS = ("acl-validate:persist:multi", "acl-validate:persist:lua")


async def persist(url: str, mode: str) -> None:
    r = Redis.from_url(url)
    if mode == "write":
        async with r.pipeline(transaction=True) as tr:  # MULTI/EXEC, as arq's enqueue
            tr.set(MARKERS[0], "1", ex=3600)
            await tr.execute()
        await r.eval("redis.call('hset', KEYS[1], 'f', 1) return redis.call('pexpire', KEYS[1], 3600000)",
                     1, MARKERS[1])  # a script's effects are replicated to the AOF inside MULTI/EXEC
        check(bool(await r.exists(*MARKERS) == 2), "persist markers written (MULTI/EXEC and Lua); now restart redis")
    else:
        n = await r.exists(*MARKERS)
        check(n == 2, f"persist markers survived the restart/AOF reload ({n}/2)")
        await r.delete(*MARKERS)
    await r.aclose()


async def main() -> int:
    url = os.environ.get("REDIS_URL", "")
    if not urlsplit(url).password:
        print("REDIS_URL with credentials is required (redis://agent:<password>@host:port/0)", file=sys.stderr)
        return 2
    if len(sys.argv) > 1 and sys.argv[1] in ("--persist-write", "--persist-check"):
        await persist(url, sys.argv[1].removeprefix("--persist-"))
        return 0 if all(ok for ok, _ in results) else 1
    print(f"validating {urlsplit(url).username}@{urlsplit(url).hostname}:{urlsplit(url).port} queue={QUEUE}")
    await arq_checks(RedisSettings.from_dsn(url))
    await limits_checks(url)
    await forbidden_checks(url)
    failed = [w for ok, w in results if not ok]
    print(f"{'ALL REDIS ACL CHECKS PASSED' if not failed else 'REDIS ACL CHECKS FAILED'} "
          f"({len(results) - len(failed)}/{len(results)})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
