"""Putting words on Fanmoot: posting, replying, reacting.

``S5-2``, ``S5-3``, ``S5-4`` — the three tools this whole project is for. Twelve
read tools make a football service; these make it the thing a fan can *argue*
through, which is the difference §1 of the plan is arguing for.

They are the most dangerous tools on the surface and every decision here comes
from that:

**A retry must not become a second take.** A voice turn is retried by clients
for reasons that have nothing to do with the fan — a dropped socket, a timeout
inside a skill — and a duplicate opinion published under somebody's handle is
not something an apology fixes. So every compose carries an idempotency key
derived from the frame that asked for it (``S5-2``, ``app.mcp.tools.acting``).

**A held take is never reported as live.** Fanmoot's football-topicality gate
can hold a post for a moderator. The answer says so, and says Fanmoot's own
reason word for word (HAD-7).

**Nothing is trimmed.** A dictated take past the character cap comes back as a
question — "shall I shorten it?" — rather than a truncated opinion published
under a fan's name.

**The descriptions tell a host to confirm first.** ``2025-11-25`` annotations
say a tool writes; they do not say what it writes. A host that is going to ask
"post this as @thegunner?" needs the description to have told it to.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from app import voice
from app.fanmoot import endpoints
from app.mcp.errors import ToolInputError
from app.mcp.tools import common
from app.mcp.tools.acting import Body, idempotency_key, refusals, signed_in, writes
from app.mcp.tools.common import answer, icon
from app.mcp.tools.shapes import PostAnswer, ReactionAnswer, TakeCard

#: Fanmoot's reaction set, which is football-flavoured on purpose: "like" says
#: nothing about a tactical argument. Declared as a ``Literal`` so the five
#: words are in the tool's input schema and a model never has to guess one.
Reaction = Literal["respect", "heat", "tactics", "banter", "offside"]


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="Post a take",
        # Not idempotent, and deliberately so: the key below makes a *retried
        # frame* safe, but a host that decides to call this again has a new
        # frame and gets a second take. See `acting.writes`.
        annotations=writes("Post a take"),
        icons=icon("compose"),
        description=(
            "Post a take on Fanmoot, publicly, under the signed-in fan's own handle. Use this "
            "for 'post that', 'say something about the game', 'tell Fanmoot the ref was "
            "terrible'. Pass 'match' when the take is about a specific game — say it the way "
            "a fan would, 'arsenal vs spurs' — and it is anchored to that match's thread. "
            "Read the wording back to the fan and get their agreement before you call this: "
            "it publishes immediately and everything they say goes out under their name. "
            "Fanmoot holds a take for review when it cannot tell it is about football; when "
            "that happens, say so and never report it as live."
        ),
    )
    async def post_moot(body: Body, match: str | None = None) -> PostAnswer:
        fan = await signed_in()
        text = _said(body)
        match_id = (await common.resolve_match(match)).id if match else None

        async with refusals(action="post that"):
            composed = await endpoints.compose_moot(
                token=fan.token,
                body=text,
                match_id=match_id,
                idempotency_key=idempotency_key(
                    request_id=_frame(server), grant_id=fan.grant_id, body=text
                ),
            )

        return answer(
            PostAnswer(
                spoken=voice.posted_line(
                    composed.moot, held=composed.held, gate_reason=composed.gate_reason or ""
                ),
                take=TakeCard.of(composed.moot),
                held=composed.held,
                gate_reason=composed.gate_reason,
            )
        )

    @server.tool(
        title="Reply to a take",
        annotations=writes("Reply to a take"),
        icons=icon("compose"),
        description=(
            "Reply to one take, in its thread, as the signed-in fan. Pass the take's id or its "
            "fanmoot.com/m/ link — read_match_conversation, read_thread, trending_takes and "
            "search_fanmoot all return them — and what to say. Use this rather than post_moot "
            "when the fan is answering somebody rather than starting something. The reply is "
            "public and goes out under their handle, so read it back to them first."
        ),
    )
    async def reply_to_moot(take: str, body: Body) -> PostAnswer:
        fan = await signed_in()
        text = _said(body)
        parent_id = await common.resolve_take_id(take)

        async with refusals(action="post that reply"):
            composed = await endpoints.compose_moot(
                token=fan.token,
                body=text,
                parent_id=parent_id,
                idempotency_key=idempotency_key(
                    request_id=_frame(server), grant_id=fan.grant_id, body=f"{parent_id}:{text}"
                ),
            )

        return answer(
            PostAnswer(
                spoken=voice.posted_line(
                    composed.moot,
                    held=composed.held,
                    gate_reason=composed.gate_reason or "",
                    replying=True,
                ),
                take=TakeCard.of(composed.moot),
                held=composed.held,
                gate_reason=composed.gate_reason,
                replying_to=parent_id,
            )
        )

    @server.tool(
        title="React to a take",
        annotations=writes("React to a take"),
        icons=icon("reaction"),
        description=(
            "React to somebody's take as the signed-in fan. The five reactions are Fanmoot's "
            "own: respect, heat, tactics, banter, offside — 'offside' is the disagree one. "
            "Reacting again with the reaction already given clears it, which is how a fan "
            "takes one back. Pass the take's id or its fanmoot.com/m/ link. This is public "
            "and shows on the take, so confirm which of the five the fan means before calling it."
        ),
    )
    async def react_to_moot(take: str, reaction: Reaction) -> ReactionAnswer:
        fan = await signed_in()
        take_id = await common.resolve_take_id(take)

        async with refusals(action="react to that"):
            state = await endpoints.react_to_moot(take_id, token=fan.token, kind=reaction)

        return answer(
            ReactionAnswer(
                spoken=voice.reaction_line(state.my_reaction, total=state.reaction_count),
                take_id=state.moot_id,
                reaction=state.my_reaction,
                reaction_count=state.reaction_count,
                reactions=state.reaction_counts,
            )
        )


def _said(body: str) -> str:
    """The take, trimmed of whitespace and refused if there is nothing in it.

    The length ceiling is pydantic's (``acting.Body``) — it belongs in the
    schema where a client can read it. Emptiness is checked here instead,
    because "say something" wants to be a question to the fan rather than a
    validation failure, and a ``min_length`` would have produced the latter.
    """
    text = body.strip()
    if not text:
        raise ToolInputError("What would you like it to say?")
    return text


def _frame(server: FastMCP[Any]) -> str:
    """The id of the JSON-RPC request being answered (``S5-2``).

    Read off the request context rather than taken as an argument, because it
    identifies the *frame* — what a client repeats verbatim when it retries —
    and a tool argument would be something the model chose, which is the one
    thing it must not be.

    A missing context is only reachable if a tool is called outside a request,
    which the tests do not do and a transport cannot. A fresh uuid then, so an
    absent id can never make two unrelated takes share a key.
    """
    try:
        return server.get_context().request_id
    except ValueError:  # pragma: no cover - there is always a request here
        return str(uuid.uuid4())
