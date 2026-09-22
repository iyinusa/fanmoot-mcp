"""``S2-5`` — every frame logged, every call recorded, no payloads anywhere.

The trace *is* the demo (HAD-12): the panel in the web app and the lines in
this server's logs are the same events seen twice, joined by a request id. So
these assert what a line carries — and, just as firmly, what it does not.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.message import SessionMessage
from mcp.types import (
    ErrorData,
    JSONRPCError,
    JSONRPCMessage,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

from app.mcp import frames, telemetry
from app.mcp.frames import FrameLog, LoggedSendStream
from app.mcp.instructions import SERVER_DESCRIPTION, SERVER_TITLE
from app.mcp.server import FanmootMCP


class _Recorder:
    """Stands in for the module logger, so a test reads lines instead of stdout."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.lines.append((event, fields))

    warning = info
    exception = info


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(frames, "log", recorder)
    return recorder


def _request(id_: int, method: str, params: dict[str, Any] | None = None) -> SessionMessage:
    return SessionMessage(
        JSONRPCMessage(JSONRPCRequest(jsonrpc="2.0", id=id_, method=method, params=params))
    )


def _response(id_: int, result: dict[str, Any] | None = None) -> SessionMessage:
    return SessionMessage(
        JSONRPCMessage(JSONRPCResponse(jsonrpc="2.0", id=id_, result=result or {}))
    )


def test_a_response_is_logged_with_the_method_that_produced_it(logged: _Recorder) -> None:
    """A response frame carries an id and nothing else. Without the
    correlation the log would say a result came back and never say what for."""
    log = FrameLog()
    log.record("in", _request(1, "tools/list"))
    log.record("out", _response(1))

    _, response = logged.lines[1]
    assert response["kind"] == "response"
    assert response["method"] == "tools/list"
    assert response["duration_ms"] >= 0


def test_a_tool_call_names_the_tool_and_nothing_else(logged: _Recorder) -> None:
    """``post_moot``'s arguments are a fan's opinion (``S7-4``). The tool's
    name is not, and "a tools/call happened" is not observability."""
    log = FrameLog()
    log.record(
        "in",
        _request(
            7, "tools/call", {"name": "post_moot", "arguments": {"body": "VAR is a disgrace"}}
        ),
    )

    _, line = logged.lines[0]
    assert line["subject"] == "post_moot"
    assert "VAR is a disgrace" not in str(line)
    assert "arguments" not in line


def test_a_notification_is_logged_without_an_id(logged: _Recorder) -> None:
    log = FrameLog()
    log.record(
        "in",
        SessionMessage(
            JSONRPCMessage(JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized"))
        ),
    )

    event, line = logged.lines[0]
    assert event == "mcp_frame"
    assert line["kind"] == "notification"
    assert "id" not in line


def test_an_error_frame_carries_its_code_but_not_its_data(logged: _Recorder) -> None:
    """``data`` is whatever a handler put there. The code is protocol-level."""
    log = FrameLog()
    log.record("in", _request(3, "resources/read", {"uri": "ui://scoreboard"}))
    log.record(
        "out",
        SessionMessage(
            JSONRPCMessage(
                JSONRPCError(
                    jsonrpc="2.0",
                    id=3,
                    error=ErrorData(code=-32602, message="nope", data="a fan's email address"),
                )
            )
        ),
    )

    _, line = logged.lines[1]
    assert line["kind"] == "error"
    assert line["code"] == -32602
    assert "email" not in str(line)


def test_an_unmatched_response_is_still_logged(logged: _Recorder) -> None:
    """A session that resumed mid-flight has responses whose requests this
    process never saw. Dropping them would hide exactly the frames worth
    seeing."""
    FrameLog().record("out", _response(99))

    _, line = logged.lines[0]
    assert line["method"] is None
    assert line["duration_ms"] is None


class _Collecting:
    def __init__(self) -> None:
        self.sent: list[SessionMessage] = []

    async def send(self, item: SessionMessage) -> None:
        self.sent.append(item)

    async def aclose(self) -> None:  # pragma: no cover - never closed in these tests
        return None


async def test_server_info_is_completed_on_the_way_out(logged: _Recorder) -> None:
    """``ServerSession`` builds ``serverInfo`` from four fields and drops the
    two a person reads. This is the one place a frame is changed."""
    wrapped = _Collecting()
    stream = LoggedSendStream(
        wrapped,  # type: ignore[arg-type]
        FrameLog(),
        title=SERVER_TITLE,
        description=SERVER_DESCRIPTION,
    )

    await stream.send(
        _response(
            1,
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "serverInfo": {"name": "fanmoot"},
            },
        )
    )

    info = wrapped.sent[0].message.root.result["serverInfo"]  # type: ignore[union-attr]
    assert info["title"] == SERVER_TITLE
    assert info["description"] == SERVER_DESCRIPTION


