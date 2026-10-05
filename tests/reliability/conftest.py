"""Reliability / fault-injection tests reuse the integration fixtures (throwaway Postgres DB,
fake portal / LLM / SMTP / DNS) from tests/integration/conftest.py.

One deviation: they use Redis db 14 instead of db 15. The integration fixture flushes its db
around every test, and these tests run real arq workers that consume `process_request` jobs, so
sharing db 15 with a concurrent `pytest -m integration` run makes both runs flaky (observed:
jobs vanishing mid-test, a burst worker stealing another run's jobs). The `h` fixture is reused
unchanged; only the db number it asserts and connects to is switched for this directory.
"""

from urllib.parse import urlsplit, urlunsplit

import pytest

import tests.integration.conftest as integration
from tests.integration.conftest import _service_settings, database_url, h  # noqa: F401

RELIABILITY_REDIS_DB = 14


@pytest.fixture(scope="session")
def redis_url() -> str:
    base = urlsplit(_service_settings().redis_url)
    return urlunsplit(base._replace(path=f"/{RELIABILITY_REDIS_DB}"))


@pytest.fixture(autouse=True)
def _reliability_redis_db(monkeypatch: pytest.MonkeyPatch) -> None:
    # autouse: instantiated before `h`, whose isolation asserts read this module global
    monkeypatch.setattr(integration, "REDIS_TEST_DB", RELIABILITY_REDIS_DB)
