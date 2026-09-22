"""ASGI middleware.

One so far: request correlation. There is no canonical-host redirect here —
``mcp.fanmoot.com`` is an API origin with a single host, and an MCP client that
followed a 301 mid-session would be a bug we caused. Origin *validation* for
the ``/mcp`` mount is a different thing and lands with ``S2-3``; it returns 403
rather than redirecting.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.logging_config import get_logger

log = get_logger("http")

#: Not logged as a request line. Docker's HEALTHCHECK curls this every 30
#: seconds and Traefik probes it too; at that rate the health check would be
#: most of the log. It still gets a request id and the header, so a failing
#: probe can still be correlated — it just does not narrate the healthy case.
_QUIET_PATHS = frozenset({"/healthz"})


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request id to every request, response and log line.

    An inbound ``X-Request-ID`` is honoured so a trace can be followed across a
    proxy — and, here, across the boundary between the demo's trace panel and
    this server. Otherwise one is minted. The id is bound into structlog's
    context-local storage, which is what makes every line emitted while handling
    one request findable together without passing a logger around.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Log with the request context still bound, then let the exception
            # handlers turn it into a 500 — swallowing it here would hide the
            # failure from the client.
            log.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        finally:
            structlog.contextvars.unbind_contextvars("request_id")

        response.headers["X-Request-ID"] = request_id
        if request.url.path not in _QUIET_PATHS:
            log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return response
