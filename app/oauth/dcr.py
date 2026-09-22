"""Dynamic Client Registration — the endpoint that makes this server general.

`S4-16`, and §2.2's whole argument in one route. Alexa cannot self-register, so
this server needs static credentials; **Claude, ChatGPT, Cursor and MCP
Inspector all self-register**, because that is how they connect to a remote MCP
server they have never seen. A server with only static registration is an Alexa
add-on wearing an MCP costume: every other client would need a human to mint
credentials by hand first.

So registration is **open**, with the guards that implies:

* **Per-IP rate limiting** (`app.oauth.ratelimit`) — registration writes rows, so
  an unlimited endpoint is an unlimited table.
* **Redirect URI policy** — ``https``, plus ``http://localhost`` and
  ``http://127.0.0.1`` for the loopback callbacks desktop clients genuinely need
  (``app.oauth.clients.validate_redirect_uris``).
* **A server-generated ``client_id``** — RFC 7591 requires it, and the ``fmcp_``
  prefix makes a self-registered client visible in a log line rather than merely
  distinguishable.
* **A TTL** — a registration that never completes a grant is reaped, so anyone
  can create a row and nobody can leave litter.

RFC 7592's read endpoint is here too. A client that registered is handed a
registration access token, and this is where it can come back and read its own
registration — which is what the Inspector does to confirm what it got.

Client ID metadata documents (SEP-991, `S4-17`) need no endpoint of their own:
the client's ``client_id`` *is* the URL, and ``clients.find_or_resolve`` fetches
it on first use. It is advertised in the authorization-server metadata alongside
DCR.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.db import get_session
from app.logging_config import get_logger
from app.oauth import clients as client_store
from app.oauth import ratelimit, tokens
from app.oauth.errors import NO_STORE, InvalidClient, InvalidRequest, TemporarilyUnavailable
from app.settings import get_settings

router = APIRouter(prefix="/oauth", tags=["oauth-registration"])
log = get_logger("oauth.dcr")


@router.post("/register", include_in_schema=False)
async def register(request: Request, session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """RFC 7591 — create a client from the metadata in the request body."""
    settings = get_settings()

    if not settings.oauth_dcr_enabled:
        # 403 rather than 404: the endpoint exists and is switched off, and a
        # client integrating against this server should be told which.
        return JSONResponse(
            {
                "error": "access_denied",
                "error_description": (
                    "Dynamic client registration is disabled on this server. "
                    "Ask the operator to register a client."
                ),
            },
            status_code=403,
            headers=dict(NO_STORE),
        )

    decision = ratelimit.hit(
        "oauth.register",
        ratelimit.client_ip(request),
        limit=settings.oauth_register_limit,
        window_s=settings.oauth_register_window_s,
    )
    if not decision.allowed:
        return TemporarilyUnavailable(
            "Too many registrations from this address. Try again shortly.",
            retry_after=decision.retry_after,
        ).response()

    try:
        metadata = await request.json()
    except Exception:
        return InvalidRequest("The request body must be JSON client metadata.").response()
    if not isinstance(metadata, dict):
        return InvalidRequest("The request body must be a JSON object.").response()

    try:
        client, secret, registration_token = await client_store.register(
            session, metadata, settings=settings
        )
    except client_store.RegistrationRefused as exc:
        # RFC 7591 §3.2.2 has its own error codes, distinct from RFC 6749's, and
        # they are returned verbatim: a client that sent a bad redirect URI can
        # act on `invalid_redirect_uri` and cannot act on `invalid_request`.
        return JSONResponse(
            {"error": exc.error, "error_description": exc.description},
            status_code=400,
            headers=dict(NO_STORE),
        )

    body: dict[str, Any] = client.as_registration_response(
        client_id_issued_at=int(client.created_at.timestamp())
        if client.created_at is not None
        else None,
        registration_access_token=registration_token,
        registration_client_uri=settings.url_for(f"/oauth/register/{client.client_id}"),
    )
    if secret is not None:
        body["client_secret"] = secret
        # 0 means "does not expire" in RFC 7591. The *registration* can be
        # reaped before it is ever used (`S4-16`); the secret itself has no
        # separate life, and saying so stops a client scheduling a rotation it
        # cannot perform.
        body["client_secret_expires_at"] = 0

    return JSONResponse(body, status_code=201, headers=dict(NO_STORE))


@router.get("/register/{client_id}", include_in_schema=False)
async def read_registration(
    client_id: str, request: Request, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    """RFC 7592 — a client reading back its own registration.

    Authenticated with the registration access token it was handed, compared in
    constant time against the stored hash. Any failure is the same
    ``invalid_client``: this endpoint must not report whether a ``client_id``
    exists to a caller who cannot authenticate as it.
    """
    header = request.headers.get("authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""

    client = await client_store.find(session, client_id)
    if client is None or not tokens.matches(presented, client.registration_access_token_hash):
        return InvalidClient(
            "Unknown client, or the registration access token is wrong."
        ).response()

    return JSONResponse(client.as_registration_response(), headers=dict(NO_STORE))
