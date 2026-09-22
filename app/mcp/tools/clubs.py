"""``find_club`` and ``find_competition`` — the two tools that turn talk into ids.

``S3-2`` ⟵ ``S1-5``. Everything else on this surface takes a club or a
competition, and a fan never says either the way the database holds it: they
say "the gunners", "spurs", "los blancos", "the Prem", or a misheard
"totenham". These two tools are where that becomes a slug, and — the part that
matters — where it becomes a *question* when it cannot honestly become a slug.

They are the only tools that return candidates rather than raising them. Every
other tool raises, because it cannot do its job without an answer; these two
exist to be asked, so the candidates come back as ``structuredContent`` a
client can render as three buttons.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app import voice
from app.fanmoot import endpoints
from app.mcp.tools import common
from app.mcp.tools.common import answer, icon, read_only
from app.mcp.tools.shapes import ClubAnswer, ClubCard, CompetitionAnswer, CompetitionCard

#: How many of a competition's clubs travel in the payload. A competition has
#: twenty-odd clubs and the hard cap is twenty (``S3-9``); the spoken line says
#: the real total either way, so nothing is silently lost.
CLUBS_IN_A_COMPETITION = common.LIMIT_MAX


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="Find a club",
        annotations=read_only("Find a club"),
        icons=icon("club"),
        description=(
            "Turn what a fan called a club — a nickname, an abbreviation, a misspelling, "
            "'the gunners', 'spurs', 'los blancos' — into the club Fanmoot knows. Use this "
            "first whenever a fan names a club and you need to pass it to another tool. "
            "If more than one club could be meant, this returns the candidates instead of "
            "choosing: ask the fan which, then call again with their answer."
        ),
    )
    async def find_club(name: str) -> ClubAnswer:
        club, choices, spoken_choices = await common.lookup_club(name)

        if club is None:
            spoken = (
                voice.ask_which(spoken_choices, kind="club")
                if choices
                else f"I cannot find a club called {name} on Fanmoot."
            )
            return answer(ClubAnswer(spoken=spoken, query=name, resolved=False, choices=choices))

        return answer(
            ClubAnswer(
                spoken=voice.club_line(
                    club.name,
                    competition=club.league.name if club.league else "",
                    nickname=club.nickname or "",
                    members=club.member_count,
                ),
                query=name,
                resolved=True,
                club=ClubCard.of(club),
            )
        )

    @server.tool(
        title="Find a competition",
        annotations=read_only("Find a competition"),
        icons=icon("competition"),
        description=(
            "Turn what a fan called a competition — 'the Prem', 'La Liga', 'the Champions "
            "League' — into the competition Fanmoot knows, along with the clubs in it. Use "
            "this when a fan asks about a league rather than a club, or when you need a "
            "competition to filter fixtures by."
        ),
    )
    async def find_competition(name: str) -> CompetitionAnswer:
        league, choices, spoken_choices = await common.lookup_competition(name)

        if league is None:
            spoken = (
                voice.ask_which(spoken_choices, kind="competition")
                if choices
                else f"I cannot find a competition called {name} on Fanmoot."
            )
            return answer(
                CompetitionAnswer(spoken=spoken, query=name, resolved=False, choices=choices)
            )

        detail = await endpoints.get_league(league.slug)
        clubs = detail.clubs[:CLUBS_IN_A_COMPETITION]
        spoken = f"{detail.name}"
        if detail.clubs:
            spoken = f"{spoken}, {voice.count_words(len(detail.clubs), 'club')} on Fanmoot"
        return answer(
            CompetitionAnswer(
                spoken=f"{spoken}.",
                query=name,
                resolved=True,
                competition=CompetitionCard.of(detail, club_count=len(detail.clubs)),
                clubs=[ClubCard.of(club) for club in clubs],
            )
        )
