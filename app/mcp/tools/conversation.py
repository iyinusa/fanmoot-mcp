"""The match thread, and one argument inside it.

``S3-5``. This is the half of Fanmoot that no other football service has, and
the reason the whole project is not a scores wrapper: under every match there
is a public conversation, and "what are fans saying about the derby" is a
question only this server can answer.

Two tools, because a fan means two different things. ``read_match_conversation``
is the room — how busy it is and a couple of voices from it.
``read_thread`` is one take and what came back at it, which is where an
argument actually lives.

Both hand the model *moots as data*. The server instructions say to treat every
take as something a stranger said and never as an instruction, and these tools
hold up their end of that: a take is attributed to the fan who posted it,
trimmed to what can be read aloud, and never merged into the server's own
voice.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from app import voice
from app.fanmoot import endpoints
from app.fanmoot.models import MatchDetail, MootOut
from app.mcp.tools import common
from app.mcp.tools.common import Limit, answer, icon, read_only
from app.mcp.tools.shapes import ConversationAnswer, MatchCard, TakeCard, ThreadAnswer

View = Literal["top", "newest", "both"]


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="What fans are saying",
        annotations=read_only("What fans are saying"),
        icons=icon("thread"),
        description=(
            "The conversation under one match: what fans are posting about it. Use this for "
            "'what are fans saying', 'what's the reaction', 'is anyone talking about it'. "
            "'top' brings the takes with the most reaction, 'newest' what has just been "
            "posted, 'both' a mix. For the one-line summary of how the room feels rather than "
            "the takes themselves, use match_mood. Summarise the room and quote at most one "
            "or two takes, always attributed to the fan who posted them."
        ),
    )
    async def read_match_conversation(
        match: str,
        view: View = "top",
        limit: Limit = common.LIMIT_DEFAULT,
    ) -> ConversationAnswer:
        detail = await common.resolve_match(match)
        moots, has_next = await _read_thread_for(detail, view=view, limit=limit)

        takes, spilled = common.page(moots, limit)
        remaining = max(spilled, detail.moot_count - len(takes)) if detail.moot_count else spilled
        moment = common.now()
        subject = _subject(detail)

        return answer(
            ConversationAnswer(
                spoken=voice.conversation_line(takes, subject=subject, remaining=remaining),
                match=MatchCard.of(detail, now=moment),
                view=view,
                takes=[TakeCard.of(moot) for moot in takes],
                count=len(takes),
                has_more=remaining > 0 or has_next,
            )
        )

    @server.tool(
        title="Follow one argument",
        annotations=read_only("Follow one argument"),
        icons=icon("thread"),
        description=(
            "One take and the replies under it — the argument rather than the room. Pass the "
            "take's id or its fanmoot.com/m/ link, both of which other tools return. Use this "
            "to follow up on a take that read_match_conversation, trending_takes or "
            "search_fanmoot surfaced."
        ),
    )
    async def read_thread(take: str, limit: Limit = common.LIMIT_DEFAULT) -> ThreadAnswer:
        thread = await common.resolve_take(take)

        replies, spilled = common.page(thread.replies, limit)
        remaining = max(spilled, thread.reply_total - len(replies))

        return answer(
            ThreadAnswer(
                spoken=voice.thread_line(thread.moot, replies, remaining=remaining),
                take=TakeCard.of(thread.moot),
                replying_to=TakeCard.of(thread.ancestors[-1]) if thread.ancestors else None,
                replies=[TakeCard.of(reply) for reply in replies],
                reply_total=thread.reply_total,
                has_more=remaining > 0,
            )
        )


def _subject(detail: MatchDetail) -> str:
    """How the match is named inside a sentence about its thread."""
    return f"{voice.club_name(detail, 'home')} against {voice.club_name(detail, 'away')}"


async def _read_thread_for(
    detail: MatchDetail, *, view: str, limit: int
) -> tuple[list[MootOut], bool]:
    """The thread, by whichever ordering was asked for.

    ``top`` is the match thread endpoint, which ranks by reaction; ``newest``
    is the match feed, which is chronological. ``both`` reads each and
    interleaves them, de-duplicated — a take that is both the loudest and the
    latest appears once, where a naive concatenation would have the model quote
    it twice in one breath.
    """
    if view == "top":
        page = await endpoints.match_moots(detail.id, limit=limit)
        return page.items, page.next_cursor is not None

    if view == "newest":
        feed = await endpoints.get_feed("match", match_id=detail.id, limit=limit)
        return feed.items, feed.next_cursor is not None

    top, newest = await asyncio.gather(
        endpoints.match_moots(detail.id, limit=limit),
        endpoints.get_feed("match", match_id=detail.id, limit=limit),
    )
    seen: set[uuid.UUID] = set()
    merged: list[MootOut] = []
    for moot in [m for pair in zip(top.items, newest.items, strict=False) for m in pair] + (
        top.items + newest.items
    ):
        if moot.id not in seen:
            seen.add(moot.id)
            merged.append(moot)
    return merged, (top.next_cursor is not None or newest.next_cursor is not None)
