"""``S3-8``, ``S3-9`` — what every tool declares, and what it refuses to exceed.

A per-tool test proves a tool works. This file proves the *surface* is one
surface: twelve tools that a model chooses between using nothing but what
``tools/list`` says about them. A tool with no output schema, no icon, no
read-only annotation or a description written for a REST client is a tool that
loses the Design mark quietly, and none of the tests in the other files would
catch it.
"""

from __future__ import annotations

import httpx
import pytest

from app.mcp.tools.common import LIMIT_DEFAULT, LIMIT_MAX
from tests.replay import Replay
from tests.tooling import refusal, surface

#: The read surface, exactly as §5 of the plan tables it.
READ_TOOLS = {
    "find_club",
    "find_competition",
    "list_matches",
    "upcoming_matches",
    "get_match",
    "live_scores",
    "read_match_conversation",
    "read_thread",
    "search_fanmoot",
    "trending_takes",
    "club_news",
    "match_mood",
}


async def test_the_surface_is_the_twelve_read_tools(replay: Replay, mcp_url: str) -> None:
    listed = await surface(mcp_url)

    assert set(listed) == READ_TOOLS


async def test_every_tool_declares_both_schemas(replay: Replay, mcp_url: str) -> None:
    """FastMCP derives both from the typed signature, so the schema a client
    reads and the object this server builds cannot drift (``S3-8``)."""
    for name, tool in (await surface(mcp_url)).items():
        assert tool.inputSchema["type"] == "object", name
        assert tool.outputSchema is not None, name
        assert tool.outputSchema["properties"]["spoken"]["type"] == "string", name


async def test_every_tool_is_annotated_read_only(replay: Replay, mcp_url: str) -> None:
    """Nothing in ``S3`` writes. A client uses these to decide what it may
    retry without asking the fan — and ``S5``'s tools will say the opposite."""
    for name, tool in (await surface(mcp_url)).items():
        assert tool.annotations is not None, name
        assert tool.annotations.readOnlyHint is True, name
        assert tool.annotations.destructiveHint is False, name
        assert tool.annotations.openWorldHint is True, name


async def test_every_tool_has_an_icon_and_a_title(replay: Replay, mcp_url: str) -> None:
    """Icons are new in ``2025-11-25`` and are what a client's tool list shows
    a person."""
    for name, tool in (await surface(mcp_url)).items():
        assert tool.title, name
        assert tool.icons, name
        assert str(tool.icons[0].src).endswith(".svg"), name
        assert tool.icons[0].mimeType == "image/svg+xml", name


async def test_the_icons_a_tool_points_at_are_actually_served(
    client: httpx.AsyncClient, replay: Replay, mcp_url: str
) -> None:
    """A declared icon that 404s is worse than no icon, and this server serves
    its own rather than borrowing fanmoot.com's origin."""
    for tool in (await surface(mcp_url)).values():
        path = str(tool.icons[0].src).removeprefix("http://testserver")
        response = await client.get(path)

        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("image/svg")


async def test_every_description_is_written_for_a_model_choosing_a_tool(
    replay: Replay, mcp_url: str
) -> None:
    """Not "returns a list of matches" — the thing a fan would say, and when to
    reach for something else instead (HAD-7)."""
    for name, tool in (await surface(mcp_url)).items():
        assert tool.description is not None
        assert len(tool.description) > 120, name
        assert "'" in tool.description, name  # it quotes what a fan actually says


async def test_every_list_is_bounded_in_the_schema_itself(replay: Replay, mcp_url: str) -> None:
    """``S3-9``. Five by default, twenty at the cap, declared where a client
    can see it rather than enforced only where it cannot."""
    limited = {
        name: tool
        for name, tool in (await surface(mcp_url)).items()
        if "limit" in tool.inputSchema["properties"]
    }

    assert len(limited) >= 8
    for name, tool in limited.items():
        field = tool.inputSchema["properties"]["limit"]
        assert field["maximum"] == LIMIT_MAX, name
        assert field["minimum"] == 1, name
        assert field["default"] in (LIMIT_DEFAULT, 3), name


@pytest.mark.parametrize("tool", ["trending_takes", "list_matches", "club_news"])
async def test_asking_past_the_cap_teaches_the_cap(tool: str, replay: Replay, mcp_url: str) -> None:
    """A range violation is a tool execution error carrying the bound, not a
    422 and not "I could not make sense of limit" (``S2-6``)."""
    spoken = await refusal(mcp_url, tool, limit=50)

    assert spoken == "I can manage limit up to 20 at a time. Ask for fewer?"


async def test_a_tool_that_needs_an_argument_asks_for_it_by_name(
    replay: Replay, mcp_url: str
) -> None:
    spoken = await refusal(mcp_url, "get_match")

    assert spoken == "I still need match for that."


async def test_nothing_on_the_read_surface_asks_for_a_credential(
    replay: Replay, mcp_url: str
) -> None:
    """Twelve tools, no authentication (§5). A fan gets football answers
    without linking anything; the write half arrives with ``S4`` and ``S5``."""
    for name, tool in (await surface(mcp_url)).items():
        properties = tool.inputSchema["properties"]
        assert not {"token", "access_token", "handle", "email"} & set(properties), name
