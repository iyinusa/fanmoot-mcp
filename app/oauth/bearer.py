"""What the ``/mcp`` mount does with a bearer token — and what it does without one.

`S4-9` and `S4-12`, and the shape of both is decided by one fact: **twelve of
this server's tools need no credential at all.** A fan can ask the score without
linking anything. So the mount cannot simply require a token — that would close
the read surface — and it cannot simply ignore one either.

Hence two pieces:

``GrantVerifier`` turns a presented token into a grant, or into nothing. It
populates the SDK's auth context, which is what makes ``get_access_token()``
work inside a tool and what ``app.mcp.telemetry`` reads to mark a call
authenticated. A token minted for a different ``resource`` is refused here, by
the SDK's own backend — RFC 8707's whole point being that a token for one
audience must not be replayable at another.

``ToolAuthGate`` is the part that has to exist because of how account linking
actually starts. Alexa begins linking when it gets a **401 with an RFC 9728
challenge** — an HTTP status, not a tool result. By the time a tool function
runs, the response is already an SSE stream and it is far too late to choose a
status code. So the gate reads the JSON-RPC frame *before* the session manager
sees it, and when the frame is a ``tools/call`` for a tool that needs a scope:

* **no usable token → 401** with ``WWW-Authenticate: Bearer …
  resource_metadata="…"``, naming the protected-resource document. Alexa starts
  account linking from this; every other client discovers the authorization
  server from it.
* **a token that lacks the scope → 403 ``insufficient_scope``**, naming the
  scope needed. That is SEP-835's incremental consent (`S4-12`): a read-only
  grant that reaches ``post_moot`` is *upgraded* by sending the fan through
  authorization again for the missing scope, rather than failing outright.

Which tools need which scope is ``app.oauth.scopes.TOOL_SCOPES``, declared
independently of the tools themselves — the gate has to answer before the tool
exists in the request, so the mapping cannot live on the tool.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.provider import AccessToken
from pydantic import AnyHttpUrl
from starlette.authentication import AuthCredentials
from starlette.datastructures import Headers
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db import get_sessionmaker
from app.logging_config import get_logger
from app.models.base import as_utc
from app.oauth import scopes as scope_module
from app.settings import Settings

log = get_logger("oauth.bearer")

#: The biggest JSON-RPC frame the gate will parse. An MCP request frame is a few
#: hundred bytes; anything past this is not a ``tools/call`` worth inspecting and
#: is handed straight through to the session manager, which has its own limits.
_MAX_INSPECTED_BYTES = 1024 * 1024


class GrantVerifier:
    """Resolves an opaque access token to the grant behind it (HAD-3).

    Implements the SDK's ``TokenVerifier`` protocol. Returning ``None`` means
    "not authenticated", which for this server is an ordinary state rather than
    an error — most calls arrive without a token and get a football answer.

    The ``claims`` carry ``grant_id`` so a tool can reach the link without the
    grant being threaded through every signature, and ``iss`` so the SDK's own
    session-binding check has an issuer to compare.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        from app.oauth import grants as grants_module

        try:
            async with get_sessionmaker()() as session:
                grant = await grants_module.resolve_access_token(session, token)
                if grant is None:
                    return None
                # Stamped here rather than in the tool: this is the one place
                # every authenticated call passes through, and "last used" on a
                # connected-apps page should mean "last did anything".
                await grants_module.touch(session, grant)
                await session.commit()

                expires_at = as_utc(grant.access_token_expires_at)
                return AccessToken(
                    token=token,
                    client_id=grant.client_id,
                    scopes=sorted(grant.scopes),
                    expires_at=int(expires_at.timestamp()) if expires_at else None,
                    resource=grant.resource,
                    subject=str(grant.fanmoot_user_id),
                    claims={
                        "iss": self._settings.public_base_url,
                        "grant_id": str(grant.id),
                        "handle": grant.fanmoot_handle,
                    },
                )
        except Exception as exc:
            # A database that is down must not turn every MCP request into a
            # 500: the read tools do not need it, and an unverifiable token is
            # indistinguishable from an absent one as far as this mount cares.
            log.warning("bearer_verification_failed", error_type=type(exc).__name__)
            return None


