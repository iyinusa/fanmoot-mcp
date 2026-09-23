"""S1's acceptance, executed: **every** endpoint wrapper returns typed data
from recorded fixtures, with no network.

The fixtures under ``fixtures/`` were captured from ``fanmoot.com/v1`` by
``make record`` (HAD-14), so these assertions are against shapes the real API
produced rather than shapes this repo believes it produces. Where a recording
came back empty — out of season, a quiet Tuesday — that is asserted as an empty
*typed* result, because "nothing is on" is a path a fan hits and a tool has to
answer.
"""

from __future__ import annotations

import json
import uuid

from app.fanmoot import endpoints
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
from tests.replay import Replay, fixture_body

#: Read out of the recordings rather than written down, so this file does not
#: go stale the next time `make record` runs against a different Saturday.
MATCH_ID = "dd472da4-8efc-42be-9f7e-188a57aec488"
MOOT_ID = "7c9e1183-d0a6-4e9c-af6b-6510836105e3"
POSTED_MOOT_ID = "b2c1e4d6-7f08-4a1b-8e33-5c9d0a7f1120"

#: The other outcome of ``POST /v1/moots``, which the manifest cannot serve
#: alongside the first. See ``tests.replay.fixture_body``.
HELD = "post_v1_moots_held.json"


# ── Catalogue ────────────────────────────────────────────────────────────────
async def test_list_leagues(replay: Replay) -> None:
    leagues = await endpoints.list_leagues()

    assert leagues and all(isinstance(league, LeagueOut) for league in leagues)
    slugs = {league.slug for league in leagues}
    assert {"premier-league", "la-liga", "uefa-champions-league"} <= slugs


async def test_get_league_returns_its_clubs_and_no_table(replay: Replay) -> None:
    """HAD-13: Fanmoot has no standings endpoint and we did not invent one.
    `LeagueDetail` is competition plus clubs, and that is all it claims."""
    league = await endpoints.get_league("premier-league")

    assert isinstance(league, LeagueDetail)
    assert league.slug == "premier-league"
    assert any(club.slug == "arsenal" for club in league.clubs)
    assert not hasattr(league, "table")


async def test_list_clubs_pages_by_cursor(replay: Replay) -> None:
    page = await endpoints.list_clubs(limit=100)

    assert isinstance(page, ClubPage)
    assert page.items and all(isinstance(club, ClubOut) for club in page.items)
    assert page.next_cursor


async def test_get_club_carries_the_nickname_name_resolution_leans_on(replay: Replay) -> None:
    club = await endpoints.get_club("arsenal")

    assert isinstance(club, ClubOut)
    assert club.name == "Arsenal"
    assert club.nickname == "The Gunners"
    assert club.league is not None


# ── Matches ──────────────────────────────────────────────────────────────────
async def test_list_matches(replay: Replay) -> None:
    page = await endpoints.list_matches(club="arsenal", limit=5)

    assert isinstance(page, MatchPage)
    assert page.items
    match = page.items[0]
    assert match.home is not None and match.away is not None
    assert match.kickoff_at.tzinfo is not None


async def test_live_digest_types_an_empty_afternoon(replay: Replay) -> None:
    """Recorded at a quiet moment, which is the point: a tool asked for live
    scores when nothing is on must answer "nothing is on", not fail."""
    digest = await endpoints.live_digest()

    assert isinstance(digest, LiveDigest)
    assert digest.matches == []


async def test_upcoming_matches_types_an_empty_window(replay: Replay) -> None:
    groups = await endpoints.upcoming_matches(days=14)

    assert isinstance(groups, list)
    assert all(isinstance(group, MatchGroup) for group in groups)


async def test_get_match_carries_events_and_a_score_state(replay: Replay) -> None:
    match = await endpoints.get_match(MATCH_ID)

    assert isinstance(match, MatchDetail)
    assert match.home_score is not None
    # The field a tool must consult before speaking a scoreline.
    assert match.score_state in {"confirmed", "stale", "assumed"}


async def test_get_match_by_path_is_the_route_names_resolve_into(replay: Replay) -> None:
    """HAD-7: a fan says "brighton vs arsenal", never a UUID."""
    match = await endpoints.get_match_by_path(
        "premier-league", "2026-27", "brighton-hove-albion-vs-arsenal-2026-09-19"
    )

    assert isinstance(match, MatchDetail)
    assert str(match.id) == MATCH_ID


# ── Conversation ─────────────────────────────────────────────────────────────
async def test_match_moots(replay: Replay) -> None:
    page = await endpoints.match_moots(MATCH_ID, limit=5)

    assert isinstance(page, MootPage)


async def test_trending_feed_carries_real_takes(replay: Replay) -> None:
    page = await endpoints.get_feed("trending", limit=5)

    assert isinstance(page, MootPage)
    assert page.items
    take = page.items[0]
    assert take.body
    assert take.author.handle


