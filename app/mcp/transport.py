"""Streamable HTTP, mounted inside the FastAPI application.

``S2-2``. ``POST /mcp`` carries a JSON-RPC frame and gets an SSE stream back;
``GET /mcp`` opens the standalone stream the server pushes notifications down;
``DELETE /mcp`` ends the session. One path, three methods, which is what
``2025-11-25`` specifies and what Alexa's account-linking contract points at.

**Why a route and not a mount.** ``app.mount("/mcp", …)`` answers ``POST /mcp``
with a 307 to ``/mcp/`` — Starlette's mounts match a prefix *and a slash*. A
redirect on every frame would cost a round trip inside a voice turn, and a
client that declines to re-send a POST body across a redirect would see the
server as broken. A route bound to the exact path has neither problem, and
Starlette treats a class instance given as ``endpoint`` as a raw ASGI app,
which is exactly what the session manager is.

**Why the manager is built per application and not once.** A
``StreamableHTTPSessionManager`` may be ``run()`` once and never restarted, and
it owns the task group every session lives in. Tying its lifetime to the
FastAPI app that mounts it means the test suite can stand up a server per test
without the second one inheriting the first one's dead task group.

**Stateful, with session ids.** The alternative — stateless mode, a fresh
transport per request — would scale across workers for free, but it gives up
server-initiated notifications and resumability, and those are what the demo's
trace panel is showing off (HAD-12). One worker is the trade, and the
Dockerfile says so.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.routing import Route

from app.logging_config import get_logger
from app.mcp.events import InMemoryEventStore
from app.mcp.security import OriginValidation
from app.mcp.server import FanmootMCP
from app.oauth import bearer
from app.settings import Settings

log = get_logger("mcp.transport")

#: The endpoint, exactly. No trailing slash, no versioning: the MCP version is
#: negotiated in the handshake, not carried in the path.
MCP_PATH = "/mcp"

#: What the server suggests a disconnected SSE client waits before reconnecting,
#: in milliseconds. Short, because a fan whose stream dropped mid-match wants
#: the next goal — and safe, because the idle timeout below is three orders of
#: magnitude larger, so ordinary reconnection gaps cannot reap a live session.
_SSE_RETRY_MS = 3_000


def build_session_manager(server: FanmootMCP, settings: Settings) -> StreamableHTTPSessionManager:
    """The session manager for one application instance."""
    return StreamableHTTPSessionManager(
        app=server.lowlevel,
        # Resumability. Without a store, a dropped stream loses what was in
        # flight and the client has no way to ask for it back (`S2-2`).
        event_store=InMemoryEventStore(),
        # SSE responses, not buffered JSON. A tool that streams progress can,
        # and a client that expects `text/event-stream` gets it.
        json_response=False,
        stateless=False,
        # DNS-rebinding protection is off *here* because `OriginValidation`
        # does it, once, with this server's own policy. Two origin checks in
        # series is one that can disagree with the other. The setting still
        # governs the POST `Content-Type` check, which stays on.
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        retry_interval=_SSE_RETRY_MS,
        session_idle_timeout=settings.mcp_session_idle_timeout_s,
        max_sessions=settings.mcp_max_sessions,
    )


def build_route(manager: StreamableHTTPSessionManager, settings: Settings) -> Route:
    """The ``/mcp`` route: origin validation, then authorization, then the transport.

    ``DELETE`` is in the method list because ending a session is part of the
    transport: a client that closes cleanly frees its session immediately
    rather than leaving it to the idle timeout.

    The order of the two wrappers is deliberate. Origin validation is outermost
    because it is the cheapest check and the one that must hold whatever else is
    true — a browser on a hostile origin is refused before a token is looked up
    in the database. Authorization (`S4-9`) sits inside it: it verifies a bearer
    token if one was sent, publishes it to the auth context, and answers a
    ``tools/call`` that needs a scope the caller has not got with a 401 or 403
    carrying the RFC 9728 challenge. A call to one of the twelve read tools
    passes through both untouched, which is the property that keeps "what's the
    score" answerable without linking an account.
    """
    endpoint = OriginValidation(
        bearer.protect(manager.handle_request, settings),
        settings.mcp_allowed_origin_list,
        allow_loopback=not settings.is_production,
    )
    return Route(MCP_PATH, endpoint=endpoint, methods=["GET", "POST", "DELETE"])


@asynccontextmanager
async def running(manager: StreamableHTTPSessionManager) -> AsyncIterator[None]:
    """Hold the manager's task group open for the life of the application.

    Every session runs as a task in this group. Entering it in the app's
    lifespan — rather than letting a mounted sub-app's own lifespan do it, which
    Starlette would never call — is what makes ``/mcp`` work at all when it
    lives inside FastAPI.
    """
    async with manager.run():
        log.info("mcp_transport_ready", path=MCP_PATH)
        yield
