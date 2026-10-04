"""Fixtures for the pipeline integration tests.

Isolation from the running services: a throwaway Postgres database (agent_test_<random>,
created with the same credentials and dropped at session end) and Redis db 15, flushed around
every test. Nothing here connects to the `agent` database or Redis db 0.
"""

import asyncio
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import arq
import asyncpg
import pytest
from arq.connections import RedisSettings

import agent.citations.claims
import agent.citations.ground
import agent.llm
import agent.mail.auth
import agent.mail.outbound
from agent import db
from agent.config import Settings, get_settings
from agent.providers import base as providers_base

from .harness import (
    AGENT_ADDRESS,
    DEFAULT_COUNTS,
    MATTER,
    PUBLIC_BASE_URL,
    FakeAuth,
    FakeLLM,
    FakeProvider,
    FakeSMTP,
    Harness,
)

REPO = Path(__file__).resolve().parents[2]
REDIS_TEST_DB = 15


def _service_settings() -> Settings:
    """Connection details of the compose services (credentials from .env)."""
    return Settings(_env_file=REPO / ".env")


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    base = urlsplit(_service_settings().database_url)
    name = f"agent_test_{secrets.token_hex(4)}"
    admin_url = urlunsplit(base._replace(path="/postgres"))
    test_url = urlunsplit(base._replace(path=f"/{name}"))

    async def admin(sql: str) -> None:
        conn = await asyncpg.connect(admin_url)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    try:
        yield test_url
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture(scope="session")
def redis_url() -> str:
    base = urlsplit(_service_settings().redis_url)
    return urlunsplit(base._replace(path=f"/{REDIS_TEST_DB}"))


async def _no_network(*_args, **_kwargs):
    raise AssertionError("integration tests must not reach a real LLM")


@pytest.fixture
async def h(database_url: str, redis_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[Harness]:
    env = {
        "DATABASE_URL": database_url,
        "REDIS_URL": redis_url,
        "DATA_DIR": str(tmp_path / "data"),
        "AGENT_MAIL_ADDRESS": AGENT_ADDRESS,
        "SENDER_AUTH_MODE": "dmarc",
        "SENDER_ALLOWLIST": "[]",
        "PUBLIC_BASE_URL": PUBLIC_BASE_URL,
        "RATE_PER_SENDER_HOUR": "6",
        "RATE_PER_DOMAIN_HOUR": "30",
        "RATE_GLOBAL_HOUR": "300",
        "MAX_REQUESTS_PER_THREAD": "5",
        "MAX_DOCS_PER_REQUEST": "10",
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": "9",
        "OPENROUTER_API_KEY": "not-used-in-tests",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    s = get_settings()
    assert urlsplit(s.database_url).path.startswith("/agent_test_"), s.database_url
    assert urlsplit(s.redis_url).path == f"/{REDIS_TEST_DB}", s.redis_url

    monkeypatch.setattr(db, "_pool", None)
    pool = await db.create_pool(max_size=10)
    async with pool.acquire() as conn:
        await db.migrate(conn)
        tables = await conn.fetchval(
            "SELECT string_agg(format('%I', tablename), ', ') FROM pg_tables WHERE schemaname = 'public'"
        )
        await conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")

    redis = await arq.create_pool(RedisSettings.from_dsn(redis_url))
    assert redis.connection_pool.connection_kwargs.get("db") == REDIS_TEST_DB
    await redis.flushdb()

    provider = FakeProvider()
    provider.add_matter(MATTER, DEFAULT_COUNTS)
    monkeypatch.setattr(providers_base, "_REGISTRY", {"uarb": lambda: provider})

    llm = FakeLLM()
    monkeypatch.setattr(agent.llm, "structured", llm.structured)  # classifier: llm.structured(...)
    monkeypatch.setattr(agent.citations.claims, "structured", llm.structured)  # from agent.llm import structured
    monkeypatch.setattr(agent.citations.ground, "structured", llm.structured)
    monkeypatch.setattr(agent.llm, "_http", _no_network)

    auth = FakeAuth()
    monkeypatch.setattr(agent.mail.auth, "verify_sender", auth.verify)
    smtp = FakeSMTP()
    monkeypatch.setattr(agent.mail.outbound, "send", smtp.send)

    harness = Harness(redis=redis, provider=provider, llm=llm, auth=auth, smtp=smtp, monkeypatch=monkeypatch)
    try:
        yield harness
    finally:
        await redis.flushdb()
        await redis.aclose()
        await db.close_pool()
        get_settings.cache_clear()
