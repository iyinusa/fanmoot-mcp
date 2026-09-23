"""Keeping the reads a voice turn starts with already in cache (`S7-2`).

HAD-6's cache only helps from the second call. The first ``live_scores`` after a
deploy, or after ten quiet seconds, pays a full round trip to fanmoot.com, and
that is the call a judge makes first. So the process warms what a session
starts with and then keeps it warm:

* **the live digest**, and the match record behind each game in it, which is
  what ``live_scores`` joins;
* **the next fortnight of fixtures**, under exactly the key
  ``upcoming_matches`` asks for when a fan names no club;
* **the catalogue** of clubs and competitions, which every name resolution
  reads, and which takes a dozen pages to build from cold;
* **the mood of every live match**, because ``match_mood`` is a Bedrock call
  of most of a second on a thread it has not read, and "what's the mood" gets
  asked during a match. ``app.mood`` caches a reading for five minutes per
  thread, so this costs one model call per live match per five minutes at most.

After the first pass it repeats every ``FMCP_WARM_INTERVAL_S``. That is cheap,
because the cache does the deciding: a fresh entry is not fetched at all, and a
stale one is revalidated with its ETag, which for an unchanged live digest is a
``304`` with no body. The interval is shorter than the ten-second live TTL, so
the digest is refreshed by this loop rather than by a fan waiting on it.

Every failure is logged and ignored. A warm-up that fails leaves the cache cold,
which is where it started, and must never keep the process from serving.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from app import mood
from app.fanmoot import endpoints, names
from app.logging_config import get_logger
from app.mcp.tools.common import LIMIT_MAX
from app.mcp.tools.matches import DAYS_DEFAULT
from app.settings import get_settings

log = get_logger("warm")


@dataclass
class Warmed:
    """What one pass reached. Logged on the first pass; returned for the tests."""

    live: int = 0
    fixtures: int = 0
    clubs: int = 0
    failed: int = 0


async def warm_once() -> Warmed:
    """One pass over everything a session starts with."""
    result = Warmed()

    async def live() -> None:
        digest = await endpoints.live_digest()
        # The same cap `live_scores` reads, so every record it will join is here.
        in_play = digest.matches[:LIMIT_MAX]
        outcomes = await asyncio.gather(
            *(_live_match(match.id) for match in in_play), return_exceptions=True
        )
        result.live = sum(1 for outcome in outcomes if not isinstance(outcome, BaseException))

    async def fixtures() -> None:
        groups = await endpoints.upcoming_matches(days=DAYS_DEFAULT)
        result.fixtures = sum(len(group.items) for group in groups)

    async def catalogue() -> None:
        result.clubs = len((await names.get_catalogue()).clubs)

    for outcome in await asyncio.gather(live(), fixtures(), catalogue(), return_exceptions=True):
        if isinstance(outcome, BaseException):
            result.failed += 1
            log.warning("cache_warm_failed", error_type=type(outcome).__name__, error=str(outcome))
    return result


async def _live_match(match_id: uuid.UUID) -> None:
    """One live match: its record, and the mood of its thread, as the tools ask."""
    await endpoints.get_match(match_id)
    page = await endpoints.match_moots(match_id, limit=get_settings().mood_sample_size)
    await mood.read_mood(page.items, subject="a live match")


async def keep_warm() -> None:
    """Warm now, then keep warm until cancelled. Started by the lifespan."""
    first = await warm_once()
    log.info(
        "cache_warmed",
        live=first.live,
        fixtures=first.fixtures,
        clubs=first.clubs,
        failed=first.failed,
    )
    interval = get_settings().warm_interval_s
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        await warm_once()