class ToolAuthGate:
    """Answers a ``tools/call`` that needs a scope the caller has not got.

    Reads the request body, decides, and replays the body downstream untouched —
    the session manager must see exactly the bytes the client sent, so the buffer
    is handed back through a substitute ``receive``.

    Anything it cannot parse is passed through rather than refused. A gate that
    rejected malformed JSON would be a second, worse error path for frames the
    MCP SDK already answers properly.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        body, replay = await _buffer(receive)
        needed = _scope_needed(body)
        if needed is None:
            await self.app(scope, replay, send)
            return

        token = scope.get("user")
        granted = scope.get("auth")
        has_token = token is not None and getattr(token, "access_token", None) is not None

        if not has_token:
            presented = _has_bearer_header(scope)
            log.info("mcp_auth_challenge", scope=needed, token_presented=presented)
            await self._challenge(
                send,
                status=401,
                error="invalid_token",
                description=(
                    "This tool acts as you on Fanmoot, so it needs your account connected."
                    if not presented
                    else "That access token is expired or no longer valid."
                ),
                scope=needed,
            )
            return

        if not isinstance(granted, AuthCredentials) or needed not in granted.scopes:
            # SEP-835 (`S4-12`): not a refusal, an upgrade request. The client
            # re-authorizes for the named scope and the fan sees one extra
            # consent screen rather than a broken tool.
            log.info("mcp_scope_upgrade_required", scope=needed)
            await self._challenge(
                send,
                status=403,
                error="insufficient_scope",
                description=f"This needs permission to {_phrase(needed)}.",
                scope=needed,
            )
            return

        await self.app(scope, replay, send)

    async def _challenge(
        self, send: Send, *, status: int, error: str, description: str, scope: str
    ) -> None:
        """A 401 or 403 carrying the RFC 9728 challenge (`S4-9`).

        ``resource_metadata`` is the whole point: it is how a client that has
        never seen this server finds the authorization server, and how Alexa
        knows to start account linking rather than to give up.
        """
        challenge = (
            f'Bearer error="{error}", error_description="{description}", '
            f'scope="{scope}", '
            f'resource_metadata="{self.settings.resource_metadata_url}"'
        )
        payload = json.dumps(
            {
                "error": error,
                "error_description": description,
                "scope": scope,
                "resource_metadata": self.settings.resource_metadata_url,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"www-authenticate", challenge.encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def _has_bearer_header(scope: Scope) -> bool:
    header = Headers(scope=scope).get("authorization", "")
    return header.lower().startswith("bearer ")


async def _buffer(receive: Receive) -> tuple[bytes, Receive]:
    """Read the whole request body and return it with a ``receive`` that replays it."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            # A disconnect mid-body. Replay what there is; the app below will
            # see the disconnect too and unwind.
            chunks.append(b"")
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)

    sent = False

    async def replay() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return await receive()

    return body, replay


def _scope_needed(body: bytes) -> str | None:
    """The scope this frame requires, or None if it needs no credential.

    Handles a batch — ``2025-11-25`` still permits a JSON-RPC array — by
    returning the first scope any member needs. One challenge for the batch is
    correct: the client has to re-authorize before any of it can run.
    """
    if not body or len(body) > _MAX_INSPECTED_BYTES:
        return None
    try:
        frame = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None

    for call in frame if isinstance(frame, list) else [frame]:
        needed = _scope_for_frame(call)
        if needed is not None:
            return needed
    return None


def _scope_for_frame(frame: Any) -> str | None:
    if not isinstance(frame, dict) or frame.get("method") != "tools/call":
        return None
    params = frame.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    if not isinstance(name, str):
        return None
    return scope_module.required_for(name)


def _phrase(scope: str) -> str:
    """The scope, as the half-sentence a challenge description can end with."""
    described = scope_module.SCOPES_BY_NAME.get(scope)
    if described is None:  # pragma: no cover - TOOL_SCOPES only names real ones
        return scope
    return described.summary[0].lower() + described.summary[1:]


def protect(app: ASGIApp, settings: Settings) -> ASGIApp:
    """Wrap the MCP application in verification, context and the gate.

    Order matters and reads outside in: authenticate the token, publish it to
    the auth context so a tool can read it, then decide whether this particular
    frame may proceed. The gate is innermost of the three because it needs both
    of the others to have run.
    """
    gated = ToolAuthGate(app, settings)
    with_context = AuthContextMiddleware(gated)
    return AuthenticationMiddleware(
        with_context,
        backend=BearerAuthBackend(
            GrantVerifier(settings),
            # RFC 8707: a token minted for another resource is not valid here,
            # whatever else is true about it.
            resource_server_url=AnyHttpUrl(settings.resource_uri),
        ),
        on_error=_auth_error,
    )


def _auth_error(conn: Any, exc: Exception) -> Any:
    """Starlette's hook for a backend that raised. Ours does not.

    It returns None on every failure — an unverifiable token is an anonymous
    request — so this exists only so a future change cannot silently produce
    Starlette's default 400, which would take the read tools down with it.
    """
    from starlette.responses import JSONResponse

    log.warning("bearer_backend_error", error_type=type(exc).__name__)
    return JSONResponse({"error": "invalid_token"}, status_code=401)
