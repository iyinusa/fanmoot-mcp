"""What the ``/mcp`` mount does with a token, and without one (`S4-9`, `S4-12`).

This is the pair of HTTP responses account linking actually starts from. Alexa
begins linking when a tool call comes back **401 with an RFC 9728 challenge**; a
client whose grant is read-only and reaches a write tool is meant to see **403
``insufficient_scope``** naming what it needs, and re-authorize for that one
scope rather than failing.

Both are statuses on the HTTP response, not tool results, which is why the gate
reads the JSON-RPC frame before the session manager does — by the time a tool
function runs, the response is already a stream.

The frames below are posted raw rather than through the client SDK on purpose:
the assertion is about the status and the headers, and an SDK that turned a 401
into an exception would hide exactly the thing being tested.
"""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import select

from app import db as app_db
from app.mcp import telemetry
from app.mcp.server import FanmootMCP
from app.models import AccessGrant
from app.oauth import scopes
from tests import oauth, tooling
from tests.conftest import STATIC_CLIENT_SECRET
from tests.replay import Replay

# Fixture order is load-bearing in this file. `mcp_url` builds an application,
# and an application captures `Settings` as it is built — so `oauth_env`, which
# sets this server's public base URL, has to be asked for *first*. Otherwise the
# uvicorn server is protecting a resource at one URI while the authorization
# server is issuing tokens for another, and every token is correctly refused.
_FRAME_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


async def _post(url: str, frame: dict[str, Any], token: str | None = None) -> httpx.Response:
    headers = dict(_FRAME_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient() as http:
        return await http.post(url, json=frame, headers=headers)


async def test_an_authenticated_tool_without_a_token_answers_401_with_the_challenge(
    oauth_env: object, oauth_db: object, mcp_url: str
) -> None:
    """The response Alexa starts account linking from.

    ``resource_metadata`` is the load-bearing part: it is how a client that has
    never seen this server finds the authorization server.
    """
    response = await _post(mcp_url, _call("post_moot", body="VAR is a disgrace"))

    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'error="invalid_token"' in challenge
    assert "resource_metadata=" in challenge
    assert "/.well-known/oauth-protected-resource/mcp" in challenge
    assert response.json()["scope"] == scopes.MOOT_WRITE


async def test_a_read_tool_is_not_challenged(
    oauth_env: object, oauth_db: object, mcp_url: str
) -> None:
    """The property that keeps "what's the score" answerable without linking an
    account: twelve of this server's tools take no credential at all."""
    response = await _post(mcp_url, _call("live_scores"))

    assert response.status_code != 401
    assert "www-authenticate" not in response.headers


async def test_an_unparseable_frame_is_passed_through_not_refused(
    oauth_env: object, oauth_db: object, mcp_url: str
) -> None:
    """A gate that rejected malformed JSON would be a second, worse error path
    for frames the MCP SDK already answers properly."""
    async with httpx.AsyncClient() as http:
        response = await http.post(mcp_url, content=b"{not json", headers=_FRAME_HEADERS)

    assert response.status_code != 401


async def test_a_token_that_is_not_a_token_is_challenged_as_expired(
    oauth_env: object, oauth_db: object, mcp_url: str
) -> None:
    """A presented-but-useless token gets a different sentence from no token at
    all, because the client's next move differs: refresh, or link."""
    response = await _post(mcp_url, _call("post_moot", body="hello"), token="not-a-token")

    assert response.status_code == 401
    assert "no longer valid" in response.json()["error_description"]


async def test_a_read_only_grant_reaching_a_write_tool_is_upgraded_not_failed(
    oauth_http: httpx.AsyncClient,
    static_client: str,
    fanmoot_auth: Replay,
    mcp_url: str,
) -> None:
    """SEP-835 (`S4-12`). The fan ticked only "see who you are"; the client asks
    to post; the answer names the scope to come back with."""
    body = await oauth.linked(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        approve_scopes=[scopes.FAN_READ],
    )

    response = await _post(
        mcp_url, _call("post_moot", body="up the arsenal"), token=body["access_token"]
    )

    assert response.status_code == 403
    challenge = response.headers["www-authenticate"]
    assert 'error="insufficient_scope"' in challenge
    assert f'scope="{scopes.MOOT_WRITE}"' in challenge
    assert "resource_metadata=" in challenge
    # In the words the scope is described to a fan in, so a client can say why.
    assert "post takes as you" in response.json()["error_description"].lower()


async def test_a_granted_scope_runs_the_tool_and_is_recorded_against_the_grant(
    oauth_http: httpx.AsyncClient,
    static_client: str,
    fanmoot_auth: Replay,
    mcp_server: FanmootMCP,
    mcp_url: str,
) -> None:
    """The happy path through the gate, and the telemetry that proves which link
    it ran under (`S4-1`)."""

    @mcp_server.tool()
    async def post_moot(body: str) -> str:
        """Post a take."""
        return f"posted: {body}"

    token = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )

    async with tooling.session_at(
        mcp_url, headers={"Authorization": f"Bearer {token['access_token']}"}
    ) as session:
        result = await session.call_tool("post_moot", {"body": "up the arsenal"})

    assert result.isError is False

    async with app_db.get_sessionmaker()() as session_db:
        grant = (await session_db.execute(select(AccessGrant))).scalar_one()

    recorded = [call for call in telemetry.recent() if call.tool == "post_moot"]
    assert recorded, "the call was not recorded"
    assert recorded[-1].authenticated is True
    assert recorded[-1].grant_id == grant.id
    # And the grant knows it was used, which is what "last used" means on the
    # connected-apps page.
    assert grant.last_used_at is not None


async def test_a_revoked_grant_stops_working_immediately(
    oauth_http: httpx.AsyncClient,
    static_client: str,
    fanmoot_auth: Replay,
    mcp_url: str,
) -> None:
    """ "Disconnect this app" has to mean now, not in an hour."""
    from app.models.grant import REVOKED_BY_FAN
    from app.oauth import grants as grant_store

    body = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )

    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()
        await grant_store.revoke_grant(session, grant, reason=REVOKED_BY_FAN)

    response = await _post(
        mcp_url, _call("post_moot", body="still here?"), token=body["access_token"]
    )

    assert response.status_code == 401


async def test_every_authenticated_tool_in_the_plan_is_behind_the_gate(
    oauth_db: object,
) -> None:
    """§5's table, as a check rather than a claim.

    `S5` registers these functions; this is the contract it registers against,
    and a write tool that never reached ``TOOL_SCOPES`` would be a write tool
    anyone could call.
    """
    assert set(scopes.TOOL_SCOPES) == {
        "whoami",
        "my_predictions",
        "post_moot",
        "reply_to_moot",
        "react_to_moot",
        "predict_match",
        "follow",
    }
    assert set(scopes.TOOL_SCOPES.values()) <= set(scopes.SUPPORTED_SET)