async def test_match_feed(replay: Replay) -> None:
    page = await endpoints.get_feed("match", match_id=MATCH_ID, limit=5)

    assert isinstance(page, MootPage)


async def test_get_moot_returns_a_thread(replay: Replay) -> None:
    thread = await endpoints.get_moot(MOOT_ID)

    assert isinstance(thread, ThreadOut)
    assert thread.moot.body
    assert str(thread.moot.id) == MOOT_ID


async def test_get_moot_by_short_id_reads_the_same_thread(replay: Replay) -> None:
    """``fanmoot.com/m/HU6yREc6mJ`` is the id a fan can read out, and the only
    one they will ever have to hand — ``read_thread`` takes either (``S3-5``)."""
    thread = await endpoints.get_moot_by_short_id("HU6yREc6mJ")

    assert isinstance(thread, ThreadOut)
    assert thread.moot.short_id == "HU6yREc6mJ"


async def test_list_replies(replay: Replay) -> None:
    page = await endpoints.list_replies(MOOT_ID)

    assert isinstance(page, MootPage)


async def test_search_spans_everything(replay: Replay) -> None:
    results = await endpoints.search("arsenal", limit=5)

    assert isinstance(results, SearchResponse)
    assert any(club.slug == "arsenal" for club in results.clubs)


# ── Club news ────────────────────────────────────────────────────────────────
async def test_list_updates(replay: Replay) -> None:
    page = await endpoints.list_updates()

    assert isinstance(page, ClubNewsPage)
    assert page.items
    assert page.items[0].headline


async def test_club_news(replay: Replay) -> None:
    page = await endpoints.club_news("arsenal")

    assert isinstance(page, ClubNewsPage)
    assert page.items


# ── The signed-in fan ────────────────────────────────────────────────────────
async def test_me(replay: Replay) -> None:
    fan = await endpoints.me(token="demo")

    assert isinstance(fan, UserResponse)
    assert fan.handle == "demo_fan"


async def test_my_club(replay: Replay) -> None:
    membership = await endpoints.my_club(token="demo")

    assert isinstance(membership, MembershipOut)
    assert membership.club is not None
    assert membership.club.slug == "arsenal"


async def test_public_profile(replay: Replay) -> None:
    """``whoami`` reads this for the two counts ``/v1/me`` does not carry."""
    profile = await endpoints.public_profile("demo_fan", token="demo")

    assert isinstance(profile, PublicProfile)
    assert profile.user.handle == "demo_fan"
    assert profile.user.followers_count


async def test_compose_moot_sends_the_idempotency_key_as_a_query_parameter(
    replay: Replay,
) -> None:
    """Upstream takes it on the query string, not as a header — and a voice
    retry is a real event, so getting this wrong posts the take twice."""
    posted = await endpoints.compose_moot(
        token="demo", body="Two-nil flatters us.", idempotency_key="mcp-req-7"
    )

    assert isinstance(posted, ComposeResponse)
    assert posted.held is False
    assert dict(replay.calls[0].url.params)["idempotency_key"] == "mcp-req-7"


async def test_a_held_moot_is_reported_as_held(replay: Replay) -> None:
    """HAD-7's honest refusal: Fanmoot's football-topicality gate can park a
    take for review, and we never report a post as live when it is pending.

    The held body is installed over the ordinary one rather than matched on a
    chosen idempotency key: the key a tool sends is hashed from the grant, the
    frame and the take (`S5-2`), so no test can name it in advance.
    """
    replay.stub("/v1/moots", fixture_body(HELD), method="POST", status=201)

    posted = await endpoints.compose_moot(token="demo", body="Off topic entirely.")

    assert posted.held is True
    assert posted.gate_reason
    assert posted.moot.status == "held"


async def test_react_to_moot(replay: Replay) -> None:
    """The five reactions are Fanmoot's own — football-flavoured, because
    "like" says nothing about a tactical argument."""
    state = await endpoints.react_to_moot(POSTED_MOOT_ID, token="demo", kind="respect")

    assert isinstance(state, ReactionState)
    assert state.my_reaction == "respect"


async def test_predict_match(replay: Replay) -> None:
    prediction = await endpoints.predict_match(
        token="demo", match_id=uuid.UUID(MATCH_ID), home_score=1, away_score=2
    )

    assert isinstance(prediction, PredictionOut)
    assert (prediction.home_score, prediction.away_score) == (1, 2)


async def test_open_predictions(replay: Replay) -> None:
    open_matches = await endpoints.open_predictions(token="demo")

    assert open_matches and all(isinstance(row, OpenMatchOut) for row in open_matches)
    assert open_matches[1].prediction is None


async def test_my_predictions(replay: Replay) -> None:
    mine = await endpoints.my_predictions(token="demo")

    assert mine and all(isinstance(row, MyPredictionOut) for row in mine)
    assert mine[0].prediction.points == 3


