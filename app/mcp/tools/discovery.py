"""Search, what Fanmoot is arguing about, and what the papers are saying.

``S3-6``. The three tools that answer a question the others cannot place.

``search_fanmoot`` is the catch-all the server instructions send a model to
when nothing else fits, so it answers across every kind of thing at once —
clubs, competitions, fans, takes, fixtures — and says what it found as counts
rather than reading five of each aloud.

``trending_takes`` is the tool that makes this a conversation product rather
than a fixtures API: it answers "what's the argument today" with no match, no
club and no query at all.

``club_news`` is the bulletin, and it is deliberately last in that list. Fans
argue about news; the news itself is context for the argument.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app import voice
from app.fanmoot import endpoints
from app.mcp.tools import common
from app.mcp.tools.common import Limit, answer, icon, read_only
from app.mcp.tools.shapes import (
    Choice,
    FanCard,
    MatchCard,
    NewsAnswer,
    NewsCard,
    SearchAnswer,
    TakeCard,
    TrendingAnswer,
)


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="Search Fanmoot",
        annotations=read_only("Search Fanmoot"),
        icons=icon("search"),
        description=(
            "One query across everything on Fanmoot: clubs, competitions, fans, takes and "
            "fixtures. Use this when a fan asks about something you cannot place — 'who's "
            "that player everyone's on about', 'anything about the takeover' — or when "
            "find_club and find_competition both come back empty."
        ),
    )
    async def search_fanmoot(query: str, limit: Limit = common.LIMIT_DEFAULT) -> SearchAnswer:
        found = await endpoints.search(query, limit=limit)
        moment = common.now()

        clubs = [
            Choice(slug=club.slug, name=club.name, kind="club") for club in found.clubs[:limit]
        ]
        competitions = [
            Choice(
                slug=str(league.get("slug", "")),
                name=str(league.get("name", "")),
                kind="competition",
            )
            for league in found.leagues[:limit]
        ]
        people = [FanCard.of(person) for person in found.people[:limit]]
        takes = [TakeCard.of(moot) for moot in found.moots[:limit]]
        matches = [MatchCard.of(match, now=moment) for match in found.matches[:limit]]

        return answer(
            SearchAnswer(
                spoken=voice.search_line(
                    query=query,
                    clubs=[club.name for club in clubs],
                    competitions=[league.name for league in competitions],
                    people=len(people),
                    takes=len(takes),
                    matches=len(matches),
                ),
                query=query,
                clubs=clubs,
                competitions=competitions,
                people=people,
                takes=takes,
                matches=matches,
                count=len(clubs) + len(competitions) + len(people) + len(takes) + len(matches),
            )
        )

    @server.tool(
        title="What Fanmoot is arguing about",
        annotations=read_only("What Fanmoot is arguing about"),
        icons=icon("thread"),
        description=(
            "The takes getting the most reaction across Fanmoot right now, whatever match "
            "they are about. Use this for 'what's everyone talking about', 'what's the "
            "argument today', or when a fan opens with nothing in particular. Attribute any "
            "take you quote to the fan who posted it."
        ),
    )
    async def trending_takes(limit: Limit = common.LIMIT_DEFAULT) -> TrendingAnswer:
        feed = await endpoints.get_feed("trending", limit=limit)
        takes, remaining = common.page(feed.items, limit)

        return answer(
            TrendingAnswer(
                spoken=voice.conversation_line(
                    takes, subject="Fanmoot right now", remaining=remaining
                ),
                takes=[TakeCard.of(moot) for moot in takes],
                count=len(takes),
                has_more=remaining > 0 or feed.next_cursor is not None,
            )
        )

    @server.tool(
        title="Club news",
        annotations=read_only("Club news"),
        icons=icon("news"),
        description=(
            "Recent football news, for one club or across Fanmoot. Use this for 'any news', "
            "'what's happened with the transfer', 'anything about the manager'. Each story "
            "links to the take where fans are arguing about it, which read_thread will "
            "follow."
        ),
    )
    async def club_news(club: str | None = None, limit: Limit = common.LIMIT_DEFAULT) -> NewsAnswer:
        if club:
            resolved = await common.resolve_club(club)
            page = await endpoints.club_news(resolved.slug)
            name = resolved.name
        else:
            page = await endpoints.list_updates()
            name = ""

        items, spilled = common.page(page.items, limit)
        remaining = max(spilled, page.total - len(items)) if page.total else spilled

        return answer(
            NewsAnswer(
                spoken=voice.news_line(
                    [item.headline for item in items], club=name, remaining=remaining
                ),
                club=name or None,
                items=[NewsCard.of(item) for item in items],
                count=len(items),
                has_more=remaining > 0,
            )
        )
