"""The fan themselves: who they are, and what they choose to follow.

``S5-1`` and ``S5-6``. Two tools that bracket the authenticated surface — the
first thing a client calls after linking, and the cheapest thing a fan can ask
it to do on their behalf.

They are together because they are the same subject seen twice. ``whoami``
reads the account; ``follow`` changes what that account pays attention to.
Neither touches the conversation, which is why neither is in ``compose``.

**``whoami`` is the tool that makes a linked account feel linked.** "Alexa, who
am I on Fanmoot" is the question a fan asks to check the connection worked, and
answering it with a handle, a club and how long they have been signed to it is
the difference between a working integration and one they are not sure about.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from app import untrusted, voice
from app.fanmoot import endpoints
from app.fanmoot.client import FanmootError
from app.fanmoot.models import PublicProfile
from app.logging_config import get_logger
from app.mcp.errors import ToolInputError
from app.mcp.tools import common
from app.mcp.tools.acting import Fan, refusals, signed_in, writes
from app.mcp.tools.common import answer, icon, read_only
from app.mcp.tools.shapes import ClubCard, FanAnswer, FollowAnswer, absolute, profile_url

log = get_logger("mcp.tools.fan")

Kind = Literal["club", "competition", "fan"]


def register(server: FastMCP[Any]) -> None:
    @server.tool(
        title="Who am I on Fanmoot",
        annotations=read_only("Who am I on Fanmoot"),
        icons=icon("fan"),
        description=(
            "The signed-in fan's own account: their handle, the club they are signed to, how "
            "long they have been signed to it, and how many follow them. Use this for 'who am "
            "I', 'am I connected', 'which club am I signed to', and before posting when you "
            "need to say whose handle a take will go out under. Needs the fan's Fanmoot "
            "account connected; if it comes back asking for that, let the client link it."
        ),
    )
    async def whoami() -> FanAnswer:
        fan = await signed_in()
        # Three requests, one round trip. Sequencing them so the profile could
        # be asked for by the handle `/v1/me` returns would be correct in the
        # one case where a fan has renamed since linking, and slower in every
        # other — so the grant's handle is used and a rename simply costs the
        # two numbers `_profile` exists for.
        async with refusals(action="read your account"):
            account, membership, profile = await asyncio.gather(
                endpoints.me(token=fan.token),
                endpoints.my_club(token=fan.token),
                _profile(fan),
            )

        club = ClubCard.of(membership.club) if membership.club else None
        followers = profile.user.followers_count if profile else 0
        return answer(
            FanAnswer(
                spoken=voice.fan_line(
                    handle=account.handle,
                    club=club.name if club else "",
                    tenure_days=membership.tenure_days,
                    followers=followers,
                    unverified=not account.email_verified,
                ),
                handle=account.handle,
                name=untrusted.clean(account.name),
                avatar_url=absolute(account.avatar_url),
                bio=untrusted.visible(account.bio or ""),
                club=club,
                season=membership.season,
                joined_at=membership.joined_at,
                tenure_days=membership.tenure_days,
                followers=followers,
                following=profile.user.following_count if profile else 0,
                is_verified=account.is_verified,
                email_verified=account.email_verified,
                url=profile_url(account.handle),
            )
        )

    @server.tool(
        title="Follow on Fanmoot",
        # Idempotent upstream in the strong sense: following something already
        # followed answers with the same state and changes nothing, so a client
        # that lost the response may safely send it again.
        annotations=writes("Follow on Fanmoot", idempotent=True),
        icons=icon("follow"),
        description=(
            "Follow a club, a competition or another fan, as the signed-in fan. Say the name "
            "the way a fan would — 'the gunners', 'the Prem', '@thegunner' — and it is "
            "resolved the same way find_club does. A handle with an at sign is always taken "
            "as a fan. This adds a follow under their handle and cannot unfollow anything or "
            "change which club they are signed to; confirm with the fan before you call this."
        ),
    )
    async def follow(target: str, kind: Kind | None = None) -> FollowAnswer:
        fan = await signed_in()
        target_type, identifier, name, url = await _resolve_target(target, kind)

        async with refusals(action=f"follow {name}"):
            state = await endpoints.follow(target_type, identifier, token=fan.token)

        return answer(
            FollowAnswer(
                spoken=voice.follow_line(
                    name=name, kind=state.target_type, followers=state.follower_count
                ),
                target_type=state.target_type,
                name=name,
                following=state.following,
                follower_count=state.follower_count,
                url=url,
            )
        )


async def _profile(fan: Fan) -> PublicProfile | None:
    """The fan's public profile, or nothing.

    Best-effort on purpose. It is read for two numbers that are pleasant to
    hear and are not the answer to "who am I" — so a profile route that is slow,
    renamed or unavailable must not take the handle and the club down with it.
    """
    try:
        return await endpoints.public_profile(fan.handle, token=fan.token)
    except FanmootError as exc:
        log.info("profile_unavailable", error_type=type(exc).__name__)
        return None


async def _resolve_target(target: str, kind: str | None) -> tuple[str, str, str, str | None]:
    """What the fan named → ``(target_type, identifier, name, url)``.

    Fanmoot's three types are ``club``, ``league`` and ``user``, and it accepts
    the human identifier — a slug, or a handle — so nothing here has to look up
    an id first.

    When ``kind`` is not given the name is resolved against the catalogue: club
    first, because "arsenal" is a club far more often than it is anything else,
    then competition. An ambiguous name becomes the same question every other
    tool asks rather than a guess (``S1-5``).
    """
    text = target.strip()
    if not text:
        raise ToolInputError("What would you like to follow?")

    if kind == "fan" or text.startswith("@"):
        handle = text.lstrip("@").strip()
        if not handle:
            raise ToolInputError("Which fan? Say their handle, like at-thegunner.")
        return "user", handle, f"@{handle}", profile_url(handle)

    clubs: list[str] = []
    if kind != "competition":
        club, _, clubs = await common.lookup_club(text)
        if club is not None:
            return "club", club.slug, club.name, absolute(f"/clubs/{club.slug}")

    if kind != "club":
        league, _, competitions = await common.lookup_competition(text)
        if league is not None:
            return "league", league.slug, league.name, absolute(f"/competitions/{league.slug}")
        if competitions:
            raise ToolInputError(voice.ask_which(competitions, kind="competition"))

    # Asked last rather than first: a name that matched several clubs *and* no
    # competition is a question about clubs, and asking it before the
    # competition lookup had run would have refused "the Prem" as an ambiguous
    # club name.
    if clubs:
        raise ToolInputError(voice.ask_which(clubs, kind="club"))

    raise ToolInputError(
        f"I cannot find a club or competition called {text} on Fanmoot. "
        "If you mean a fan, say their handle with an at sign in front of it."
    )
