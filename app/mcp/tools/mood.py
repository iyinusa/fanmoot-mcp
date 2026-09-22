"""``match_mood`` — how a match thread feels, computed rather than fetched.

``S3-7``. The one read tool on this server that is not a shaped proxy for a
Fanmoot endpoint: it samples the public thread under a match and labels it with
this server's own Bedrock call (``app.mood``). Fanmoot's own sentiment agent is
organisation-scoped and club-wide, and "what's the mood on the derby" is
neither.

The three rules the labelling runs under live in ``app.mood`` and are enforced
there: counts out with labels never stored, public moots only and never a
handle, and the moots framed as data rather than as instructions.

One consequence of the first rule shows up here, and it is deliberate: the
representative takes this tool returns are **the ones that earned the most
reaction**, not one specimen per label. Pairing a take with the verdict a model
gave it is precisely the storage that rule forbids, and "the takes the thread
itself pushed to the top" is both defensible and more useful — it is what a fan
scrolling the thread would have seen first.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app import mood as mood_module
from app import voice
from app.fanmoot import endpoints
from app.mcp.tools import common
from app.mcp.tools.common import Limit, answer, icon, read_only
from app.mcp.tools.shapes import MatchCard, MoodAnswer, TakeCard
from app.settings import get_settings

#: Three, because that is how many opinions fit in an answer before it stops
#: being a summary of a thread and starts being the thread.
TAKES_DEFAULT = 3


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="The mood on a match",
        annotations=read_only("The mood on a match"),
        icons=icon("mood"),
        description=(
            "How the conversation under a match feels: how many takes are happy, how many are "
            "furious, how many are neither, plus the takes the thread pushed to the top. Use "
            "this for 'what's the mood', 'are fans happy', 'how's everyone taking it'. For the "
            "takes themselves rather than the summary, use read_match_conversation."
        ),
    )
    async def match_mood(match: str, limit: Limit = TAKES_DEFAULT) -> MoodAnswer:
        detail = await common.resolve_match(match)
        subject = f"{voice.club_name(detail, 'home')} against {voice.club_name(detail, 'away')}"

        page = await endpoints.match_moots(detail.id, limit=get_settings().mood_sample_size)
        reading = await mood_module.read_mood(page.items, subject=subject)

        loudest = sorted(
            mood_module.sample(page.items, limit=get_settings().mood_sample_size),
            key=lambda moot: (moot.reaction_count, moot.reply_count),
            reverse=True,
        )[:limit]

        return answer(
            MoodAnswer(
                spoken=voice.mood_line(
                    positive=reading.positive,
                    negative=reading.negative,
                    neutral=reading.neutral,
                    subject=subject,
                    summary=reading.summary,
                ),
                subject=subject,
                match=MatchCard.of(detail, now=common.now()),
                positive=reading.positive,
                negative=reading.negative,
                neutral=reading.neutral,
                sampled=reading.sampled,
                method=reading.method,
                summary=reading.summary,
                takes=[TakeCard.of(moot) for moot in loudest],
            )
        )
