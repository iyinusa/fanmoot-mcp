"""ASGI middleware.

Two: request correlation, and the security headers every response carries
(`S7-5`). There is no canonical-host redirect here —
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


# ── Security headers (S7-5) ──────────────────────────────────────────────────
#: On every response. `nosniff` because `/static` serves SVG, which a browser
#: that guessed would happily run as a document; `no-referrer` because an OAuth
#: URL is full of things a referrer should not carry to a client's website.
_ALWAYS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)

#: On HTML only — the landing, sign-in, sign-up, consent, connected-apps and
#: legal pages. Every one of them is script-free and draws only its own
#: stylesheets, icons and (on the landing page) self-hosted Lato, so the policy
#: can say exactly that. `frame-ancestors 'none'` is the one that
#: matters most: a consent screen somebody else can frame is a consent screen
#: somebody else can trick a fan into clicking.
#:
#: There is deliberately **no `form-action`**. Chrome applies it to the redirect
#: that *follows* a form post, and the consent form's whole job is to redirect to
#: a client's callback — Alexa's, or a loopback port on the fan's own machine. A
#: `form-action 'self'` would break every account link in the one browser most
#: fans use.
_HTML_CSP = (
    b"default-src 'none'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
    b"base-uri 'none'; frame-ancestors 'none'"
)
_HTML_ONLY = (
    (b"content-security-policy", _HTML_CSP),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
)

#: Two years, the preload list's own floor. `includeSubDomains` covers only
#: hosts *under* mcp.fanmoot.com — there are none — and never fanmoot.com itself.
_HSTS = (b"strict-transport-security", b"max-age=63072000; includeSubDomains")


class SecurityHeadersMiddleware:
    """Add the headers a public HTTPS origin owes its callers.

    Raw ASGI for the same reason as ``RequestIdMiddleware``: ``/mcp`` answers
    in SSE, and this only touches ``http.response.start``. A header a handler
    already set wins. The OAuth pages set their own ``Cache-Control``, and the
    bearer gate its own challenge, and neither is second-guessed here.

    HSTS goes out only when this server's public URL is ``https``. Sent from
    ``http://localhost`` it would be ignored by a browser anyway, and an
    operator reading local headers should see what production actually sends.
    Traefik terminates TLS, so the request that reaches here is plain HTTP
    whatever the fan typed. The *configured* origin is the honest signal, and
    the scheme of this hop is not.
    """

    def __init__(self, app: ASGIApp, *, https: bool) -> None:
        self.app = app
        self.https = https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                extra = list(_ALWAYS)
                if headers.get("content-type", "").startswith("text/html"):
                    extra.extend(_HTML_ONLY)
                if self.https:
                    extra.append(_HSTS)
                for name, value in extra:
                    key = name.decode("latin-1")
                    if key not in headers:
                        headers[key] = value.decode("latin-1")
            await send(message)

        await self.app(scope, receive, secured)
