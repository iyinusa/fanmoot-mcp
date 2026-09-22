"""Shared test fixtures.

The suite is **offline and needs no PostgreSQL**. That is a property worth
keeping: this server's own database holds OAuth state and nothing else (HAD-3),
so nothing before ``S4`` touches it, and the health check's database probe is
exercised by substituting the probe rather than by standing up a server. Tests
that genuinely need rows arrive with ``S4`` and bring their own database
fixture.

The environment is configured **before** ``app`` is imported: ``Settings`` is
read once and cached, and ``app.main`` builds the application at import time,
so a test that set ``FMCP_*`` afterwards would be configuring an app that had
already read its configuration. Hence the ``os.environ`` block above the
imports, and ruff's E402 waiver for this file.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

# A syntactically valid URL pointing at nothing. `Settings` refuses to start
# without one (there is no fallback engine), and no test connects — so a real
# host here would be a test suite that quietly depended on a developer's
# machine.
os.environ.setdefault("FMCP_ENV", "test")
os.environ.setdefault(
    "FMCP_DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/fanmoot_mcp_test"
)
os.environ.setdefault("FMCP_PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("FMCP_FANMOOT_API_BASE_URL", "https://fanmoot.test")
os.environ.setdefault("FMCP_SECRET_ENCRYPTION_KEY", "")

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.main import create_app  # noqa: E402
from app.settings import Settings, get_settings, reset_settings_cache  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return get_settings()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """A test that edits the environment gets a fresh ``Settings``, and the
    next test does not inherit it."""
    yield
    reset_settings_cache()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client wired straight into the ASGI app.

    No lifespan is run: startup opens nothing this suite needs, and running it
    would build a connection pool against the unreachable test database above.
    """
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http
