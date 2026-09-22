"""``S3-5`` — the match thread, and one argument inside it.

This is the half of Fanmoot no other football service has, so these tests care
about more than the data arriving: a take is attributed, a long one is trimmed,
a quiet thread says it is quiet, and the room is summarised before anybody is
quoted.
"""

from __future__ import annotations

from app import voice
from tests import scenarios
from tests.replay import Replay
from tests.tooling import call, refusal

MOOT_ID = "7c9e1183-d0a6-4e9c-af6b-6510836105e3"
SHORT_ID = "HU6yREc6mJ"


def _thread_of(match: dict[str, object], takes: list[dict[str, object]], replay: Replay) -> None:
    """Install one match thread, for both the ranked and the chronological read."""
    replay.stub(f"/v1/matches/{match['id']}", match)
    replay.stub(
        f"/v1/matches/{match['id']}/moots",
        scenarios.moot_page(takes),
        params={"limit": 5},
    )
    replay.stub(
        "/v1/feed/match",
        scenarios.moot_page(list(reversed(takes))),
        params={"match_id": match["id"], "limit": 5},
    )


# ── read_match_conversation ──────────────────────────────────────────────────
async def test_the_room_is_summarised_then_two_takes_are_quoted(
    replay: Replay, mcp_url: str
) -> None:
    match = scenarios.match(now=voice.now_utc(), moot_count=3)
    _thread_of(
        match,
        [
            scenarios.moot("That second goal was offside, all day.", name="Jo"),
            scenarios.moot("Best we've played all season.", handle="ade", name="Ade"),
            scenarios.moot("Referee has been poor both ways.", handle="sam", name="Sam"),
        ],
        replay,
    )

    spoken, payload = await call(mcp_url, "read_match_conversation", match=match["id"])

    assert spoken.startswith("Three takes on Arsenal against Spurs.")
    assert "Jo says: That second goal was offside, all day." in spoken
    assert "Ade says: Best we've played all season." in spoken
    # Three takes, two quoted: a thread read out in full is unlistenable.
    assert "Sam says" not in spoken
    assert payload["count"] == 3
    assert payload["view"] == "top"


async def test_the_thread_comes_back_with_the_match_it_is_under(
    replay: Replay, mcp_url: str
) -> None:
    match = scenarios.match(now=voice.now_utc())
    _thread_of(match, [scenarios.moot("Get in.")], replay)
    # Named rather than identified: the fan says "arsenal vs spurs" and the
    # match is found in the club's own fixture window (⟵ `S1-5`, HAD-7).
    replay.stub(
        "/v1/matches", scenarios.match_page([match]), params={"club": "arsenal", "limit": 5}
    )

    _, payload = await call(mcp_url, "read_match_conversation", match="arsenal vs spurs")

    assert payload["match"]["id"] == match["id"]
    assert payload["takes"][0]["handle"] == "gooner_jo"
    assert payload["takes"][0]["spoken"] == "Jo says: Get in."


async def test_newest_and_both_read_the_thread_a_different_way(
    replay: Replay, mcp_url: str
) -> None:
    """``both`` interleaves the ranked read with the chronological one and
    de-duplicates — a take that is the loudest *and* the latest is quoted
    once."""
    match = scenarios.match(now=voice.now_utc())
    takes = [
        scenarios.moot("First.", moot_id="dddddddd-0000-4000-8000-000000000001"),
        scenarios.moot("Second.", moot_id="dddddddd-0000-4000-8000-000000000002"),
    ]
    _thread_of(match, takes, replay)

    _, newest = await call(mcp_url, "read_match_conversation", match=match["id"], view="newest")
    _, both = await call(mcp_url, "read_match_conversation", match=match["id"], view="both")

    assert [take["body"] for take in newest["takes"]] == ["Second.", "First."]
    assert len(both["takes"]) == 2
    assert {take["body"] for take in both["takes"]} == {"First.", "Second."}


async def test_a_bounded_thread_says_how_much_of_it_was_not_read(
    replay: Replay, mcp_url: str
) -> None:
    """``S3-9``. The match knows its own thread size, so the hint is the real
    number rather than "and some more"."""
    match = scenarios.match(now=voice.now_utc(), moot_count=40)
    _thread_of(match, [scenarios.moot(f"take {n}") for n in range(5)], replay)

    spoken, payload = await call(mcp_url, "read_match_conversation", match=match["id"])

    assert payload["has_more"] is True
    assert spoken.endswith("There are thirty-five more takes.")


async def test_a_quiet_thread_is_an_answer(replay: Replay, mcp_url: str) -> None:
    match = scenarios.match(now=voice.now_utc())
    _thread_of(match, [], replay)

    spoken, payload = await call(mcp_url, "read_match_conversation", match=match["id"])

    assert payload["takes"] == []
    assert spoken == "Nobody has said anything about Arsenal against Spurs yet."


# ── read_thread ──────────────────────────────────────────────────────────────
async def test_a_thread_reads_by_id_or_by_the_link_a_fan_has(replay: Replay, mcp_url: str) -> None:
    """``fanmoot.com/m/HU6yREc6mJ`` is the id that appears in a share sheet,
    and the only one a fan will ever have to hand."""
    by_id = await call(mcp_url, "read_thread", take=MOOT_ID)
    by_short = await call(mcp_url, "read_thread", take=SHORT_ID)
    by_link = await call(mcp_url, "read_thread", take=f"https://fanmoot.com/m/{SHORT_ID}")

    assert by_id[1]["take"]["id"] == by_short[1]["take"]["id"] == by_link[1]["take"]["id"]
    assert by_id[1]["take"]["short_id"] == SHORT_ID


async def test_a_thread_with_replies_leads_with_the_take_then_the_answer(
    replay: Replay, mcp_url: str
) -> None:
    root = scenarios.moot("The manager has to go.", handle="ade", name="Ade")
    replies = [
        scenarios.moot("Absolute nonsense.", handle="sam", name="Sam"),
        scenarios.moot("He's had no money.", handle="kit", name="Kit"),
    ]
    replay.stub(f"/v1/moots/{root['id']}", scenarios.thread(root, replies=replies, reply_total=9))

    spoken, payload = await call(mcp_url, "read_thread", take=str(root["id"]))

    assert spoken.startswith("Ade says: The manager has to go. Two replies so far.")
    assert "Sam says: Absolute nonsense." in spoken
    assert spoken.endswith("There are seven more replies.")
    assert payload["reply_total"] == 9
    assert payload["has_more"] is True


async def test_a_take_nobody_answered_says_so(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "read_thread", take=MOOT_ID)

    assert payload["replies"] == []
    assert spoken.endswith("Nobody has replied yet.")


async def test_a_reply_carries_what_it_was_replying_to(replay: Replay, mcp_url: str) -> None:
    parent = scenarios.moot("Offside, clearly.", handle="ade", name="Ade")
    reply = scenarios.moot("It was level!", handle="sam", name="Sam")
    replay.stub(f"/v1/moots/{reply['id']}", scenarios.thread(reply, ancestors=[parent]))

    _, payload = await call(mcp_url, "read_thread", take=str(reply["id"]))

    assert payload["replying_to"]["body"] == "Offside, clearly."


async def test_something_that_is_not_a_take_is_answered_not_crashed(
    replay: Replay, mcp_url: str
) -> None:
    spoken = await refusal(mcp_url, "read_thread", take="what did they say about the goal?")

    assert spoken == "That does not look like a Fanmoot take. Say the link again?"
