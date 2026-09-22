"""The token exchange (`S4-8`) — the reason an Alexa token is not a Fanmoot token.

What a client holds is a handle to a grant. Turning it into something Fanmoot
will accept means decrypting the fan's upstream refresh token, spending it, and
storing the successor — Fanmoot rotates, and a token spent and not recorded is a
link that breaks on the next turn.

The three properties asserted here are the ones that go wrong quietly:

* the successor really is stored, so the second turn works;
* the result is cached, because three tool calls in a voice turn cannot be three
  round trips to ``/v1/auth/refresh`` (HAD-6);
* two concurrent calls spend the token **once**, because the second presentation
  would look to Fanmoot exactly like a replay and revoke the family.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app import db as app_db
from app.fanmoot.client import FanmootUnavailable
from app.models import AccessGrant, UpstreamSession
from app.oauth import tokens, upstream
from app.settings import Settings
from tests.replay import Replay


async def _grant(
    session_row: UpstreamSession, *, scope: str = "fan.read moot.write"
) -> AccessGrant:
    """A grant hanging off an existing upstream session, without the browser leg.

    The flow is tested in ``tests/test_oauth_flow.py``; what is under test here
    is what happens on a tool call months later.
    """
    from app.models.client import OAuthClient

    async with app_db.get_sessionmaker()() as session:
        client = OAuthClient(
            client_id="fmcp_exchange_test",
            client_name="Test",
            redirect_uris=["https://ok.example/cb"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=scope,
        )
        session.add(client)
        grant = AccessGrant(
            client_id=client.client_id,
            upstream_session_id=session_row.id,
            fanmoot_user_id=session_row.fanmoot_user_id,
            fanmoot_handle=session_row.fanmoot_handle,
            scope=scope,
            resource="https://mcp.fanmoot.test/mcp",
        )
        session.add(grant)
        await session.commit()
        return grant


async def test_the_exchange_returns_a_live_fanmoot_token(
    signed_in: UpstreamSession, fanmoot_auth: Replay
) -> None:
    grant = await _grant(signed_in)

    async with app_db.get_sessionmaker()() as session:
        token = await upstream.fanmoot_token_for(session, grant)

    assert token == "upstream-access-2"


async def test_the_rotated_refresh_token_is_stored_encrypted(
    signed_in: UpstreamSession, fanmoot_auth: Replay
) -> None:
    """Fanmoot rotates on every exchange. Storing the successor before returning
    is what makes the next turn work."""
    grant = await _grant(signed_in)

    async with app_db.get_sessionmaker()() as session:
        await upstream.fanmoot_token_for(session, grant)

    async with app_db.get_sessionmaker()() as session:
        row = (await session.execute(select(UpstreamSession))).scalar_one()
    assert tokens.decrypt(row.refresh_token_encrypted) == "upstream-refresh-2"
    assert "upstream-refresh-2" not in row.refresh_token_encrypted
    assert row.rotated_at is not None


async def test_the_token_is_cached_rather_than_fetched_per_tool_call(
    signed_in: UpstreamSession, fanmoot_auth: Replay
) -> None:
    """Three tool calls in a voice turn cannot be three upstream round trips —
    HAD-6's budget is gone before any football happens."""
    grant = await _grant(signed_in)

    async with app_db.get_sessionmaker()() as session:
        await upstream.fanmoot_token_for(session, grant)
        await upstream.fanmoot_token_for(session, grant)
        await upstream.fanmoot_token_for(session, grant)

    refreshes = [call for call in fanmoot_auth.calls if call.url.path == "/v1/auth/refresh"]
    assert len(refreshes) == 1


async def test_concurrent_calls_spend_the_upstream_token_once(
    signed_in: UpstreamSession, fanmoot_auth: Replay
) -> None:
    """The race worth closing. Two tool calls landing together would both miss
    the cache, both spend the same refresh token, and the second presentation
    would look to Fanmoot exactly like a replay — signing the fan out of an
    integration they were actively using."""
    grant = await _grant(signed_in)

    async def one() -> str:
        async with app_db.get_sessionmaker()() as session:
            return await upstream.fanmoot_token_for(session, grant)

    results = await asyncio.gather(one(), one(), one())

    assert set(results) == {"upstream-access-2"}
    refreshes = [call for call in fanmoot_auth.calls if call.url.path == "/v1/auth/refresh"]
    assert len(refreshes) == 1


