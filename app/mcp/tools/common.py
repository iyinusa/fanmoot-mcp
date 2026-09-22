"""What every read tool shares: the bound, the voice, and the way it asks.

Four things live here because twelve tools would otherwise each have their own
slightly different version, and "slightly different" is how a surface stops
sounding like one server (HAD-7):

* **The bound** (``S3-9``). Five by default, twenty at the hard cap, declared
  in the schema so a client sees it and enforced by pydantic so a client that
  ignores it gets a sentence back rather than forty kilobytes.
* **The answer** (``S3-8``). ``answer()`` is the one place a result becomes a
  ``CallToolResult`` carrying the spoken line *and* the structured payload.
* **The name resolvers** (``S3-2``, ⟵ ``S1-5``). A club name becomes a club,
  or it becomes a question — never a guess.
* **The match resolver.** "Arsenal vs Spurs", a Fanmoot URL, or a UUID: three
  ways a fan can name a match, one function, so ``get_match``,
  ``read_match_conversation`` and ``match_mood`` all accept the same things.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, cast

from mcp.types import CallToolResult, Icon, TextContent, ToolAnnotations
from pydantic import Field

from app import voice
from app.fanmoot import endpoints, names
from app.fanmoot.models import ClubOut, LeagueOut, MatchDetail, MatchOut
from app.mcp.errors import ToolInputError
from app.mcp.tools.shapes import Answer, Choice
from app.settings import get_settings

#: Five results, because that is what a person can hold in their head when it
#: is read to them rather than shown.
LIMIT_DEFAULT = 5

#: Twenty, and no argument: a voice surface cannot read fifty fixtures and a
#: 40 KB payload blows the latency budget (HAD-6, HAD-7).
LIMIT_MAX = 20

Limit = Annotated[
    int,
    Field(
        ge=1,
        le=LIMIT_MAX,
        description=(
            f"How many to bring back. {LIMIT_DEFAULT} by default, {LIMIT_MAX} at the most — "
            "this is read aloud, so ask for what can actually be listened to."
        ),
    ),
]

#: How far into a club's fixture list to look for "Arsenal vs Spurs". One page
#: either side of now is what a fan means by "the Brighton game": the last
#: result and the next few fixtures.
MATCH_WINDOW = 5

#: How a fan says "against". ``v`` and ``vs`` are what a transcript produces.
_VERSUS = re.compile(r"\s+(?:vs?\.?|versus|against|@)\s+", re.IGNORECASE)

#: ``/matches/premier-league/2026-27/brighton-hove-albion-vs-arsenal-2026-09-19``
#: — the canonical path on fanmoot.com, which is what a fan pastes.
_MATCH_PATH = re.compile(r"/matches/(?P<league>[^/]+)/(?P<season>[^/]+)/(?P<slug>[^/?#]+)")


# ── Answering ────────────────────────────────────────────────────────────────
def answer[A: Answer](payload: A) -> A:
    """One tool result: the sentence for the ear, the payload for the machine.

    A ``CallToolResult`` is returned where the signature promises the payload
    model, and that is deliberate rather than sloppy. FastMCP derives a tool's
    output schema from its return annotation and then, on seeing a
    ``CallToolResult``, validates ``structuredContent`` against that same
    schema and passes the result through untouched — which is the only way to
    declare a typed output schema *and* choose the text a model reads aloud.
    Returning the model itself would put a JSON dump in ``content[0].text``,
    and a voice client would read it out.

    The cast is the whole of the lie, it is one line, and the tests assert both
    halves of the result it produces.
    """
    result = CallToolResult(
        content=[TextContent(type="text", text=payload.spoken)],
        structuredContent=payload.model_dump(mode="json"),
    )
    return cast(A, result)


def read_only(title: str) -> ToolAnnotations:
    """The annotations every tool in ``S3`` carries (``S3-8``).

    All twelve read; none writes. ``openWorldHint`` because the answers come
    from Fanmoot rather than from this process, and ``idempotentHint`` because
    calling one twice costs a cache hit and changes nothing — both of which a
    client uses to decide what it may retry without asking the fan.
    """
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )


def icon(name: str) -> list[Icon]:
    """The tool's icon, new in ``2025-11-25`` (``S3-8``).

    Served by this server from ``/static/icons`` rather than linked to
    fanmoot.com: a client rendering a tool list has already connected here, and
    an icon that depends on a second origin is an icon that is missing whenever
    that origin is blocked.
    """
    return [
        Icon(
            src=get_settings().url_for(f"/static/icons/{name}.svg"),
            mimeType="image/svg+xml",
            sizes=["any"],
        )
    ]


def now() -> datetime:
    return voice.now_utc()


def page[T](items: list[T], limit: int) -> tuple[list[T], int]:
    """``items`` cut to ``limit``, and how many were left behind.

    The second half is what ``has_more`` and ``voice.more_hint`` are built
    from: a bounded list that does not say it is bounded is a list that lies.
    """
    kept = items[:limit]
    return kept, max(0, len(items) - len(kept))


# ── Names ────────────────────────────────────────────────────────────────────
def _choices(resolution: names.Resolution) -> list[Choice]:
    return [
        Choice(slug=c.slug, name=c.name, kind=c.kind, hint=c.hint) for c in resolution.candidates
    ]


def _spoken_choices(resolution: names.Resolution) -> list[str]:
    return [f"{c.name} in the {c.hint}" if c.hint else c.name for c in resolution.candidates]


def ask_or_fail(resolution: names.Resolution, *, kind: str) -> ToolInputError:
    """The resolver's unhappy paths, as the sentence to speak.

    Ambiguity becomes a question and an unknown name becomes a plain "I cannot
    find that" — never a guess, because answering "Manchester United" to a
    Newcastle fan who said "united" is worse than asking (``S1-5``).
    """
    if resolution.candidates:
        return ToolInputError(voice.ask_which(_spoken_choices(resolution), kind=kind))
    return ToolInputError(f"I cannot find a {kind} called {resolution.query} on Fanmoot.")


async def resolve_club(query: str) -> ClubOut:
    """A club name, however a fan says it → the club. Raises the question."""
    catalogue = await names.get_catalogue()
    resolution = catalogue.resolve_club(query)
    if resolution.match is None:
        raise ask_or_fail(resolution, kind="club")
    club = catalogue.club(resolution.match.slug)
    if club is None:  # pragma: no cover - the catalogue indexed what it holds
        raise ToolInputError(f"I cannot find a club called {query} on Fanmoot.")
    return club


async def resolve_competition(query: str) -> LeagueOut:
    """A competition name → the competition. Raises the question."""
    catalogue = await names.get_catalogue()
    resolution = catalogue.resolve_competition(query)
    if resolution.match is None:
        raise ask_or_fail(resolution, kind="competition")
    league = catalogue.competition(resolution.match.slug)
    if league is None:  # pragma: no cover
        raise ToolInputError(f"I cannot find a competition called {query} on Fanmoot.")
    return league


async def lookup_club(query: str) -> tuple[ClubOut | None, list[Choice], list[str]]:
    """``find_club``'s version: candidates come back as data, not as an error.

    The difference matters. Every other tool raises the question because it
    cannot do its job without an answer; ``find_club`` *is* the question, so it
    returns the candidates in ``structuredContent`` where a client can render
    them as buttons.
    """
    catalogue = await names.get_catalogue()
    resolution = catalogue.resolve_club(query)
    if resolution.match is not None:
        return catalogue.club(resolution.match.slug), [], []
    return None, _choices(resolution), _spoken_choices(resolution)


async def lookup_competition(query: str) -> tuple[LeagueOut | None, list[Choice], list[str]]:
    """``find_competition``'s version. See ``lookup_club``."""
    catalogue = await names.get_catalogue()
    resolution = catalogue.resolve_competition(query)
    if resolution.match is not None:
        return catalogue.competition(resolution.match.slug), [], []
    return None, _choices(resolution), _spoken_choices(resolution)


