"""``S3-6`` — search, the running argument, and the bulletin.

The three tools a model reaches for when nothing else fits. All three replay
the real corpus: a search for "arsenal" really does come back with Arsenal,
Braga and Bendel Insurance, which is a better test of what a fan hears than any
fixture anybody would have invented.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests import scenarios
from tests.replay import Replay
from tests.tooling import call

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ── search_fanmoot ───────────────────────────────────────────────────────────
async def test_a_search_says_what_it_found_rather_than_reading_it_all_out(
    replay: Replay, mcp_url: str
) -> None:
    spoken, payload = await call(mcp_url, "search_fanmoot", query="arsenal")

    # Braga and Bendel Insurance come back too, but a fan who said "arsenal"
    # named Arsenal: reading the other two aloud is heard as "which one?".
    assert spoken == "Found Arsenal on Fanmoot, in the Premier League."
    assert payload["count"] == 3
    assert [club["slug"] for club in payload["clubs"]][0] == "arsenal"


async def test_a_search_for_a_country_does_not_offer_its_neighbour(
    replay: Replay, mcp_url: str
) -> None:
    """Found in the Alexa demo: "what's happening with Slovenia today" came
    back as "Slovenia, Slovakia, Slovan Bratislava, or the Slovenian PrvaLiga?
    Which one were you looking for?" """
    catalogue = json.loads((FIXTURES / "get_v1_clubs_8dec12f3.json").read_text())
    clubs = {club["slug"]: club for club in catalogue["items"]}
    replay.stub(
        "/v1/search",
        {
            "query": "Slovenia",
            "clubs": [clubs["slovakia"], clubs["slovenia"]],
            "leagues": [{"slug": "slovenian-prvaliga", "name": "Slovenian PrvaLiga"}],
            "people": [],
            "moots": [],
            "matches": [],
        },
        params={"q": "Slovenia", "limit": 5},
    )

    spoken, payload = await call(mcp_url, "search_fanmoot", query="Slovenia")

    assert spoken.startswith("Found Slovenia on Fanmoot")
    assert "Slovakia" not in spoken
    assert "PrvaLiga" not in spoken
    # Still in the data, behind the one that was named.
    assert [club["slug"] for club in payload["clubs"]] == ["slovenia", "slovakia"]


async def test_a_search_that_finds_nothing_says_nothing_matched(
    replay: Replay, mcp_url: str
) -> None:
    replay.stub(
        "/v1/search",
        {"query": "qqqq", "clubs": [], "leagues": [], "people": [], "moots": [], "matches": []},
        params={"q": "qqqq", "limit": 5},
    )

    spoken, payload = await call(mcp_url, "search_fanmoot", query="qqqq")

    assert payload["count"] == 0
    assert spoken == "Nothing on Fanmoot matches qqqq."


async def test_a_search_carries_every_kind_of_thing_it_found(replay: Replay, mcp_url: str) -> None:
    replay.stub(
        "/v1/search",
        {
            "query": "saka",
            "clubs": [scenarios.ARSENAL],
            "leagues": [{"slug": "premier-league", "name": "Premier League"}],
            "people": [
                {
                    "id": "eeeeeeee-0000-4000-8000-000000000001",
                    "handle": "saka_fan",
                    "name": "Bukayo Watcher",
                    "club": scenarios.ARSENAL,
                }
            ],
            "moots": [scenarios.moot("Saka was unplayable.")],
            "matches": [],
        },
        params={"q": "saka", "limit": 5},
    )

    spoken, payload = await call(mcp_url, "search_fanmoot", query="saka")

    assert payload["competitions"][0]["slug"] == "premier-league"
    assert payload["people"][0]["handle"] == "saka_fan"
    assert payload["takes"][0]["body"] == "Saka was unplayable."
    assert spoken.startswith("For saka: one club, Arsenal, one competition")


# ── trending_takes ───────────────────────────────────────────────────────────
async def test_trending_reads_the_room_and_quotes_two_of_it(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "trending_takes")

    assert payload["count"] == 5
    assert spoken.startswith("Five takes on Fanmoot right now.")
    assert " says: " in spoken
    # Every take carries the fan who posted it, because the model is told to
    # attribute anything it quotes.
    assert all(take["handle"] for take in payload["takes"])


async def test_trending_is_bounded_like_everything_else(replay: Replay, mcp_url: str) -> None:
    replay.stub(
        "/v1/feed/trending",
        scenarios.moot_page([scenarios.moot(f"take {n}") for n in range(2)], next_cursor="more"),
        params={"limit": 2},
    )

    _, payload = await call(mcp_url, "trending_takes", limit=2)

    assert payload["count"] == 2
    # A cursor upstream is more takes, even when this page was not itself full.
    assert payload["has_more"] is True


# ── club_news ────────────────────────────────────────────────────────────────
async def test_club_news_is_read_as_a_bulletin(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "club_news", club="arsenal")

    assert payload["club"] == "Arsenal"
    assert payload["count"] == 5
    assert spoken.startswith("Five stories for Arsenal.")
    assert payload["has_more"] is True


async def test_news_across_fanmoot_when_no_club_is_named(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "club_news")

    assert payload["club"] is None
    assert spoken.startswith("Five stories for Fanmoot.")


async def test_a_story_links_to_where_fans_are_arguing_about_it(
    replay: Replay, mcp_url: str
) -> None:
    """The point of carrying news at all: each item points at the take
    ``read_thread`` will follow."""
    _, payload = await call(mcp_url, "club_news", club="arsenal")
    story = payload["items"][0]

    assert story["headline"]
    assert story["source"]
    assert story["take_url"].startswith("https://")
