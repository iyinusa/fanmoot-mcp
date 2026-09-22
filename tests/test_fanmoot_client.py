"""The client's policy: what it retries, what it refuses to retry, and how an
upstream failure becomes a sentence a model can say."""

from __future__ import annotations

import httpx
import pytest

from app.fanmoot import endpoints
from app.fanmoot.client import (
    FanmootClient,
    FanmootNotFound,
    FanmootRateLimited,
    FanmootRejected,
    FanmootUnauthorized,
    FanmootUnavailable,
)
from tests.replay import Replay


async def test_a_connect_error_is_retried_once_and_then_succeeds(replay: Replay) -> None:
    """A proxy restart or a rolling deploy usually clears on the next attempt,
    which is the whole reason the retry exists."""
    replay.fail("/v1/leagues", httpx.ConnectError("refused"))

    leagues = await endpoints.list_leagues()

    assert leagues
    assert replay.call_count == 2


async def test_a_5xx_is_retried_once(replay: Replay) -> None:
    replay.fail("/v1/leagues", 503)

    leagues = await endpoints.list_leagues()

    assert leagues
    assert replay.call_count == 2


async def test_a_second_failure_gives_up_rather_than_spending_the_turn(replay: Replay) -> None:
    """Two attempts, not a loop. A voice turn has a few hundred milliseconds,
    and a third try would spend them all before saying anything."""
    replay.fail("/v1/leagues", 503, 503)

    with pytest.raises(FanmootUnavailable):
        await endpoints.list_leagues()

    assert replay.call_count == 2


async def test_a_404_is_never_retried(replay: Replay) -> None:
    """A 4xx is an answer. Retrying it produces the same answer and halves the
    budget left to say so."""
    replay.fail("/v1/clubs/nope", 404)

    with pytest.raises(FanmootNotFound):
        await endpoints.get_club("nope")

    assert replay.call_count == 1


async def test_a_write_is_never_retried(replay: Replay) -> None:
    """`post_moot` guards its own duplicates with an idempotency key. A blind
    retry underneath that could post twice on a 502 Fanmoot had accepted."""
    replay.fail("/v1/moots", 503)

    with pytest.raises(FanmootUnavailable):
        await endpoints.compose_moot(token="t", body="Two-nil flatters us.")

    assert replay.call_count == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, FanmootUnauthorized),
        (403, FanmootUnauthorized),
        (404, FanmootNotFound),
        (422, FanmootRejected),
        (429, FanmootRateLimited),
    ],
)
async def test_status_codes_map_to_speakable_errors(
    replay: Replay, status: int, expected: type[Exception]
) -> None:
    """Per the 2025-11-25 clarification, an input problem must come back as a
    tool execution error the model can correct itself from — which means every
    one of these carries a sentence, not a status code."""
    replay.fail("/v1/clubs/arsenal", status)

    with pytest.raises(expected) as raised:
        await endpoints.get_club("arsenal")

    message = str(raised.value)
    assert message
    assert "http" not in message.lower()
    assert str(status) not in message


async def test_rate_limits_carry_retry_after() -> None:
    """429 + Retry-After is passed through as something a fan can act on."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, json={"detail": "Slow down."})

    client = FanmootClient(base_url="https://fanmoot.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FanmootRateLimited) as raised:
            await client.get("/v1/search", params={"q": "arsenal"})
    finally:
        await client.aclose()

    assert raised.value.retry_after == 12
    assert "12 seconds" in raised.value.message


async def test_fanmoots_own_message_survives() -> None:
    """Its API writes errors for fans. Those sentences are better than anything
    this repo could invent, so they are carried through verbatim."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "A moot cannot be empty."})

    client = FanmootClient(base_url="https://fanmoot.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FanmootRejected, match="A moot cannot be empty."):
            await client.post("/v1/moots", json={"body": ""}, token="t")
    finally:
        await client.aclose()


async def test_a_non_json_200_is_reported_as_unavailable() -> None:
    """A proxy error page or a maintenance splash answered instead of Fanmoot.
    That is an outage, not a parse bug in this repo."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    client = FanmootClient(base_url="https://fanmoot.test", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(FanmootUnavailable):
            await client.get("/v1/leagues")
    finally:
        await client.aclose()


async def test_unset_filters_are_not_sent_as_the_string_none(replay: Replay) -> None:
    """Every wrapper declares its full query signature with optional arguments.
    Without the cleaning step Fanmoot would receive `league=None` and reject it."""
    await endpoints.list_matches(club="arsenal", limit=5)

    sent = dict(replay.calls[0].url.params)
    assert "league" not in sent
    assert "season" not in sent
    assert sent["club"] == "arsenal"
