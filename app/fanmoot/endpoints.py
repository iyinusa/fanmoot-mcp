"""Typed async wrappers, one per endpoint in §5.

Each one is the same three lines — build the query, read through the cache,
validate into a mirror — and exists so that no tool anywhere above this layer
holds a URL, a query-parameter name or a raw dictionary. When Fanmoot renames a
parameter, exactly one function in this file changes.

Every wrapper takes an optional ``token``. Passing one means "as this fan" and
turns off the cache entirely (see ``cache.py``); omitting it means the public
read, which is what the twelve unauthenticated tools in §5 use.

The reads return mirrors from ``models.py``. The writes are here too, even
though the tools that call them land in ``S5``: a write is the same HTTP
plumbing, and keeping the whole upstream surface in one module is what makes
"everything this server can do, a fan could do through the public API"
checkable by reading one file.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from pydantic import BaseModel, TypeAdapter

from app.fanmoot import cache as cache_module
from app.fanmoot.client import FanmootClient, get_client
from app.fanmoot.models import (
    AuthResult,
    AuthTokens,
    ClubNewsPage,
    ClubOut,
    ClubPage,
    ComposeResponse,
    FollowState,
    LeagueDetail,
    LeagueOut,
    LiveDigest,
    MatchDetail,
    MatchGroup,
    MatchPage,
    MembershipOut,
    MootPage,
    MyPredictionOut,
    OpenMatchOut,
    PredictionOut,
    PublicProfile,
    ReactionState,
    SearchResponse,
    ThreadOut,
    UserResponse,
)

#: Fanmoot caps most list endpoints at 50 and search at 25. A voice surface
#: never wants near that (HAD-7 puts the hard cap at 20), but the clamp lives
#: here as well so a caller cannot produce a 422 by asking for 200.
_LIST_MAX = 50
_SEARCH_MAX = 25

_LEAGUE_LIST = TypeAdapter(list[LeagueOut])
_MATCH_GROUPS = TypeAdapter(list[MatchGroup])
_OPEN_MATCHES = TypeAdapter(list[OpenMatchOut])
_MY_PREDICTIONS = TypeAdapter(list[MyPredictionOut])


async def _read[T](
    validate: Callable[[Any], T],
    path: str,
    *,
    ttl: float,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> T:
    """One cached GET, whose **validated** value is what gets cached.

    Validating inside ``load`` rather than around this call is the difference
    between a cache that saves a request and one that also saves the parse: a
    fresh hit and a ``304`` both hand back an object pydantic has already seen.
    On a live digest polled every ten seconds that is most of the work.

    ``validate`` is ``Model.model_validate`` for a single shape or a
    ``TypeAdapter``'s ``validate_python`` for the endpoints that answer with a
    bare list — one path for both, so there is no second way for a response to
    reach a caller.
    """
    http = client or get_client()

    async def load(etag: str | None) -> tuple[Any, str | None, bool]:
        reply = await http.get(path, params=params, token=token, etag=etag)
        if reply.not_modified:
            return None, reply.etag, True
        return validate(reply.data), reply.etag, False

    value = await cache_module.get_cache().fetch(
        cache_module.key_for(path, params), ttl=ttl, token=token, load=load
    )
    return value  # type: ignore[no-any-return]


async def _read_model[M: BaseModel](
    model: type[M],
    path: str,
    *,
    ttl: float,
    params: dict[str, Any] | None = None,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> M:
    """``_read`` for a single modelled shape."""
    return await _read(
        model.model_validate, path, ttl=ttl, params=params, token=token, client=client
    )


def _clamp(limit: int | None, ceiling: int) -> int | None:
    if limit is None:
        return None
    return max(1, min(limit, ceiling))


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


# ── Catalogue: clubs and competitions (TTL 1 h) ──────────────────────────────
async def list_leagues(
    *,
    kind: str | None = None,
    include_inactive: bool = False,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> list[LeagueOut]:
    """``GET /v1/leagues`` — every competition. Backs ``find_competition`` and
    the ``fanmoot://competitions`` resource."""
    return await _read(
        _LEAGUE_LIST.validate_python,
        "/v1/leagues",
        ttl=cache_module.TTL_CATALOGUE,
        params={"kind": kind, "include_inactive": include_inactive or None},
        token=token,
        client=client,
    )


