"""Queue-level failure handling, driven through a *real* arq Worker (burst mode) on Redis db 15.

The integration harness's `run_job` re-runs a job on every Retry, which hides arq's own rules:
- a job that exceeds `job_timeout` is cancelled and marked failed: arq does NOT retry it;
- a non-Retry exception is final for arq (no retry);
- a Retry raised on the last try is dropped ("max retries exceeded"), so the agent sets max_tries
  out of reach and counts attempts in Postgres instead;
- a job cancelled by SIGTERM is re-queued and consumes a try;
- after a job finishes (keep_result=0) its retry counter is deleted, so a sweeper re-enqueue
  starts again at job_try=1.
These tests show what the request (and the user) ends up with under each rule.
"""

import asyncio
import os
import signal
import tempfile

import arq
import pytest
from arq.worker import Worker, func

from agent import blobs, db, queue, store, worker
from agent.config import get_settings
from agent.models import PortalUnavailable
from tests.integration.harness import make_email

pytestmark = pytest.mark.integration

REQUEST = "Hi,\n\nCan you send me the Other Documents for M12205?\n\nThanks,\nAlice"


class Recorder:
    """Wraps process_request so the test sees every job_try arq hands it."""

    def __init__(self) -> None:
        self.tries: list[int] = []

    async def process_request(self, ctx, request_id: str) -> None:
        self.tries.append(ctx["job_try"])
        await worker.process_request(ctx, request_id)


def worker_functions(rec: Recorder) -> list:
    return [func(rec.process_request, name="process_request"),
            func(worker.send_outbound, name="send_outbound", max_tries=worker.ARQ_MAX_TRIES)]


async def run_worker(h, rec: Recorder, *, job_timeout: float, max_tries: int = worker.ARQ_MAX_TRIES) -> Worker:
    """One burst of a real arq worker (own Redis pool: Worker.close() closes it), JSON jobs as in production."""
    pool = await queue.create_pool(get_settings().redis_url)
    w = Worker(
        functions=worker_functions(rec),
        redis_pool=pool,
        burst=True,
        job_timeout=job_timeout,
        max_tries=max_tries,
        keep_result=0,
        poll_delay=0.02,
        handle_signals=False,
        job_serializer=queue.serialize,
        job_deserializer=queue.deserialize,
        ctx={"deps": h.deps},
    )
    try:
        await asyncio.wait_for(w.async_run(), timeout=60)
    finally:
        await w.close()
    return w


@pytest.fixture
def fast_backoff(h):
    h.configure(retry_base_s=0.05, retry_cap_s=0.05)


async def sweep_after_idle(h, rid) -> None:
    await h.backdate(rid, minutes=20)
    await worker.sweep({"redis": h.redis})


# ======================================================================== job_timeout


async def test_job_timeout_is_final_for_arq_and_leaves_the_request_mid_flight(h, fast_backoff):
    """Evidence: arq cancels at job_timeout, does not retry, nothing is written, no apology."""
    h.provider.latency = 2.0  # the portal visit outlives the job timeout
    rid = await h.ingest(make_email(REQUEST))
    rec = Recorder()

    w = await run_worker(h, rec, job_timeout=0.5)

    assert rec.tries == [1]
    assert (w.jobs_failed, w.jobs_retried) == (1, 0)  # TimeoutError is not Retry/CancelledError
    row = await h.request(rid)
    assert row["state"] == "fetching" and row["error"] is None
    assert len(h.acks(rid)) == 1 and h.replies(rid) == []  # acked, then silence
    assert await h.redis.exists(f"lock:request:{rid}") == 0  # the lock was released on cancel
    assert await h.queued_request_ids() == []  # the job is gone; only the sweeper can revive it
    assert "retry" not in await h.event_kinds(rid)  # CancelledError bypasses process()'s handlers


