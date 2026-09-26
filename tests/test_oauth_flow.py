"""The authorization-code flow, end to end and at its edges (`S4-4`…`S4-6`, `S4-15`).

`S4`'s acceptance in one file: a client completes the dance, and every downgrade
of it is refused. The refusals are the point — a flow that only works is a flow
nobody has attacked.

Driven through real HTTP against the real application: the challenge is derived
with S256, the hidden flow field is read back out of the rendered page, and the
code comes out of a ``Location`` header. Nothing calls into the server to skip a
step.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app import db as app_db
from app.models import AccessGrant, AuthorizationCode, ConsentEvent
from app.oauth import scopes
from app.settings import Settings
from tests import oauth
from tests.conftest import STATIC_CLIENT_ID, STATIC_CLIENT_SECRET, STATIC_REDIRECT_URIS
from tests.replay import Replay


# ── The happy path ───────────────────────────────────────────────────────────
async def test_a_fan_links_an_account_and_the_client_gets_a_token(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """`S4`'s acceptance: authorize, sign in, consent, redeem."""
    verifier, challenge = oauth.pkce_pair()

    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
    assert page.status_code == 200
    assert "Sign in to Fanmoot" in page.text and "Alexa+" in page.text

    consent = await oauth.sign_in(oauth_http, page)
    assert consent.status_code == 200
    assert "Allow Alexa+ to use your Fanmoot account?" in consent.text
    # Named per scope, in the words a fan reads (`S4-10`).
    assert "Post takes as you" in consent.text

    granted = await oauth.approve(oauth_http, consent)
    assert granted.status_code == 302
    returned = oauth.query_of(granted)
    assert returned["state"] == "opaque-state"
    # RFC 9207: the issuer travels with the code.
    assert returned["iss"] == "https://mcp.fanmoot.test"

    token = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=returned["code"],
        verifier=verifier,
    )
    assert token.status_code == 200
    body = token.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 3600
    # Mandatory, always (`S4-7`): without it a fan re-links every hour.
    assert body["refresh_token"]
    assert set(body["scope"].split()) == set(scopes.SUPPORTED_SET)
    # A credential must never be cached by a proxy on the way back.
    assert token.headers["cache-control"] == "no-store"


async def test_the_redirect_uri_is_echoed_not_substituted(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """`S4-5`, and the single most likely cause of a silent linking failure.

    Alexa registers several URIs and sends whichever suits the device it is on.
    A server that kept the first one and redirected there would look fine in
    every test that only registered one.
    """
    _, challenge = oauth.pkce_pair()
    second = STATIC_REDIRECT_URIS[1]

    page = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, redirect_uri=second
    )
    consent = await oauth.sign_in(oauth_http, page)
    granted = await oauth.approve(oauth_http, consent)

    assert granted.headers["location"].startswith(second)


async def test_what_the_fan_ticked_is_what_the_grant_holds(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """Narrowing on the consent screen is real, not decoration: the grant records
    what was approved, and `S4-12` exists to upgrade it later."""
    body = await oauth.linked(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        approve_scopes=[scopes.FAN_READ],
    )

    assert body["scope"] == scopes.FAN_READ

    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()
    assert grant.scopes == {scopes.FAN_READ}


async def test_consent_is_recorded_with_the_client_and_the_scopes(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """ "Did this fan agree to let this client post as them, and when?" is a
    question asked after something has gone wrong (`S4-1`)."""
    await oauth.linked(oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET)

    async with app_db.get_sessionmaker()() as session:
        events = (await session.execute(select(ConsentEvent))).scalars().all()

    assert [event.action for event in events] == ["granted"]
    assert events[0].client_id == STATIC_CLIENT_ID
    assert events[0].fanmoot_handle == "thegunner"
    assert scopes.MOOT_WRITE in events[0].scope


async def test_declining_sends_access_denied_back_with_the_state(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
    consent = await oauth.sign_in(oauth_http, page)

    refused = await oauth_http.post(
        "/oauth/consent", data={"flow": oauth.flow_token(consent.text), "decision": "deny"}
    )

    assert refused.status_code == 302
    returned = oauth.query_of(refused)
    assert returned["error"] == "access_denied"
    # Echoed on the error path too: `state` is the client's CSRF defence.
    assert returned["state"] == "opaque-state"

    async with app_db.get_sessionmaker()() as session:
        events = (await session.execute(select(ConsentEvent))).scalars().all()
    assert [event.action for event in events] == ["denied"]


# ── PKCE (S4-4) ──────────────────────────────────────────────────────────────
async def test_plain_is_refused_even_though_rfc_7636_defines_it(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """Advertising S256 and accepting ``plain`` would be worse than doing
    neither: it passes Alexa's deploy check and silently accepts the downgrade."""
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, method="plain"
    )

    assert refused.status_code == 302
    assert oauth.query_of(refused)["error"] == "invalid_request"


async def test_an_absent_method_is_a_downgrade_not_an_omission(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """RFC 7636 says a missing ``code_challenge_method`` *means* ``plain``, so
    silence is refused for exactly the same reason ``plain`` is."""
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, method=None
    )

    assert oauth.query_of(refused)["error"] == "invalid_request"


async def test_no_pkce_at_all_is_refused(oauth_http: httpx.AsyncClient, static_client: str) -> None:
    refused = await oauth.authorize(oauth_http, client_id=static_client, challenge="")

    assert oauth.query_of(refused)["error"] == "invalid_request"


async def test_a_malformed_challenge_is_refused_at_authorize(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """Caught on the way in rather than left to fail verification at ``/token``,
    where the fan has already typed a password for nothing."""
    refused = await oauth.authorize(oauth_http, client_id=static_client, challenge="too-short")

    assert oauth.query_of(refused)["error"] == "invalid_request"


async def test_the_wrong_verifier_does_not_redeem_the_code(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """The point of PKCE: the code is useless to anyone who saw the redirect."""
    _, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)
    other_verifier, _ = oauth.pkce_pair()

    refused = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=other_verifier,
    )

    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_grant"


# ── Codes (S4-6) ─────────────────────────────────────────────────────────────
async def test_a_code_is_single_use(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)

    first = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=verifier,
    )
    replayed = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=verifier,
    )

    assert first.status_code == 200
    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"


async def test_a_replayed_code_revokes_what_it_already_produced(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """If the replay is the attacker's, the legitimate client's tokens are the
    ones at risk — so the grant that code created is taken away."""
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)
    await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=verifier,
    )

    await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=verifier,
    )

    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()
    assert grant.revoked_at is not None
    assert grant.revoked_reason == "refresh_reuse_detected"
    assert grant.access_token_hash is None


