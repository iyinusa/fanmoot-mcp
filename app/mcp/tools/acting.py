"""What every authenticated tool shares: acting as the fan, and refusing well.

``S5``'s half of the surface differs from ``S3``'s in exactly three ways, and
all three live here rather than being written out seven times:

* **Acting as somebody.** ``signed_in()`` turns the opaque token the gate
  already accepted into a live Fanmoot access token for that fan (``S4-8``).
  The tool never sees a grant, a refresh token or a database session — it gets
  a handle and a credential that is good for the next few minutes.
* **Saying no usefully** (``S5-8``). Fanmoot refuses writes for reasons a fan
  can act on — the address is unconfirmed, the account is muted, the match has
  kicked off, the take is too long — and its own sentences are written for a
  fan. They are passed through unedited, with the *next step* added where
  Fanmoot had no reason to include one.
* **Declaring what a call does** (``S5-7``). ``writes()`` is the annotation
  block a host reads before deciding whether to ask "post this as @handle?".

**Nothing here catches an exception to hide it.** Every path either returns or
raises something ``app.mcp.errors.speakable`` turns into one sentence, because
a tool that swallowed a failure would report a take as posted when it was not
(HAD-7).
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations
from pydantic import Field

from app.db import get_sessionmaker
from app.fanmoot.client import UNAUTHORIZED_FALLBACK, FanmootRejected, FanmootUnauthorized
from app.logging_config import get_logger
from app.mcp.errors import ToolInputError
from app.models.grant import AccessGrant
from app.oauth import upstream

log = get_logger("mcp.acting")

#: The longest take Fanmoot will accept from anybody: 1,500 characters, which
#: is the Plus ceiling. Declared in the schema so a client can see it before it
#: dictates a paragraph, and enforced by pydantic so what comes back is the
#: sentence in ``app.mcp.errors`` rather than a 422 from upstream.
#:
#: The **free** ceiling is 250 and this server cannot know which a fan has —
#: entitlements are Fanmoot's business, not ours (HAD-1). So the lower cap is
#: left where it is enforced, and Fanmoot's own "A moot is at most 250
#: characters." comes back through ``refusals`` with the way out attached.
MOOT_MAX = 1500

#: The free-plan ceiling, named only so the tool descriptions can be honest
#: about it. Nothing here enforces it.
MOOT_FREE_MAX = 250

Body = Annotated[
    str,
    Field(
        max_length=MOOT_MAX,
        description=(
            f"What to post, in the fan's own words. Up to {MOOT_FREE_MAX} characters on a free "
            f"Fanmoot account and {MOOT_MAX} with Plus — if it is too long you are told so, "
            "never trimmed silently."
        ),
    ),
]

#: When Fanmoot's refusal is missing the next step, what to add to it.
#:
#: The pattern side is matched against **Fanmoot's own sentence**, which is
#: the one thing here that is written for a fan; the sentence is appended, never
#: substituted. Anything unrecognised is passed through exactly as it arrived,
#: which is the safe direction: a refusal a fan can read beats an invented
#: paraphrase of one.
_NEXT_STEPS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"confirm your email|verif", re.IGNORECASE),
        "Look for the message from Fanmoot, click the link in it, and ask me again.",
    ),
    (
        re.compile(r"closed at kickoff|not open", re.IGNORECASE),
        "You can still post a take about it.",
    ),
    (
        re.compile(r"at most \d+ characters", re.IGNORECASE),
        "Say a shorter version and I will post that instead.",
    ),
    (
        re.compile(r"\bsay something\b", re.IGNORECASE),
        "What would you like it to say?",
    ),
)


@dataclass(frozen=True, slots=True)
class Fan:
    """The signed-in fan, as an authenticated tool needs them.

    ``token`` is a **Fanmoot** access token minted for this call and good for
    minutes, not the opaque one the client presented. ``handle`` is what the
    tool says out loud, so a fan hears which account just posted.
    """

    token: str
    handle: str
    grant_id: uuid.UUID


async def signed_in() -> Fan:
    """The fan this call is running as (``S4-8``).

    By the time a tool function runs, ``app.oauth.bearer`` has already refused
    anything without a usable token carrying the right scope — so the failures
    below are the ones that happen *after* that gate: a grant revoked between
    the gate and here, or a Fanmoot session that will no longer renew.

    Both come back as a sentence rather than a status, because the response is
    an SSE stream by now and there is no status left to set (``S4-9``).
    """
    presented = get_access_token()
    if presented is None:  # pragma: no cover - the gate answers first
        raise ToolInputError(
            "This acts as you on Fanmoot, so it needs your account connected first."
        )

    raw = (presented.claims or {}).get("grant_id")
    grant_id = uuid.UUID(str(raw)) if raw else None
    if grant_id is None:  # pragma: no cover - the verifier always writes it
        raise ToolInputError(upstream.LinkBroken.default)

    async with get_sessionmaker()() as session:
        grant = await session.get(AccessGrant, grant_id)
        if grant is None or not grant.is_active:
            # Revoked in the seconds since the gate looked. "Disconnect this
            # app" means now, and that includes a call already in flight.
            raise ToolInputError(upstream.LinkBroken.default)
        try:
            token = await upstream.fanmoot_token_for(session, grant)
        except upstream.LinkBroken as exc:
            raise ToolInputError(exc.message) from exc
        return Fan(token=token, handle=grant.fanmoot_handle, grant_id=grant.id)


@asynccontextmanager
async def refusals(*, action: str) -> AsyncIterator[None]:
    """Fanmoot's refusals, turned into something a fan can do next (``S5-8``).

    Only the two cases that need deciding are handled:

    * A **401 or 403 carrying Fanmoot's own sentence** is Fanmoot refusing the
      *act* — an unconfirmed address, a muted account — and that sentence goes
      in front of the fan. A 401 or 403 with no sentence is the credential
      itself failing, which means the link needs remaking and is a completely
      different thing to be told.
    * A **4xx with a message** gets the next step appended when Fanmoot had no
      reason to include one.

    Everything else — 404, 429, a timeout — already carries a spoken sentence
    from ``app.fanmoot.client`` and is left alone, which is why there is no
    bare ``except`` here.
    """
    try:
        yield
    except FanmootUnauthorized as exc:
        if exc.message == UNAUTHORIZED_FALLBACK:
            log.info("upstream_credential_refused", action=action)
            raise ToolInputError(upstream.LinkBroken.default) from exc
        raise ToolInputError(actionable(exc.message)) from exc
    except FanmootRejected as exc:
        raise ToolInputError(actionable(exc.message) or f"Fanmoot would not {action}.") from exc


def actionable(message: str) -> str:
    """Fanmoot's sentence, with the way out added when it has none."""
    for pattern, step in _NEXT_STEPS:
        if pattern.search(message):
            return f"{message.rstrip()} {step}"
    return message


