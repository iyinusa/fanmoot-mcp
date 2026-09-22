"""Pydantic mirrors of exactly the ``/v1`` shapes we consume.

Three rules hold this module together:

* **Unknown fields are ignored.** ``extra="ignore"`` everywhere, so a Fanmoot
  release that adds a field cannot break this server (HAD-1: independent
  lifecycles are the point of talking to the API rather than the database).
* **Only what we consume.** ``MootOut`` upstream carries media, link previews,
  creator tiers and profile themes; a voice surface reads none of them, so they
  are not declared here. A field that appears in this file is a field some tool
  in §5 actually speaks.
* **Nothing is invented.** Names and types match Fanmoot's own schemas
  (``app/schemas/*.py`` over there). Where a shape looks odd —
  ``SearchResponse.leagues`` being a list of plain dicts, ``ClockOut`` sending
  a clock's *origin* rather than its reading — it is odd upstream too, and
  mirroring it honestly is how the recorded fixtures keep meaning something.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Shape(BaseModel):
    """Base for every mirror: tolerant of new upstream fields, strict about the
    types of the ones we named."""

    model_config = ConfigDict(extra="ignore")


# ── Football catalogue ───────────────────────────────────────────────────────
class ClubBrief(_Shape):
    """A club as a match card or a byline shows it."""

    id: uuid.UUID
    slug: str
    name: str
    short_name: str
    badge_path: str | None = None
    primary_color: str = ""
    secondary_color: str = ""


class LeagueBrief(_Shape):
    """A competition as a match card shows it."""

    id: uuid.UUID
    slug: str
    name: str
    short_name: str
    logo_path: str | None = None


class LeagueOut(_Shape):
    id: uuid.UUID
    slug: str
    name: str
    short_name: str
    country_code: str | None = None
    #: ``domestic``, ``continental``, ``cup`` or ``international``.
    kind: str = "domestic"
    tier: int | None = None
    logo_path: str | None = None
    is_active: bool = True


class ClubOut(_Shape):
    id: uuid.UUID
    slug: str
    name: str
    short_name: str
    #: "The Gunners", "Los Blancos". Fanmoot carries one official nickname per
    #: club; the spoken forms it does *not* carry — "spurs", "psg" — are the
    #: reason ``names.py`` exists.
    nickname: str | None = None
    country_code: str | None = None
    founded: int | None = None
    stadium: str | None = None
    badge_path: str | None = None
    primary_color: str = ""
    secondary_color: str = ""
    member_count: int = 0
    is_active: bool = True
    league: LeagueOut | None = None
    competitions: list[LeagueOut] = Field(default_factory=list)


class ClubPage(_Shape):
    """Keyset pagination, not offset — ``next_cursor`` is the last slug seen."""

    items: list[ClubOut] = Field(default_factory=list)
    next_cursor: str | None = None
    count: int = 0


class LeagueDetail(LeagueOut):
    clubs: list[ClubOut] = Field(default_factory=list)


# ── Matches ──────────────────────────────────────────────────────────────────
class ClockOut(_Shape):
    """A running clock, sent as its origin rather than as its reading.

    The client is given where the period started and computes the minute
    itself, which is why this payload is byte-identical between goals and why
    an ETag over a live match holds for a whole half. ``voice.py`` (S3) turns
    it back into "sixty-seventh minute"; nothing here does arithmetic.
    """

    period: str
    #: The minute this period counts from — 0 in the first half, 45 in the
    #: second.
    base_minute: int = 0
    #: Null on a stopped clock, which is what says not to tick.
    started_at: datetime | None = None
    running: bool = False
    #: ``server`` for a derived clock, or the adapter that last confirmed it.
    source: str | None = None


class MatchEventOut(_Shape):
    id: uuid.UUID
    #: ``goal``, ``yellow``, ``red``, ``sub``, …
    kind: str
    minute: int | None = None
    extra_minute: int | None = None
    club_id: uuid.UUID | None = None
    player_name: str | None = None
    detail: str | None = None


class MatchOut(_Shape):
    id: uuid.UUID
    #: The canonical path on fanmoot.com, built server-side over there.
    url: str
    league: LeagueBrief | None = None
    season: str = ""
    home: ClubBrief | None = None
    away: ClubBrief | None = None
    kickoff_at: datetime
    status: str
    home_score: int | None = None
    away_score: int | None = None
    #: The last reading some source took. A live scoreline should speak
    #: ``clock``, not this.
    minute: int | None = None
    clock: ClockOut | None = None
    #: ``confirmed``, ``stale`` or ``assumed``. **A tool must not state the
    #: score when this is ``assumed``** — that is the 0–0 the clock started the
    #: match at and nothing has looked since. Saying "it's nil-nil" then is
    #: saying something nobody checked.
    score_state: str = "confirmed"
    venue: str | None = None
    round: str | None = None
    moot_count: int = 0


class MatchDetail(MatchOut):
    events: list[MatchEventOut] = Field(default_factory=list)


class MatchPage(_Shape):
    items: list[MatchOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pages: int = 1


class MatchGroup(_Shape):
    """One day of a matchweek grid."""

    day: str
    items: list[MatchOut] = Field(default_factory=list)


class LiveScore(_Shape):
    """One live match, cut to the things that change during one.

    Deliberately not a ``MatchOut``: the clubs, competition and venue are
    invariant for the whole match, and this is the payload that re-renders
    every few seconds.
    """

    id: uuid.UUID
    status: str
    home_score: int | None = None
    away_score: int | None = None
    clock: ClockOut | None = None
    score_state: str = "confirmed"


class LiveDigest(_Shape):
    """Everything in play across Fanmoot, right now. Viewer-independent, which
    is what makes it cacheable for everybody at once."""

    matches: list[LiveScore] = Field(default_factory=list)
    at: datetime | None = None


# ── People ───────────────────────────────────────────────────────────────────
class PublicUser(_Shape):
    """A fan as anybody sees them."""

    id: uuid.UUID
    handle: str
    name: str
    avatar_url: str | None = None
    country_code: str | None = None
    bio: str | None = None
    is_verified: bool = False
    #: Fanmoot runs its own agent accounts. Worth speaking: "that one's a bot".
    is_agent: bool = False
    created_at: datetime | None = None
    followers_count: int = 0
    following_count: int = 0
    club: ClubBrief | None = None


class UserResponse(_Shape):
    """The signed-in fan, from ``/v1/me``. Backs ``whoami``.

    ``email`` is declared because the endpoint returns it, and **no tool puts
    it in a response** (HAD-13 in spirit: a voice surface reading a fan's email
    address aloud in a kitchen is a privacy incident, not a feature).
    """

    id: uuid.UUID
    email: str
    handle: str
    name: str
    avatar_url: str | None = None
    country_code: str | None = None
    bio: str | None = None
    is_verified: bool = False
    is_agent: bool = False
    email_verified: bool = False
    created_at: datetime | None = None


class MembershipOut(_Shape):
    """Which club the fan is signed to, and for how long."""

    club: ClubOut | None = None
    joined_at: datetime | None = None
    season: str | None = None
    tenure_days: int = 0


class FollowState(_Shape):
    target_type: str
    target_id: uuid.UUID
    following: bool
    follower_count: int = 0


# ── Moots ────────────────────────────────────────────────────────────────────
class MootAuthor(_Shape):
    id: uuid.UUID
    handle: str
    name: str
    avatar_url: str | None = None
    is_verified: bool = False
    is_agent: bool = False
    club: ClubBrief | None = None


class MootOut(_Shape):
    """One take."""

    id: uuid.UUID
    #: The short, speakable id fanmoot.com/m/{short_id} resolves.
    short_id: str = ""
    url: str = ""
    author: MootAuthor
    body: str
    #: ``moot``, ``reply``, ``quote``, ``repost``.
    kind: str = "moot"
    status: str = "published"
    created_at: datetime
    edited_at: datetime | None = None
    parent_id: uuid.UUID | None = None
    root_id: uuid.UUID | None = None
    depth: int = 0
    match_id: uuid.UUID | None = None
    reply_count: int = 0
    repost_count: int = 0
    quote_count: int = 0
    reaction_count: int = 0
    reaction_counts: dict[str, int] = Field(default_factory=dict)
    my_reaction: str | None = None
    #: A creator's subscriber-only take. Read tools skip these: the body is not
    #: ours to speak.
    locked: bool = False
    audience: str = "public"
    #: A quote-take without the thing it quotes is a fragment, and
    #: ``argue-my-corner`` is built on exactly this relation.
    quote_of: MootOut | None = None


MootOut.model_rebuild()


class MootPage(_Shape):
    """A cursor page of a timeline."""

    items: list[MootOut] = Field(default_factory=list)
    next_cursor: str | None = None


class ThreadOut(_Shape):
    """One moot, what it was replying to, and what came back."""

    moot: MootOut
    ancestors: list[MootOut] = Field(default_factory=list)
    replies: list[MootOut] = Field(default_factory=list)
    reply_total: int = 0
    page: int = 1
    pages: int = 1


class ComposeResponse(_Shape):
    """What ``POST /v1/moots`` answers.

    ``held`` and ``gate_reason`` are the fields that make ``post_moot`` honest:
    Fanmoot's football-topicality gate can hold a take for review, and we never
    report a post as live when it is pending (HAD-7).
    """

    moot: MootOut
    held: bool = False
    gate_reason: str | None = None


class ReactionState(_Shape):
    moot_id: uuid.UUID
    my_reaction: str | None = None
    reaction_count: int = 0
    reaction_counts: dict[str, int] = Field(default_factory=dict)


# ── Club news ────────────────────────────────────────────────────────────────
class ClubNewsOut(_Shape):
    id: uuid.UUID
    headline: str
    summary: str = ""
    url: str = ""
    domain: str = ""
    source_name: str = ""
    image_url: str | None = None
    at: datetime | None = None
    moot_url: str | None = None
    moot_short_id: str | None = None
    club: ClubBrief | None = None


class ClubNewsPage(_Shape):
    items: list[ClubNewsOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pages: int = 1


# ── Search ───────────────────────────────────────────────────────────────────
class SearchResponse(_Shape):
    """One query across everything.

    ``leagues`` really is a list of plain dictionaries upstream rather than a
    modelled shape — mirrored as-is rather than tidied, so a fixture recorded
    from production keeps validating when this file is read next to it.
    """

    query: str = ""
    names_first: bool = False
    clubs: list[ClubBrief] = Field(default_factory=list)
    leagues: list[dict[str, str]] = Field(default_factory=list)
    people: list[PublicUser] = Field(default_factory=list)
    moots: list[MootOut] = Field(default_factory=list)
    updates: list[ClubNewsOut] = Field(default_factory=list)
    matches: list[MatchOut] = Field(default_factory=list)


# ── Predictions ──────────────────────────────────────────────────────────────
class SideOut(_Shape):
    slug: str
    name: str
    short_name: str
    badge_path: str | None = None
    primary_color: str = ""


class MatchBriefOut(_Shape):
    """A prediction's match, cut down. Not a ``MatchOut`` — upstream chose a
    separate, flatter shape here and mirroring it is cheaper than reconciling."""

    id: uuid.UUID
    path: str = ""
    kickoff_at: datetime
    status: str
    league: str | None = None
    league_slug: str | None = None
    home: SideOut | None = None
    away: SideOut | None = None
    home_score: int | None = None
    away_score: int | None = None


class PredictionOut(_Shape):
    id: uuid.UUID
    match_id: uuid.UUID
    home_score: int
    away_score: int
    #: Null until the match is settled.
    points: int | None = None
    exact: bool | None = None
    updated_at: datetime | None = None
    card_url: str = ""


class OpenMatchOut(_Shape):
    """A fixture still open to a call, and what this fan has called so far."""

    match: MatchBriefOut
    prediction: PredictionOut | None = None


class MyPredictionOut(_Shape):
    match: MatchBriefOut
    prediction: PredictionOut
