"""Dynamic client registration, and the guards on it (`S4-16`, `S4-17`, `S4-18`).

§2.2's requirement, tested: a client this server has never seen registers itself
and completes a grant. That is what makes ``mcp.fanmoot.com`` usable by Claude,
ChatGPT, Cursor and the Inspector rather than by Alexa alone.

The loopback carve-out gets its own test because it is the line between "usable
by a desktop client" and "accepts a code over cleartext".
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from app import db as app_db
from app.models import TIER_DYNAMIC, TIER_STATIC, AccessGrant, OAuthClient
from app.oauth import scopes
from app.settings import Settings
from tests import oauth
from tests.replay import Replay

LOOPBACK = "http://localhost:33418/oauth/callback"


async def _register(http: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    body: dict[str, object] = {
        "client_name": "MCP Inspector",
        "redirect_uris": [LOOPBACK],
    }
    body.update(overrides)
    return await http.post("/oauth/register", json=body)


async def test_a_client_registers_itself_and_completes_a_grant(
    oauth_http: httpx.AsyncClient, oauth_db: object, fanmoot_auth: Replay
) -> None:
    """The whole of `S4-16`'s acceptance: a server it has never seen, a
    self-registration, and a working token."""
    registered = await _register(oauth_http)
    assert registered.status_code == 201
    client = registered.json()
    assert client["client_id"].startswith("fmcp_")

    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(
        oauth_http,
        client_id=client["client_id"],
        challenge=challenge,
        redirect_uri=LOOPBACK,
    )
    token = await oauth.exchange(
        oauth_http,
        client_id=client["client_id"],
        client_secret=None,
        code=code,
        verifier=verifier,
        redirect_uri=LOOPBACK,
    )

    assert token.status_code == 200
    assert token.json()["refresh_token"]


async def test_a_self_registered_client_is_public_and_leans_on_pkce(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    """A secret written into a desktop client's config file is a secret in a
    file. PKCE is mandatory for everybody here anyway."""
    client = (await _register(oauth_http)).json()

    assert client["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in client


async def test_a_client_that_asks_for_a_secret_gets_one_exactly_once(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    registered = await _register(oauth_http, token_endpoint_auth_method="client_secret_post")
    client = registered.json()

    assert client["client_secret"]
    # 0 means "does not expire" in RFC 7591 — the registration can be reaped,
    # the secret has no separate life.
    assert client["client_secret_expires_at"] == 0

    async with app_db.get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == client["client_id"])
            )
        ).scalar_one()
    # Stored as a hash, never as itself.
    assert row.client_secret_hash != client["client_secret"]
    assert len(row.client_secret_hash or "") == 64


async def test_loopback_http_is_allowed_and_public_http_is_not(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    """Claude Desktop, Cursor and the Inspector genuinely need a loopback
    callback. A plain-http URI on a public host is a code on the wire."""
    for allowed in ("http://localhost:1/cb", "http://127.0.0.1:1/cb", "https://ok.example/cb"):
        response = await _register(oauth_http, redirect_uris=[allowed])
        assert response.status_code == 201, allowed

    refused = await _register(oauth_http, redirect_uris=["http://client.example/cb"])

    assert refused.status_code == 400
    assert refused.json()["error"] == "invalid_redirect_uri"


async def test_a_redirect_uri_with_a_fragment_is_refused(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    """RFC 6749 forbids one, and a client registering ``#/callback`` would lose
    the query the code arrives in."""
    refused = await _register(oauth_http, redirect_uris=["https://ok.example/cb#/done"])

    assert refused.json()["error"] == "invalid_redirect_uri"


async def test_a_client_cannot_choose_its_own_id(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    """RFC 7591 says the server generates it. Otherwise a registration could
    claim Alexa's."""
    registered = await _register(oauth_http, client_id="amzn1.application-oa2-client.test")

    assert registered.json()["client_id"] != "amzn1.application-oa2-client.test"


