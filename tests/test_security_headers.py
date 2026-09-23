"""``S7-5``: the headers a public HTTPS origin owes the people who call it."""

from __future__ import annotations

import httpx

from app.settings import Settings


async def test_every_response_is_nosniff_and_no_referrer(client: httpx.AsyncClient) -> None:
    response = await client.get("/.well-known/oauth-authorization-server")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_no_hsts_from_a_plain_http_origin(client: httpx.AsyncClient) -> None:
    """The suite's origin is http://testserver. HSTS there would be ignored by a
    browser and would mislead an operator reading local headers."""
    response = await client.get("/privacy")

    assert "strict-transport-security" not in response.headers


async def test_hsts_from_an_https_origin(
    oauth_env: Settings, oauth_http: httpx.AsyncClient
) -> None:
    response = await oauth_http.get("/.well-known/oauth-protected-resource")

    assert response.headers["strict-transport-security"].startswith("max-age=63072000")


async def test_html_cannot_be_framed_or_run_script(client: httpx.AsyncClient) -> None:
    response = await client.get("/privacy")
    policy = response.headers["content-security-policy"]

    assert "frame-ancestors 'none'" in policy
    assert "default-src 'none'" in policy
    assert "script-src" not in policy
    assert response.headers["x-frame-options"] == "DENY"


async def test_no_form_action_so_the_consent_redirect_still_works(
    client: httpx.AsyncClient,
) -> None:
    """Chrome applies form-action to the redirect after a form post. The consent
    form redirects to Alexa or to a loopback port, so the directive would break
    every account link."""
    response = await client.get("/privacy")

    assert "form-action" not in response.headers["content-security-policy"]


async def test_json_gets_no_html_policy(client: httpx.AsyncClient) -> None:
    response = await client.get("/.well-known/oauth-authorization-server")

    assert "content-security-policy" not in response.headers


async def test_a_header_the_handler_set_wins(
    oauth_env: Settings, oauth_http: httpx.AsyncClient
) -> None:
    """The OAuth pages set ``no-store``, and the legal pages a public cache; the
    middleware only fills gaps."""
    assert (await oauth_http.get("/privacy")).headers["cache-control"].startswith("public")
    assert (await oauth_http.get("/oauth/connected")).headers["cache-control"] == "no-store"