async def test_forgetting_a_grant_drops_its_cached_token(
    signed_in: UpstreamSession, fanmoot_auth: Replay
) -> None:
    """Without this a revoked grant keeps working for a quarter of an hour,
    which is exactly the window "disconnect this app" exists to close."""
    grant = await _grant(signed_in)

    async with app_db.get_sessionmaker()() as session:
        await upstream.fanmoot_token_for(session, grant)
        upstream.forget(grant.id)
        await upstream.fanmoot_token_for(session, grant)

    refreshes = [call for call in fanmoot_auth.calls if call.url.path == "/v1/auth/refresh"]
    assert len(refreshes) == 2


async def test_a_refused_upstream_session_breaks_the_link_and_says_so(
    signed_in: UpstreamSession, replay: Replay
) -> None:
    """A link that cannot be renewed is not a link. Leaving it in place would
    mean failing every tool call for that fan for the next ninety days."""
    replay.stub(
        "/v1/auth/refresh",
        {"detail": "Session expired."},
        method="POST",
        params={"in_body": "true"},
        status=401,
    )
    grant = await _grant(signed_in)

    with pytest.raises(upstream.LinkBroken) as raised:
        async with app_db.get_sessionmaker()() as session:
            await upstream.fanmoot_token_for(session, grant)

    # The sentence is written to be heard, because that is where it ends up.
    assert "Reconnect it" in raised.value.message

    async with app_db.get_sessionmaker()() as session:
        row = (await session.execute(select(UpstreamSession))).scalar_one()
        after = (await session.execute(select(AccessGrant))).scalar_one()
    assert row.revoked_at is not None
    assert after.revoked_at is not None


async def test_an_unreachable_fanmoot_does_not_revoke_a_working_grant(
    signed_in: UpstreamSession, replay: Replay
) -> None:
    """Revoking a link over a 502 would be the wrong repair: the caller's error
    mapping already has a sentence for "Fanmoot is having trouble"."""
    replay.fail("/v1/auth/refresh", 503)
    grant = await _grant(signed_in)

    with pytest.raises(FanmootUnavailable):
        async with app_db.get_sessionmaker()() as session:
            await upstream.fanmoot_token_for(session, grant)

    async with app_db.get_sessionmaker()() as session:
        after = (await session.execute(select(AccessGrant))).scalar_one()
    assert after.revoked_at is None


async def test_a_rotated_encryption_key_breaks_links_rather_than_leaking(
    signed_in: UpstreamSession, fanmoot_auth: Replay, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The designed consequence of rotation: every stored grant becomes
    unreadable, fans reconnect, and nothing leaked."""
    from cryptography.fernet import Fernet

    from app.settings import reset_settings_cache

    grant = await _grant(signed_in)
    monkeypatch.setenv("FMCP_SECRET_ENCRYPTION_KEY", Fernet.generate_key().decode())
    reset_settings_cache()
    tokens.reset_cache()

    with pytest.raises(upstream.LinkBroken):
        async with app_db.get_sessionmaker()() as session:
            await upstream.fanmoot_token_for(session, grant)


async def test_ending_a_session_logs_out_upstream_and_revokes_here(
    signed_in: UpstreamSession, fanmoot_auth: Replay
) -> None:
    """Best-effort upstream, definite locally: the local state is what this
    server is responsible for."""
    await _grant(signed_in)

    async with app_db.get_sessionmaker()() as session:
        row = (await session.execute(select(UpstreamSession))).scalar_one()
        await upstream.end_session(session, row, reason="fan_disconnected")

    assert any(call.url.path == "/v1/auth/logout" for call in fanmoot_auth.calls)
    async with app_db.get_sessionmaker()() as session:
        after = (await session.execute(select(AccessGrant))).scalar_one()
    assert after.revoked_at is not None


async def test_the_cached_token_expires_inside_the_real_one(
    signed_in: UpstreamSession, fanmoot_auth: Replay, oauth_env: Settings
) -> None:
    """A cached token handed out in its last few seconds is a tool call that
    fails mid-turn for no reason a fan could understand."""
    assert oauth_env.oauth_upstream_access_ttl_s < 900
