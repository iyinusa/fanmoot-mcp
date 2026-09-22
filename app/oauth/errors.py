"""OAuth failures, as the two shapes the specification allows.

An authorization server has exactly two ways to say no, and choosing the wrong
one is a security bug rather than a formatting mistake:

**A JSON error body** (RFC 6749 §5.2) for the token, registration and revocation
endpoints. ``error`` is one of a fixed set of codes the client branches on;
``error_description`` is for the developer reading the log, never for the fan.

**A redirect carrying ``error`` and ``state``** for ``/authorize`` — but *only*
once the client and its redirect URI have been established. Before that the
request cannot be redirected anywhere, because the only place to send it is a URI
supplied by whoever made the request. An open redirector is the classic
authorization-server hole, and this is where it would be: a server that helpfully
bounced "unknown client" back to an attacker-supplied URL is a server that will
forward a code there next. So ``FlowRefusal`` exists, it renders an HTML page on
*this* origin, and the router never redirects an unvalidated URI.

Every JSON response here carries ``Cache-Control: no-store``. A token endpoint's
response holds a credential, and a proxy or browser that cached one would hand it
to the next caller.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.responses import JSONResponse, RedirectResponse

#: Applied to every response in this module. ``no-store`` rather than
#: ``no-cache``: the latter permits storing and revalidating, which for a
#: credential is the wrong half of the guarantee.
NO_STORE: dict[str, str] = {"Cache-Control": "no-store", "Pragma": "no-cache"}


class OAuthError(Exception):
    """A failure with an RFC 6749 code, carried as JSON.

    ``description`` is written for whoever is integrating — it names the
    parameter and what was wrong with it — because that is who reads it. Nothing
    here is ever spoken to a fan; the fan-facing sentences live in the templates
    and in ``app.mcp.errors``.
    """

    #: Overridden per subclass. ``invalid_request`` is the RFC's catch-all.
    error = "invalid_request"
    status = 400

    def __init__(self, description: str = "", **extra: Any) -> None:
        super().__init__(description or self.error)
        self.description = description
        self.extra = extra

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.error}
        if self.description:
            body["error_description"] = self.description
        body.update(self.extra)
        return body

    def response(self) -> JSONResponse:
        headers = dict(NO_STORE)
        if self.status == 401:
            # RFC 6749 §5.2: a 401 from the token endpoint must carry a
            # challenge, because a client authenticating with HTTP Basic is
            # entitled to know which scheme was expected.
            headers["WWW-Authenticate"] = 'Basic realm="fanmoot-mcp", charset="UTF-8"'
        return JSONResponse(self.body(), status_code=self.status, headers=headers)


class InvalidRequest(OAuthError):
    """A parameter is missing, repeated or malformed."""

    error = "invalid_request"


class InvalidClient(OAuthError):
    """The client could not be identified or failed to authenticate.

    401, and deliberately not distinguished from "wrong secret": telling a caller
    which of the two it was turns the token endpoint into a client-id oracle.
    """

    error = "invalid_client"
    status = 401


class InvalidGrant(OAuthError):
    """The code or refresh token is unknown, expired, already spent, or was
    issued to a different client or redirect URI.

    One code for all of those, on purpose. A client cannot act differently on
    "expired" than on "already used", and an attacker probing codes learns
    nothing from a server that answers every failure identically.
    """

    error = "invalid_grant"


class UnauthorizedClient(OAuthError):
    """This client is registered but not for this grant type."""

    error = "unauthorized_client"


class UnsupportedGrantType(OAuthError):
    """A grant type this server does not implement — including ``implicit``,
    which OAuth 2.1 removes and Alexa does not support."""

    error = "unsupported_grant_type"


class InvalidScope(OAuthError):
    """A scope this server does not issue, or one beyond what the client
    registered for."""

    error = "invalid_scope"


class InvalidTarget(OAuthError):
    """RFC 8707: the ``resource`` asked for is not one this server issues tokens for."""

    error = "invalid_target"


class AccessDenied(OAuthError):
    """The fan said no."""

    error = "access_denied"


class ServerError(OAuthError):
    """Something on this side failed. 500, and the detail stays in the log."""

    error = "server_error"
    status = 500


class TemporarilyUnavailable(OAuthError):
    """Rate limited or shedding load. Carries ``Retry-After`` where it can."""

    error = "temporarily_unavailable"
    status = 429

    def __init__(self, description: str = "", *, retry_after: int | None = None) -> None:
        super().__init__(description)
        self.retry_after = retry_after

    def response(self) -> JSONResponse:
        response = super().response()
        if self.retry_after is not None:
            response.headers["Retry-After"] = str(self.retry_after)
        return response


class FlowRefusal(Exception):
    """A refusal that must **not** be redirected — rendered on this origin instead.

    Raised when the ``client_id`` is unknown or the ``redirect_uri`` does not
    match the registered set (`S4-5`). Both mean the same thing: there is no
    established place to send this fan, so the only safe answer is a page here
    saying so. ``headline`` and ``detail`` are read by a person, so they are
    sentences rather than codes — and they never echo an attacker-supplied URI
    back into the page.
    """

    def __init__(self, headline: str, detail: str, *, status: int = 400) -> None:
        super().__init__(headline)
        self.headline = headline
        self.detail = detail
        self.status = status


def redirect_error(
    redirect_uri: str,
    *,
    error: str,
    description: str = "",
    state: str | None = None,
) -> RedirectResponse:
    """Send a failure back to a client whose redirect URI has been validated.

    The parameters are merged into the URI's existing query rather than
    replacing it: a client may register a redirect URI that already carries
    query parameters of its own, and dropping them is how a working integration
    breaks on the error path only.

    302, and ``no-store`` on the redirect too — the URL of a redirect is a thing
    caches key on.
    """
    parts = urlsplit(redirect_uri)
    # Decoded and re-encoded rather than concatenated: appending to the raw query
    # string would double-encode a client's own parameters the first time one of
    # them contained an ampersand.
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("error", error))
    if description:
        query.append(("error_description", description))
    if state is not None:
        # Echoed whatever happens, including on the error path: `state` is the
        # client's CSRF defence and a client that cannot match the response to
        # its request has to treat it as unsolicited.
        query.append(("state", state))
    target = urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)
    )
    return RedirectResponse(target, status_code=302, headers=dict(NO_STORE))
