"""``S5-1``, ``S5-5``, ``S5-6`` — the fan, their predictions, and what they follow.

The four tools that act *for* a fan without putting words in front of anybody:
who they are, what they have called, and what they choose to pay attention to.

Like ``test_tools_compose``, every one of these runs the whole path — a real
authorization, a real token, a real exchange — because the thing worth
asserting is not that a fixture parses but that the right fan's credential
reached the right endpoint.
"""

from __future__ import annotations

from tests.replay import Replay
from tests.tooling import call, refusal

MATCH_ID = "dd472da4-8efc-42be-9f7e-188a57aec488"


# ── whoami (S5-1) ────────────────────────────────────────────────────────────
async def test_whoami_answers_with_the_handle_the_club_and_the_standing(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """The question a fan asks to check the link worked, so the answer has to
    be the three things they would use to check it."""
    spoken, payload = await call(mcp_url, "whoami", token=linked_token)

    assert payload["handle"] == "demo_fan"
    assert payload["club"]["name"] == "Arsenal"
    assert payload["tenure_days"] == 52
    assert payload["followers"] == 18
    assert spoken.startswith("You are @demo_fan, an Arsenal fan for fifty-two days.")
    assert "follow you" in spoken


async def test_whoami_reads_its_three_sources_in_one_go(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """HAD-6's budget. Three requests in parallel cost one round trip; three in
    sequence cost three, inside a turn that has a few hundred milliseconds.

    The fourth path below is the token exchange (`S4-8`), which happens once
    per grant and is cached for the rest of the fan's conversation.
    """
    before = len(replay.calls)

    await call(mcp_url, "whoami", token=linked_token)

    asked = {c.url.path for c in replay.calls[before:]}
    assert asked == {"/v1/auth/refresh", "/v1/me", "/v1/me/club", "/v1/users/demo_fan"}


async def test_whoami_survives_a_profile_it_cannot_read(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """The counts are pleasant to hear and are not the answer to "who am I",
    so a profile route that is unavailable must not take the handle with it."""
    replay.stub("/v1/users/demo_fan", {"detail": "No such account."}, status=404)

    spoken, payload = await call(mcp_url, "whoami", token=linked_token)

    assert payload["handle"] == "demo_fan"
    assert payload["followers"] == 0
    assert "follow you" not in spoken


async def test_an_unconfirmed_address_is_said_before_a_fan_dictates_a_paragraph(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """Fanmoot will not take a post from an unconfirmed account. Being told
    here beats discovering it after dictating a take (`S5-8`)."""
    replay.stub(
        "/v1/me",
        {
            "id": "a1f0d2c4-3e5b-4a90-9c11-7d2e6b8f0004",
            "email": "demo-fan@example.invalid",
            "handle": "demo_fan",
            "name": "Demo Fan",
            "email_verified": False,
        },
    )

    spoken, payload = await call(mcp_url, "whoami", token=linked_token)

    assert payload["email_verified"] is False
    assert "not confirmed" in spoken


# ── follow (S5-6) ────────────────────────────────────────────────────────────
async def test_a_club_is_followed_by_the_name_a_fan_says(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """ "Follow the gunners" resolves the same way find_club does, so a fan
    never has to know a slug (HAD-7)."""
    spoken, payload = await call(mcp_url, "follow", token=linked_token, target="the gunners")

    assert payload["target_type"] == "club"
    assert payload["name"] == "Arsenal"
    assert payload["following"] is True
    assert spoken == "Following Arsenal, the club. Three followers now."
    assert [c.url.path for c in replay.calls if c.method == "POST"][-1] == "/v1/follow/club/arsenal"


async def test_a_handle_with_an_at_sign_is_always_a_fan(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """The one unambiguous signal a fan can give by voice, so it wins over the
    catalogue rather than being looked up in it."""
    replay.stub(
        "/v1/follow/user/thegunner",
        {
            "target_type": "user",
            "target_id": "2f1c8d60-9b47-4a6e-9d1b-0c2f8e5a3b71",
            "following": True,
            "follower_count": 9,
        },
        method="POST",
    )

    spoken, payload = await call(mcp_url, "follow", token=linked_token, target="@thegunner")

    assert payload["target_type"] == "user"
    assert payload["name"] == "@thegunner"
    assert spoken.startswith("Following @thegunner, the fan.")


async def test_a_competition_is_followed_when_the_name_is_one(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    replay.stub(
        "/v1/follow/league/premier-league",
        {
            "target_type": "league",
            "target_id": "52eddc50-e916-4fc7-ae6d-9a64a919597e",
            "following": True,
            "follower_count": 4,
        },
        method="POST",
    )

    spoken, payload = await call(mcp_url, "follow", token=linked_token, target="the prem")

    assert payload["target_type"] == "league"
    assert spoken.startswith("Following Premier League, the competition.")


async def test_a_name_that_is_nothing_says_how_to_name_a_fan(
    linked_token: str, mcp_url: str
) -> None:
    spoken = await refusal(mcp_url, "follow", token=linked_token, target="whoever that was")

    assert "cannot find a club or competition" in spoken
    assert "at sign" in spoken


async def test_an_ambiguous_club_name_asks_rather_than_guesses(
    linked_token: str, mcp_url: str
) -> None:
    """``S1-5``. Following the wrong club for somebody is a mistake they have
    to go and undo on a website."""
    spoken = await refusal(mcp_url, "follow", token=linked_token, target="united")

    assert spoken.startswith("Do you mean ")


# ── predict_match and my_predictions (S5-5) ──────────────────────────────────
async def test_a_scoreline_is_called_and_read_back(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    spoken, payload = await call(
        mcp_url,
        "predict_match",
        token=linked_token,
        match="arsenal vs brighton",
        home_score=1,
        away_score=2,
    )

    assert payload["prediction"]["called"] is True
    assert payload["prediction"]["home_score"] == 1
    assert payload["prediction"]["away_score"] == 2
    assert spoken.startswith("Called it: Brighton one, Arsenal two")
    # The fixture the fan named, resolved to the id Fanmoot wants, without the
    # fan ever having seen one.
    called = [c for c in replay.calls if c.url.path == "/v1/predictions"][-1]
    assert MATCH_ID in called.content.decode()


async def test_a_scoreline_nobody_would_call_is_refused(linked_token: str, mcp_url: str) -> None:
    """A transcription error — "one" heard as "a hundred and one" — comes back
    as a question rather than as a prediction nobody made."""
    spoken = await refusal(
        mcp_url,
        "predict_match",
        token=linked_token,
        match="arsenal vs brighton",
        home_score=101,
        away_score=0,
    )

    assert spoken == "I can manage home score up to 9 at a time. Ask for fewer?"


async def test_a_prediction_after_kickoff_says_what_can_still_be_done(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """``S5-8``. Fanmoot closes predictions at kickoff, and the sentence it
    sends says so without saying what to do instead."""
    replay.stub(
        "/v1/predictions",
        {"detail": "Predictions for this match closed at kickoff."},
        method="POST",
        status=409,
    )

    spoken = await refusal(
        mcp_url,
        "predict_match",
        token=linked_token,
        match="arsenal vs brighton",
        home_score=1,
        away_score=2,
    )

    assert spoken.startswith("Predictions for this match closed at kickoff.")
    assert "You can still post a take about it." in spoken


async def test_my_predictions_answers_with_both_halves(linked_token: str, mcp_url: str) -> None:
    """ "What have I got on this weekend" means what is called *and* what is
    still open, so two tools would have made a fan ask twice."""
    spoken, payload = await call(mcp_url, "my_predictions", token=linked_token)

    assert payload["count"] == 1
    assert payload["open_count"] == 1
    assert spoken == "One prediction on the go. One match still open."


async def test_a_settled_prediction_shows_the_real_score_beside_the_called_one(
    linked_token: str, mcp_url: str
) -> None:
    _, payload = await call(mcp_url, "my_predictions", token=linked_token)

    settled = payload["called"][0]
    assert (settled["home_score"], settled["away_score"]) == (1, 2)
    assert (settled["actual_home"], settled["actual_away"]) == (3, 0)
    assert settled["points"] == 3
    assert settled["exact"] is False


async def test_the_open_half_leaves_out_what_has_already_been_called(
    linked_token: str, mcp_url: str
) -> None:
    """The recorded ``/v1/predictions/open`` carries one fixture the fan has
    called and one they have not. Listing a match twice in a five-item spoken
    answer is how five items stops meaning five."""
    _, payload = await call(mcp_url, "my_predictions", token=linked_token)

    assert [row["called"] for row in payload["open"]] == [False]
