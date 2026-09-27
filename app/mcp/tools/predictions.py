"""Calling a scoreline, and hearing back what you called.

``S5-5``. Two tools and one scope between them, because they are two halves of
the same habit: a fan calls a result on Friday and asks on Sunday how they did.

``predict_match`` is the one write on this surface that is genuinely idempotent
— calling two-one twice leaves Fanmoot in the state it was already in, and
calling three-one replaces the earlier call rather than adding to it. So the
annotation says so, and a client may retry it without asking.

``my_predictions`` reads, and reads *two* things in one call: what the fan has
already called, and the fixtures still open to them. A fan asking "what have I
got on this weekend" means both, and two tools would have made them ask twice.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from app import voice
from app.fanmoot import endpoints
from app.mcp.tools import common
from app.mcp.tools.acting import refusals, signed_in, writes
from app.mcp.tools.common import Limit, answer, icon, read_only
from app.mcp.tools.shapes import (
    PredictionAnswer,
    PredictionCard,
    PredictionsAnswer,
    absolute,
)

#: A scoreline a fan would actually call. Nine, not ninety: the bound is in the
#: schema so a transcription error — "one" heard as "a hundred and one" — comes
#: back as a question instead of a prediction nobody made.
Goals = Annotated[
    int,
    Field(ge=0, le=9, description="Goals for this side. A called scoreline, so 0 to 9."),
]


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="Call a scoreline",
        # Genuinely idempotent: one prediction per fan per match upstream, and
        # a second call with the same scoreline changes nothing.
        annotations=writes("Call a scoreline", idempotent=True),
        icons=icon("prediction"),
        description=(
            "Predict the final score of a match, as the signed-in fan. Say the match the way "
            "a fan would — 'arsenal vs spurs' — or pass its id. Scores are home first, then "
            "away, in the order the match is listed. This replaces any earlier call the fan "
            "made on the same match, and predictions close at kickoff. It is public once the "
            "match starts, so read the scoreline back and confirm it before you call this."
        ),
    )
    async def predict_match(match: str, home_score: Goals, away_score: Goals) -> PredictionAnswer:
        fan = await signed_in()
        fixture = await common.resolve_match(match)

        async with refusals(action="take that prediction"):
            called = await endpoints.predict_match(
                token=fan.token,
                match_id=fixture.id,
                home_score=home_score,
                away_score=away_score,
            )

        moment = common.now()
        home, away = voice.club_name(fixture, "home"), voice.club_name(fixture, "away")
        card = PredictionCard(
            match_id=fixture.id,
            competition=fixture.league.name if fixture.league else None,
            home=home,
            away=away,
            home_crest_url=absolute(fixture.home.badge_path) if fixture.home else None,
            away_crest_url=absolute(fixture.away.badge_path) if fixture.away else None,
            kickoff_at=fixture.kickoff_at,
            kickoff_spoken=voice.kickoff_phrase(fixture.kickoff_at, now=moment),
            status=fixture.status,
            called=True,
            home_score=called.home_score,
            away_score=called.away_score,
            url=absolute(fixture.url),
            card_url=called.card_url or None,
        )
        return answer(
            PredictionAnswer(
                spoken=voice.prediction_line(
                    home=home,
                    away=away,
                    home_score=called.home_score,
                    away_score=called.away_score,
                    kickoff=card.kickoff_spoken,
                ),
                prediction=card,
            )
        )

    @server.tool(
        title="My predictions",
        annotations=read_only("My predictions"),
        icons=icon("prediction"),
        description=(
            "What the signed-in fan has called, and the fixtures still open to a call. Use "
            "this for 'what have I predicted', 'how are my predictions doing', 'what can I "
            "still call this weekend'. Settled predictions come back with the points they "
            "scored and the real score beside the called one."
        ),
    )
    async def my_predictions(limit: Limit = common.LIMIT_DEFAULT) -> PredictionsAnswer:
        fan = await signed_in()
        async with refusals(action="read your predictions"):
            mine, open_fixtures = await asyncio.gather(
                endpoints.my_predictions(token=fan.token),
                endpoints.open_predictions(token=fan.token),
            )

        moment = common.now()
        called_rows, called_spilled = common.page(mine, limit)
        # The fixtures the fan has *not* called yet: the ones they have are
        # already in the other half, and listing a match twice in one spoken
        # answer is how a five-item list stops being five items.
        uncalled = [row for row in open_fixtures if row.prediction is None]
        open_rows, open_spilled = common.page(uncalled, limit)

        return answer(
            PredictionsAnswer(
                spoken=voice.predictions_line(called=len(mine), open_matches=len(uncalled)),
                called=[
                    PredictionCard.of(row.match, row.prediction, now=moment) for row in called_rows
                ],
                open=[
                    PredictionCard.of(row.match, row.prediction, now=moment) for row in open_rows
                ],
                count=len(called_rows),
                open_count=len(open_rows),
                has_more=called_spilled > 0 or open_spilled > 0,
            )
        )
