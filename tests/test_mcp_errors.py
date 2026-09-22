"""``S2-6`` — a bad argument is a tool execution error, and it is a sentence.

The compliance half: ``2025-11-25`` says input validation comes back as a tool
error the model can correct itself from, not a protocol error that ends the
turn. The half that is actually hard: the message. FastMCP's own wrapper text
is `"Error executing tool get_match: 1 validation error for ..."`, and a voice
model handed that will read it out (HAD-7).
"""

from __future__ import annotations

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent
from pydantic import BaseModel, ValidationError

from app.fanmoot.client import (
    FanmootNotFound,
    FanmootRateLimited,
    FanmootUnavailable,
)
from app.mcp import errors
from app.mcp.server import FanmootMCP


def _wrapped(exc: Exception, tool: str = "get_match") -> ToolError:
    """What FastMCP hands us: its envelope, with the real failure as the cause."""
    try:
        raise ToolError(f"Error executing tool {tool}: {exc}") from exc
    except ToolError as wrapper:
        return wrapper


def _validation_error(**payload: object) -> ValidationError:
    class Arguments(BaseModel):
        club: str
        limit: int

    try:
        Arguments.model_validate(payload)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected the model to refuse those arguments")


def test_an_upstream_failure_keeps_the_sentence_the_client_wrote() -> None:
    """``app.fanmoot.client`` is where an HTTP status becomes something sayable.
    Re-describing it here would be a second translation of one failure."""
    spoken = errors.speakable(_wrapped(FanmootNotFound("I cannot find that match.")), tool="t")

    assert spoken == "I cannot find that match."


def test_a_missing_argument_becomes_a_question() -> None:
    spoken = errors.speakable(_wrapped(_validation_error(limit=5)), tool="get_match")

    assert spoken == "I still need club for that."
    assert "validation" not in spoken.lower()


def test_an_unusable_argument_asks_the_fan_to_repeat_themselves() -> None:
    spoken = errors.speakable(_wrapped(_validation_error(club="arsenal", limit="loads")), tool="t")

    assert spoken == "I could not make sense of limit. Say that again?"


def test_several_problems_are_reported_in_one_breath() -> None:
    spoken = errors.speakable(_wrapped(_validation_error(limit="loads")), tool="t")

    assert spoken == "I still need club, and I could not make sense of limit."


def test_a_long_list_of_fields_is_not_read_out_in_full() -> None:
    class Arguments(BaseModel):
        one: str
        two: str
        three: str
        four: str
        five: str

    try:
        Arguments.model_validate({})
    except ValidationError as exc:
        spoken = errors.speakable(_wrapped(exc), tool="t")

    assert spoken == "I still need one, two, three and a couple more things for that."


def test_an_unknown_tool_is_addressed_to_the_model() -> None:
    spoken = errors.speakable(ToolError("Unknown tool: get_standings"), tool="get_standings")

    assert spoken == "There is no tool called get_standings on this server."


def test_an_unrecognised_failure_says_nothing_it_should_not() -> None:
    """A fan hears a generic sentence; the operator gets the truth in the log.
    Neither hears a stack, a URL or a status code."""
    spoken = errors.speakable(_wrapped(KeyError("moot_id")), tool="t")

    assert spoken == "Something went wrong reaching Fanmoot. Try that again in a moment."


def test_a_rate_limit_keeps_its_own_words() -> None:
    spoken = errors.speakable(
        _wrapped(FanmootRateLimited("Fanmoot has had enough for now.", 30)), tool="t"
    )

    assert spoken == "Fanmoot has had enough for now."


# ── End to end, through the protocol ─────────────────────────────────────────


@pytest.fixture
def failing_tools(mcp_server: FanmootMCP) -> FanmootMCP:
    """Two tools that fail on demand.

    Named for what they do here rather than after the real surface: the server
    carries the twelve read tools of ``S3`` from the moment it is built, and a
    stub sharing one of their names would be quietly refused as a duplicate —
    leaving this test asserting against the real tool's behaviour instead of
    the contract it means to check.
    """

    @mcp_server.tool()
    async def match_report(club: str) -> str:
        """The match a club is playing."""
        raise FanmootUnavailable("Fanmoot is not answering right now.")

    @mcp_server.tool()
    async def competition_scores(competition: str) -> str:
        """Scores in a competition."""
        return f"nothing on in {competition}"

    return mcp_server


async def test_a_failing_tool_answers_in_words_not_a_protocol_error(
    mcp_url: str, failing_tools: FanmootMCP
) -> None:
    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("match_report", {"club": "arsenal"})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "Fanmoot is not answering right now."


async def test_a_missing_argument_does_not_end_the_session(
    mcp_url: str, failing_tools: FanmootMCP
) -> None:
    """The point of a tool error over a protocol error: the model gets told
    what was wrong and can try again inside the same turn."""
    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        failed = await session.call_tool("competition_scores", {})
        recovered = await session.call_tool(
            "competition_scores", {"competition": "the premier league"}
        )

    assert failed.isError is True
    assert isinstance(failed.content[0], TextContent)
    assert failed.content[0].text == "I still need competition for that."
    assert recovered.isError is False


async def test_calling_a_tool_that_is_not_there_is_answered_not_refused(
    mcp_url: str, failing_tools: FanmootMCP
) -> None:
    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        result = await session.call_tool("get_standings", {})

    assert result.isError is True
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text == "There is no tool called get_standings on this server."