async def test_registration_defaults_to_every_scope_and_the_fan_narrows_it(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    """A client cannot know which scopes a person will agree to. Registering low
    would send every fan through a second authorization the first time it posted."""
    client = (await _register(oauth_http)).json()

    assert set(client["scope"].split()) == set(scopes.SUPPORTED_SET)


async def test_an_unsupported_grant_type_is_refused_at_registration(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    refused = await _register(oauth_http, grant_types=["implicit"])

    assert refused.json()["error"] == "invalid_client_metadata"


async def test_the_tier_says_where_a_client_came_from(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """`S4-18`: an operator reading the audit log can tell Alexa's pre-seeded
    credentials from something that registered itself thirty seconds ago."""
    dynamic = (await _register(oauth_http)).json()

    async with app_db.get_sessionmaker()() as session:
        rows = {
            row.client_id: row.tier
            for row in (await session.execute(select(OAuthClient))).scalars()
        }

    assert rows[static_client] == TIER_STATIC
    assert rows[dynamic["client_id"]] == TIER_DYNAMIC


async def test_a_registration_expires_until_it_completes_a_grant(
    oauth_http: httpx.AsyncClient, oauth_db: object, fanmoot_auth: Replay
) -> None:
    """Open registration means anybody can create a row; the TTL is what stops
    the table being a place to leave litter (`S4-16`)."""
    client = (await _register(oauth_http)).json()

    async with app_db.get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == client["client_id"])
            )
        ).scalar_one()
        assert row.expires_at is not None
        assert row.last_grant_at is None

    await oauth.linked(
        oauth_http, client_id=client["client_id"], client_secret=None, redirect_uri=LOOPBACK
    )

    async with app_db.get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == client["client_id"])
            )
        ).scalar_one()
    # A client in real use never expires.
    assert row.expires_at is None
    assert row.last_grant_at is not None


async def test_the_reaper_takes_lapsed_registrations_and_leaves_the_rest(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    from datetime import timedelta

    from app.models.base import utcnow
    from app.oauth import clients as client_store

    used = (await _register(oauth_http, client_name="Used")).json()
    await oauth.linked(
        oauth_http, client_id=used["client_id"], client_secret=None, redirect_uri=LOOPBACK
    )
    lapsed = (await _register(oauth_http, client_name="Abandoned")).json()

    async with app_db.get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(OAuthClient).where(OAuthClient.client_id == lapsed["client_id"])
            )
        ).scalar_one()
        row.expires_at = utcnow() - timedelta(days=1)
        await session.commit()

        reaped = await client_store.reap_stale(session)
        remaining = {
            client.client_id for client in (await session.execute(select(OAuthClient))).scalars()
        }

    assert reaped == 1
    assert lapsed["client_id"] not in remaining
    # Alexa's row and a client in real use are untouched.
    assert {static_client, used["client_id"]} <= remaining


async def test_a_client_can_read_its_own_registration_back(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    """RFC 7592, which is what the Inspector uses to confirm what it got."""
    client = (await _register(oauth_http)).json()

    read = await oauth_http.get(
        f"/oauth/register/{client['client_id']}",
        headers={"Authorization": f"Bearer {client['registration_access_token']}"},
    )
    without = await oauth_http.get(f"/oauth/register/{client['client_id']}")

    assert read.status_code == 200
    assert read.json()["client_name"] == "MCP Inspector"
    assert without.status_code == 401


async def test_registration_is_rate_limited_per_address(
    oauth_http: httpx.AsyncClient, oauth_db: object, oauth_env: Settings
) -> None:
    for _ in range(oauth_env.oauth_register_limit):
        assert (await _register(oauth_http)).status_code == 201

    refused = await _register(oauth_http)

    assert refused.status_code == 429


async def test_registration_can_be_switched_off(
    oauth_http: httpx.AsyncClient, oauth_db: object, monkeypatch: object
) -> None:
    """Turning DCR off closes tier 2 and says so — a client that reads a 403
    knows to ask an operator; one that reads a 404 concludes the server is
    broken."""
    import os

    from app.settings import reset_settings_cache

    os.environ["FMCP_OAUTH_DCR_ENABLED"] = "false"
    reset_settings_cache()
    try:
        refused = await _register(oauth_http)
        metadata = (await oauth_http.get("/.well-known/oauth-authorization-server")).json()
    finally:
        os.environ.pop("FMCP_OAUTH_DCR_ENABLED", None)
        reset_settings_cache()

    assert refused.status_code == 403
    # And the endpoint is not advertised, rather than advertised and closed.
    assert "registration_endpoint" not in metadata


async def test_a_dynamic_client_is_not_second_class(
    oauth_http: httpx.AsyncClient, oauth_db: object, fanmoot_auth: Replay
) -> None:
    """HAD-5: same table, same scopes, same flow. Only ``tier`` differs."""
    client = (await _register(oauth_http)).json()
    body = await oauth.linked(
        oauth_http, client_id=client["client_id"], client_secret=None, redirect_uri=LOOPBACK
    )

    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()

    assert set(body["scope"].split()) == set(scopes.SUPPORTED_SET)
    assert grant.resource == "https://mcp.fanmoot.test/mcp"