async def test_a_mismatched_redirect_uri_does_not_redeem_the_code(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """RFC 6749 §4.1.3. The code was issued for one URI and is spent against it,
    or not at all."""
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)

    refused = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=verifier,
        redirect_uri=STATIC_REDIRECT_URIS[1],
    )

    assert refused.json()["error"] == "invalid_grant"


async def test_an_expired_code_is_refused(
    oauth_http: httpx.AsyncClient,
    static_client: str,
    fanmoot_auth: Replay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sixty seconds, because a code travels through a browser and anything
    longer is a longer window for one sitting in somebody's history."""
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)

    async with app_db.get_sessionmaker()() as session:
        row = (await session.execute(select(AuthorizationCode))).scalar_one()
        row.expires_at = row.created_at.replace(year=2020)
        await session.commit()

    refused = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=verifier,
    )

    assert refused.json()["error"] == "invalid_grant"


# ── The refusals that must not redirect (S4-5) ───────────────────────────────
async def test_an_unknown_client_gets_a_page_not_a_redirect(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """There is nowhere safe to send this: the only candidate is a URI the
    caller supplied. A server that redirected here is an open redirector."""
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(oauth_http, client_id="not-a-client", challenge=challenge)

    assert refused.status_code == 400
    assert "location" not in refused.headers
    assert "We do not recognise that app" in refused.text


async def test_an_unregistered_redirect_uri_gets_a_page_not_a_redirect(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(
        oauth_http,
        client_id=static_client,
        challenge=challenge,
        redirect_uri="https://attacker.example/collect",
    )

    assert refused.status_code == 400
    assert "location" not in refused.headers
    # And never echoes the URI it refused back into the page.
    assert "attacker.example" not in refused.text


async def test_a_missing_redirect_uri_is_never_defaulted(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """Even with several registered, this server does not pick one (`S4-5`)."""
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, redirect_uri=None
    )

    assert refused.status_code == 400
    assert "location" not in refused.headers


# ── Scopes and the resource indicator ────────────────────────────────────────
async def test_an_unknown_scope_is_refused_rather_than_narrowed(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, scope="fan.read admin"
    )

    assert oauth.query_of(refused)["error"] == "invalid_scope"


async def test_a_resource_for_another_audience_is_refused(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """RFC 8707: this server issues tokens for its own MCP endpoint and nothing
    else."""
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(
        oauth_http,
        client_id=static_client,
        challenge=challenge,
        resource="https://someone-else.example/mcp",
    )

    assert oauth.query_of(refused)["error"] == "invalid_target"


async def test_the_resource_alexa_sends_is_honoured(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """Alexa sends ``resource`` on authorize **and** token; both have to accept
    it, and the grant records it so the mount can check a token against it."""
    verifier, challenge = oauth.pkce_pair()
    resource = "https://mcp.fanmoot.test/mcp"
    code = await oauth.code_for(
        oauth_http, client_id=static_client, challenge=challenge, resource=resource
    )

    token = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        code=code,
        verifier=verifier,
        resource=resource,
    )

    assert token.status_code == 200
    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()
    assert grant.resource == resource


async def test_only_response_type_code_is_supported(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """OAuth 2.1 removes the implicit grant and Alexa does not support it."""
    _, challenge = oauth.pkce_pair()

    refused = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, response_type="token"
    )

    assert oauth.query_of(refused)["error"] == "unsupported_response_type"


# ── Without an encryption key, linking is closed rather than unsafe ──────────
async def test_without_a_fernet_key_authorize_declines_to_start(
    oauth_db: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HAD-3's safe direction: the read tools keep working and the server
    declines to hold a fan's Fanmoot session at all."""
    from app.settings import get_settings, reset_settings_cache

    monkeypatch.setenv("FMCP_SECRET_ENCRYPTION_KEY", "")
    monkeypatch.setenv("FMCP_PUBLIC_BASE_URL", "https://mcp.fanmoot.test")
    reset_settings_cache()
    settings: Settings = get_settings()
    assert settings.can_store_grants is False

    from app.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="https://mcp.fanmoot.test") as http:
        response = await http.get("/authorize", params={"client_id": "anything"})

    assert response.status_code == 503
    assert "Account linking is switched off" in response.text