async def test_an_ordinary_result_is_passed_through_untouched(logged: _Recorder) -> None:
    wrapped = _Collecting()
    stream = LoggedSendStream(wrapped, FrameLog(), title="x", description="y")  # type: ignore[arg-type]

    await stream.send(_response(2, {"tools": []}))

    assert wrapped.sent[0].message.root.result == {"tools": []}  # type: ignore[union-attr]


# ── The tool_calls record ────────────────────────────────────────────────────


async def test_every_call_is_recorded_with_its_outcome(
    mcp_url: str, mcp_server: FanmootMCP
) -> None:
    """One row per call — tool, duration, status, authenticated (``S2-5``).
    ``S4-1`` turns this into an insert; the shape is already the table's."""

    @mcp_server.tool()
    async def competition_scores(competition: str) -> str:
        """Scores. Named around the real `live_scores`, which `S3` registers."""
        if competition == "the moon":
            raise ValueError("no football there")
        return "two-all"

    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        await session.call_tool("competition_scores", {"competition": "the premier league"})
        await session.call_tool("competition_scores", {"competition": "the moon"})

    recorded = telemetry.recent()
    assert [(call.tool, call.status) for call in recorded] == [
        ("competition_scores", "ok"),
        ("competition_scores", "error"),
    ]
    assert all(call.duration_ms >= 0 for call in recorded)
    # The read tools take no credential, so False here is the right answer
    # rather than a placeholder — see `tests/test_oauth_challenge.py` for the
    # tools that do.
    assert all(call.authenticated is False for call in recorded)


async def test_a_record_holds_no_arguments_and_no_result(
    mcp_url: str, mcp_server: FanmootMCP
) -> None:
    """A take's *text* never reaches a record (``S7-4``); its tool's name does.

    The tool here is deliberately not called ``post_moot``: from `S4-9` that name
    is in ``TOOL_SCOPES`` and a call without a grant is answered with a 401 before
    a record is ever made, which is a different property and is asserted in
    ``tests/test_oauth_challenge.py``. What is asserted here is the one this test
    is named for, on a tool the gate lets through.
    """

    @mcp_server.tool()
    async def shout(body: str) -> str:
        """Say something about the game."""
        return f"posted: {body}"

    async with (
        streamablehttp_client(mcp_url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        await session.call_tool("shout", {"body": "the referee was bought"})

    assert telemetry.recent(), "the call was not recorded at all"
    assert "bought" not in str(telemetry.recent())


def test_the_ring_forgets_the_oldest_calls_first() -> None:
    """Memory a client controls needs a ceiling; ``make latency`` (``S7-1``)
    reads the recent end of it, not the whole history."""
    for index in range(telemetry.RING_SIZE + 5):
        telemetry.record(tool=f"t{index}", duration_ms=1.0, status="ok", authenticated=False)

    recorded = telemetry.recent()
    assert len(recorded) == telemetry.RING_SIZE
    assert recorded[0].tool == "t5"
