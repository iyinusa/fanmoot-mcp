"""One record per tool call — what ran, how long it took, and whether it worked.

``S2-5``, and the reason it is worth a module of its own: the trace *is* the
demo (HAD-12). The judges cannot see inside a voice interaction, so every
number the demo's trace panel shows and every figure ``make latency`` prints
(``S7-1``, where **any tool over 400 ms p95 is a bug**) comes from here.

Records land in two places, for two different readers:

* a **log line**, which is what an operator greps in production; and
* an in-process **ring buffer**, which is what ``make latency`` and the demo
  read without a round trip to Postgres in the middle of a voice turn.

The buffer is deliberately not the durable copy. ``S4-1`` created the
``tool_calls`` table and ``set_sink`` is the write-through to it — installed by
the application lifespan, and by nothing else. That is what keeps this module
free of a database dependency: a server writes both copies, and a test suite
that installs no sink writes one, so the twelve read tools are still testable
without Postgres.

**No bodies, ever** (``S7-4``). A record holds the tool's *name*, a duration
and a status. It never holds arguments or results: ``post_moot``'s argument is
a fan's opinion and ``whoami``'s result is their identity, and neither belongs
in a log that an operator reads over someone's shoulder.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
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

#: Where a recorded call goes *as well as* the ring. ``app.audit`` installs the
#: durable one from the lifespan; there is none in a test, and none in a process
#: that has not started up.
Sink = Callable[["ToolCall"], None]
_sink: Sink | None = None


def set_sink(sink: Sink | None) -> None:
    """Install (or drop) the write-through. The lifespan's hook, and a test's."""
    global _sink
    _sink = sink


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
    #: Which grant it ran under, when it ran under one. Read from the token the
    #: `/mcp` mount verified (`S4-8`), so an authenticated call can be tied to a
    #: link without the tool having to pass anything.
    grant_id: uuid.UUID | None = None


def record(
    *,
    tool: str,
    duration_ms: float,
    status: Status,
    authenticated: bool,
    request_id: str | None = None,
    grant_id: uuid.UUID | None = None,
) -> ToolCall:
    """Log and remember one call. Returns the record, for a caller that wants it."""
    call = ToolCall(
        tool=tool,
        duration_ms=round(duration_ms, 2),
        status=status,
        authenticated=authenticated,
        request_id=request_id,
        at=datetime.now(UTC),
        grant_id=grant_id,
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

    if _sink is not None:
        try:
            _sink(call)
        except Exception as exc:
            # A telemetry failure never fails a tool call. The ring already has
            # the record and the log line above is already written.
            log.warning("tool_call_sink_failed", error_type=type(exc).__name__, error=str(exc))

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

    Reads the SDK's auth context, populated by the bearer-token middleware
    ``S4`` installs on the ``/mcp`` mount (``app.oauth.bearer``). False for every
    read tool, which take no credential at all.
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    return get_access_token() is not None


def grant_now() -> uuid.UUID | None:
    """The grant the call in flight is running under, if any.

    Carried in the verified token's ``claims`` rather than passed down through
    the tool signature, because the twelve read tools have no business holding a
    grant id and the alternative is threading one through all of them.
    """
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    raw = (token.claims or {}).get("grant_id") if token is not None else None
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:  # pragma: no cover - the verifier writes it
        return None


# ── The latency budget (S7-1) ────────────────────────────────────────────────
#: Alexa+ asks for an answer inside 500 ms. This is the server's share of it,
#: leaving a hundred for the network and the client, and **a tool whose p95 is
#: over it is a bug** (HAD-6), not a figure to report.
BUDGET_MS = 400.0


@dataclass(frozen=True, slots=True)
class Latency:
    """One tool's row in ``make latency``."""

    tool: str
    calls: int
    errors: int
    p50_ms: float
    p95_ms: float
    max_ms: float

    @property
    def over_budget(self) -> bool:
        return self.p95_ms > BUDGET_MS


def percentile(durations: list[float], fraction: float) -> float:
    """Nearest-rank percentile, so the figure is a call that really happened.

    Interpolating between two calls would report a latency nobody saw. With
    twenty samples the p95 is the nineteenth-fastest, which is the honest
    answer to "how slow is it for one fan in twenty".
    """
    if not durations:
        return 0.0
    ordered = sorted(durations)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def latency_table(calls: Iterable[tuple[str, float, str]]) -> list[Latency]:
    """``(tool, duration_ms, status)`` rows in, one ``Latency`` per tool out.

    Slowest p95 first, because the first line is the one someone reads.
    """
    durations: dict[str, list[float]] = defaultdict(list)
    errors: dict[str, int] = defaultdict(int)
    for tool, duration_ms, status in calls:
        durations[tool].append(duration_ms)
        if status != "ok":
            errors[tool] += 1
    rows = [
        Latency(
            tool=tool,
            calls=len(values),
            errors=errors[tool],
            p50_ms=round(percentile(values, 0.50), 1),
            p95_ms=round(percentile(values, 0.95), 1),
            max_ms=round(max(values), 1),
        )
        for tool, values in durations.items()
    ]
    return sorted(rows, key=lambda row: row.p95_ms, reverse=True)


def render_table(rows: list[Latency], *, title: str) -> str:
    """The table ``make latency`` prints."""
    lines = [
        title,
        f"{'tool':<26}{'calls':>7}{'errors':>8}{'p50 ms':>9}{'p95 ms':>9}{'max ms':>9}",
    ]
    for row in rows:
        flag = "  ← over budget" if row.over_budget else ""
        lines.append(
            f"{row.tool:<26}{row.calls:>7}{row.errors:>8}{row.p50_ms:>9.1f}"
            f"{row.p95_ms:>9.1f}{row.max_ms:>9.1f}{flag}"
        )
    over = [row.tool for row in rows if row.over_budget]
    lines.append(
        f"\nbudget: p95 ≤ {BUDGET_MS:.0f} ms — "
        + (f"FAILED by {', '.join(over)}" if over else "every tool inside it")
    )
    return "\n".join(lines)
