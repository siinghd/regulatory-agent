"""Session-wide checks, for every test directory and marker selection."""

import os

import pytest

# Tests never read the operator's .env (production secrets, least-privilege URLs). Must run
# before agent.config is imported anywhere.
os.environ["AGENT_ENV_FILE"] = ""

from tests.metrics_privacy import violations


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """No metric label value any test made the code record may break the public-metrics privacy
    rules (deploy/observability/METRICS_CONTRACT.md): the integration suite runs real requests,
    with real-looking addresses, IPs and matter numbers, through every instrumented path."""
    found = violations()
    if not found:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    for line in ["metrics privacy: label values recorded during this run break the contract:", *found]:
        if reporter is not None:
            reporter.write_line(line, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