# ── Matches ──────────────────────────────────────────────────────────────────
def _as_uuid(text: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(text.strip())
    except ValueError:
        return None


async def _fixtures_matching(slug: str, wanted: set[str] | None) -> list[MatchOut]:
    """One club's current fixtures, cut to the ones against the named opponent."""
    fixtures = await endpoints.list_matches(club=slug, limit=MATCH_WINDOW)
    return [match for match in fixtures.items if wanted is None or _sides(match) == wanted]


def _sides(match: MatchOut) -> set[str]:
    return {club.slug for club in (match.home, match.away) if club is not None}


def _kickoff_key(match: MatchOut, moment: datetime) -> tuple[int, float]:
    """Sort key: what is being played, then what is closest to now.

    A fan saying "the Arsenal game" on a Saturday afternoon means the one
    happening; on a Tuesday they mean the next one; on a Sunday morning they
    might mean yesterday's. Distance from now, with live first, answers all
    three without asking.
    """
    live = 0 if match.status == voice.LIVE else 1
    return live, abs((match.kickoff_at - moment).total_seconds())


async def resolve_match(query: str) -> MatchDetail:
    """ "Arsenal vs Spurs", a fanmoot.com URL, or a UUID → one match.

    A tool that demanded the UUID would force a wasted turn every time, because
    a fan has never seen one (HAD-7). So the words come first and the id is
    just another accepted form.
    """
    text = query.strip()
    if not text:
        raise ToolInputError("Which match do you mean?")

    match_id = _as_uuid(text)
    if match_id is not None:
        return await endpoints.get_match(match_id)

    path = _MATCH_PATH.search(text)
    if path is not None:
        return await endpoints.get_match_by_path(path["league"], path["season"], path["slug"])

    sides = _VERSUS.split(text, maxsplit=1)
    home = await resolve_club(sides[0])
    away = await resolve_club(sides[1]) if len(sides) > 1 else None

    wanted = {home.slug, away.slug} if away is not None else None
    candidates = await _fixtures_matching(home.slug, wanted)
    if not candidates and away is not None:
        # The second side, because a club's fixture window is a window: a match
        # a fortnight out can sit inside the busier club's five and outside the
        # quieter one's. Asked for "Arsenal vs Leeds", look under Leeds too
        # before telling a fan there is no such match.
        candidates = await _fixtures_matching(away.slug, wanted)
    if not candidates:
        if away is not None:
            raise ToolInputError(
                f"I cannot find a match between {home.name} and {away.name} around now."
            )
        raise ToolInputError(f"I cannot find a match involving {home.name} around now.")

    moment = now()
    chosen = min(candidates, key=lambda m: _kickoff_key(m, moment))
    return await endpoints.get_match(chosen.id)
