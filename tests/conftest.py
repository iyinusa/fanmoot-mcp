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
# No model id means `match_mood` answers from the offline lexicon in `app.mood`
# rather than reaching for Bedrock — the suite is offline, and a test that
# depended on a live model would be a test that failed on a train.
os.environ.setdefault("FMCP_BEDROCK_MODEL_ID", "")
# Kick-off times are spoken in a fixed zone, so "half past three" means the
# same thing in CI as it does on a laptop in Lagos.
os.environ.setdefault("FMCP_VOICE_TIMEZONE", "Europe/London")

import asyncio  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402

from app.fanmoot import cache  # noqa: E402
from app.fanmoot import client as fanmoot_client  # noqa: E402
from app.fanmoot import names as fanmoot_names  # noqa: E402
from app.fanmoot.names import Catalogue, load_catalogue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.mcp import telemetry  # noqa: E402
from app.mcp.server import FanmootMCP, set_server  # noqa: E402
from app.settings import Settings, get_settings, reset_settings_cache  # noqa: E402
from tests.replay import Replay  # noqa: E402


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
def replay() -> Iterator[Replay]:
    """The recorded Fanmoot, installed as the process-wide client.

    Both the client and the cache are reset around every test: a cache that
    survived into the next test would make an assertion about a network call
    depend on which tests ran first.
    """
    cache.reset_cache()
    fanmoot_names.reset_catalogue()
    tape = Replay.from_fixtures()
    fanmoot_client.set_client(
        fanmoot_client.FanmootClient(base_url="https://fanmoot.test", transport=tape.transport())
    )
    yield tape
    fanmoot_client.set_client(None)
    cache.reset_cache()
    fanmoot_names.reset_catalogue()


@pytest.fixture
async def catalogue(replay: Replay) -> Catalogue:
    """Every club and competition, built from the recorded catalogue."""
    return await load_catalogue()


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


# ── The MCP server, over a real socket ───────────────────────────────────────
# Streamable HTTP is SSE, and httpx's ASGI transport buffers a whole response
# before returning it — so an in-process ASGI client would deadlock the moment
# the server held a stream open. These tests therefore drive a real uvicorn on
# a loopback port with the MCP *client SDK*, which is the only way to assert
# that a client Alexa+ will use can actually talk to this server (HAD-14).


class _Server(uvicorn.Server):
    """uvicorn without its signal handlers, which would replace pytest's."""

    def install_signal_handlers(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _forget_tool_calls() -> Iterator[None]:
    """The tool-call ring is process-wide; one test must not see another's."""
    telemetry.reset()
    yield
    telemetry.reset()


@pytest.fixture
def mcp_server() -> Iterator[FanmootMCP]:
    """A fresh MCP server, installed as the process's own for one test.

    Tests register their own tools on it — there are none until `S3` — and a
    shared instance would carry each test's tools into every later
    `tools/list`.
    """
    server = FanmootMCP()
    set_server(server)
    yield server
    set_server(None)


@pytest.fixture
async def mcp_url(mcp_server: FanmootMCP) -> AsyncIterator[str]:
    """``http://127.0.0.1:<port>/mcp``, served by a real application.

    Port 0 so tests can run in parallel and never collide on a fixed one.
    """
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=0, log_level="warning", access_log=False
    )
    server = _Server(config)
    serving = asyncio.create_task(server.serve())

    deadline = asyncio.get_running_loop().time() + 10
    while not server.started:
        if serving.done():  # pragma: no cover - only on a startup failure
            await serving
            raise RuntimeError("the test server exited during startup")
        if asyncio.get_running_loop().time() > deadline:  # pragma: no cover
            raise TimeoutError("the test server did not start")
        await asyncio.sleep(0.01)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await serving
