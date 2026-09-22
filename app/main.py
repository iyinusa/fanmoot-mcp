"""Application factory.

One process, one Traefik route. The MCP Streamable HTTP endpoint (``/mcp``),
the OAuth 2.1 authorization server, its three discovery documents under
``/.well-known``, the consent and sign-up HTML, and ``/healthz`` are all served
from ``https://mcp.fanmoot.com`` by this app (HAD-2, HAD-4). The MCP server
mounts as an ASGI sub-app rather than running as its own service, so a client
that has completed an OAuth flow is already talking to the right origin.

At ``S0`` only ``/healthz`` exists; the mounts arrive with ``S2`` and ``S4``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__, health
from app.db import dispose_engine
from app.logging_config import configure_logging, get_logger
from app.middleware import RequestIdMiddleware
from app.settings import Settings, get_settings


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.env)

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

    yield

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
    # including every JSON-RPC frame on the /mcp mount, which is what makes the
    # demo's trace panel line up with this server's logs (HAD-12).
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)

    return app


app = create_app()