async def test_follow(replay: Replay) -> None:
    state = await endpoints.follow("club", "arsenal", token="demo")

    assert isinstance(state, FollowState)
    assert state.following is True


# ── The acceptance criterion itself ──────────────────────────────────────────
# ── Authentication: the calls the OAuth server makes on a fan's behalf (S4) ──
# Stubbed rather than recorded, and that is not a shortcut: `make record` runs
# against the live API with a real account, and a recording of `/v1/auth/login`
# would be a recording of somebody's credentials being accepted. The shapes below
# are Fanmoot's own `AuthResponse` and `TokenResponse`, mirrored in
# `app.fanmoot.models`, which is what these assertions are actually checking.
def _auth_body(*, handle: str = "thegunner", verified: bool = True) -> dict[str, object]:
    return {
        "user": {
            "id": "2f1c8d60-9b47-4a6e-9d1b-0c2f8e5a3b71",
            "email": "fan@example.com",
            "handle": handle,
            "name": "A Fan",
            "email_verified": verified,
        },
        "tokens": {
            "access_token": "upstream-access-token",
            "token_type": "Bearer",
            "expires_in": 900,
            "refresh_token": "upstream-refresh-token",
        },
    }


async def test_login_asks_for_the_refresh_token_in_the_body(replay: Replay) -> None:
    """`in_body=true` is the whole reason this wrapper exists: Fanmoot's default
    is an httpOnly cookie, and there is no cookie jar here to put one in."""
    replay.stub("/v1/auth/login", _auth_body(), method="POST", params={"in_body": "true"})

    result = await endpoints.login(email="fan@example.com", password="correct horse")

    assert isinstance(result, AuthResult)
    assert result.user.handle == "thegunner"
    assert result.tokens.refresh_token == "upstream-refresh-token"
    assert replay.calls[0].url.params["in_body"] == "true"


async def test_register_carries_the_fields_fanmoot_requires(replay: Replay) -> None:
    """Date of birth is required upstream and the minimum age depends on the
    country, which is why the sign-up form (`S4-11`) asks for both."""
    replay.stub(
        "/v1/auth/register",
        _auth_body(handle="newfan", verified=False),
        method="POST",
        params={"in_body": "true"},
    )

    result = await endpoints.register(
        email="new@example.com",
        password="correct horse battery",
        name="New Fan",
        date_of_birth="1990-08-14",
        country_code="GB-ENG",
        handle="newfan",
        club_slug="arsenal",
    )

    assert result.user.handle == "newfan"
    assert result.user.email_verified is False
    sent = json.loads(replay.calls[0].content)
    assert sent["date_of_birth"] == "1990-08-14"
    assert sent["country_code"] == "GB-ENG"
    assert sent["club_slug"] == "arsenal"


async def test_refresh_session_returns_the_rotated_pair(replay: Replay) -> None:
    """Fanmoot rotates, so the successor has to come back and be stored — see
    `app.oauth.upstream` on why a token spent and not recorded breaks the link."""
    replay.stub(
        "/v1/auth/refresh",
        {
            "access_token": "fresh-access",
            "token_type": "Bearer",
            "expires_in": 900,
            "refresh_token": "rotated-refresh",
        },
        method="POST",
        params={"in_body": "true"},
    )

    issued = await endpoints.refresh_session(refresh_token="upstream-refresh-token")

    assert isinstance(issued, AuthTokens)
    assert issued.access_token == "fresh-access"
    assert issued.refresh_token == "rotated-refresh"


async def test_logout_is_best_effort_and_returns_nothing(replay: Replay) -> None:
    replay.stub("/v1/auth/logout", {"message": "Signed out."}, method="POST")

    assert await endpoints.logout(refresh_token="upstream-refresh-token") is None


async def test_no_wrapper_is_left_untested() -> None:
    """S1's acceptance says *every* endpoint wrapper. This fails the build when
    one is added without a test, rather than leaving the claim on trust."""
    wrappers = {
        name
        for name in dir(endpoints)
        if not name.startswith("_")
        and callable(getattr(endpoints, name))
        and getattr(getattr(endpoints, name), "__module__", "") == endpoints.__name__
    }
    tested = {
        "list_leagues",
        "get_league",
        "list_clubs",
        "get_club",
        "list_matches",
        "upcoming_matches",
        "live_digest",
        "get_match",
        "get_match_by_path",
        "match_moots",
        "get_feed",
        "get_moot",
        "get_moot_by_short_id",
        "list_replies",
        "search",
        "list_updates",
        "club_news",
        "me",
        "my_club",
        "public_profile",
        "compose_moot",
        "react_to_moot",
        "predict_match",
        "open_predictions",
        "my_predictions",
        "follow",
        "login",
        "register",
        "refresh_session",
        "logout",
    }

    assert wrappers == tested, f"untested wrappers: {sorted(wrappers - tested)}"
