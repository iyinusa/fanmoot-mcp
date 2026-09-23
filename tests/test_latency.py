"""``S7-1``: the latency budget, as arithmetic and as a measurement.

The measurement runs the workload ``make latency --drive`` runs, through the
real server over a real socket against the recorded API. It cannot say what
production will see, because there is no network in it. It does say this
server's own share of the time is a small fraction of the 400 ms, which is
what a regression in the server would eat into.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.mcp import telemetry
from app.mcp.telemetry import BUDGET_MS, latency_table, percentile, render_table
from tests.replay import Replay, fixture_body, load_manifest
from tests.tooling import session_at

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from latency import WORKLOAD  # noqa: E402


def test_a_percentile_is_a_call_that_really_happened() -> None:
    durations = [float(n) for n in range(1, 21)]

    assert percentile(durations, 0.5) == 10.0
    assert percentile(durations, 0.95) == 19.0
    assert percentile([], 0.95) == 0.0


def test_the_slowest_tool_comes_first_and_is_flagged() -> None:
    rows = latency_table(
        [("find_club", 5.0, "ok")] * 20
        + [("match_mood", 900.0, "ok")] * 19
        + [("match_mood", 950.0, "error")]
    )

    assert [row.tool for row in rows] == ["match_mood", "find_club"]
    assert rows[0].over_budget and not rows[1].over_budget
    assert rows[0].errors == 1
    assert "FAILED by match_mood" in render_table(rows, title="t")


async def test_the_workload_fits_the_budget_with_room_to_spare(
    replay: Replay, mcp_url: str
) -> None:
    # `match_mood` samples forty takes; the recording holds a page of five.
    # The same five, under the key the tool asks for.
    moots = next(r for r in load_manifest() if r.path.endswith("/moots") and not r.synthetic)
    replay.stub(moots.path, fixture_body(moots.file), params={"limit": 40})

    async with session_at(mcp_url) as session:
        for _ in range(5):
            for tool, arguments in WORKLOAD:
                result = await session.call_tool(tool, arguments)
                assert result.isError is False, (tool, result.content)

    rows = latency_table((c.tool, c.duration_ms, c.status) for c in telemetry.recent())
    assert {row.tool for row in rows} == {tool for tool, _ in WORKLOAD}
    # A quarter of the budget: the rest belongs to the network and to Fanmoot.
    assert all(row.p95_ms < BUDGET_MS / 4 for row in rows), render_table(rows, title="replay")
