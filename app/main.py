"""Application factory.

One process, one Traefik route. The MCP Streamable HTTP endpoint (``/mcp``),
the OAuth 2.1 authorization server, its three discovery documents under
``/.well-known``, the consent and sign-up HTML, and ``/healthz`` are all served
from ``https://mcp.fanmoot.com`` by this app (HAD-2, HAD-4). The MCP server
mounts as an ASGI route rather than running as its own service, so a client
that has completed an OAuth flow is already talking to the right origin.

At ``S2`` ``/healthz`` and ``/mcp`` exist; the OAuth half arrives with ``S4``.

**Everything is built per application, not per process.** The MCP session
manager owns a task group that can be started exactly once, so a module-level
singleton would mean the second application built in a process — which is every
test after the first — inherits a manager that has already been shut down. The
manager therefore lives on ``app.state`` and the lifespan reads it from there.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__, health
from app.db import dispose_engine
from app.fanmoot import cache as fanmoot_cache
from app.fanmoot import names as fanmoot_names
from app.fanmoot.client import close_client, get_client
from app.logging_config import configure_logging, get_logger
from app.mcp import transport
from app.mcp.server import get_server
from app.middleware import RequestIdMiddleware
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

    # The MCP transport's task group wraps the yield: every session is a task
    # inside it, so it opens before the first request can arrive and closes
    # after the last one has been answered.
    async with transport.running(app.state.mcp_session_manager):
        yield

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

    # Outermost, so every request that is handled gets an id and a log line —
    # including every JSON-RPC frame on the /mcp route, which is what makes the
    # demo's trace panel line up with this server's logs (HAD-12).
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    session_manager = transport.build_session_manager(get_server(), settings)
    app.state.mcp_session_manager = session_manager
    app.router.routes.append(transport.build_route(session_manager, settings))

    return app


app = create_app()
