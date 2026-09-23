"""ASGI middleware.

One so far: request correlation. There is no canonical-host redirect here —
``mcp.fanmoot.com`` is an API origin with a single host, and an MCP client that
followed a 301 mid-session would be a bug we caused. Origin *validation* for
the ``/mcp`` mount is a different thing and lands with ``S2-3``; it returns 403
rather than redirecting.

**Written as raw ASGI rather than as a ``BaseHTTPMiddleware``.** That is not a
style choice. ``BaseHTTPMiddleware`` runs the application it wraps in a separate
task and pipes the response back through an ``anyio`` memory stream, which is
fine for a JSON handler and is not fine for ``/mcp``: every frame this server
answers is an SSE stream held open for the life of a call, and under that shape
the bridge can see its end of the stream close before a response has started
and raise ``RuntimeError("No response returned.")`` — a 500 on a request the
MCP server had already answered correctly. Wrapping ``send`` instead adds the
header and the log line without putting anything between the transport and the
stream.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.logging_config import get_logger

log = get_logger("http")

#: Not logged as a request line. Docker's HEALTHCHECK curls this every 30
#: seconds and Traefik probes it too; at that rate the health check would be
#: most of the log. It still gets a request id and the header, so a failing
#: probe can still be correlated — it just does not narrate the healthy case.
_QUIET_PATHS = frozenset({"/healthz"})


class RequestIdMiddleware:
    """Attach a request id to every request, response and log line.

    An inbound ``X-Request-ID`` is honoured so a trace can be followed across a
    proxy — and, here, across the boundary between the demo's trace panel and
    this server. Otherwise one is minted. The id is bound into structlog's
    context-local storage, which is what makes every line emitted while handling
    one request findable together without passing a logger around.

    Because this is raw ASGI, the binding is on the **same** task as the handler
    rather than on a bridging one, so a log line written deep inside a tool call
    carries the id of the frame that caused it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get("x-request-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        # Only read after the response has started; a request whose application
        # never answered is the failure path below, not a status.
        status = 0

        async def tagged(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, tagged)
        except Exception:
            # Logged with the request context still bound, then re-raised: the
            # exception handlers turn it into a 500, and swallowing it here
            # would hide the failure from the client.
            log.exception(
                "request_failed",
                method=scope.get("method"),
                path=scope.get("path"),
                duration_ms=_since(started),
            )
            raise
        else:
            # After the application returns, which for an SSE response means
            # after the stream has closed — so the duration is the whole call
            # rather than the time to the first byte.
            if scope.get("path") not in _QUIET_PATHS:
                log.info(
                    "request",
                    method=scope.get("method"),
                    path=scope.get("path"),
                    status=status,
                    duration_ms=_since(started),
                )
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


def _since(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
