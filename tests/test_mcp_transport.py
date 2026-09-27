"""``S2-2``, ``S2-4``, ``S2-8`` — the handshake, the surface and the session.

Driven through the **MCP client SDK**, not raw HTTP. A test that posts JSON at
``/mcp`` and reads the SSE back asserts that our reading of the spec matches
our writing of it, which is no assurance at all. Driving the real client is the
only way to find out whether a client Alexa+ will use can talk to this server
(HAD-14).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import LATEST_PROTOCOL_VERSION

from app import __version__
from app.mcp.instructions import SERVER_DESCRIPTION, SERVER_TITLE
from app.mcp.server import SERVER_NAME, SERVER_WEBSITE, FanmootMCP

#: The era Alexa+ implements (HAD-2). Written out rather than imported so a
#: dependency bump that moved the SDK on cannot move the assertion with it.
HANDSHAKE_ERA = "2025-11-25"


async def test_negotiates_exactly_the_version_alexa_asks_for(mcp_url: str) -> None:
    """The single most important compliance test in the repo (``S2-4``).

    Alexa+ speaks `2025-11-25`. The SDK also ships the modern era, and a
    server that served only that would fail the track — silently, at the
    handshake, with nothing in our own logs to explain it.
    """
    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        result = await session.initialize()

    assert result.protocolVersion == HANDSHAKE_ERA
    assert LATEST_PROTOCOL_VERSION == HANDSHAKE_ERA


async def test_server_info_identifies_this_build_not_the_sdk(mcp_url: str) -> None:
    """``S2-1``.

    The version is the load-bearing assertion: a server that does not set one
    reports the `mcp` package's version instead, so every deployment would
    announce itself as whichever SDK release it was built against.
    """
    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        result = await session.initialize()

    info = result.serverInfo
    assert info.name == SERVER_NAME
    assert info.version == __version__
    assert info.title == SERVER_TITLE
    assert str(info.websiteUrl) == SERVER_WEBSITE
    # `description` rides as an extra field: see `app.mcp.frames`.
    assert (info.model_extra or {}).get("description") == SERVER_DESCRIPTION


async def test_the_model_is_told_how_to_speak_a_scoreline(mcp_url: str) -> None:
    """Instructions reach the client, and carry the part a schema cannot say."""
    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        result = await session.initialize()

    assert result.instructions is not None
    assert "nil" in result.instructions
    assert "moot" in result.instructions


async def test_the_whole_surface_answers(mcp_url: str, mcp_server: FanmootMCP) -> None:
    """``S2-8``. Each capability is *declared and handled*.

    A client that finds `prompts/list` unimplemented stops asking, and the
    prompts a later milestone adds would never be discovered — so an empty
    list is the answer that matters here, not an error.

    A tool registered by the test appears alongside the server's own installs,
    which is the other half of the assertion: the surface is the server's, and
    a test can still add to it.
    """

    @mcp_server.tool()
    async def whistle() -> str:
        """Blow the whistle."""
        return "peep"

    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        resources = await session.list_resources()
        prompts = await session.list_prompts()

    names = [tool.name for tool in tools.tools]
    assert "whistle" in names
    assert "live_scores" in names
    # `S6`'s four `ui://` views are the resources so far; the `fanmoot://`
    # catalogue is not registered yet, and `prompts/list` still answers with
    # an empty list rather than an error, which is the thing being asserted.
    assert {str(resource.uri) for resource in resources.resources} == {
        "ui://fanmoot/scoreboard",
        "ui://fanmoot/thread",
        "ui://fanmoot/fixtures",
        "ui://fanmoot/mood",
        "ui://fanmoot/moot",
        "ui://fanmoot/fan",
        "ui://fanmoot/club",
        "ui://fanmoot/predictions",
    }
    assert prompts.prompts == []


async def test_a_tool_declares_input_and_output_schemas(
    mcp_url: str, mcp_server: FanmootMCP
) -> None:
    """The shape ``S3-8`` depends on: both schemas, derived from the signature."""

    @mcp_server.tool()
    async def score(club: str) -> str:
        """What the score is."""
        return f"{club} are winning"

    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()

    tool = next(t for t in tools.tools if t.name == "score")
    assert tool.inputSchema["properties"]["club"]["type"] == "string"
    assert tool.inputSchema["required"] == ["club"]
    assert tool.outputSchema is not None


async def test_a_session_id_is_issued_and_held_across_calls(mcp_url: str) -> None:
    """``S2-2``. Stateful, because resumability and server-initiated
    notifications both need a session to belong to."""
    async with streamablehttp_client(mcp_url) as (read, write, session_id):
        assert session_id() is None  # not yet — the id arrives with `initialize`
        async with ClientSession(read, write) as session:
            await session.initialize()
            first = session_id()
            await session.list_tools()
            assert session_id() == first

    assert first is not None


async def test_an_unknown_session_id_is_404_not_a_new_session(mcp_url: str) -> None:
    """A stale id must not be silently upgraded into a fresh session.

    That is what tells a client its session is gone and it should initialize
    again — and it is how a one-worker deployment would announce a restart
    rather than answering with a server that has forgotten the conversation.
    """
    async with httpx.AsyncClient() as http:
        response = await http.post(
            mcp_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "mcp-session-id": "a-session-this-process-never-issued",
                "mcp-protocol-version": HANDSHAKE_ERA,
            },
        )

    assert response.status_code == 404


async def test_the_endpoint_is_mcp_with_no_redirect(mcp_url: str) -> None:
    """``POST /mcp`` is answered, not redirected to ``/mcp/``.

    A mount would 307 here. Inside a voice turn that is a wasted round trip,
    and a client that declines to re-send a POST body across a redirect sees
    this server as broken.
    """
    async with httpx.AsyncClient() as http:
        response = await http.post(
            mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": HANDSHAKE_ERA,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 200
    assert response.headers["mcp-session-id"]


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
async def test_the_endpoint_refuses_methods_the_transport_does_not_define(
    mcp_url: str, method: str
) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.request(method, mcp_url)

    assert response.status_code == 405


async def test_a_request_id_crosses_the_boundary(mcp_url: str) -> None:
    """The demo's trace panel and this server's logs name the same request.

    An inbound ``X-Request-ID`` is honoured and echoed, which is what lets a
    frame in the panel be matched to a line in the log (HAD-12).
    """
    headers: dict[str, Any] = {"X-Request-ID": "trace-me"}
    async with (
        streamablehttp_client(mcp_url, headers=headers) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()

    async with httpx.AsyncClient() as http:
        response = await http.request("PUT", mcp_url, headers={"X-Request-ID": "trace-me"})

    assert response.headers["X-Request-ID"] == "trace-me"


async def test_correlation_does_not_sit_between_the_transport_and_its_stream(
    replay: Any, mcp_url: str
) -> None:
    """The regression that made ``RequestIdMiddleware`` raw ASGI.

    As a ``BaseHTTPMiddleware`` it ran ``/mcp`` in a second task and piped the
    response back through a memory stream — and every frame this server answers
    is an SSE stream. In a loaded process the bridge could see its end close
    before a response had started and raise ``RuntimeError("No response
    returned.")``: a 500 on a call the MCP server had already answered, and
    from then on every session against that process failed the same way.

    Sessions one after another, against one server, is the shape that surfaced
    it. Correlation must be a wrapped ``send``, never a second task.
    """
    from tests.tooling import call, surface

    for _ in range(5):
        spoken, _payload = await call(mcp_url, "live_scores")
        assert spoken

    assert await surface(mcp_url)
