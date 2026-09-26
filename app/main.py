"""Application factory.

One process, one Traefik route. The MCP Streamable HTTP endpoint (``/mcp``),
the OAuth 2.1 authorization server, its three discovery documents under
``/.well-known``, the consent and sign-up HTML, and ``/healthz`` are all served
from ``https://mcp.fanmoot.com`` by this app (HAD-2, HAD-4). The MCP server
mounts as an ASGI route rather than running as its own service, so a client
that has completed an OAuth flow is already talking to the right origin.

``/healthz``, ``/mcp``, the OAuth 2.1 authorization server (`S4`) and the three
discovery documents under ``/.well-known`` are all here, in that order of
addition.

**Everything is built per application, not per process.** The MCP session
manager owns a task group that can be started exactly once, so a module-level
singleton would mean the second application built in a process — which is every
test after the first — inherits a manager that has already been shut down. The
manager therefore lives on ``app.state`` and the lifespan reads it from there.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__, audit, health, home, legal, mood, registry, warm
from app.db import dispose_engine, get_sessionmaker
from app.fanmoot import cache as fanmoot_cache
from app.fanmoot import names as fanmoot_names
from app.fanmoot.client import close_client, get_client
from app.logging_config import configure_logging, get_logger
from app.mcp import transport
from app.mcp.server import get_server
from app.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from app.oauth import clients as oauth_clients
from app.oauth import dcr as oauth_dcr
from app.oauth import flow as oauth_flow
from app.oauth import metadata as oauth_metadata
from app.oauth import router as oauth_router
from app.oauth import upstream as oauth_upstream
from app.oauth.errors import FlowRefusal
from app.settings import Settings, get_settings

#: Icons, and from `S8` the brand assets an add-on manifest points at. Served
#: by this server rather than linked to fanmoot.com: a client rendering the
#: tool list has already connected here, and an icon behind a second origin is
#: an icon that is missing whenever that origin is blocked (`S3-8`).
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.env)

    # One httpx client for the process, built here rather than on the first
    # tool call: the connection pool is most of the latency budget (HAD-6), and
    # a pool created mid-turn has already spent it.
    get_client()
    fanmoot_cache.reset_cache()
    # The indexed catalogue is built from the cache, so it is dropped with it:
    # a worker that started with a warm index and a cold cache would resolve
    # names against a Fanmoot it has not spoken to (`S3-2`).
    fanmoot_names.reset_catalogue()
    # Cached Fanmoot access tokens belong to the process that minted them
    # (`S4-8`), so a restarted worker starts from the encrypted refresh tokens
    # rather than from whatever a previous process held.
    oauth_upstream.reset_cache()
    mood.reset_readings()

    # Alexa's credentials, upserted by client_id (`S4-5`). Best-effort on
    # purpose: a database that is down at boot must not take the read surface —
    # which needs no database at all — down with it. `/healthz` already reports
    # an unreachable database, and the log line below is what an operator greps
    # when account linking is failing.
    await _seed_oauth_clients()
    housekeeping = asyncio.create_task(_housekeeping()) if settings.can_store_grants else None

    # Every tool call is written through to `tool_calls` from here on (`S4-1`).
    # Installed in the lifespan and nowhere else, which is what keeps the test
    # suite's read-tool tests free of a database.
    audit.install_tool_call_sink()

    log = get_logger("startup")
    log.info(
        "mcp_server_starting",
        env=settings.env,
        version=__version__,
        base_url=settings.public_base_url,
        upstream=settings.fanmoot_api_base_url,
        # Said out loud at startup because a deployment that cannot store a
        # grant serves a smaller surface than the one its README describes.
        grant_storage=settings.can_store_grants,
    )

    # Warmed in the background (`S7-2`): the server takes traffic at once, and
    # a fanmoot.com that is slow to answer at boot delays nothing but itself.
    warming = asyncio.create_task(warm.keep_warm()) if settings.warm_caches else None

    # The MCP transport's task group wraps the yield: every session is a task
    # inside it, so it opens before the first request can arrive and closes
    # after the last one has been answered.
    async with transport.running(app.state.mcp_session_manager):
        yield

    for background in (warming, housekeeping):
        if background is not None:
            background.cancel()

    # Telemetry inserts are scheduled rather than awaited (`app.audit`), so the
    # last few are still in flight here. Dropped rather than waited on for long:
    # a latency row is not worth delaying a shutdown that a deploy is waiting for.
    for task in audit.pending_writes():
        task.cancel()

    await close_client()
    await dispose_engine()
    get_logger("shutdown").info("mcp_server_stopping")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title="Fanmoot MCP server",
        version=__version__,
        lifespan=lifespan,
        # No public schema. This server's surface is described by MCP's own
        # `tools/list`, `resources/list` and `prompts/list` — an OpenAPI
        # document alongside them would be a second, drifting description of
        # the same thing, and the OAuth endpoints are specified by RFC rather
        # than by us.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )

    # Inside the request id, so a 500 the id middleware logs still went out
    # with its headers (`S7-5`).
    app.add_middleware(
        SecurityHeadersMiddleware, https=settings.public_base_url.startswith("https://")
    )

    # Outermost, so every request that is handled gets an id and a log line —
    # including every JSON-RPC frame on the /mcp route, which is what makes the
    # demo's trace panel line up with this server's logs (HAD-12).
    app.add_middleware(RequestIdMiddleware)

    app.include_router(home.router)
    app.include_router(health.router)
    app.include_router(legal.router)
    app.include_router(registry.router)

    # The OAuth 2.1 authorization server (`S4`, HAD-4). Three routers because
    # they answer to three different specifications: the discovery documents
    # (RFC 9728, RFC 8414, OIDC Discovery), dynamic client registration
    # (RFC 7591/7592), and the flow itself plus the pages a fan sees.
    app.include_router(oauth_metadata.router)
    app.include_router(oauth_dcr.router)
    app.include_router(oauth_router.router)
    # A refusal that must not be redirected renders a page on this origin —
    # see `app.oauth.errors.FlowRefusal` on why redirecting it would be an open
    # redirector.
    app.add_exception_handler(FlowRefusal, oauth_router.flow_refusal_handler)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    session_manager = transport.build_session_manager(get_server(), settings)
    app.state.mcp_session_manager = session_manager
    app.router.routes.append(transport.build_route(session_manager, settings))

    return app


async def _seed_oauth_clients() -> None:
    """Upsert the static clients before the first request can need them.

    Wrapped in a refusal to bring the process down: the failure mode of a
    missing static client is "Alexa cannot link", which is bad, and the failure
    mode of raising here is "nothing works at all", which is worse. Reaping
    lapsed registrations moved to ``_housekeeping``, which runs daily rather
    than only at a deploy.
    """
    log = get_logger("startup")

    # Nothing to do when this server cannot hold a fan's Fanmoot session at all
    # (HAD-3): with no encryption key the whole authorization half is closed, so
    # seeding clients nobody can link against and reaping registrations that
    # cannot be created is work whose only output would be a database error in
    # the log of a server that is behaving exactly as configured.
    if not get_settings().can_store_grants:
        log.info("oauth_client_seeding_skipped", reason="no encryption key configured")
        return

    try:
        async with get_sessionmaker()() as session:
            await oauth_clients.seed_static(session)
    except Exception as exc:
        log.error(
            "oauth_client_seeding_failed",
            error_type=type(exc).__name__,
            error=str(exc),
            hint="account linking will fail until this is fixed; the read tools are unaffected",
        )


app = create_app()


#: Once a day is plenty: the shortest retention is measured in days.
_HOUSEKEEPING_INTERVAL_S = 24 * 3600


async def _housekeeping() -> None:
    """Reap lapsed registrations and prune the logs, now and then daily.

    Both run in the background so a slow database never delays startup, and a
    failure is logged and retried tomorrow rather than raised. What `/privacy`
    promises about retention (`S7-12`) is kept here.
    """
    log = get_logger("housekeeping")
    while True:
        try:
            async with get_sessionmaker()() as session:
                await oauth_clients.reap_stale(session)
                await audit.prune(session)
                # Five minutes past the browser session, so nobody half way
                # through linking a second app loses the sign-in under them.
                await oauth_upstream.end_unused_sessions(
                    session, grace_s=oauth_flow.SESSION_TTL_S + 300
                )
        except Exception as exc:
            log.warning("housekeeping_failed", error_type=type(exc).__name__, error=str(exc))
        await asyncio.sleep(_HOUSEKEEPING_INTERVAL_S)
