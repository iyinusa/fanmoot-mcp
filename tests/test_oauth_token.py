"""``POST /token``: client authentication, rotation, and the reuse defence (`S4-6`, `S4-7`).

The refresh chain is where a mistake is quietest — everything keeps working and
a stolen token keeps working too — so the reuse case is asserted on the database
rather than on the response: the whole family has to be revoked, not just the
token that was replayed.
"""

from __future__ import annotations

import base64

import httpx
from sqlalchemy import select

from app import db as app_db
from app.models import AccessGrant, RefreshToken
from app.oauth import scopes
from app.settings import Settings
from tests import oauth
from tests.conftest import STATIC_CLIENT_SECRET
from tests.replay import Replay


async def test_a_refresh_token_returns_a_new_pair(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    first = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )

    second = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=first["refresh_token"],
    )

    assert second.status_code == 200
    body = second.json()
    assert body["access_token"] != first["access_token"]
    # Rotating: the successor comes back and the old one is spent.
    assert body["refresh_token"] != first["refresh_token"]
    assert body["scope"] == first["scope"]


async def test_the_previous_access_token_stops_working_after_a_refresh(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """One outstanding access token per grant, which is why its hash lives on
    the grant row rather than in a table of its own."""
    from app.oauth import grants as grant_store

    first = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )
    await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=first["refresh_token"],
    )

    async with app_db.get_sessionmaker()() as session:
        assert await grant_store.resolve_access_token(session, first["access_token"]) is None


async def test_replaying_a_spent_refresh_token_revokes_the_whole_family(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """The one defence that matters here. A spent token presented again is
    either a retry or a theft, and there is no way to tell — so a stolen token
    is worth at most one use, at the cost of asking that fan to link again."""
    first = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )
    second = (
        await oauth.refresh(
            oauth_http,
            client_id=static_client,
            client_secret=STATIC_CLIENT_SECRET,
            refresh_token=first["refresh_token"],
        )
    ).json()

    replayed = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=first["refresh_token"],
    )

    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"

    # And the successor the legitimate client is holding is dead too.
    after = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=second["refresh_token"],
    )
    assert after.status_code == 400

    async with app_db.get_sessionmaker()() as session:
        rows = (await session.execute(select(RefreshToken))).scalars().all()
    assert rows and all(row.revoked_at is not None for row in rows)
    assert {row.revoked_reason for row in rows} == {"refresh_reuse_detected"}
    assert len({row.family_id for row in rows}) == 1


async def test_refresh_without_a_resource_is_the_normal_case(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """Alexa sends ``resource`` on authorize and token and **not** on refresh
    (`S4-6`). Absent means "the one this grant was issued for"."""
    resource = "https://mcp.fanmoot.test/mcp"
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(
        oauth_http, client_id=static_client, challenge=challenge, resource=resource
    )
    first = (
        await oauth.exchange(
            oauth_http,
            client_id=static_client,
            client_secret=STATIC_CLIENT_SECRET,
            code=code,
            verifier=verifier,
            resource=resource,
        )
    ).json()

    rotated = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=first["refresh_token"],
    )

    assert rotated.status_code == 200


async def test_refresh_for_a_different_resource_is_refused(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    first = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )

    refused = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=first["refresh_token"],
        resource="https://someone-else.example/mcp",
    )

    assert refused.json()["error"] == "invalid_target"


async def test_a_refresh_may_narrow_scopes_but_never_widen_them(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """RFC 6749 §6. Nobody sends this, and refusing a widening attempt is
    cheaper than reasoning about a token whose scope exceeds its grant."""
    first = await oauth.linked(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        approve_scopes=[scopes.FAN_READ],
    )

    widened = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=first["refresh_token"],
        scope=f"{scopes.FAN_READ} {scopes.MOOT_WRITE}",
    )

    assert widened.json()["error"] == "invalid_scope"


# ── Client authentication ────────────────────────────────────────────────────
async def test_a_confidential_client_may_authenticate_with_basic(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """Both methods are accepted because clients differ on which they use, and
    RFC 6749 permits both."""
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)
    credentials = base64.b64encode(f"{static_client}:{STATIC_CLIENT_SECRET}".encode()).decode()

    token = await oauth_http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": oauth.DEFAULT_REDIRECT_URI,
        },
        headers={"Authorization": f"Basic {credentials}"},
    )

    assert token.status_code == 200


async def test_the_wrong_client_secret_is_refused(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)

    refused = await oauth.exchange(
        oauth_http,
        client_id=static_client,
        client_secret="not-the-secret",
        code=code,
        verifier=verifier,
    )

    assert refused.status_code == 401
    assert refused.json()["error"] == "invalid_client"
    # RFC 6749 §5.2: a 401 from the token endpoint carries a challenge.
    assert "Basic" in refused.headers["www-authenticate"]


async def test_an_unknown_client_is_not_distinguished_from_a_wrong_secret(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """Otherwise this endpoint is an oracle for which client ids exist."""
    unknown = await oauth_http.post(
        "/token",
        data={"grant_type": "authorization_code", "client_id": "nope", "code": "x"},
    )
    wrong_secret = await oauth_http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": static_client,
            "client_secret": "wrong",
            "code": "x",
        },
    )

    assert unknown.status_code == wrong_secret.status_code == 401
    assert unknown.json()["error"] == wrong_secret.json()["error"] == "invalid_client"


async def test_an_unsupported_grant_type_is_named_as_such(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    refused = await oauth_http.post(
        "/token",
        data={
            "grant_type": "client_credentials",
            "client_id": static_client,
            "client_secret": STATIC_CLIENT_SECRET,
        },
    )

    assert refused.json()["error"] in {"unsupported_grant_type", "unauthorized_client"}


async def test_a_code_cannot_be_redeemed_by_a_different_client(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """A code issued to Alexa, presented by something that registered itself."""
    verifier, challenge = oauth.pkce_pair()
    code = await oauth.code_for(oauth_http, client_id=static_client, challenge=challenge)

    registered = (
        await oauth_http.post(
            "/oauth/register",
            json={
                "client_name": "Opportunist",
                "redirect_uris": ["http://localhost:8765/callback"],
            },
        )
    ).json()

    refused = await oauth.exchange(
        oauth_http,
        client_id=registered["client_id"],
        client_secret=None,
        code=code,
        verifier=verifier,
    )

    assert refused.json()["error"] == "invalid_grant"


# ── Rate limiting (S4-14) ────────────────────────────────────────────────────
async def test_the_token_endpoint_is_rate_limited(
    oauth_http: httpx.AsyncClient, static_client: str, oauth_env: Settings
) -> None:
    """Every attempt here is a database write, so an unlimited endpoint is an
    invitation to grind through codes."""
    limit = oauth_env.oauth_token_limit
    form = {"grant_type": "authorization_code", "client_id": static_client, "code": "nope"}

    for _ in range(limit):
        await oauth_http.post("/token", data=form)
    refused = await oauth_http.post("/token", data=form)

    assert refused.status_code == 429
    assert refused.json()["error"] == "temporarily_unavailable"
    assert int(refused.headers["retry-after"]) >= 1


async def test_a_grant_survives_a_restart_because_it_is_a_row(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """An access token is a *handle* (HAD-3), not a self-describing JWT — which
    is what makes it revocable, and what this row is for."""
    body = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )

    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()

    assert grant.access_token_hash is not None
    # The plaintext is nowhere in the database.
    assert body["access_token"] not in str(grant.__dict__)
