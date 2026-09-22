"""One record per tool call — what ran, how long it took, and whether it worked.

``S2-5``, and the reason it is worth a module of its own: the trace *is* the
demo (HAD-12). The judges cannot see inside a voice interaction, so every
number the demo's trace panel shows and every figure ``make latency`` prints
(``S7-1``, where **any tool over 400 ms p95 is a bug**) comes from here.

Records land in two places, for two different readers:

* a **log line**, which is what an operator greps in production; and
* an in-process **ring buffer**, which is what ``make latency`` and the demo
  read without a round trip to Postgres in the middle of a voice turn.

The buffer is deliberately not the durable copy. ``S4-1`` creates the
``tool_calls`` table and ``record()`` grows a write-through to it; until then
this server has no database migration at all, and a telemetry module that
insisted on one would make the whole test suite need Postgres for the sake of
a latency histogram.

**No bodies, ever** (``S7-4``). A record holds the tool's *name*, a duration
and a status. It never holds arguments or results: ``post_moot``'s argument is
a fan's opinion and ``whoami``'s result is their identity, and neither belongs
in a log that an operator reads over someone's shoulder.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.logging_config import get_logger

log = get_logger("mcp.tools")

Status = Literal["ok", "error"]

#: How many calls are kept. A voice session is a handful of calls and a demo
#: run is a few dozen, so this holds every call of an entire judging session
#: while costing a few hundred kilobytes. The oldest fall off the end.
RING_SIZE = 2048

_ring: deque[ToolCall] = deque(maxlen=RING_SIZE)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """One ``tools/call``, as it will be stored in ``tool_calls`` from ``S4``.

    The field names match the columns that migration will create, so the write
    -through is an insert of this object rather than a translation of it.
    """

    tool: str
    duration_ms: float
    status: Status
    authenticated: bool
    request_id: str | None
    at: datetime


def record(
    *,
    tool: str,
    duration_ms: float,
    status: Status,
    authenticated: bool,
    request_id: str | None = None,
) -> ToolCall:
    """Log and remember one call. Returns the record, for a caller that wants it."""
    call = ToolCall(
        tool=tool,
        duration_ms=round(duration_ms, 2),
        status=status,
        authenticated=authenticated,
        request_id=request_id,
        at=datetime.now(UTC),
    )
    _ring.append(call)

    # `request_id` is already bound into structlog's context by the HTTP
    # middleware, so it is on the line whether or not it is passed here. It is
    # kept on the record because the ring buffer outlives the request context
    # that would otherwise have carried it.
    log.info(
        "tool_call",
        tool=call.tool,
        duration_ms=call.duration_ms,
        status=call.status,
        authenticated=call.authenticated,
    )
    return call


def recent(limit: int | None = None) -> list[ToolCall]:
    """The most recent calls, oldest first. All of them when ``limit`` is None."""
    calls = list(_ring)
    return calls if limit is None else calls[-limit:]


def reset() -> None:
    """Test hook: forget every recorded call, so one test cannot see another's."""
    _ring.clear()


def authenticated_now() -> bool:
    """Whether the call in flight carries a fan's grant.

    Reads the SDK's auth context, which is populated by the bearer-token
    middleware ``S4`` installs on the ``/mcp`` mount. Until then there is no
    middleware and this is always False — which is correct rather than
    provisional: before ``S4`` no call *is* authenticated, because the read
    tools take no credential at all.
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    return get_access_token() is not None
