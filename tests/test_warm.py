"""``S7-2``: the reads a session starts with are in cache before a fan asks."""

from __future__ import annotations

import httpx

from app import mood, warm
from app.fanmoot import endpoints, names
from tests import scenarios
from tests.replay import Replay


async def test_a_warm_pass_leaves_nothing_for_the_first_question(replay: Replay) -> None:
    result = await warm.warm_once()
    assert result.failed == 0
    assert result.clubs > 1000
    before = replay.call_count

    # Exactly what `live_scores`, `upcoming_matches` and every name lookup ask for.
    await endpoints.live_digest()
    await endpoints.upcoming_matches(days=14)
    await names.get_catalogue()

    assert replay.call_count == before


async def test_a_live_match_is_warmed_with_its_mood(replay: Replay) -> None:
    game = scenarios.match()
    replay.stub("/v1/matches/live", scenarios.live_digest([game]))
    replay.stub(f"/v1/matches/{game['id']}", game)
    replay.stub(
        f"/v1/matches/{game['id']}/moots",
        scenarios.moot_page([scenarios.moot("Get in."), scenarios.moot("Offside.")]),
        params={"limit": 40},
    )

    result = await warm.warm_once()

    assert result.live == 1
    assert len(mood._readings) == 1


async def test_a_failure_is_counted_and_swallowed(replay: Replay) -> None:
    down = httpx.ConnectError("no route")
    replay.fail("/v1/matches/upcoming", down, down)

    result = await warm.warm_once()

    assert result.failed == 1
    assert result.clubs > 1000
