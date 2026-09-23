"""Shared test fixtures.

The suite is **offline and needs no PostgreSQL**. That is a property worth
keeping: this server's own database holds OAuth state and nothing else (HAD-3),
so the twelve read tools touch it not at all, and the health check's database
probe is exercised by substituting the probe rather than by standing up a server.

``S4`` needs rows — an authorization code has to be findable by its hash, and a
replayed refresh token has to revoke a real family — so the ``oauth_db`` fixture
below substitutes an **in-memory SQLite** engine and creates the schema from
``Base.metadata``. That exercises the real models and the real queries rather
than a fake store, and it keeps the CI workflow's promise that no ``postgres:``
service appears in it. The one concession SQLite extracts is paid in
``app.models.base.as_utc``, which normalises the naive datetimes it returns.

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
#
# Port 1, not 5432. Something does try to connect: every test server's lifespan
# installs the tool-call sink, which writes through to this URL. On a laptop
# that runs its own PostgreSQL, 5432 *answers*. The attempts then start TLS,
# fail authentication, and leave sockets for the garbage collector. When one is
# finally collected it closes a descriptor number a live test connection has
# since reused, and that test hangs in `initialize` for good. Port 1 refuses at
# once, as 5432 does in CI.
os.environ.setdefault("FMCP_ENV", "test")
os.environ.setdefault(
    "FMCP_DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:1/fanmoot_mcp_test"
)
os.environ.setdefault("FMCP_PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("FMCP_FANMOOT_API_BASE_URL", "https://fanmoot.test")
# Empty by default, so `/healthz` and `Settings` are tested in the posture a
# deployment without a key actually has. The OAuth tests set a real one — see
# the `oauth_env` fixture.
os.environ.setdefault("FMCP_SECRET_ENCRYPTION_KEY", "")
# No model id means `match_mood` answers from the offline lexicon in `app.mood`
# rather than reaching for Bedrock — the suite is offline, and a test that
# depended on a live model would be a test that failed on a train.
os.environ.setdefault("FMCP_BEDROCK_MODEL_ID", "")
# The warm-up loop (`S7-2`) would fetch from the moment a test server starts,
# and a test that counts upstream calls would count its calls too.
# `tests/test_warm.py` drives it directly.
os.environ.setdefault("FMCP_WARM_CACHES", "false")
# Kick-off times are spoken in a fixed zone, so "half past three" means the
# same thing in CI as it does on a laptop in Lagos.
os.environ.setdefault("FMCP_VOICE_TIMEZONE", "Europe/London")

import asyncio  # noqa: E402
import uuid  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import uvicorn  # noqa: E402
from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import db as app_db  # noqa: E402
from app import mood  # noqa: E402
from app.db import Base  # noqa: E402
from app.fanmoot import cache  # noqa: E402
from app.fanmoot import client as fanmoot_client  # noqa: E402
from app.fanmoot import names as fanmoot_names  # noqa: E402
from app.fanmoot.names import Catalogue, load_catalogue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.mcp import telemetry  # noqa: E402
from app.mcp.server import FanmootMCP, set_server  # noqa: E402
from app.models import UpstreamSession  # noqa: E402
from app.oauth import clients as oauth_clients  # noqa: E402
from app.oauth import ratelimit, tokens, upstream  # noqa: E402
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
    """The tool-call ring is process-wide; one test must not see another's.

    The durable sink is dropped as well. It is installed by the application
    lifespan, which the `mcp_url` fixture really does run — so without this, one
    test that started a server would leave every later test writing rows into
    whatever database happened to be installed at the time.
    """
    telemetry.reset()
    telemetry.set_sink(None)
    # Mood readings are cached per thread (`S7-1`), and two tests that label
    # the same three takes must each see their own labelling run.
    mood.reset_readings()
    yield
    telemetry.reset()
    telemetry.set_sink(None)
    mood.reset_readings()


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


# ── The OAuth store, substituted (S4) ────────────────────────────────────────
#: The static client every OAuth test authorizes as — Alexa's row, in miniature.
#: Two redirect URIs, because one is the case that cannot catch the bug `S4-5`
#: is about: a server that registered several and substituted the first.
STATIC_CLIENT_ID = "amzn1.application-oa2-client.test"
STATIC_CLIENT_SECRET = "alexa-shared-secret"  # noqa: S105 - a fixture, not a credential
STATIC_REDIRECT_URIS = [
    "https://pitangui.amazon.com/api/skill/link/TEST",
    "https://layla.amazon.com/api/skill/link/TEST",
]


@pytest.fixture
async def oauth_db() -> AsyncIterator[AsyncEngine]:
    """An in-memory SQLite engine installed as this server's own database.

    ``StaticPool`` because every connection has to be the *same* connection: a
    ``:memory:`` database belongs to its connection, and a pool that opened a
    second one would hand a test an empty schema half way through.

    Foreign keys are switched on, which SQLite does not do by default. Without
    it a cascade that the migration declares would silently not happen here, and
    a test could pass against behaviour PostgreSQL does not have.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    app_db.set_engine(engine)
    yield engine
    app_db.set_engine(None)
    await engine.dispose()


