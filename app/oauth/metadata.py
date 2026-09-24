"""The three discovery documents — how a client finds this server's OAuth at all.

`S4-2` and `S4-3`. Between them they answer the only two questions a client that
has never seen this server needs answered: *where do I get a token*, and *what is
this token for*.

**Protected-resource metadata (RFC 9728)** describes the MCP endpoint. Two
details are load-bearing:

* ``resource`` must be **exactly** the canonical URI of the resource — the same
  string ``addon.json`` names and the same string a token is minted for. One
  character of difference (a trailing slash, a missing ``/mcp``) and a compliant
  client discards every token it gets.
* ``authorization_servers`` lists ours **first**, because Alexa reads only the
  first entry. There is only one entry today; the ordering comment stays because
  the day a second appears is the day this matters and nobody will remember.

**Authorization-server metadata (RFC 8414)** describes the OAuth endpoints, and
``code_challenge_methods_supported: ["S256"]`` is not optional here in a stronger
sense than "the spec says so": **Alexa's add-on deploy fails without it.**

**OpenID Connect Discovery** is served from ``/.well-known/openid-configuration``
with the same document, because ``2025-11-25`` added that as a supported
discovery path and some clients probe it before the OAuth one. This server is
**not** an OpenID provider and the document is careful not to claim to be: there
is no ``openid`` scope, no ``id_token_signing_alg_values_supported`` and no
``userinfo_endpoint``, so a client that reads this cannot conclude it may ask for
an ID token. It is a second address for one document, not a second protocol.

All three are public, unauthenticated, and cacheable. Alexa caches authorization
metadata **at deploy time**, which is the runbook note that matters: changing
anything here means redeploying the add-on, not waiting for a TTL.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from starlette.responses import JSONResponse

from app import __version__
from app.oauth import scopes
from app.settings import Settings, get_settings

router = APIRouter(tags=["oauth-metadata"])

#: An hour. These documents change when this server is redeployed, and a client
#: that re-reads one every request is a client paying a round trip to learn
#: nothing. Public because there is nothing in them that is not.
_CACHE = {"Cache-Control": "public, max-age=3600"}

#: The path-insertion form of RFC 9728, for the resource at ``/mcp``.
PRM_PATH = "/.well-known/oauth-protected-resource/mcp"
#: The bare form. Served as well, because Alexa's account linking reads this one.
PRM_ROOT_PATH = "/.well-known/oauth-protected-resource"
AS_PATH = "/.well-known/oauth-authorization-server"
OIDC_PATH = "/.well-known/openid-configuration"


def protected_resource(settings: Settings | None = None) -> dict[str, Any]:
    """RFC 9728 — what the MCP endpoint is and who authorizes access to it."""
    settings = settings or get_settings()
    return {
        "resource": settings.resource_uri,
        # Ours first. Alexa reads only the first entry (`S4-2`).
        "authorization_servers": [settings.public_base_url],
        "scopes_supported": [scope.name for scope in scopes.SCOPES],
        # Header only. A token in a query string ends up in access logs and in
        # browser history, and RFC 6750 permits it precisely so that servers can
        # decline to.
        "bearer_methods_supported": ["header"],
        "resource_name": "Fanmoot",
        "resource_documentation": "https://fanmoot.com",
    }


def authorization_server(settings: Settings | None = None) -> dict[str, Any]:
    """RFC 8414 — every endpoint, and the S256 line Alexa's deploy checks for."""
    settings = settings or get_settings()
    document: dict[str, Any] = {
        # Exactly ``public_base_url``, with no trailing slash: a client stores
        # this and compares it byte for byte against the ``iss`` it later sees.
        "issuer": settings.public_base_url,
        "authorization_endpoint": settings.url_for("/authorize"),
        "token_endpoint": settings.url_for("/token"),
        "revocation_endpoint": settings.url_for("/oauth/revoke"),
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        # No ``implicit``, no ``password``. OAuth 2.1 removes both, and Alexa
        # supports neither.
        "grant_types_supported": ["authorization_code", "refresh_token"],
        # The line that makes Alexa's deploy pass (`S4-3`). ``plain`` is absent
        # because it is refused — see ``app.oauth.pkce``.
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "revocation_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ],
        "scopes_supported": [scope.name for scope in scopes.SCOPES],
        "service_documentation": "https://github.com/gensix/fanmoot-mcp#authentication",
        "ui_locales_supported": ["en-GB", "en-US"],
        # RFC 9207. The success redirect carries ``iss``, so a client with more
        # than one authorization server configured cannot be tricked into
        # redeeming a code at the wrong one.
        "authorization_response_iss_parameter_supported": True,
        # `prompt=create` opens the flow on the sign-up form (OpenID Connect
        # "Initiating User Registration" §4, which registers this field for
        # OAuth metadata too). No other prompt value is honoured.
        "prompt_values_supported": ["create"],
        # SEP-991 (`S4-17`): a client may identify itself by a URL that serves
        # its own metadata, instead of registering. Advertised alongside DCR
        # rather than instead of it — Claude Desktop, Cursor and the Inspector
        # use DCR today.
        "client_id_metadata_document_supported": settings.oauth_dcr_enabled,
        # This server's own build, so a judge or an operator can tell which
        # deployment answered. Not a specified field; harmless as an extension.
        "fanmoot_mcp_version": __version__,
    }
    if settings.oauth_dcr_enabled:
        # Omitted rather than pointed at a closed door when DCR is off: a client
        # that reads a registration endpoint and gets a 403 has a worse
        # experience than one that reads no endpoint and says so.
        document["registration_endpoint"] = settings.url_for("/oauth/register")
    return document


@router.get(PRM_PATH, include_in_schema=False)
@router.get(PRM_ROOT_PATH, include_in_schema=False)
async def protected_resource_metadata() -> JSONResponse:
    """Served at both paths — the spec-derived one and the one Alexa reads."""
    return JSONResponse(protected_resource(), headers=dict(_CACHE))


@router.get(AS_PATH, include_in_schema=False)
@router.get(OIDC_PATH, include_in_schema=False)
async def authorization_server_metadata() -> JSONResponse:
    """One document, two addresses. See the module docstring on why OIDC's path
    is served by a server that is not an OpenID provider."""
    return JSONResponse(authorization_server(), headers=dict(_CACHE))
