"""Postgres access: one asyncpg pool per process, idempotent migrations, JSON codecs.

Every connection is acquired with a timeout, so an exhausted pool fails fast (and the job is
retried) instead of waiting forever. Only `ragent migrate` runs migrations, as the owner role
(MIGRATION_DATABASE_URL); the app connects with DATABASE_URL.
"""

import json
from pathlib import Path

import asyncpg

from agent.config import Settings, get_settings

_pool: asyncpg.Pool | None = None
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
ACQUIRE_TIMEOUT_S = 10.0


async def _init_conn(conn: asyncpg.Connection) -> None:
    for typ in ("json", "jsonb"):
        await conn.set_type_codec(typ, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str | None = None, *, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn or get_settings().database_url,
            min_size=min_size,
            max_size=max_size,
            init=_init_conn,
            command_timeout=30,
        )
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialised: call create_pool() at startup")
    return _pool


def acquire():
    """A pooled connection (`async with db.acquire() as conn`), waiting at most ACQUIRE_TIMEOUT_S."""
    return pool().acquire(timeout=ACQUIRE_TIMEOUT_S)


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def migration_dsn(settings: Settings | None = None) -> str:
    """The owner role's DSN when one is configured, else the app's."""
    s = settings or get_settings()
    return s.migration_database_url.get_secret_value() if s.migration_database_url else s.database_url


async def migrate(conn: asyncpg.Connection) -> None:
    """Apply every migrations/*.sql in order. Files are written to be idempotent."""
    # advisory lock: several processes boot at once and must not race the DDL
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7342001)")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            await conn.execute(path.read_text())


async def run_migrations(dsn: str | None = None) -> None:
    """`ragent migrate`: a dedicated connection as the migration role, closed afterwards."""
    conn = await asyncpg.connect(dsn or migration_dsn(), timeout=ACQUIRE_TIMEOUT_S)
    try:
        await migrate(conn)
    finally:
        await conn.close()


async def fetchrow(query: str, *args):
    async with acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetch(query: str, *args):
    async with acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchval(query: str, *args):
    async with acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args):
    async with acquire() as conn:
        return await conn.execute(query, *args)