async def test_sweeper_restarts_job_try_at_one_so_final_attempt_is_never_reached(h, fast_backoff):
    """Evidence: every sweep cycle hands the job job_try=1 again; requests.attempts keeps growing."""
    h.provider.latency = 2.0
    rid = await h.ingest(make_email(REQUEST))
    rec = Recorder()
    for _ in range(3):
        await run_worker(h, rec, job_timeout=0.5)
        await sweep_after_idle(h, rid)

    assert rec.tries == [1, 1, 1]
    row = await h.request(rid)
    assert row["state"] == "fetching" and row["attempts"] == 3


async def test_a_request_that_always_times_out_ends_with_one_apology(h, fast_backoff):
    h.provider.latency = 2.0
    rid = await h.ingest(make_email(REQUEST))
    rec = Recorder()
    for _ in range(get_settings().max_attempts + 2):
        await run_worker(h, rec, job_timeout=0.5)
        if (await h.request(rid))["state"] in store.TERMINAL:
            break
        await sweep_after_idle(h, rid)

    row = await h.request(rid)
    assert row["state"] == "failed" and row["attempts"] == get_settings().max_attempts
    assert len(h.replies(rid)) == 1
    assert "dead_letter" in await h.event_kinds(rid)


# ======================================================================== poison message


async def test_poison_request_reaches_a_terminal_state(h, fast_backoff):
    rid = await h.ingest(make_email(REQUEST))
    row = await h.request(rid)
    os.remove(blobs._root("raw") / row["raw_sha256"][:2] / row["raw_sha256"])  # deterministic crash
    rec = Recorder()
    for _ in range(get_settings().max_attempts + 2):
        await run_worker(h, rec, job_timeout=5)
        if (await h.request(rid))["state"] in store.TERMINAL:
            break
        await sweep_after_idle(h, rid)

    assert (await h.request(rid))["state"] in store.TERMINAL
    assert "dead_letter" in await h.event_kinds(rid)
    assert h.smtp.attempts == []  # the sender was never verified: no email at all


# ======================================================================== Retry on the last try


async def test_lock_contention_on_the_final_try_does_not_raise_retry(h):
    rid = await h.ingest(make_email(REQUEST))
    await h.redis.set(f"lock:request:{rid}", "held-by-a-zombie-job", ex=60)

    with pytest.raises(worker.Park):  # an ordinary try defers, without using an attempt
        await h.process(rid, 1)
    await h.process(rid, worker.ARQ_MAX_TRIES)  # Retry here == silently dropped by arq

    assert (await h.request(rid))["attempts"] == 0


async def test_portal_down_with_real_arq_retries_six_times_then_apologises(h, fast_backoff):
    """Evidence (correct): the happy failure path also holds under real arq semantics."""
    h.provider.portal_down = lambda: PortalUnavailable("proxy refused")
    rid = await h.ingest(make_email(REQUEST))
    rec = Recorder()

    w = await run_worker(h, rec, job_timeout=10)

    max_attempts = get_settings().max_attempts
    assert rec.tries == list(range(1, max_attempts + 1))
    assert w.jobs_retried == max_attempts - 1 and w.jobs_complete == 1
    row = await h.request(rid)
    assert row["state"] == "failed" and len(h.replies(rid)) == 1


# ======================================================================== thundering herd / breaker


async def test_retry_delays_are_jittered_across_requests(h):
    h.provider.portal_down = lambda: PortalUnavailable("portal timed out")
    defers = []
    for i in range(8):
        rid = await h.ingest(make_email(REQUEST, from_addr=f"user{i}@example.com"))
        with pytest.raises(arq.Retry) as exc:
            await h.process(rid, 1)
        defers.append(exc.value.defer_score)

    assert len(set(defers)) > 1, f"all {len(defers)} requests retry after exactly {defers[0] / 1000}s"


