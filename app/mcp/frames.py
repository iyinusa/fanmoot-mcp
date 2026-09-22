"""Every JSON-RPC frame, in and out, as a log line.

``S2-5``, and the reason it is not optional: the trace *is* the demo (HAD-12).
The web demo builds its own trace panel from its own MCP session, but a panel
that cannot be reconciled with the server's logs proves nothing. Both sides
carry the same request id — the HTTP middleware binds it, the demo sends it as
``X-Request-ID`` — so a frame in the panel and a line in the log are the same
event seen twice.

**Where this hooks in, and why there.** The SDK hands the low-level server a
pair of memory object streams and does the whole session over them: requests
and notifications arrive on one, responses and server-initiated frames leave on
the other. Wrapping *those* catches every frame exactly once, already parsed
into typed messages — whereas wrapping the ASGI app would mean re-parsing
bodies, and re-assembling SSE events to find the frames inside them. It is also
the only layer that sees a frame the transport generates on its own.

**No payloads.** A frame's method, its id and how long it took are recorded; its
``params`` and ``result`` are not (``S7-4``). ``post_moot``'s arguments are a
fan's opinion and ``whoami``'s result is their identity. The one exception is
the *name* of the thing being acted on — which tool, which resource — because
"a tools/call happened" is not observability.
"""

from __future__ import annotations

import time
from typing import Any, cast

from anyio.abc import ObjectReceiveStream, ObjectSendStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.models import InitializationOptions
from mcp.shared.message import SessionMessage
from mcp.types import (
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    RequestId,
)
from starlette.requests import Request

from app.logging_config import get_logger

log = get_logger("mcp.frame")

#: Methods whose params name the thing being acted on. The value is the param
#: key to record; everything else about the params stays unlogged.
_SUBJECT_PARAM = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
    "resources/subscribe": "uri",
    "resources/unsubscribe": "uri",
}


class FrameLog:
    """Shared state for one session's two streams.

    Holds only the in-flight requests, so a response can be logged with the
    method that produced it and the time it took. A response frame carries an
    id and nothing else — without this correlation the log would say a result
    came back and never say what for.
    """

    def __init__(self) -> None:
        self._in_flight: dict[str, tuple[str, float]] = {}

    def opened(self, request_id: RequestId, method: str) -> None:
        self._in_flight[str(request_id)] = (method, time.perf_counter())

    def closed(self, request_id: RequestId) -> tuple[str | None, float | None]:
        started = self._in_flight.pop(str(request_id), None)
        if started is None:
            return None, None
        method, at = started
        return method, round((time.perf_counter() - at) * 1000, 2)

    def record(self, direction: str, message: SessionMessage) -> None:
        root = message.message.root

        if isinstance(root, JSONRPCRequest):
            self.opened(root.id, root.method)
            log.info(
                "mcp_frame",
                direction=direction,
                kind="request",
                method=root.method,
                id=root.id,
                **_subject(root.method, root.params),
            )
            return

        if isinstance(root, JSONRPCNotification):
            log.info("mcp_frame", direction=direction, kind="notification", method=root.method)
            return

        if isinstance(root, JSONRPCResponse):
            method, duration_ms = self.closed(root.id)
            log.info(
                "mcp_frame",
                direction=direction,
                kind="response",
                method=method,
                id=root.id,
                duration_ms=duration_ms,
            )
            return

        if isinstance(root, JSONRPCError):  # pragma: no branch
            method, duration_ms = self.closed(root.id)
            # The error *code* is protocol-level and safe; `data` can carry
            # anything the handler put there, so it is left out.
            log.warning(
                "mcp_frame",
                direction=direction,
                kind="error",
                method=method,
                id=root.id,
                duration_ms=duration_ms,
                code=root.error.code,
            )


