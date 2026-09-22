"""``S3-2`` — turning what a fan said into a club, or into a question.

Driven through the protocol against the recorded catalogue: a thousand real
clubs, with the real collisions in them. "United" is ambiguous here because
Fanmoot really does carry Adamawa United, Akwa United and Atlanta United, not
because a fixture was arranged to make it so.
"""

from __future__ import annotations

from tests.replay import Replay
from tests.tooling import call


async def test_a_nickname_resolves_to_the_club(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "find_club", name="the gunners")

    assert payload["resolved"] is True
    assert payload["club"]["slug"] == "arsenal"
    assert spoken.startswith("Arsenal, The Gunners, in the Premier League.")


async def test_a_misspelling_still_lands(replay: Replay, mcp_url: str) -> None:
    """ "arsnal" is what a transcript produces, and a tool that refuses it
    spends a turn the fan did not have to spend."""
    _, payload = await call(mcp_url, "find_club", name="arsnal")

    assert payload["club"]["slug"] == "arsenal"


async def test_an_ambiguous_name_comes_back_as_a_question(replay: Replay, mcp_url: str) -> None:
    """The candidates are *data*, not an error: ``find_club`` is the question,
    so a client can render them as buttons and the model can ask which."""
    spoken, payload = await call(mcp_url, "find_club", name="united")

    assert payload["resolved"] is False
    assert len(payload["choices"]) > 1
    assert spoken.startswith("Do you mean ")
    assert all(choice["hint"] for choice in payload["choices"])


async def test_a_name_nobody_has_says_so_plainly(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "find_club", name="qqqq")

    assert payload["resolved"] is False
    assert payload["choices"] == []
    assert spoken == "I cannot find a club called qqqq on Fanmoot."


async def test_a_resolved_club_carries_what_a_client_renders(replay: Replay, mcp_url: str) -> None:
    _, payload = await call(mcp_url, "find_club", name="arsenal")
    club = payload["club"]

    assert club["competition"] == "Premier League"
    assert club["stadium"] == "Emirates Stadium"
    # Absolute, because a client of this server is not on fanmoot.com's origin
    # and a relative crest path there is a broken image.
    assert club["crest_url"].startswith("https://")


async def test_a_competition_resolves_with_its_clubs(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "find_competition", name="the prem")

    assert payload["resolved"] is True
    assert payload["competition"]["slug"] == "premier-league"
    assert payload["competition"]["club_count"] > 15
    assert any(club["slug"] == "arsenal" for club in payload["clubs"])
    assert spoken.startswith("Premier League,")


async def test_an_unknown_competition_asks_nothing_it_cannot_ask(
    replay: Replay, mcp_url: str
) -> None:
    spoken, payload = await call(mcp_url, "find_competition", name="the interplanetary cup")

    assert payload["resolved"] is False
    assert spoken.startswith("I cannot find a competition called")
