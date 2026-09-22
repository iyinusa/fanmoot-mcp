"""Fixtures, results, one match, and everything in play.

``S3-3`` and ``S3-4`` — the four tools a fan reaches for without knowing they
are reaching for anything: "who are we playing", "when's the next one", "what's
the score", "is anything on".

The one piece of real engineering in here is ``live_scores``. Fanmoot's
``/v1/matches/live`` is deliberately thin — ids, scores and clocks, nothing
else — because it is the payload the whole site polls every few seconds, and
the club names, competition and venue on a live match do not change while it is
being played. That thinness is what makes it ETag-stable and cheap (HAD-6), and
it is also why this tool cannot speak from the digest alone: "two-one up" needs
to know who.

So the digest supplies what moves and the match records supply what does not,
fetched concurrently and behind their own thirty-second cache — and where the
two disagree, the digest wins, because it is the fresher of the two by twenty
seconds. A fan is told a score this server actually believes.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from app import voice
from app.fanmoot import endpoints
from app.fanmoot.models import LiveScore, MatchDetail, MatchOut
from app.mcp.tools import common
from app.mcp.tools.common import Limit, answer, icon, read_only
from app.mcp.tools.shapes import (
    EventCard,
    LiveAnswer,
    MatchAnswer,
    MatchCard,
    MatchDay,
    MatchListAnswer,
    UpcomingAnswer,
)

#: The window ``upcoming_matches`` looks over by default. A fortnight covers
#: "who are we playing next" and the international break that sometimes sits in
#: the middle of it.
DAYS_DEFAULT = 14
DAYS_MAX = 60

Status = Literal["scheduled", "live", "finished", "postponed"]


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="Fixtures and results",
        annotations=read_only("Fixtures and results"),
        icons=icon("fixtures"),
        description=(
            "Fixtures and results, filtered by club, by competition, or by status. Use this "
            "for 'who are Arsenal playing', 'how did they get on', 'what's on in the Premier "
            "League'. Club and competition names can be said the way a fan says them. For "
            "'what's on this week' prefer upcoming_matches, which groups by day; for a score "
            "in a match being played now, prefer live_scores."
        ),
    )
    async def list_matches(
        club: str | None = None,
        competition: str | None = None,
        status: Status | None = None,
        limit: Limit = common.LIMIT_DEFAULT,
    ) -> MatchListAnswer:
        club_slug = (await common.resolve_club(club)).slug if club else None
        league_slug = (await common.resolve_competition(competition)).slug if competition else None

        page = await endpoints.list_matches(
            club=club_slug, league=league_slug, status=status, limit=limit
        )
        moment = common.now()
        matches, _ = common.page(page.items, limit)
        remaining = max(0, page.total - len(matches))

        return answer(
            MatchListAnswer(
                spoken=voice.fixtures_line(matches, remaining=remaining, now=moment),
                matches=[MatchCard.of(match, now=moment) for match in matches],
                count=len(matches),
                total=page.total,
                has_more=remaining > 0,
            )
        )

    @server.tool(
        title="What's coming up",
        annotations=read_only("What's coming up"),
        icons=icon("fixtures"),
        description=(
            "What's on over the next few days, grouped by day. Use this for 'what's on this "
            "weekend', 'when do we play next', 'anything on tonight'. Optionally narrowed to "
            "one club or one competition."
        ),
    )
    async def upcoming_matches(
        club: str | None = None,
        competition: str | None = None,
        days: int = DAYS_DEFAULT,
        limit: Limit = common.LIMIT_DEFAULT,
    ) -> UpcomingAnswer:
        if not 1 <= days <= DAYS_MAX:
            days = max(1, min(days, DAYS_MAX))

        club_slug = (await common.resolve_club(club)).slug if club else None
        league_slug = (await common.resolve_competition(competition)).slug if competition else None

        groups = await endpoints.upcoming_matches(club=club_slug, league=league_slug, days=days)
        moment = common.now()

        # Bounded across the whole answer rather than per day: five fixtures is
        # five fixtures whether they are all on Saturday or spread over a week.
        flattened = [(group.day, match) for group in groups for match in group.items]
        kept, remaining = common.page(flattened, limit)

        days_out: list[MatchDay] = []
        for day, match in kept:
            if not days_out or days_out[-1].day != day:
                days_out.append(
                    MatchDay(
                        day=day,
                        day_spoken=voice.day_phrase(match.kickoff_at, now=moment),
                        matches=[],
                    )
                )
            days_out[-1].matches.append(MatchCard.of(match, now=moment))

        return answer(
            UpcomingAnswer(
                spoken=voice.fixtures_line(
                    [match for _, match in kept], remaining=remaining, now=moment
                ),
                days=days_out,
                count=len(kept),
                has_more=remaining > 0,
            )
        )

    @server.tool(
        title="One match",
        annotations=read_only("One match"),
        icons=icon("scoreboard"),
        description=(
            "One match in full: the score, the clock, where it is being played, the goals and "
            "cards, and how busy its conversation is. Name the match the way a fan does — "
            "'Arsenal vs Spurs', 'the Brighton game' — or pass a fanmoot.com match link. Use "
            "this when the fan means one specific game; use live_scores when they mean "
            "'what's the score anywhere'."
        ),
    )
    async def get_match(match: str) -> MatchAnswer:
        detail = await common.resolve_match(match)
        moment = common.now()

        clubs = {
            club.id: club.short_name or club.name
            for club in (detail.home, detail.away)
            if club is not None
        }
        spoken = voice.match_line(detail, now=moment)
        if detail.moot_count:
            takes = voice.sentence(f"{voice.count_words(detail.moot_count, 'take')} on it.")
            spoken = f"{spoken} {takes}"

        return answer(
            MatchAnswer(
                spoken=spoken,
                match=MatchCard.of(detail, now=moment),
                events=[EventCard.of(event, clubs=clubs) for event in detail.events],
            )
        )

    @server.tool(
        title="Live scores",
        annotations=read_only("Live scores"),
        icons=icon("scoreboard"),
        description=(
            "Every match being played right now, with the score and the minute. Use this for "
            "'what's the score', 'is anything on', 'are we winning' when the fan has not named "
            "a specific match. A match whose score has not been confirmed by anybody is "
            "reported as under way without a scoreline — do not state a score for it."
        ),
    )
    async def live_scores(
        competition: str | None = None,
        limit: Limit = common.LIMIT_DEFAULT,
    ) -> LiveAnswer:
        digest = await endpoints.live_digest()
        league_slug = (await common.resolve_competition(competition)).slug if competition else None

        # The cap, not the fan's limit: filtering by competition happens after
        # the records arrive, so a Premier League question must not be answered
        # out of the first five matches in the world.
        in_play = digest.matches[: common.LIMIT_MAX]
        details = await asyncio.gather(
            *(endpoints.get_match(live.id) for live in in_play), return_exceptions=True
        )

        merged = [
            _with_live_score(detail, live)
            for live, detail in zip(in_play, details, strict=True)
            if isinstance(detail, MatchDetail)
        ]
        if league_slug:
            merged = [m for m in merged if m.league and m.league.slug == league_slug]

        matches, remaining = common.page(merged, limit)
        moment = common.now()

        return answer(
            LiveAnswer(
                spoken=voice.live_line(matches, remaining=remaining, now=moment),
                matches=[MatchCard.of(match, now=moment) for match in matches],
                count=len(matches),
                has_more=remaining > 0,
                at=digest.at,
            )
        )


def _with_live_score(detail: MatchDetail, live: LiveScore) -> MatchOut:
    """The match record, with the digest's numbers laid over it.

    The record carries what does not change during a match — clubs,
    competition, venue — from a thirty-second cache. The digest carries what
    does, from a ten-second one. Where they disagree the digest is simply
    newer, and a scoreline is the last thing this server should serve stale.
    """
    return detail.model_copy(
        update={
            "status": live.status,
            "home_score": live.home_score,
            "away_score": live.away_score,
            "clock": live.clock,
            "score_state": live.score_state,
        }
    )
