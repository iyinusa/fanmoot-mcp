"""The browser leg: what is carried between ``/authorize``, sign-in and consent.

An authorization request arrives at ``/authorize`` and cannot be answered yet —
the fan has to sign in, or sign up, and then agree. That is two or three page
loads, and the request's parameters have to survive them **unaltered**: the
``redirect_uri``, the PKCE challenge, the ``state``, the scopes, the resource. If
any of them can be edited between the first page and the last, the whole flow is
theatre.

There are two ways to carry that: a row per pending authorization, or a signed
token in the form. This server signs a token, for three reasons — an eighth table
for something that lives ninety seconds is a table to reap; a pending
authorization that *is* its signature cannot be tampered with, so there is no
"was this the request I validated?" question at the end; and nothing about a
half-finished authorization needs to survive a restart.

Two token kinds live here, distinguished by ``typ``:

* **``authorize``** — the pending request, in a hidden form field. It gains the
  signed-in fan's upstream session id once they have signed in, and is what the
  consent POST hands back.
* **``session``** — an httpOnly cookie on this origin saying "this browser has a
  Fanmoot session with us". It is what lets a second ``/authorize`` skip the
  password, and what the "not you?" escape (`S4-10`) clears.

Both are HS256, signed with a key derived from ``FMCP_SECRET_ENCRYPTION_KEY``
(see ``tokens.signing_key``), and both are short-lived. Neither carries a
credential: the cookie names an ``upstream_sessions`` row, it does not contain
the Fanmoot token, so a stolen cookie is worth a consent screen on a device that
still has to get past the client's own redirect URI.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import jwt

from app.models.base import utcnow
from app.oauth import tokens
from app.settings import Settings, get_settings

_ALGORITHM = "HS256"

#: The cookie the AS sets for its own pages. ``__Host-`` is not used because the
#: prefix requires ``Secure``, which cannot be set on the http://localhost origin
#: a developer runs against — the flag itself is set from ``is_production``.
SESSION_COOKIE = "fmcp_as_session"

#: How long a browser session on this origin lasts. An hour: long enough to link
#: two clients in one sitting, short enough that a shared laptop does not hand
#: the next person a consent screen.
SESSION_TTL_S = 3600


class FlowExpired(Exception):
    """The carried state is missing, expired or has been tampered with.

    All three are one case on purpose. The page says "that took too long, start
    again" and the flow restarts at ``/authorize`` — where every parameter is
    validated from scratch — rather than trying to rescue a request whose
    integrity is in question.
    """


@dataclass(frozen=True, slots=True)
class Pending:
    """One authorization request, mid-flight."""

    client_id: str
    #: The registered name, carried so the sign-in page can say which app is
    #: asking without a database lookup — and carried *signed*, so it is the name
    #: ``/authorize`` validated rather than one from a query parameter.
    client_name: str
    redirect_uri: str
    scope: str
    code_challenge: str
    code_challenge_method: str
    state: str | None = None
    resource: str | None = None
    #: Set once the fan has signed in or signed up. Until then the flow knows
    #: what was asked for and not by whom.
    upstream_session_id: uuid.UUID | None = None
    #: Carried so the consent screen can say "signed in as @handle" without a
    #: second query, and so "not you?" has something to name.
    handle: str | None = None
    #: True when the account was created in this flow (`S4-11`), so the page
    #: after consent can mention the verification email.
    fresh_account: bool = False

    def signed_in_as(
        self, *, upstream_session_id: uuid.UUID, handle: str, fresh: bool = False
    ) -> Pending:
        return replace(
            self,
            upstream_session_id=upstream_session_id,
            handle=handle,
            fresh_account=fresh or self.fresh_account,
        )

    def signed_out(self) -> Pending:
        """The same request with nobody attached — the "not you?" escape (`S4-10`)."""
        return replace(self, upstream_session_id=None, handle=None, fresh_account=False)


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """ "This browser is signed in to Fanmoot with us", as a cookie."""

    upstream_session_id: uuid.UUID
    fanmoot_user_id: uuid.UUID
    handle: str


def issue(pending: Pending, *, settings: Settings | None = None) -> str:
    """Sign a pending authorization for the hidden field."""
    settings = settings or get_settings()
    now = utcnow()
    claims: dict[str, Any] = {
        "typ": "authorize",
        "iat": now,
        "exp": now + timedelta(seconds=settings.oauth_flow_ttl_s),
        "client_id": pending.client_id,
        "client_name": pending.client_name,
        "redirect_uri": pending.redirect_uri,
        "scope": pending.scope,
        "code_challenge": pending.code_challenge,
        "code_challenge_method": pending.code_challenge_method,
    }
    if pending.state is not None:
        claims["state"] = pending.state
    if pending.resource is not None:
        claims["resource"] = pending.resource
    if pending.upstream_session_id is not None:
        claims["ups"] = str(pending.upstream_session_id)
    if pending.handle is not None:
        claims["handle"] = pending.handle
    if pending.fresh_account:
        claims["fresh"] = True
    return jwt.encode(claims, tokens.signing_key(), algorithm=_ALGORITHM)


def read(token: str | None) -> Pending:
    """Verify and unpack a pending authorization. Raises ``FlowExpired``."""
    claims = _decode(token, expected="authorize")
    upstream = claims.get("ups")
    return Pending(
        client_id=str(claims["client_id"]),
        client_name=str(claims.get("client_name") or ""),
        redirect_uri=str(claims["redirect_uri"]),
        scope=str(claims.get("scope") or ""),
        code_challenge=str(claims["code_challenge"]),
        code_challenge_method=str(claims["code_challenge_method"]),
        state=claims.get("state"),
        resource=claims.get("resource"),
        upstream_session_id=uuid.UUID(upstream) if upstream else None,
        handle=claims.get("handle"),
        fresh_account=bool(claims.get("fresh", False)),
    )


def issue_session(session: BrowserSession) -> str:
    """Sign the browser-session cookie."""
    now = utcnow()
    return jwt.encode(
        {
            "typ": "session",
            "iat": now,
            "exp": now + timedelta(seconds=SESSION_TTL_S),
            "ups": str(session.upstream_session_id),
            "sub": str(session.fanmoot_user_id),
            "handle": session.handle,
        },
        tokens.signing_key(),
        algorithm=_ALGORITHM,
    )


def read_session(token: str | None) -> BrowserSession | None:
    """Unpack the cookie, or None if there isn't a usable one.

    None rather than an exception: an absent or stale cookie is the ordinary
    case — it means "ask them to sign in" — and a flow that raised on it would
    be a flow that broke every time one expired.
    """
    try:
        claims = _decode(token, expected="session")
    except FlowExpired:
        return None
    return BrowserSession(
        upstream_session_id=uuid.UUID(str(claims["ups"])),
        fanmoot_user_id=uuid.UUID(str(claims["sub"])),
        handle=str(claims.get("handle") or ""),
    )


def _decode(token: str | None, *, expected: str) -> dict[str, Any]:
    if not token:
        raise FlowExpired("nothing to read")
    try:
        claims = jwt.decode(
            token,
            tokens.signing_key(),
            algorithms=[_ALGORITHM],
            # Belt and braces: PyJWT verifies `exp` by default, and saying so
            # here means a future option change cannot quietly turn it off.
            options={"require": ["exp", "typ"], "verify_exp": True},
        )
    except (jwt.InvalidTokenError, tokens.SecretsUnavailable) as exc:
        raise FlowExpired(str(exc)) from None
    if claims.get("typ") != expected:
        # A session cookie replayed into the form field, or the reverse. Not a
        # thing that happens by accident.
        raise FlowExpired("wrong token type")
    return claims
