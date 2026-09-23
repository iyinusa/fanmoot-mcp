"""What a tool answers with — the ``structuredContent`` half of every result.

``S3-8``. Each tool returns one of these models, FastMCP derives its JSON
Schema 2020-12 output schema from the return annotation, and the low-level
server validates the payload against that schema on the way out. So the schema
a client reads in ``tools/list`` and the object this server builds are the same
declaration, and cannot drift.

Three decisions worth stating, because they shape every model below:

* **These are not the upstream mirrors.** ``app.fanmoot.models`` is what
  Fanmoot sends; this is what a *client* renders. A ``MatchCard`` has the
  minute already computed and the badge already absolute, because a voice
  client cannot do arithmetic on a clock origin and an MCP App cannot resolve
  a relative path against a host it never saw.
* **Every card carries its own spoken line.** The text a model reads aloud is
  ``content[0].text``, but the four MCP Apps views in ``S6`` and the demo's
  trace panel render ``structuredContent`` — and a card that cannot say itself
  would make each of them re-derive a sentence this server already wrote.
* **Nothing private is declared.** There is no email field, no viewer-specific
  flag and no id a fan would have to read out. What is here is what a stranger
  could see on fanmoot.com.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app import untrusted, voice
from app.fanmoot.models import (
    ClubBrief,
    ClubNewsOut,
    ClubOut,
    LeagueOut,
    MatchBriefOut,
    MatchEventOut,
    MatchOut,
    MootOut,
    PredictionOut,
    PublicUser,
)
from app.settings import get_settings


def absolute(path: str | None) -> str | None:
    """A Fanmoot-relative path, made absolute.

    Badges, avatars and moot URLs come back as ``/static/...`` because on
    fanmoot.com they are same-origin. A client of this server is not on that
    origin, so a relative path there is a broken image in a card.
    """
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    return get_settings().fanmoot_url_for(path)


class Card(BaseModel):
    """Base for everything a tool returns.

    ``extra="forbid"`` is not decoration: it makes the derived schema closed,
    so a client can rely on the field list and a typo in a builder below fails
    a test rather than shipping an ignored key.
    """

    model_config = ConfigDict(extra="forbid")


class Answer(Card):
    """A whole tool result. ``spoken`` is the sentence in ``content[0].text``."""

    spoken: str = Field(description="The answer as a sentence, ready to be read aloud.")


# ── Football catalogue ───────────────────────────────────────────────────────
class ClubCard(Card):
    slug: str
    name: str
    short_name: str
    nickname: str | None = None
    competition: str | None = None
    country: str | None = None
    stadium: str | None = None
    crest_url: str | None = None
    colour: str = ""
    member_count: int = 0
    url: str | None = None

    @classmethod
    def of(cls, club: ClubOut) -> ClubCard:
        return cls(
            slug=club.slug,
            name=club.name,
            short_name=club.short_name,
            nickname=club.nickname,
            competition=club.league.name if club.league else None,
            country=club.country_code,
            stadium=club.stadium,
            crest_url=absolute(club.badge_path),
            colour=club.primary_color,
            member_count=club.member_count,
            url=absolute(f"/clubs/{club.slug}"),
        )


class CompetitionCard(Card):
    slug: str
    name: str
    short_name: str
    #: ``domestic``, ``continental``, ``cup`` or ``international``.
    kind: str = "domestic"
    country: str | None = None
    tier: int | None = None
    logo_url: str | None = None
    club_count: int = 0
    url: str | None = None

    @classmethod
    def of(cls, league: LeagueOut, *, club_count: int = 0) -> CompetitionCard:
        return cls(
            slug=league.slug,
            name=league.name,
            short_name=league.short_name,
            kind=league.kind,
            country=league.country_code,
            tier=league.tier,
            logo_url=absolute(league.logo_path),
            club_count=club_count,
            url=absolute(f"/competitions/{league.slug}"),
        )


class Choice(Card):
    """One thing the fan might have meant.

    ``hint`` is what makes the spoken question answerable — "Rangers in the
    Scottish Premiership, or Queens Park Rangers?" needs the competition, not
    just the name (``S1-5``).
    """

    slug: str
    name: str
    kind: str
    hint: str = ""


class ClubAnswer(Answer):
    query: str
    resolved: bool
    club: ClubCard | None = None
    choices: list[Choice] = Field(default_factory=list)


class CompetitionAnswer(Answer):
    query: str
    resolved: bool
    competition: CompetitionCard | None = None
    clubs: list[ClubCard] = Field(default_factory=list)
    choices: list[Choice] = Field(default_factory=list)


# ── Matches ──────────────────────────────────────────────────────────────────
class SideCard(Card):
    slug: str
    name: str
    short_name: str
    crest_url: str | None = None
    colour: str = ""
    score: int | None = None

    @classmethod
    def of(cls, club: ClubBrief | None, score: int | None) -> SideCard | None:
        if club is None:
            return None
        return cls(
            slug=club.slug,
            name=club.name,
            short_name=club.short_name,
            crest_url=absolute(club.badge_path),
            colour=club.primary_color,
            score=score,
        )


class EventCard(Card):
    kind: str
    minute: int | None = None
    added: int | None = None
    club: str | None = None
    player: str | None = None
    detail: str | None = None

    @classmethod
    def of(cls, event: MatchEventOut, *, clubs: dict[uuid.UUID, str]) -> EventCard:
        return cls(
            kind=event.kind,
            minute=event.minute,
            added=event.extra_minute,
            club=clubs.get(event.club_id) if event.club_id else None,
            player=event.player_name,
            detail=event.detail,
        )


class MatchCard(Card):
    """One match, with the clock already read and the score already judged.

    ``score_confirmed`` is the field a client must respect: when it is false
    the scoreline is a 0–0 a server-side clock started the match at, which
    nobody has looked at since, and no surface should state it.
    """

    id: uuid.UUID
    status: str
    competition: str | None = None
    season: str = ""
    round: str | None = None
    venue: str | None = None
    kickoff_at: datetime
    kickoff_spoken: str = ""
    home: SideCard | None = None
    away: SideCard | None = None
    minute: int | None = None
    added: int | None = None
    period: str | None = None
    score_confirmed: bool = True
    take_count: int = 0
    url: str | None = None
    spoken: str = ""

    @classmethod
    def of(cls, match: MatchOut, *, now: datetime | None = None) -> MatchCard:
        moment = now or voice.now_utc()
        reading = voice.clock_reading(match.clock, now=moment)
        return cls(
            id=match.id,
            status=match.status,
            competition=match.league.name if match.league else None,
            season=match.season,
            round=match.round,
            venue=match.venue,
            kickoff_at=match.kickoff_at,
            kickoff_spoken=voice.kickoff_phrase(match.kickoff_at, now=moment),
            home=SideCard.of(match.home, match.home_score),
            away=SideCard.of(match.away, match.away_score),
            minute=reading.minute,
            added=reading.extra,
            period=match.clock.period if match.clock else None,
            score_confirmed=match.score_state != voice.ASSUMED,
            take_count=match.moot_count,
            url=absolute(match.url),
            spoken=voice.match_line(match, now=moment),
        )


class MatchListAnswer(Answer):
    matches: list[MatchCard] = Field(default_factory=list)
    count: int = 0
    #: How many the upstream query has in total, where it says. A voice surface
    #: reads five and needs to say "there are four more" (``S3-9``).
    total: int = 0
    has_more: bool = False


class MatchDay(Card):
    """One day of a matchweek, which is how ``upcoming_matches`` is grouped."""

    day: str
    day_spoken: str
    matches: list[MatchCard] = Field(default_factory=list)


class UpcomingAnswer(Answer):
    days: list[MatchDay] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False


class MatchAnswer(Answer):
    match: MatchCard
    events: list[EventCard] = Field(default_factory=list)


class LiveAnswer(Answer):
    matches: list[MatchCard] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False
    at: datetime | None = None


# ── The conversation ─────────────────────────────────────────────────────────
class TakeCard(Card):
    """One moot. No viewer-specific field: a read tool has no viewer."""

    id: uuid.UUID
    short_id: str = ""
    handle: str
    name: str
    club: str | None = None
    avatar_url: str | None = None
    is_agent: bool = False
    is_verified: bool = False
    body: str
    posted_at: datetime
    reply_count: int = 0
    reaction_count: int = 0
    reactions: dict[str, int] = Field(default_factory=dict)
    url: str | None = None
    spoken: str = ""

    @classmethod
    def of(cls, moot: MootOut) -> TakeCard:
        return cls(
            id=moot.id,
            short_id=moot.short_id,
            handle=moot.author.handle,
            # Cleaned, not escaped (`S7-3`): a bidi override in a name or a
            # body makes a card show one thing while the model reads another.
            name=untrusted.clean(moot.author.name),
            club=moot.author.club.name if moot.author.club else None,
            avatar_url=absolute(moot.author.avatar_url),
            is_agent=moot.author.is_agent,
            is_verified=moot.author.is_verified,
            body=untrusted.visible(moot.body),
            posted_at=moot.created_at,
            reply_count=moot.reply_count,
            reaction_count=moot.reaction_count,
            reactions=moot.reaction_counts,
            url=absolute(moot.url or (f"/m/{moot.short_id}" if moot.short_id else None)),
            spoken=voice.take(moot),
        )


class ConversationAnswer(Answer):
    match: MatchCard | None = None
    #: ``top``, ``newest`` or ``both`` — which way the thread was read.
    view: str = "top"
    takes: list[TakeCard] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False


class ThreadAnswer(Answer):
    take: TakeCard
    replying_to: TakeCard | None = None
    replies: list[TakeCard] = Field(default_factory=list)
    reply_total: int = 0
    has_more: bool = False


class TrendingAnswer(Answer):
    takes: list[TakeCard] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False


# ── Search and news ──────────────────────────────────────────────────────────
class FanCard(Card):
    handle: str
    name: str
    club: str | None = None
    avatar_url: str | None = None
    is_agent: bool = False
    is_verified: bool = False
    url: str | None = None

    @classmethod
    def of(cls, user: PublicUser) -> FanCard:
        return cls(
            handle=user.handle,
            name=user.name,
            club=user.club.name if user.club else None,
            avatar_url=absolute(user.avatar_url),
            is_agent=user.is_agent,
            is_verified=user.is_verified,
            url=absolute(f"/@{user.handle}"),
        )


class SearchAnswer(Answer):
    query: str
    clubs: list[Choice] = Field(default_factory=list)
    competitions: list[Choice] = Field(default_factory=list)
    people: list[FanCard] = Field(default_factory=list)
    takes: list[TakeCard] = Field(default_factory=list)
    matches: list[MatchCard] = Field(default_factory=list)
    count: int = 0


class NewsCard(Card):
    headline: str
    summary: str = ""
    source: str = ""
    url: str = ""
    image_url: str | None = None
    at: datetime | None = None
    club: str | None = None
    #: Where the conversation about this story is, if there is one.
    take_url: str | None = None

    @classmethod
    def of(cls, item: ClubNewsOut) -> NewsCard:
        return cls(
            headline=item.headline,
            summary=item.summary,
            source=item.source_name or item.domain,
            url=item.url,
            image_url=absolute(item.image_url),
            at=item.at,
            club=item.club.name if item.club else None,
            take_url=absolute(item.moot_url),
        )


class NewsAnswer(Answer):
    club: str | None = None
    items: list[NewsCard] = Field(default_factory=list)
    count: int = 0
    has_more: bool = False


# ── Mood ─────────────────────────────────────────────────────────────────────
class MoodAnswer(Answer):
    """Three integers, three takes and a sentence (``S3-7``).

    ``method`` says what produced the labels — ``bedrock`` or ``lexicon`` — so
    a client is never left guessing how much to trust the split. No label is
    attached to a take here, and none is stored anywhere: the counts are the
    output.
    """

    subject: str
    match: MatchCard | None = None
    positive: int = 0
    negative: int = 0
    neutral: int = 0
    sampled: int = 0
    method: str = "lexicon"
    summary: str = ""
    takes: list[TakeCard] = Field(default_factory=list)


# ── Acting as the fan (S5) ───────────────────────────────────────────────────
class FanAnswer(Answer):
    """``whoami``. The fan's own account, which is a narrower thing than a
    profile: what is here is what they would say about themselves, plus the two
    flags that change what the rest of this surface can do for them."""

    handle: str
    name: str
    club: ClubCard | None = None
    #: How long they have been signed to that club, which on Fanmoot is
    #: standing rather than trivia.
    season: str | None = None
    joined_at: datetime | None = None
    tenure_days: int = 0
    followers: int = 0
    following: int = 0
    is_verified: bool = False
    #: False means they cannot post yet, whatever scope the grant carries.
    email_verified: bool = True
    url: str | None = None


class PostAnswer(Answer):
    """``post_moot`` and ``reply_to_moot``.

    ``held`` and ``gate_reason`` are the fields that must never be dropped: a
    take Fanmoot is holding for review is not live, and a client that rendered
    this card without them would be telling a fan otherwise (HAD-7, ``S5-2``).
    """

    take: TakeCard
    held: bool = False
    gate_reason: str | None = None
    #: The take this one answers, when it answers one.
    replying_to: uuid.UUID | None = None


class ReactionAnswer(Answer):
    """``react_to_moot``. One call sets, changes or clears — ``reaction`` is
    null when the fan has just cleared theirs."""

    take_id: uuid.UUID
    reaction: str | None = None
    reaction_count: int = 0
    reactions: dict[str, int] = Field(default_factory=dict)


class PredictionCard(Card):
    """One fixture and what the fan has called on it, if anything.

    One card for both halves of ``my_predictions`` — the fixtures still open
    and the calls already made — because they are the same thing at two points
    in its life, and two cards would be two schemas to render.
    """

    match_id: uuid.UUID
    competition: str | None = None
    home: str
    away: str
    kickoff_at: datetime
    kickoff_spoken: str = ""
    status: str
    called: bool = False
    home_score: int | None = None
    away_score: int | None = None
    #: The real score, once there is one. Distinct from the called one above,
    #: and that distinction is the whole card.
    actual_home: int | None = None
    actual_away: int | None = None
    points: int | None = None
    exact: bool | None = None
    url: str | None = None
    card_url: str | None = None

    @classmethod
    def of(
        cls,
        match: MatchBriefOut,
        prediction: PredictionOut | None,
        *,
        now: datetime | None = None,
    ) -> PredictionCard:
        return cls(
            match_id=match.id,
            competition=match.league,
            home=match.home.short_name if match.home else "someone",
            away=match.away.short_name if match.away else "someone",
            kickoff_at=match.kickoff_at,
            kickoff_spoken=voice.kickoff_phrase(match.kickoff_at, now=now or voice.now_utc()),
            status=match.status,
            called=prediction is not None,
            home_score=prediction.home_score if prediction else None,
            away_score=prediction.away_score if prediction else None,
            actual_home=match.home_score,
            actual_away=match.away_score,
            points=prediction.points if prediction else None,
            exact=prediction.exact if prediction else None,
            url=absolute(match.path) if match.path else None,
            card_url=absolute(prediction.card_url) if prediction and prediction.card_url else None,
        )


class PredictionAnswer(Answer):
    """``predict_match``. One call, read back so the fan can hear it."""

    prediction: PredictionCard


class PredictionsAnswer(Answer):
    """``my_predictions``. What has been called, and what still can be."""

    called: list[PredictionCard] = Field(default_factory=list)
    open: list[PredictionCard] = Field(default_factory=list)
    count: int = 0
    open_count: int = 0
    has_more: bool = False


class FollowAnswer(Answer):
    """``follow``. Idempotent upstream, so this is the state rather than the
    event: ``following`` is true whether or not this call is what made it so."""

    #: ``club``, ``league`` or ``user`` — Fanmoot's own words for the three.
    target_type: str
    name: str
    following: bool = True
    follower_count: int = 0
    url: str | None = None