def _subject(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """The one param worth logging for this method, if it has one."""
    key = _SUBJECT_PARAM.get(method)
    if key is None or not params:
        return {}
    value = params.get(key)
    return {"subject": value} if isinstance(value, str) else {}


class LoggedReceiveStream(ObjectReceiveStream[SessionMessage | Exception]):
    """The inbound half. Logs each frame, changes none of them."""

    def __init__(
        self,
        wrapped: MemoryObjectReceiveStream[SessionMessage | Exception],
        frames: FrameLog,
    ) -> None:
        self._wrapped = wrapped
        self._frames = frames

    async def receive(self) -> SessionMessage | Exception:
        message = await self._wrapped.receive()
        if isinstance(message, SessionMessage):
            self._frames.record("in", message)
        return message

    async def aclose(self) -> None:
        await self._wrapped.aclose()


class LoggedSendStream(ObjectSendStream[SessionMessage]):
    """The outbound half. Logs each frame, and completes ``serverInfo``.

    The enrichment is narrow and worth naming, because it is the one place this
    server changes a frame the SDK produced. ``ServerSession`` builds the
    ``initialize`` result's ``serverInfo`` from four fields — name, version,
    website and icons — and drops ``title`` and ``description``, which
    ``2025-11-25`` allows and every client surfaces when present. Without this
    the Inspector, Claude's connector list and the demo's own header show a bare
    identifier where the server's name should be (``S2-1``).
    """

    def __init__(
        self,
        wrapped: MemoryObjectSendStream[SessionMessage],
        frames: FrameLog,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._frames = frames
        self._title = title
        self._description = description

    async def send(self, item: SessionMessage) -> None:
        root = item.message.root
        if isinstance(root, JSONRPCResponse):
            self._complete_server_info(root.result)
        self._frames.record("out", item)
        await self._wrapped.send(item)

    def _complete_server_info(self, result: dict[str, Any]) -> None:
        """Add ``title`` and ``description`` to an ``initialize`` result.

        Identified by shape rather than by remembering which id was an
        ``initialize`` — the pair of a ``protocolVersion`` and a ``serverInfo``
        occurs in exactly one result type, and a shape test cannot go stale
        against a session that reconnected.
        """
        if "protocolVersion" not in result:
            return
        server_info = result.get("serverInfo")
        if not isinstance(server_info, dict):  # pragma: no cover - always present
            return
        if self._title is not None:
            server_info.setdefault("title", self._title)
        if self._description is not None:
            server_info.setdefault("description", self._description)

    async def aclose(self) -> None:
        await self._wrapped.aclose()


class InstrumentedServer(MCPServer[Any, Request]):
    """The low-level server, with its session streams wrapped on the way in.

    Overriding ``run`` rather than patching the session is what keeps protocol
    version negotiation — the single most important compliance behaviour in this
    repo (``S2-4``) — entirely the SDK's, and entirely untouched.
    """

    #: Set by `app.mcp.server` after construction; both ride on the
    #: `initialize` result. Kept as attributes rather than constructor
    #: arguments so `Server.__init__`'s signature stays the SDK's.
    title: str | None = None
    description: str | None = None

    async def run(
        self,
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
        write_stream: MemoryObjectSendStream[SessionMessage],
        initialization_options: InitializationOptions,
        raise_exceptions: bool = False,
        stateless: bool = False,
    ) -> None:
        frames = FrameLog()
        logged_read = LoggedReceiveStream(read_stream, frames)
        logged_write = LoggedSendStream(
            write_stream, frames, title=self.title, description=self.description
        )
        # The SDK types these as the concrete memory streams it created, but
        # only ever uses the `ObjectStream` surface — `async with`, `async
        # for`, `send`. The casts say that, in the one place it is true.
        await super().run(
            cast(MemoryObjectReceiveStream[SessionMessage | Exception], logged_read),
            cast(MemoryObjectSendStream[SessionMessage], logged_write),
            initialization_options,
            raise_exceptions=raise_exceptions,
            stateless=stateless,
        )
