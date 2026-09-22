"""Calling one tool over the protocol, and reading both halves of the answer.

Every ``S3`` test asserts the same things — the sentence a model would read
aloud, the structured payload a client would render, and which of the two paths
the call took — so the unwrapping lives here rather than five lines into each
test.

Each call opens its own session. That costs a handshake per assertion and buys
two things worth more than the milliseconds: a test can never be affected by
what a previous call left in a session, and the path exercised is the whole one
a client takes — ``initialize``, ``tools/call``, teardown — rather than a tool
function imported and awaited. A tool that works in-process and fails over
Streamable HTTP is a tool that does not work (HAD-14).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent, Tool


@asynccontextmanager
async def session_at(url: str) -> AsyncIterator[ClientSession]:
    """An initialised MCP session against a running server."""
    async with (
        streamablehttp_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


async def call(url: str, tool: str, /, **arguments: Any) -> tuple[str, dict[str, Any]]:
    """Call a tool that is expected to work. Returns ``(spoken, structured)``."""
    async with session_at(url) as session:
        result = await session.call_tool(tool, arguments)

    assert result.isError is False, _text(result.content)
    assert result.content, f"{tool} answered with nothing to say"
    assert result.structuredContent is not None, f"{tool} answered with no structuredContent"
    return _text(result.content), result.structuredContent


async def refusal(url: str, tool: str, /, **arguments: Any) -> str:
    """Call a tool that is expected to answer with a question or a refusal.

    ``isError`` is asserted true because that is the ``2025-11-25`` contract
    this server is held to: a bad argument comes back as a tool *execution*
    error the model can act on inside the same turn, never as a protocol error
    (``S2-6``, HAD-7).
    """
    async with session_at(url) as session:
        result = await session.call_tool(tool, arguments)

    assert result.isError is True, _text(result.content)
    return _text(result.content)


async def surface(url: str) -> dict[str, Tool]:
    """``tools/list``, keyed by name."""
    async with session_at(url) as session:
        listing = await session.list_tools()
    return {tool.name: tool for tool in listing.tools}


def _text(content: list[Any]) -> str:
    return " ".join(block.text for block in content if isinstance(block, TextContent))
