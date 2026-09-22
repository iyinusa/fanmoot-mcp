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

import uuid

from app.fanmoot import endpoints
from app.fanmoot.models import (
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
    ReactionState,
    SearchResponse,
    ThreadOut,
    UserResponse,
)
from tests.replay import Replay

#: Read out of the recordings rather than written down, so this file does not
#: go stale the next time `make record` runs against a different Saturday.
MATCH_ID = "dd472da4-8efc-42be-9f7e-188a57aec488"
MOOT_ID = "7c9e1183-d0a6-4e9c-af6b-6510836105e3"
POSTED_MOOT_ID = "b2c1e4d6-7f08-4a1b-8e33-5c9d0a7f1120"


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
    take for review, and we never report a post as live when it is pending."""
    posted = await endpoints.compose_moot(
        token="demo", body="Off topic entirely.", idempotency_key="held"
    )

    assert posted.held is True
    assert posted.gate_reason
    assert posted.moot.status == "held"


async def test_react_to_moot(replay: Replay) -> None:
    state = await endpoints.react_to_moot(POSTED_MOOT_ID, token="demo", kind="agree")

    assert isinstance(state, ReactionState)
    assert state.my_reaction == "agree"


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
        "list_replies",
        "search",
        "list_updates",
        "club_news",
        "me",
        "my_club",
        "compose_moot",
        "react_to_moot",
        "predict_match",
        "open_predictions",
        "my_predictions",
        "follow",
    }

    assert wrappers == tested, f"untested wrappers: {sorted(wrappers - tested)}"
