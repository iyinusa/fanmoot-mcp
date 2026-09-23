"""``S3-8``, ``S3-9``, ``S5-7`` — what every tool declares, and what it refuses
to exceed.

A per-tool test proves a tool works. This file proves the *surface* is one
surface: nineteen tools that a model chooses between using nothing but what
``tools/list`` says about them. A tool with no output schema, no icon, no
annotation or a description written for a REST client is a tool that loses the
Design mark quietly, and none of the tests in the other files would catch it.

The split between the two halves is asserted here as well, because it is the
one property a client can see before authenticating: twelve tools that take no
credential, seven that say plainly that they write as the fan.
"""

from __future__ import annotations

import httpx
import pytest

from app.mcp.tools.common import LIMIT_DEFAULT, LIMIT_MAX
from app.oauth import scopes
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

#: The authenticated half (``S5``). Two of them read — they read the fan's own
#: account rather than Fanmoot at large, which is why they need a scope.
WRITE_TOOLS = {
    "whoami",
    "post_moot",
    "reply_to_moot",
    "react_to_moot",
    "predict_match",
    "my_predictions",
    "follow",
}

#: The ones that actually change something. ``readOnlyHint`` must be false on
#: exactly these.
ACTING_TOOLS = {"post_moot", "reply_to_moot", "react_to_moot", "predict_match", "follow"}


async def test_the_surface_is_the_two_halves_of_section_five(replay: Replay, mcp_url: str) -> None:
    listed = await surface(mcp_url)

    assert set(listed) == READ_TOOLS | WRITE_TOOLS


async def test_every_authenticated_tool_is_one_the_gate_guards(
    replay: Replay, mcp_url: str
) -> None:
    """The two lists are declared in different files for a reason `S4-9`
    explains — the gate answers before the tool exists in the request — so
    this is the assertion that keeps them the same list.

    A write tool missing from ``TOOL_SCOPES`` is a write tool anybody can call.
    """
    listed = await surface(mcp_url)

    assert set(scopes.TOOL_SCOPES) == WRITE_TOOLS
    assert set(listed) >= WRITE_TOOLS
    assert not (READ_TOOLS & set(scopes.TOOL_SCOPES))


async def test_the_write_tools_are_visible_before_the_fan_has_linked(
    replay: Replay, mcp_url: str
) -> None:
    """Not hidden behind authentication, deliberately.

    A client that cannot see ``post_moot`` cannot ask to be linked, and the
    401 challenge it gets from calling it is the whole mechanism account
    linking starts from (`S4-9`).
    """
    listed = await surface(mcp_url)

    assert set(listed) >= WRITE_TOOLS


async def test_every_tool_declares_both_schemas(replay: Replay, mcp_url: str) -> None:
    """FastMCP derives both from the typed signature, so the schema a client
    reads and the object this server builds cannot drift (``S3-8``)."""
    for name, tool in (await surface(mcp_url)).items():
        assert tool.inputSchema["type"] == "object", name
        assert tool.outputSchema is not None, name
        assert tool.outputSchema["properties"]["spoken"]["type"] == "string", name


async def test_annotations_say_which_tools_write(replay: Replay, mcp_url: str) -> None:
    """``S3-8`` and ``S5-7``. A client uses these to decide what it may retry
    without asking the fan, and what to confirm before calling at all.

    ``destructiveHint`` is false on all nineteen and that is the truth rather
    than a convenience: nothing on this surface deletes or overwrites. Posting
    adds a take; predicting replaces only the fan's own earlier call.
    """
    for name, tool in (await surface(mcp_url)).items():
        assert tool.annotations is not None, name
        assert tool.annotations.readOnlyHint is (name not in ACTING_TOOLS), name
        assert tool.annotations.destructiveHint is False, name
        assert tool.annotations.openWorldHint is True, name


async def test_only_the_genuinely_repeatable_tools_claim_to_be_idempotent(
    replay: Replay, mcp_url: str
) -> None:
    """``S5-7``. ``post_moot``'s idempotency key makes a retried *frame* safe;
    it does not make a second call safe, and a hint that said otherwise would
    be the one a client retries on."""
    listed = await surface(mcp_url)

    repeatable = {
        name
        for name, tool in listed.items()
        if tool.annotations.idempotentHint  # type: ignore[union-attr]
    }
    assert repeatable == (READ_TOOLS | WRITE_TOOLS) - {
        "post_moot",
        "reply_to_moot",
        "react_to_moot",
    }


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


async def test_no_tool_anywhere_asks_for_a_credential(replay: Replay, mcp_url: str) -> None:
    """A fan gets football answers without linking anything, and the tools that
    *do* act as them take their credential from the OAuth grant rather than
    from an argument a model could invent (HAD-3)."""
    for name, tool in (await surface(mcp_url)).items():
        properties = tool.inputSchema["properties"]
        assert not {"token", "access_token", "password", "email"} & set(properties), name


async def test_every_acting_tool_tells_a_host_to_confirm_first(
    replay: Replay, mcp_url: str
) -> None:
    """``S5-7``. The annotation says a tool writes; it does not say what. A
    host that is going to ask "post this as @thegunner?" needs the description
    to have told it to."""
    for name, tool in (await surface(mcp_url)).items():
        if name not in ACTING_TOOLS:
            continue
        assert tool.description is not None
        spoken = tool.description.lower()
        assert "signed-in fan" in spoken, name
        assert any(word in spoken for word in ("confirm", "read it back", "before you call")), name
