"""``Origin`` validation for the ``/mcp`` mount — a ``2025-11-25`` MUST.

``S2-3``. The attack it closes is DNS rebinding: a page on ``evil.example``
points a hostname it controls at ``127.0.0.1`` (or, once a fan has this server
on their network, at it) and then makes ordinary ``fetch`` calls from the
browser. Same-origin policy does not stop the *request* — it stops the attacker
reading the response, which is no help at all when the request itself posts a
moot. What does stop it is the browser's own ``Origin`` header, which it always
sends and a page cannot forge. So the spec requires the server to check it, and
this returns **403** when it does not like what it sees.

Three deliberate choices:

**An absent ``Origin`` is allowed.** Alexa+, Claude Desktop, Cursor, ``curl``
and the MCP Inspector's proxy are not browsers and send no ``Origin``. Refusing
them would close the server to every client it exists for while stopping
nothing: the header's value is that a browser *cannot omit it*.

**``Origin: null`` is refused.** That is what a sandboxed iframe, a ``file://``
page or a redirect chain that lost its origin sends — a browser declining to
say where it came from. Treating it as "no browser" would hand an attacker the
one string that gets them past the check.

**``Host`` is not validated here.** This server sits behind Traefik on one
hostname, and the SDK answers a ``Host`` it dislikes with 421, which a proxy
misconfiguration would turn into a total outage that looks like a protocol bug.
Host pinning belongs in the reverse proxy, where it already is.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.logging_config import get_logger

log = get_logger("mcp.security")

#: What a rejected browser is told. No detail: a page that is probing which
#: origins this server accepts learns nothing from it, and a developer who has
#: misconfigured one reads the log line, which names the origin.
_REFUSAL = "Origin not allowed."

#: Loopback in the three spellings a browser may use. Allowed only outside
#: production, where they are the MCP Inspector and a locally served demo.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def normalise_origin(origin: str) -> str:
    """An origin reduced to ``scheme://host[:port]``, lowercased.

    Browsers are consistent about this, but operators writing
    ``FMCP_MCP_ALLOWED_ORIGINS`` by hand are not — a trailing slash or a
    capital letter in a configured origin would silently refuse the very client
    it was added for.
    """
    parts = urlsplit(origin.strip())
    if not parts.scheme or not parts.netloc:
        return origin.strip().lower()
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


class OriginValidation:
    """ASGI middleware wrapping the Streamable HTTP app.

    Wrapped around the ``/mcp`` app rather than added to the whole application:
    the OAuth consent and sign-up pages ``S4`` serves are ordinary web pages
    that a browser reaches by navigating, and a navigation carries no ``Origin``
    a policy written for an API endpoint could sensibly judge.
    """

    def __init__(
        self,
        app: ASGIApp,
        allowed: Iterable[str],
        *,
        allow_loopback: bool = False,
    ) -> None:
        self.app = app
        self.allowed = frozenset(normalise_origin(origin) for origin in allowed if origin.strip())
        self.allow_loopback = allow_loopback

    def permits(self, origin: str | None) -> bool:
        """Whether a request carrying this ``Origin`` may proceed."""
        if origin is None:
            return True
        candidate = normalise_origin(origin)
        if candidate in self.allowed:
            return True
        if not self.allow_loopback:
            return False
        parts = urlsplit(candidate)
        if parts.scheme not in ("http", "https"):
            return False
        return (parts.hostname or "") in _LOOPBACK_HOSTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - the mount is HTTP only
            await self.app(scope, receive, send)
            return

        origin = Headers(scope=scope).get("origin")
        if not self.permits(origin):
            log.warning("mcp_origin_refused", origin=origin)
            await PlainTextResponse(_REFUSAL, status_code=403)(scope, receive, send)
            return

        await self.app(scope, receive, send)
