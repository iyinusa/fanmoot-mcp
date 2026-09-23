"""Per-tool p50/p95, against the 400 ms budget (`S7-1`, HAD-6).

    make latency                                  # tool_calls, last 24 hours
    make latency ARGS="--since 2"                 # …last two hours
    make latency ARGS="--drive https://mcp.fanmoot.com/mcp"

Two sources, answering two different questions:

* **``tool_calls``** (the default) is what the server measured: the time from
  a ``tools/call`` arriving to its answer leaving, for every call real clients
  made. This is the number the budget is about.
* **``--drive URL``** runs a fixed read workload against a server over MCP, the
  way a client does, and times each call from the client's side. That adds the
  network and the handshake, and it works with no database access, which is
  how you check a deployment before anybody has used it. Its first round is
  cold on purpose (`S7-2` should make it warm anyway) and is reported
  separately.

Exits 1 when any tool's p95 is over budget, so it can gate a deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mcp.telemetry import latency_table, render_table  # noqa: E402

#: What a fan asks in the first minute, as tool calls. Read tools only: the
#: workload must be safe to point at production.
WORKLOAD: list[tuple[str, dict[str, Any]]] = [
    ("live_scores", {}),
    ("upcoming_matches", {}),
    ("find_club", {"name": "the gunners"}),
    ("find_competition", {"name": "the prem"}),
    ("list_matches", {"club": "arsenal"}),
    ("get_match", {"match": "arsenal"}),
    ("read_match_conversation", {"match": "arsenal"}),
    ("match_mood", {"match": "arsenal"}),
    ("search_fanmoot", {"query": "arsenal"}),
    ("trending_takes", {}),
    ("club_news", {"club": "arsenal"}),
]


async def from_database(since_hours: float) -> int:
    from sqlalchemy import select

    from app.db import dispose_engine, get_sessionmaker
    from app.models.audit import ToolCall

    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    try:
        async with get_sessionmaker()() as session:
            rows = (
                await session.execute(
                    select(ToolCall.tool, ToolCall.duration_ms, ToolCall.status).where(
                        ToolCall.created_at >= cutoff
                    )
                )
            ).all()
    finally:
        await dispose_engine()

    if not rows:
        print(f"No tool calls recorded in the last {since_hours:g} hours.")
        print("Drive some first: make latency ARGS='--drive <url>'")
        return 2
    table = latency_table((tool, duration, status) for tool, duration, status in rows)
    print(render_table(table, title=f"tool_calls, last {since_hours:g} h (server-side)"))
    return 1 if any(row.over_budget for row in table) else 0


async def drive(url: str, rounds: int) -> int:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    cold: list[tuple[str, float, str]] = []
    warm: list[tuple[str, float, str]] = []

    async with (
        streamablehttp_client(url) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        take_id: str | None = None
        for index in range(rounds):
            calls = list(WORKLOAD)
            if take_id:
                calls.append(("read_thread", {"take": take_id}))
            for tool, arguments in calls:
                started = time.perf_counter()
                result = await session.call_tool(tool, arguments)
                elapsed = (time.perf_counter() - started) * 1000
                status = "error" if result.isError else "ok"
                (cold if index == 0 else warm).append((tool, elapsed, status))
                if tool == "trending_takes" and not take_id and result.structuredContent:
                    takes = result.structuredContent.get("takes") or []
                    take_id = takes[0]["id"] if takes else None

    print(render_table(latency_table(cold), title=f"{url} — first round, cold (client-side)"))
    print()
    if not warm:
        return 0
    table = latency_table(warm)
    print(render_table(table, title=f"{url} — {rounds - 1} warm rounds (client-side)"))
    return 1 if any(row.over_budget for row in table) else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--since", type=float, default=24.0, help="hours of tool_calls to read")
    parser.add_argument("--drive", metavar="URL", help="drive a read workload at this /mcp URL")
    parser.add_argument("--rounds", type=int, default=20, help="rounds of the workload (--drive)")
    args = parser.parse_args()
    if args.drive:
        return asyncio.run(drive(args.drive, max(2, args.rounds)))
    return asyncio.run(from_database(args.since))


if __name__ == "__main__":
    sys.exit(main())