def writes(title: str, *, idempotent: bool = False) -> ToolAnnotations:
    """The annotations every tool in ``S5`` carries (``S5-7``).

    ``destructiveHint=False`` on all of them and it is the truth rather than a
    convenience: nothing on this surface deletes or overwrites. Posting adds a
    take, following adds a follow, and predicting replaces only the fan's own
    earlier call on the same match.

    ``idempotentHint`` is set only where repeating the call really does leave
    Fanmoot in the same state — following something already followed, calling
    the same scoreline twice. It is **false for posting**: the idempotency key
    makes a *retried request* safe, but a host that decides to call the tool
    again has a new request and gets a second take, and a hint that claimed
    otherwise would be the one a client retries on.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=True,
    )


def idempotency_key(*, request_id: str, grant_id: uuid.UUID, body: str) -> str:
    """The key that stops a retried voice turn becoming two takes (``S5-2``).

    Derived from three things, and each one is load-bearing:

    * **the MCP request id**, because that is what a client repeats when it
      retries — the same frame, sent again, is the event this exists for;
    * **the grant**, because request ids are small integers scoped to a
      session and two fans are trivially both on their third call;
    * **a digest of the take itself**, because a request id restarts at one on
      every new session. Without it, a fan's first post of the evening would
      collide with their first post of the previous evening and Fanmoot would
      hand back last night's moot. With it, two different takes can never
      collide, and a genuine retry — same words, same frame — still does.

    Hashed rather than concatenated so nothing of what a fan wrote travels in a
    query parameter, and truncated to the 64 characters Fanmoot keeps.
    """
    digest = hashlib.sha256(f"{grant_id}:{request_id}:{body}".encode()).hexdigest()
    return f"mcp-{digest}"[:64]
