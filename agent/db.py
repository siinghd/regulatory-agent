"""Postgres access: one asyncpg pool per process, idempotent migrations, JSON codecs."""

import json
from pathlib import Path

import asyncpg

from agent.config import get_settings

_pool: asyncpg.Pool | None = None
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


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


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def migrate(conn: asyncpg.Connection) -> None:
    """Apply every migrations/*.sql in order. Files are written to be idempotent."""
    # advisory lock: several processes boot at once and must not race the DDL
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(7342001)")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            await conn.execute(path.read_text())


async def fetchrow(query: str, *args):
    return await pool().fetchrow(query, *args)


async def fetch(query: str, *args):
    return await pool().fetch(query, *args)


async def execute(query: str, *args):
    return await pool().execute(query, *args)