@pytest.fixture
def oauth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """The environment an authorization server actually needs.

    A real Fernet key, because without one this server refuses to hold a fan's
    Fanmoot session at all (HAD-3) and every OAuth path answers 503 — which is
    its own test, not the setup for everything else.
    """
    monkeypatch.setenv("FMCP_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("FMCP_PUBLIC_BASE_URL", "https://mcp.fanmoot.test")
    reset_settings_cache()
    tokens.reset_cache()
    ratelimit.reset()
    upstream.reset_cache()
    yield get_settings()
    reset_settings_cache()
    tokens.reset_cache()
    ratelimit.reset()
    upstream.reset_cache()


@pytest.fixture
async def oauth_http(
    oauth_env: Settings, oauth_db: AsyncEngine
) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client over a freshly built app, with redirects **not** followed.

    Not following them is the point: half of `S4`'s assertions are about where a
    redirect goes and what it carries, and a client that chased them would
    assert against Amazon's servers instead.
    """
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://mcp.fanmoot.test", follow_redirects=False
    ) as http:
        yield http


@pytest.fixture
async def static_client(oauth_db: AsyncEngine, oauth_env: Settings) -> str:
    """Alexa's pre-seeded row, through the same seeder the lifespan runs."""
    import json
    import os

    os.environ["FMCP_OAUTH_CLIENTS"] = json.dumps(
        [
            {
                "client_id": STATIC_CLIENT_ID,
                "client_secret": STATIC_CLIENT_SECRET,
                "client_name": "Alexa+",
                "redirect_uris": STATIC_REDIRECT_URIS,
            }
        ]
    )
    reset_settings_cache()
    async with app_db.get_sessionmaker()() as session:
        await oauth_clients.seed_static(session)
    os.environ.pop("FMCP_OAUTH_CLIENTS", None)
    reset_settings_cache()
    return STATIC_CLIENT_ID


#: The fan every `S4` page test signs in as.
OAUTH_USER = {
    "id": "2f1c8d60-9b47-4a6e-9d1b-0c2f8e5a3b71",
    "email": "fan@example.com",
    "handle": "thegunner",
    "name": "A Fan",
    "email_verified": True,
}


@pytest.fixture
def fanmoot_auth(replay: Replay) -> Replay:
    """Fanmoot's ``/v1/auth/*``, stubbed for the `S4` fan."""
    return _stub_auth(replay, OAUTH_USER)


@pytest.fixture
async def signed_in(oauth_db: AsyncEngine, oauth_env: Settings) -> UpstreamSession:
    """A fan already signed in on this origin, without going through the form.

    For the tests whose subject is what happens *after* sign-in — consent, the
    token exchange, revocation — so they do not each re-assert that a password
    reaches Fanmoot.
    """
    async with app_db.get_sessionmaker()() as session:
        row = UpstreamSession(
            fanmoot_user_id=uuid.UUID("2f1c8d60-9b47-4a6e-9d1b-0c2f8e5a3b71"),
            fanmoot_handle="thegunner",
            refresh_token_encrypted=tokens.encrypt("upstream-refresh-1"),
        )
        session.add(row)
        await session.commit()
        return row


#: The corpus's own fan. ``fixtures/get_v1_me.json``, ``/v1/me/club`` and
#: ``/v1/users/demo_fan`` are all this person, so an `S5` test that links *as*
#: them gets a grant whose handle matches the recorded account — which is what
#: lets `whoami` read all three in one go.
DEMO_USER = {
    "id": "a1f0d2c4-3e5b-4a90-9c11-7d2e6b8f0004",
    "email": "demo-fan@example.invalid",
    "handle": "demo_fan",
    "name": "Demo Fan",
    "email_verified": True,
}


def _stub_auth(tape: Replay, user: dict[str, object]) -> Replay:
    """Fanmoot's ``/v1/auth/*``, for one identity.

    Stubbed rather than recorded for the reason ``tests/test_fanmoot_endpoints``
    gives: ``make record`` runs against the live API with a real account, and a
    recording of ``/v1/auth/login`` would be a recording of somebody's
    credentials being accepted.
    """
    tape.stub(
        "/v1/auth/login",
        {
            "user": user,
            "tokens": {
                "access_token": "upstream-access-1",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_token": "upstream-refresh-1",
            },
        },
        method="POST",
        params={"in_body": "true"},
    )
    tape.stub(
        "/v1/auth/refresh",
        {
            "access_token": "upstream-access-2",
            "token_type": "Bearer",
            "expires_in": 900,
            "refresh_token": "upstream-refresh-2",
        },
        method="POST",
        params={"in_body": "true"},
    )
    tape.stub("/v1/auth/logout", {"message": "Signed out."}, method="POST")
    return tape


@pytest.fixture
def fanmoot_demo(replay: Replay) -> Replay:
    """``fanmoot_auth``, but signing in as the fan the fixtures are about."""
    return _stub_auth(replay, DEMO_USER)


@pytest.fixture
async def linked_token(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_demo: Replay
) -> str:
    """A completed account link, as the access token it produced.

    The whole OAuth dance really runs — authorize, sign in, consent, token —
    because an `S5` test asserting that a tool posts as the right fan is
    worthless if the grant behind it was inserted rather than earned (`S4-15`).
    """
    from tests import oauth as oauth_flow

    body = await oauth_flow.linked(
        oauth_http, client_id=STATIC_CLIENT_ID, client_secret=STATIC_CLIENT_SECRET
    )
    token: str = body["access_token"]
    return token
