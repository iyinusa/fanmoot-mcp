"""The MCP server instance — who this server says it is, and what every tool inherits.

``S2-1``. Three things are decided here and nowhere else:

**Identity.** ``name`` is the programmatic handle a client writes into a config
file and the id this server will claim in the MCP registry (``S7-11``), so it
is stable and lowercase. ``title`` and ``description`` are for people, and
``instructions`` is for the model (see ``app.mcp.instructions``). ``version`` is
*this repo's* version: the SDK falls back to reporting the ``mcp`` package's
version when a server does not set one, which would have every deployment of
this service announcing itself as whatever release of the SDK it happened to be
built against.

**The error contract.** Every tool written in ``S3`` and ``S5`` gets the
``2025-11-25`` behaviour for free — a raised exception becomes a tool execution
error carrying a spoken sentence, never a protocol error (``S2-6``, HAD-7).
Tools are therefore written to raise, not to catch.

**The measurement.** Every call is timed and recorded on the way past
(``S2-5``), so no tool has to remember to do it and none can forget.

``FastMCP`` rather than the low-level server, because ``S3-8`` wants JSON Schema
2020-12 input *and* output schemas on every tool and FastMCP derives both from
a typed Python signature. A hand-written schema beside a typed function is two
descriptions of one thing, and they drift.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import Server as LowLevelServer
from mcp.types import CallToolResult, TextContent
from starlette.requests import Request

from app import __version__
from app.mcp import errors, telemetry
from app.mcp.frames import InstrumentedServer
from app.mcp.instructions import (
    SERVER_DESCRIPTION,
    SERVER_INSTRUCTIONS,
    SERVER_TITLE,
)

#: The handle, not the name. Clients key configuration off this and the MCP
#: registry entry will use it, so it does not change once published.
SERVER_NAME = "fanmoot"

#: Where a person goes to find out what Fanmoot is. Part of ``serverInfo`` in
#: ``2025-11-25``, and the thing a client's connector list links to.
SERVER_WEBSITE = "https://fanmoot.com"


class FanmootMCP(FastMCP[Any]):
    """``FastMCP`` with this server's identity, error mapping and timing.

    The constructor does one thing the SDK does not expect: it replaces the
    low-level server FastMCP built with an ``InstrumentedServer``, so every
    JSON-RPC frame is logged (``S2-5``) and ``serverInfo`` carries a title. The
    handlers are then re-registered onto it — they are bound to a server
    instance, not to this object, so a swap without a re-registration would
    leave a server that answers nothing.
    """

    def __init__(self) -> None:
        super().__init__(
            name=SERVER_NAME,
            instructions=SERVER_INSTRUCTIONS,
            website_url=SERVER_WEBSITE,
        )

        server = InstrumentedServer(
            name=SERVER_NAME,
            version=__version__,
            instructions=SERVER_INSTRUCTIONS,
            website_url=SERVER_WEBSITE,
        )
        server.title = SERVER_TITLE
        server.description = SERVER_DESCRIPTION
        self._mcp_server = server
        self._setup_handlers()

        # Registered after `_setup_handlers`, replacing the tool handler it
        # installed. `validate_input=False` matches FastMCP's own choice:
        # arguments are validated by the tool's pydantic model, which coerces
        # what a voice client sends — "3" for an int, a date as a string —
        # where a raw JSON Schema check would refuse it.
        server.call_tool(validate_input=False)(self._call_tool_recorded)

    @property
    def lowlevel(self) -> LowLevelServer[Any, Request]:
        """The protocol server the Streamable HTTP transport drives.

        FastMCP keeps it private because most servers never need it; this one
        hands it to a session manager it builds itself (``app.mcp.transport``),
        so the mount lives inside the FastAPI application rather than beside it.
        """
        return self._mcp_server

    async def _call_tool_recorded(self, name: str, arguments: dict[str, Any]) -> Any:
        """Run one tool, timing it and turning any failure into words.

        Cancellation is not caught: ``anyio``'s cancelled exception derives from
        ``BaseException``, so a client that hangs up mid-call unwinds cleanly
        instead of being recorded as a tool failure.
        """
        started = time.perf_counter()
        authenticated = telemetry.authenticated_now()

        try:
            result = await self._tool_manager.call_tool(
                name, arguments, context=self.get_context(), convert_result=True
            )
        except Exception as exc:
            telemetry.record(
                tool=name,
                duration_ms=(time.perf_counter() - started) * 1000,
                status="error",
                authenticated=authenticated,
            )
            return CallToolResult(
                content=[TextContent(type="text", text=errors.speakable(exc, tool=name))],
                isError=True,
            )

        telemetry.record(
            tool=name,
            duration_ms=(time.perf_counter() - started) * 1000,
            status="ok",
            authenticated=authenticated,
        )
        return result


_server: FanmootMCP | None = None


def get_server() -> FanmootMCP:
    """The process's MCP server, built on first use.

    One instance, because the tool, resource and prompt registries live on it
    and a second would serve a different surface on the same URL. The transport
    session manager is *not* a singleton — see ``app.mcp.transport``.
    """
    global _server
    if _server is None:
        _server = FanmootMCP()
    return _server


def set_server(server: FanmootMCP | None) -> None:
    """Test hook: install a server carrying test tools, or drop the cached one.

    Mirrors ``app.fanmoot.client.set_client``. A test that registers a tool on
    the shared instance would leak it into every later test's ``tools/list``.
    """
    global _server
    _server = server
