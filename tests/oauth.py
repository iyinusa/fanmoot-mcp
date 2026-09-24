"""Playing the client side of the OAuth dance, so each test can assert one thing.

The full flow is five HTTP calls — ``/authorize``, sign in, consent, ``/token``,
and then a tool call — and every ``S4`` test needs some prefix of it. Doing that
inline in each test would bury the assertion; doing it here means a test about
redirect URIs is four lines about redirect URIs.

**It really is the client side.** The verifier is generated, the challenge is
derived with the same S256 the server verifies, the hidden flow field is read
back out of the rendered HTML rather than minted, and the code comes out of a
``Location`` header. Nothing here reaches inside the server to shortcut a step —
a helper that called ``issue_code`` directly would test the helper.
"""

from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from app.oauth import pkce
from tests.conftest import STATIC_REDIRECT_URIS

#: The hidden field carrying the pending authorization between pages
#: (``app.oauth.flow``). Read out of the HTML because that is what a browser
#: does; a test that minted its own would not be exercising the signature.
_FLOW_FIELD = re.compile(r'name="flow"\s+value="([^"]+)"')

DEFAULT_REDIRECT_URI = STATIC_REDIRECT_URIS[0]
EMAIL = "fan@example.com"
PASSWORD = "correct horse battery staple"  # noqa: S105 - a fixture, not a credential


def pkce_pair() -> tuple[str, str]:
    """``(verifier, challenge)``, generated the way a real client does."""
    verifier = secrets.token_urlsafe(48)[:64]
    return verifier, pkce.challenge_for(verifier)


def flow_token(html: str) -> str:
    """The pending authorization out of a rendered page."""
    found = _FLOW_FIELD.search(html)
    assert found is not None, f"no flow field in the page: {html[:400]}"
    return found.group(1)


def query_of(response: httpx.Response) -> dict[str, str]:
    """The ``Location`` header's query, flattened."""
    location = response.headers.get("location", "")
    assert location, f"expected a redirect, got {response.status_code}"
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def location_path(response: httpx.Response) -> str:
    return urlsplit(response.headers.get("location", "")).path


async def authorize(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    challenge: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    state: str | None = "opaque-state",
    scope: str | None = None,
    resource: str | None = None,
    method: str | None = pkce.S256,
    response_type: str = "code",
    prompt: str | None = None,
) -> httpx.Response:
    """``GET /authorize`` with whatever a test wants to vary."""
    params: dict[str, Any] = {"client_id": client_id, "response_type": response_type}
    if prompt is not None:
        params["prompt"] = prompt
    if redirect_uri is not None:
        params["redirect_uri"] = redirect_uri
    if challenge:
        params["code_challenge"] = challenge
    if method is not None:
        params["code_challenge_method"] = method
    if state is not None:
        params["state"] = state
    if scope is not None:
        params["scope"] = scope
    if resource is not None:
        params["resource"] = resource
    return await http.get("/authorize", params=params)


async def sign_in(http: httpx.AsyncClient, page: httpx.Response) -> httpx.Response:
    """Post the sign-in form, arriving at consent."""
    return await http.post(
        "/oauth/login",
        data={"flow": flow_token(page.text), "email": EMAIL, "password": PASSWORD},
    )


async def approve(
    http: httpx.AsyncClient, page: httpx.Response, *, scopes: list[str] | None = None
) -> httpx.Response:
    """Press Allow, ticking either everything offered or exactly ``scopes``."""
    offered = re.findall(r'name="scope" value="([^"]+)"', page.text)
    return await http.post(
        "/oauth/consent",
        data={
            "flow": flow_token(page.text),
            "decision": "allow",
            "scope": scopes if scopes is not None else offered,
        },
    )


async def code_for(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    challenge: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scope: str | None = None,
    resource: str | None = None,
    approve_scopes: list[str] | None = None,
) -> str:
    """The whole browser leg, returning the authorization code."""
    page = await authorize(
        http,
        client_id=client_id,
        challenge=challenge,
        redirect_uri=redirect_uri,
        scope=scope,
        resource=resource,
    )
    assert page.status_code == 200, page.text[:400]
    consent = await sign_in(http, page)
    assert consent.status_code == 200, consent.text[:400]
    granted = await approve(http, consent, scopes=approve_scopes)
    assert granted.status_code == 302, granted.text[:400]
    return query_of(granted)["code"]


async def exchange(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str | None,
    code: str,
    verifier: str,
    redirect_uri: str | None = DEFAULT_REDIRECT_URI,
    resource: str | None = None,
) -> httpx.Response:
    """``POST /token`` for ``grant_type=authorization_code``."""
    form: dict[str, str] = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": code,
    }
    if client_secret is not None:
        form["client_secret"] = client_secret
    if verifier:
        form["code_verifier"] = verifier
    if redirect_uri is not None:
        form["redirect_uri"] = redirect_uri
    if resource is not None:
        form["resource"] = resource
    return await http.post("/token", data=form)


async def refresh(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
    resource: str | None = None,
    scope: str | None = None,
) -> httpx.Response:
    """``POST /token`` for ``grant_type=refresh_token``."""
    form: dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret is not None:
        form["client_secret"] = client_secret
    if resource is not None:
        form["resource"] = resource
    if scope is not None:
        form["scope"] = scope
    return await http.post("/token", data=form)


async def linked(
    http: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scope: str | None = None,
    approve_scopes: list[str] | None = None,
) -> dict[str, Any]:
    """The whole dance. Returns the token response body."""
    verifier, challenge = pkce_pair()
    code = await code_for(
        http,
        client_id=client_id,
        challenge=challenge,
        redirect_uri=redirect_uri,
        scope=scope,
        approve_scopes=approve_scopes,
    )
    response = await exchange(
        http,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        verifier=verifier,
        redirect_uri=redirect_uri,
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body
