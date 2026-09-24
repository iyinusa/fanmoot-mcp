"""The three discovery documents (`S4-2`, `S4-3`).

Two of these assertions are the difference between a working add-on and a
deploy that fails with no useful message, so they are asserted as *rules* rather
than as shapes:

* ``code_challenge_methods_supported`` **must** contain ``S256``. Alexa's deploy
  checks it.
* ``resource`` must be exactly the canonical URI — the same string a token is
  minted for and ``addon.json`` names. One character out and a compliant client
  discards every token it is issued.
"""

from __future__ import annotations

import httpx

from app.oauth import scopes
from app.settings import Settings


async def test_protected_resource_metadata_names_the_mcp_endpoint(
    oauth_http: httpx.AsyncClient, oauth_env: Settings
) -> None:
    response = await oauth_http.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == "https://mcp.fanmoot.test/mcp"
    # Ours first: Alexa reads only the first entry.
    assert body["authorization_servers"][0] == "https://mcp.fanmoot.test"
    assert set(body["scopes_supported"]) == set(scopes.SUPPORTED_SET)
    # Header only. A token in a query string ends up in access logs.
    assert body["bearer_methods_supported"] == ["header"]


async def test_protected_resource_metadata_is_served_at_both_paths(
    oauth_http: httpx.AsyncClient,
) -> None:
    """RFC 9728's path-insertion form is what a spec-derived client computes for
    a resource at ``/mcp``; the bare path is what Alexa's account linking reads.
    Both have to answer, with the same document."""
    derived = await oauth_http.get("/.well-known/oauth-protected-resource/mcp")
    bare = await oauth_http.get("/.well-known/oauth-protected-resource")

    assert derived.status_code == 200
    assert derived.json() == bare.json()


async def test_authorization_server_metadata_advertises_s256(
    oauth_http: httpx.AsyncClient,
) -> None:
    """Alexa's add-on deploy fails without this line. It is not a nicety."""
    body = (await oauth_http.get("/.well-known/oauth-authorization-server")).json()

    assert body["code_challenge_methods_supported"] == ["S256"]
    assert "plain" not in body["code_challenge_methods_supported"]


async def test_authorization_server_metadata_has_every_endpoint(
    oauth_http: httpx.AsyncClient,
) -> None:
    body = (await oauth_http.get("/.well-known/oauth-authorization-server")).json()

    assert body["issuer"] == "https://mcp.fanmoot.test"
    assert body["authorization_endpoint"] == "https://mcp.fanmoot.test/authorize"
    assert body["token_endpoint"] == "https://mcp.fanmoot.test/token"
    assert body["revocation_endpoint"] == "https://mcp.fanmoot.test/oauth/revoke"
    # So a self-registering client finds DCR without being told (`S4-16`).
    assert body["registration_endpoint"] == "https://mcp.fanmoot.test/oauth/register"
    assert body["response_types_supported"] == ["code"]
    assert set(body["grant_types_supported"]) == {"authorization_code", "refresh_token"}
    # OAuth 2.1 removes both, and Alexa supports neither.
    assert "implicit" not in body["grant_types_supported"]
    assert "password" not in body["grant_types_supported"]
    # SEP-991 (`S4-17`), advertised alongside DCR rather than instead of it.
    assert body["client_id_metadata_document_supported"] is True
    # RFC 9207, so a client with several authorization servers cannot be tricked
    # into redeeming a code at the wrong one.
    assert body["authorization_response_iss_parameter_supported"] is True
    # A client may open the flow on the sign-up form, and read here that it can.
    assert body["prompt_values_supported"] == ["create"]


async def test_openid_configuration_serves_the_same_document(
    oauth_http: httpx.AsyncClient,
) -> None:
    """``2025-11-25`` added OIDC Discovery as a supported path and some clients
    probe it first — but this server is not an OpenID provider, and the document
    is careful not to claim to be."""
    oidc = await oauth_http.get("/.well-known/openid-configuration")
    oauth = await oauth_http.get("/.well-known/oauth-authorization-server")

    assert oidc.status_code == 200
    assert oidc.json() == oauth.json()
    body = oidc.json()
    assert "id_token_signing_alg_values_supported" not in body
    assert "userinfo_endpoint" not in body
    assert "openid" not in body["scopes_supported"]


async def test_discovery_documents_are_cacheable_and_public(
    oauth_http: httpx.AsyncClient,
) -> None:
    """Alexa caches authorization metadata at deploy time; everyone else should
    not re-read it per request. There is nothing in either document that is not
    already public."""
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ):
        response = await oauth_http.get(path)
        assert "public" in response.headers["cache-control"]
        assert "max-age" in response.headers["cache-control"]