async def test_a_dead_portal_opens_a_breaker_instead_of_burning_every_try(h):
    h.configure(breaker_failures=5)  # the harness default keeps breakers out of other tests' way
    h.provider.portal_down = lambda: PortalUnavailable("portal timed out")
    n = 5
    rids = []
    for i in range(n):
        rid = await h.ingest(make_email(REQUEST, from_addr=f"user{i}@example.com"))
        await h.run_job(rid)  # returns once the job is parked
        rids.append(rid)

    assert h.provider.calls["list"] < n * get_settings().max_attempts / 2, h.provider.calls
    assert h.provider.calls["list"] == 5  # the failures that opened it; the rest never visited
    for rid in rids[1:]:  # parked without using an attempt, still waiting (not failed)
        row = await h.request(rid)
        assert row["state"] == "fetching" and row["attempts"] == 0
        assert "parked" in await h.event_kinds(rid)


# ======================================================================== graceful shutdown


async def test_sigterm_mid_job_requeues_it_and_the_rerun_completes_cleanly(h, fast_backoff):
    """Evidence (correct): SIGTERM cancels the job, arq re-queues it, temp dirs and the lock are
    cleaned up, and the re-run sends exactly one ack and one reply."""
    h.provider.latency = 1.5
    rid = await h.ingest(make_email(REQUEST))
    rec = Recorder()
    pool = await queue.create_pool(get_settings().redis_url)
    w = Worker(functions=worker_functions(rec), redis_pool=pool, burst=True, job_timeout=30, keep_result=0,
               poll_delay=0.02, handle_signals=False, job_serializer=queue.serialize,
               job_deserializer=queue.deserialize, ctx={"deps": h.deps})
    task = asyncio.create_task(w.async_run())
    for _ in range(200):
        if (await h.request(rid))["state"] == "fetching":
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)  # inside the portal visit
    w.handle_sig(signal.SIGTERM)
    await asyncio.gather(task, *w.tasks.values(), return_exceptions=True)
    await w.close()

    assert w.jobs_retried == 1  # "cancelled, will be run again"
    assert await h.queued_request_ids() == [str(rid)]
    assert await h.redis.exists(f"lock:request:{rid}") == 0
    assert await h.redis.get(f"arq:retry:req:{rid}") == b"1"  # the cancelled run consumed a try
    leftovers = [d for d in os.listdir(tempfile.gettempdir()) if d.startswith(f"req-{rid}")]
    leftovers += [d for d in os.listdir(get_settings().data_dir) if d.startswith("dl-")]
    assert leftovers == []

    h.provider.latency = 0
    await run_worker(h, rec, job_timeout=30)
    assert rec.tries == [1, 2]
    assert (await h.request(rid))["state"] == "done"
    assert len(h.acks(rid)) == 1 and len(h.replies(rid)) == 1


# ======================================================================== Postgres outage during a job


async def test_db_outage_during_failure_handling_is_retried_by_the_queue(h, monkeypatch):
    real_pool = db.pool
    down = {"on": False}

    def pool():
        if down["on"]:
            raise ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 5442)")
        return real_pool()

    def portal_fails_and_db_goes_down():
        down["on"] = True
        return PortalUnavailable("portal timed out")

    monkeypatch.setattr(db, "pool", pool)
    h.provider.portal_down = portal_fails_and_db_goes_down
    rid = await h.ingest(make_email(REQUEST))

    with pytest.raises(arq.Retry):
        await h.process(rid, 1)


async def test_postgres_connections_killed_mid_job_recover_on_the_next_try(h, database_url):
    """Evidence (correct): pg_terminate_backend on every pooled connection of the throwaway DB
    in the middle of a job; the pool reconnects and the retry completes."""
    import asyncpg

    async def kill_backends():
        conn = await asyncpg.connect(database_url)
        try:
            return await conn.fetchval(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        finally:
            await conn.close()

    orig = h.provider.download
    killed = []

    async def download(matter, refs, dest_dir):
        if not killed:
            killed.append(await kill_backends())
        async for f in orig(matter, refs, dest_dir):
            yield f

    h.provider.download = download
    rid = await h.ingest(make_email(REQUEST))
    defers = await h.run_job(rid)

    assert killed and killed[0] >= 1
    row = await h.request(rid)
    assert row["state"] == "done" and len(h.replies(rid)) == 1 and len(h.acks(rid)) == 1
    assert len(defers) <= 1