async def get_league(
    slug: str, *, token: str | None = None, client: FanmootClient | None = None
) -> LeagueDetail:
    """``GET /v1/leagues/{slug}`` — a competition and its clubs.

    Note what this does *not* return: a table. Fanmoot has no standings
    endpoint, and HAD-13 says we will not invent one under deadline.
    """
    return await _read_model(
        LeagueDetail,
        f"/v1/leagues/{slug}",
        ttl=cache_module.TTL_CATALOGUE,
        token=token,
        client=client,
    )


async def list_clubs(
    *,
    league: str | None = None,
    country: str | None = None,
    q: str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    exclude_nations: bool = False,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> ClubPage:
    """``GET /v1/clubs`` — the club catalogue, keyset-paged."""
    return await _read_model(
        ClubPage,
        "/v1/clubs",
        ttl=cache_module.TTL_CATALOGUE,
        params={
            "league": league,
            "country": country,
            "q": q,
            "cursor": cursor,
            "limit": _clamp(limit, 100),
            "exclude_nations": exclude_nations or None,
        },
        token=token,
        client=client,
    )


async def get_club(
    slug: str, *, token: str | None = None, client: FanmootClient | None = None
) -> ClubOut:
    """``GET /v1/clubs/{slug}`` — one club, with its competitions."""
    return await _read_model(
        ClubOut,
        f"/v1/clubs/{slug}",
        ttl=cache_module.TTL_CATALOGUE,
        token=token,
        client=client,
    )


# ── Matches ──────────────────────────────────────────────────────────────────
async def list_matches(
    *,
    league: str | None = None,
    club: str | None = None,
    season: str | None = None,
    status: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    limit: int | None = None,
    newest_first: bool = False,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> MatchPage:
    """``GET /v1/matches`` — fixtures and results. Backs ``list_matches``.

    ``status``, ``from`` and ``to`` are aliased upstream, which is why the
    query keys below do not match this signature's argument names.
    """
    return await _read_model(
        MatchPage,
        "/v1/matches",
        ttl=cache_module.TTL_FIXTURES,
        params={
            "league": league,
            "club": club,
            "season": season,
            "status": status,
            "from": _iso(date_from),
            "to": _iso(date_to),
            "page": page if page > 1 else None,
            "limit": _clamp(limit, 100),
            "newest_first": newest_first or None,
        },
        token=token,
        client=client,
    )


async def upcoming_matches(
    *,
    league: str | None = None,
    club: str | None = None,
    days: int = 14,
    past: int = 0,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> list[MatchGroup]:
    """``GET /v1/matches/upcoming`` — what's on, already grouped by day."""
    return await _read(
        _MATCH_GROUPS.validate_python,
        "/v1/matches/upcoming",
        ttl=cache_module.TTL_FIXTURES,
        params={
            "league": league,
            "club": club,
            "days": max(1, min(days, 60)),
            "past": max(0, min(past, 14)) or None,
        },
        token=token,
        client=client,
    )


async def live_digest(
    *, token: str | None = None, client: FanmootClient | None = None
) -> LiveDigest:
    """``GET /v1/matches/live`` — everything in play. Backs ``live_scores``.

    Ten-second TTL and a conditional request behind it: between goals the
    payload is byte-identical, so a poll costs a ``304`` and no parsing.
    """
    return await _read_model(
        LiveDigest, "/v1/matches/live", ttl=cache_module.TTL_LIVE, token=token, client=client
    )


async def get_match(
    match_id: uuid.UUID | str, *, token: str | None = None, client: FanmootClient | None = None
) -> MatchDetail:
    """``GET /v1/matches/{id}`` — one match with its events."""
    return await _read_model(
        MatchDetail,
        f"/v1/matches/{match_id}",
        ttl=cache_module.TTL_MATCH,
        token=token,
        client=client,
    )


async def get_match_by_path(
    league_slug: str,
    season: str,
    match_slug: str,
    *,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> MatchDetail:
    """``GET /v1/matches/by-path/{league}/{season}/{slug}``.

    The route that makes ``get_match`` able to take "arsenal vs spurs" (HAD-7):
    names resolve to a path, and a path resolves to a match, without the fan
    ever holding a UUID.
    """
    return await _read_model(
        MatchDetail,
        f"/v1/matches/by-path/{league_slug}/{season}/{match_slug}",
        ttl=cache_module.TTL_MATCH,
        token=token,
        client=client,
    )


# ── Conversation ─────────────────────────────────────────────────────────────
async def match_moots(
    match_id: uuid.UUID | str,
    *,
    cursor: str | None = None,
    limit: int | None = None,
    phase: str | None = None,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> MootPage:
    """``GET /v1/matches/{id}/moots`` — the match thread.

    ``phase`` is ``pre``, ``during`` or ``post``: the build-up, the match, and
    the reckoning. It is the filter a fan actually means by "what were they
    saying at half time".
    """
    return await _read_model(
        MootPage,
        f"/v1/matches/{match_id}/moots",
        ttl=cache_module.TTL_THREAD,
        params={"cursor": cursor, "limit": _clamp(limit, _LIST_MAX), "phase": phase},
        token=token,
        client=client,
    )


async def get_feed(
    name: str,
    *,
    club: str | None = None,
    league: str | None = None,
    handle: str | None = None,
    match_id: uuid.UUID | str | None = None,
    cursor: str | None = None,
    limit: int | None = None,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> MootPage:
    """``GET /v1/feed/{name}`` — home, club, league, match, profile, trending,
    latest.

    ``trending`` backs ``trending_takes`` and ``match`` backs the second half
    of ``read_match_conversation``; the TTL follows which one was asked for,
    because a trending list moves on a different clock from a live thread.
    """
    return await _read_model(
        MootPage,
        f"/v1/feed/{name}",
        ttl=cache_module.TTL_TRENDING if name == "trending" else cache_module.TTL_THREAD,
        params={
            "club": club,
            "league": league,
            "handle": handle,
            "match_id": str(match_id) if match_id else None,
            "cursor": cursor,
            "limit": _clamp(limit, _LIST_MAX),
        },
        token=token,
        client=client,
    )


async def get_moot(
    moot_id: uuid.UUID | str,
    *,
    page: int = 1,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> ThreadOut:
    """``GET /v1/moots/{id}`` — one moot with its ancestors and replies."""
    return await _read_model(
        ThreadOut,
        f"/v1/moots/{moot_id}",
        ttl=cache_module.TTL_THREAD,
        params={"page": page if page > 1 else None},
        token=token,
        client=client,
    )


async def get_moot_by_short_id(
    short_id: str, *, token: str | None = None, client: FanmootClient | None = None
) -> ThreadOut:
    """``GET /v1/m/{short_id}`` — the same thread, by the id a fan can read.

    ``fanmoot.com/m/sBUE6OlITD`` is the URL that appears in a club news item
    and in a share sheet, so it is the id a fan actually has to hand.
    ``read_thread`` accepts either (``S3-5``), and this is the half a UUID
    route cannot serve.
    """
    return await _read_model(
        ThreadOut,
        f"/v1/m/{short_id}",
        ttl=cache_module.TTL_THREAD,
        token=token,
        client=client,
    )


async def list_replies(
    moot_id: uuid.UUID | str,
    *,
    page: int = 1,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> MootPage:
    """``GET /v1/moots/{id}/replies``."""
    return await _read_model(
        MootPage,
        f"/v1/moots/{moot_id}/replies",
        ttl=cache_module.TTL_THREAD,
        params={"page": page if page > 1 else None},
        token=token,
        client=client,
    )


async def search(
    q: str,
    *,
    limit: int | None = None,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> SearchResponse:
    """``GET /v1/search`` — clubs, competitions, fans, moots, fixtures."""
    return await _read_model(
        SearchResponse,
        "/v1/search",
        ttl=cache_module.TTL_SEARCH,
        params={"q": q, "limit": _clamp(limit, _SEARCH_MAX)},
        token=token,
        client=client,
    )


# ── Club news ────────────────────────────────────────────────────────────────
async def list_updates(
    *,
    club: str | None = None,
    q: str | None = None,
    page: int = 1,
    token: str | None = None,
    client: FanmootClient | None = None,
) -> ClubNewsPage:
    """``GET /v1/updates`` — club news across the platform."""
    return await _read_model(
        ClubNewsPage,
        "/v1/updates",
        ttl=cache_module.TTL_NEWS,
        params={"club": club, "q": q, "page": page if page > 1 else None},
        token=token,
        client=client,
    )


async def club_news(
    slug: str, *, page: int = 1, token: str | None = None, client: FanmootClient | None = None
) -> ClubNewsPage:
    """``GET /v1/clubs/{slug}/news`` — one club's recent updates."""
    return await _read_model(
        ClubNewsPage,
        f"/v1/clubs/{slug}/news",
        ttl=cache_module.TTL_NEWS,
        params={"page": page if page > 1 else None},
        token=token,
        client=client,
    )


# ── Authentication: the calls the OAuth server makes on a fan's behalf (S4) ──
# This server is not an identity provider. It authenticates a fan by asking
# Fanmoot, with the fan's own credentials, on a page the fan is looking at
# (HAD-4) — and then holds the refresh token that comes back, encrypted, so a
# voice turn months later can mint a short-lived access token without asking
# again (HAD-3).
#
# `in_body=true` on all three. Fanmoot's default is an httpOnly cookie, which is
# right for a browser and useless to a server: there is no cookie jar here, and
# the token has to be readable to be encrypted.
_IN_BODY = {"in_body": "true"}


async def login(*, email: str, password: str, client: FanmootClient | None = None) -> AuthResult:
    """``POST /v1/auth/login`` — the consent screen's sign-in (`S4-10`).

    Never cached, never retried, and the caller never sees the password again:
    it arrives on one form POST, goes straight out in this request, and is not
    written anywhere in this service. A 401 comes back as
    ``FanmootUnauthorized`` carrying Fanmoot's own sentence, which is the one
    worth showing a fan — "Email or password is incorrect", not "authentication
    failed".
    """
    reply = await (client or get_client()).post(
        "/v1/auth/login", json={"email": email, "password": password}, params=_IN_BODY
    )
    return AuthResult.model_validate(reply.data)


async def register(
    *,
    email: str,
    password: str,
    name: str,
    date_of_birth: str,
    country_code: str | None = None,
    handle: str | None = None,
    club_slug: str | None = None,
    client: FanmootClient | None = None,
) -> AuthResult:
    """``POST /v1/auth/register`` — a brand new fan, created inside the flow (`S4-11`).

    This is the creator's explicit requirement: someone who has never heard of
    Fanmoot should be able to say "Alexa, post to Fanmoot" and come out the other
    side with a live account, without leaving the linking flow.

    ``date_of_birth`` is required upstream and the minimum age depends on the
    country, which is why the sign-up form asks for both. Fanmoot answers a
    known address with a 409 and emails the real owner a sign-in link rather than
    confirming the account exists; that refusal is passed through as
    ``FanmootRejected`` with Fanmoot's own wording, and the form shows it.
    """
    payload: dict[str, Any] = {
        "email": email,
        "password": password,
        "name": name,
        "date_of_birth": date_of_birth,
    }
    if country_code:
        payload["country_code"] = country_code
    if handle:
        payload["handle"] = handle
    if club_slug:
        payload["club_slug"] = club_slug

    reply = await (client or get_client()).post("/v1/auth/register", json=payload, params=_IN_BODY)
    return AuthResult.model_validate(reply.data)


async def refresh_session(*, refresh_token: str, client: FanmootClient | None = None) -> AuthTokens:
    """``POST /v1/auth/refresh`` — the other half of the token exchange (`S4-8`).

    Fanmoot rotates: this returns a **new** refresh token as well as an access
    token, and the old one is spent. The caller must store what comes back or
    the link is broken on the next turn — and presenting a spent token there
    revokes the whole family upstream, which is Fanmoot's own theft defence and
    not something to trip over twice.
    """
    reply = await (client or get_client()).post(
        "/v1/auth/refresh", json={"refresh_token": refresh_token}, params=_IN_BODY
    )
    return AuthTokens.model_validate(reply.data)


async def logout(*, refresh_token: str, client: FanmootClient | None = None) -> None:
    """``POST /v1/auth/logout`` — end the upstream session.

    Called when a fan disconnects a client (`S4-13`). Best-effort by nature:
    this server's own grant is revoked whatever happens here, and upstream
    answers 200 whether or not the token was still valid.
    """
    await (client or get_client()).post("/v1/auth/logout", json={"refresh_token": refresh_token})


# ── The signed-in fan (S5's tools call these) ────────────────────────────────
async def me(*, token: str, client: FanmootClient | None = None) -> UserResponse:
    """``GET /v1/me`` — never cached, because it is one specific person."""
    reply = await (client or get_client()).get("/v1/me", token=token)
    return UserResponse.model_validate(reply.data)


async def my_club(*, token: str, client: FanmootClient | None = None) -> MembershipOut:
    """``GET /v1/me/club`` — which club this fan is signed to."""
    reply = await (client or get_client()).get("/v1/me/club", token=token)
    return MembershipOut.model_validate(reply.data)


async def public_profile(
    handle: str, *, token: str | None = None, client: FanmootClient | None = None
) -> PublicProfile:
    """``GET /v1/users/{handle}`` — the public side of an account.

    ``whoami`` reads it for the follower counts, which ``/v1/me`` does not
    carry, and runs it alongside the other two rather than after them: three
    requests in parallel cost one round trip, three in sequence cost three
    (HAD-6).

    Passed the fan's own token on purpose: upstream counts a profile view and
    explicitly never counts the fan's own, so asking as them is the version of
    this call that does not inflate their own number.
    """
    reply = await (client or get_client()).get(f"/v1/users/{handle}", token=token)
    return PublicProfile.model_validate(reply.data)


async def compose_moot(
    *,
    token: str,
    body: str,
    parent_id: uuid.UUID | str | None = None,
    quote_of_id: uuid.UUID | str | None = None,
    match_id: uuid.UUID | str | None = None,
    idempotency_key: str | None = None,
    client: FanmootClient | None = None,
) -> ComposeResponse:
    """``POST /v1/moots`` — post, reply or quote.

    ``idempotency_key`` is a **query parameter** upstream, not a header, and it
    is not optional in practice: a voice retry is a real event and a duplicate
    take is embarrassing (§5). It is scoped to the author over there, so two
    fans cannot collide on one, and a retry is answered with the moot the first
    attempt created rather than a conflict.

    The answer carries ``held`` and ``gate_reason`` when Fanmoot's
    football-topicality gate holds the take for review. Callers surface both
    verbatim; we never report a post as live when it is pending (HAD-7).
    """
    payload: dict[str, Any] = {"body": body}
    if parent_id:
        payload["parent_id"] = str(parent_id)
    if quote_of_id:
        payload["quote_of_id"] = str(quote_of_id)
    if match_id:
        payload["match_id"] = str(match_id)

    reply = await (client or get_client()).post(
        "/v1/moots",
        json=payload,
        params={"idempotency_key": idempotency_key},
        token=token,
    )
    return ComposeResponse.model_validate(reply.data)


async def react_to_moot(
    moot_id: uuid.UUID | str, *, token: str, kind: str, client: FanmootClient | None = None
) -> ReactionState:
    """``POST /v1/moots/{id}/react`` — set, change or clear.

    One call does all three: reacting with the kind already chosen clears it.
    """
    reply = await (client or get_client()).post(
        f"/v1/moots/{moot_id}/react", json={"kind": kind}, token=token
    )
    return ReactionState.model_validate(reply.data)


async def predict_match(
    *,
    token: str,
    match_id: uuid.UUID | str,
    home_score: int,
    away_score: int,
    client: FanmootClient | None = None,
) -> PredictionOut:
    """``POST /v1/predictions`` — call a scoreline before kickoff."""
    reply = await (client or get_client()).post(
        "/v1/predictions",
        json={"match_id": str(match_id), "home_score": home_score, "away_score": away_score},
        token=token,
    )
    return PredictionOut.model_validate(reply.data)


async def open_predictions(
    *, token: str, client: FanmootClient | None = None
) -> list[OpenMatchOut]:
    """``GET /v1/predictions/open`` — fixtures still open to a call."""
    reply = await (client or get_client()).get("/v1/predictions/open", token=token)
    return _OPEN_MATCHES.validate_python(reply.data)


async def my_predictions(
    *, token: str, client: FanmootClient | None = None
) -> list[MyPredictionOut]:
    """``GET /v1/predictions/mine`` — what this fan has called."""
    reply = await (client or get_client()).get("/v1/predictions/mine", token=token)
    return _MY_PREDICTIONS.validate_python(reply.data)


async def follow(
    target_type: str, identifier: str, *, token: str, client: FanmootClient | None = None
) -> FollowState:
    """``POST /v1/follow/{type}/{id}`` — a club, a competition or a fan.
    Idempotent upstream."""
    reply = await (client or get_client()).post(
        f"/v1/follow/{target_type}/{identifier}", token=token
    )
    return FollowState.model_validate(reply.data)
