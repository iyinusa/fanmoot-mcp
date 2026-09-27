"""The moot card and the four views built on it.

Two halves, as for the first four views:

* **The shapes.** What ``TakeCard`` and friends carry out of a Fanmoot moot —
  and what they refuse to: a third-party image the sandbox would block, the
  clock's assumed 0–0, a quote of a quote, a link to a profile route that does
  not exist.
* **The views**, rendered by ``tests/apps/render.mjs`` from payloads built with
  those shapes, with every action a card can take asserted as the
  ``tools/call`` or ``ui/message`` the host would receive. A card that changes
  state is only ever changed by what the tool answered with.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.fanmoot.models import MootOut
from app.mcp.tools.shapes import (
    Choice,
    ClubAnswer,
    ClubCard,
    FanAnswer,
    ImageCard,
    LinkCard,
    MatchLink,
    NewsAnswer,
    NewsCard,
    PostAnswer,
    PredictionAnswer,
    PredictionCard,
    PredictionsAnswer,
    TakeCard,
    TrendingAnswer,
)
from tests import scenarios
from tests.test_apps_render import HARNESS, _render, calls, take, thread_payload

NOW = datetime(2026, 9, 19, 15, 7, tzinfo=UTC)
MATCH_ID = uuid.UUID("dd472da4-8efc-42be-9f7e-188a57aec488")


@pytest.fixture
def rendered(tmp_path: Path) -> Any:
    if shutil.which("node") is None or not HARNESS.exists():
        pytest.skip("the view renderer needs node; the shape tests cover the rest")

    def go(view: str, payload: Any, **kwargs: Any) -> dict[str, Any]:
        return _render(tmp_path, view, payload, **kwargs)

    return go


def text_of(result: dict[str, Any]) -> str:
    return str(result["text"])


# ── The shapes ───────────────────────────────────────────────────────────────
def _moot(**extra: Any) -> MootOut:
    raw = scenarios.moot("Saka was the only one who turned up.")
    raw.update(extra)
    return MootOut.model_validate(raw)


def test_a_take_carries_its_club_crest_and_colour() -> None:
    card = TakeCard.of(_moot())

    assert card.club == "Arsenal"
    assert card.club_crest_url and card.club_crest_url.startswith("https://fanmoot.test/")
    assert card.club_colour.startswith("#")


def test_a_take_links_to_the_profile_route_fanmoot_serves() -> None:
    """``/@handle`` is not a route on fanmoot.com — it answered with the app's
    "Not found". ``/u/{handle}`` is the profile."""
    card = TakeCard.of(_moot())

    assert card.profile_url == "https://fanmoot.test/u/gooner_jo"


def test_only_pictures_fanmoot_serves_reach_a_card() -> None:
    """The sandbox loads images from Fanmoot's origin and nowhere else, so a
    third-party picture would be drawn as a broken image. It is left out."""
    card = TakeCard.of(
        _moot(
            media=[
                {"id": str(uuid.uuid4()), "url": "/media/ab/cd.webp", "width": 900, "height": 600},
                {"id": str(uuid.uuid4()), "url": "https://elsewhere.test/x.png"},
            ]
        )
    )

    assert [image.url for image in card.images] == ["https://fanmoot.test/media/ab/cd.webp"]
    assert card.images[0].width == 900


def test_a_link_keeps_its_words_but_not_a_third_party_thumbnail() -> None:
    card = TakeCard.of(
        _moot(
            link_preview={
                "url": "https://www.bbc.co.uk/sport/football/123",
                "domain": "bbc.co.uk",
                "title": "Arteta‮ on the win",
                "image_url": "https://ichef.bbci.co.uk/x.jpg",
            }
        )
    )

    assert card.link is not None
    assert card.link.title == "Arteta on the win"
    assert card.link.image_url is None


def test_a_link_that_is_not_http_is_not_a_link() -> None:
    assert LinkCard.of(None) is None
    card = TakeCard.of(_moot(link_preview={"url": "javascript:alert(1)", "title": "x"}))
    assert card.link is None


def test_a_match_moot_carries_the_way_back_to_its_match() -> None:
    card = TakeCard.of(_moot(match=scenarios.match(status="finished")))

    assert card.match is not None
    assert (card.match.home, card.match.away) == ("Arsenal", "Spurs")
    assert (card.match.home_score, card.match.away_score) == (2, 1)
    assert card.match.url and card.match.url.endswith("/arsenal-vs-tottenham-hotspur-2026-09-22")


def test_the_way_back_never_carries_a_score_nobody_confirmed() -> None:
    """HAD-7 in the chip too: an ``assumed`` 0–0 is the clock's, not a score."""
    link = MatchLink.of(
        MootOut.model_validate(
            {
                **scenarios.moot("x"),
                "match": scenarios.match(home_score=0, away_score=0, score_state="assumed"),
            }
        ).match
    )

    assert link is not None
    assert link.home_score is None and link.away_score is None


def test_a_quote_is_carried_one_level_deep() -> None:
    inner = scenarios.moot("The original.")
    inner["quote_of"] = scenarios.moot("A quote of a quote.")
    card = TakeCard.of(_moot(quote_of=inner))

    assert card.quote is not None
    assert card.quote.body == "The original."
    assert card.quote.quote is None


def test_a_locked_quote_is_not_carried() -> None:
    """A subscriber-only take's words are not ours to show."""
    card = TakeCard.of(_moot(quote_of=scenarios.moot("Secret.", locked=True)))

    assert card.quote is None


def test_an_agents_tier_is_never_shown() -> None:
    raw = scenarios.moot("Beep.")
    raw["author"] = {**raw["author"], "is_agent": True, "tier": "ultra"}
    assert TakeCard.of(MootOut.model_validate(raw)).tier == ""


def test_an_older_recording_without_the_new_fields_still_reads() -> None:
    """Fanmoot added ``match``, ``media`` and ``link_preview`` after the tapes
    were recorded. A take without them is a plain take, not a failure."""
    card = TakeCard.of(MootOut.model_validate(scenarios.moot("Plain.")))

    assert card.images == [] and card.link is None and card.match is None


# ── Payload builders for the views ───────────────────────────────────────────
def rich_take(**over: Any) -> TakeCard:
    return take(
        "Saka was the only one who turned up.",
        club_crest_url="https://fanmoot.test/static/badges/premier-league/arsenal.webp",
        club_colour="#EF0107",
        tier="regular",
        images=[ImageCard(url="https://fanmoot.test/media/ab/cd.webp", width=900, height=600)],
        link=LinkCard(
            url="https://www.bbc.co.uk/sport/1", domain="bbc.co.uk", title="Arteta speaks"
        ),
        quote=take("The midfield lost every second ball.", handle="ade", name="Ade"),
        match=MatchLink(
            id=MATCH_ID,
            home="Arsenal",
            away="Spurs",
            home_crest_url="https://fanmoot.test/static/badges/premier-league/arsenal.webp",
            status="finished",
            home_score=2,
            away_score=1,
            kickoff_at=NOW,
            url="https://fanmoot.test/matches/premier-league/2026-27/arsenal-vs-spurs",
        ),
        profile_url="https://fanmoot.test/u/demo_fan",
        **over,
    )


# ── The shared card, in the thread ───────────────────────────────────────────
def test_a_take_in_a_host_looks_like_the_take_on_fanmoot(rendered: Any) -> None:
    result = rendered("thread", TrendingAnswer(spoken="x", takes=[rich_take()], count=1))
    text = text_of(result)

    assert "regular" in text
    assert "Arteta speaks" in text and "bbc.co.uk" in text
    assert "The midfield lost every second ball." in text and "@ade" in text
    assert "Arsenal 2–1 Spurs" in text and "Match thread" in text
    assert "Open on Fanmoot" in text
    alts = [image["alt"] for image in result["images"]]
    assert "Arsenal badge" in alts
    assert "Image from Demo Fan" in alts


def test_every_image_a_card_draws_is_on_fanmoots_origin(rendered: Any) -> None:
    result = rendered("thread", TrendingAnswer(spoken="x", takes=[rich_take()], count=1))

    for image in result["images"]:
        assert image["src"].startswith("https://fanmoot.test/"), image


def test_a_matchs_own_thread_does_not_link_back_to_itself(rendered: Any) -> None:
    """In ``read_match_conversation`` the fan is already in that match."""
    result = rendered("thread", thread_payload(rich_take()))

    assert "Match thread ›" not in text_of(result)
    assert "Open the match on Fanmoot" in text_of(result)


def test_an_unconfirmed_match_is_named_without_a_score(rendered: Any) -> None:
    unscored = rich_take()
    assert unscored.match is not None
    unscored.match.home_score = None
    unscored.match.away_score = None
    unscored.match.status = "live"

    result = rendered("thread", TrendingAnswer(spoken="x", takes=[unscored], count=1))

    assert "Arsenal v Spurs" in text_of(result)
    assert "Live" in text_of(result)


def test_links_are_opened_by_the_host(rendered: Any) -> None:
    """The sandbox has no navigation; ``ui/open-link`` lets the host ask first."""
    links = [
        node
        for node in rendered("thread", TrendingAnswer(spoken="x", takes=[rich_take()], count=1))[
            "nodes"
        ]
        if node["tag"] == "a"
    ]

    assert links
    assert all(node["attributes"]["href"].startswith("https://") for node in links)


# ── Reactions from the card ──────────────────────────────────────────────────
def test_a_reaction_is_sent_as_the_fan_through_the_host(rendered: Any) -> None:
    result = rendered(
        "thread",
        TrendingAnswer(spoken="x", takes=[take("Two-nil flatters us.")], count=1),
        acts=[{"click": "react-more"}, {"click": "react:heat"}],
        toolResult={
            "structuredContent": {
                "reaction": "heat",
                "reactions": {"respect": 9, "offside": 3, "heat": 1},
            },
            "content": [],
        },
    )

    sent = calls(result, "tools/call")
    assert len(sent) == 1
    assert sent[0]["params"] == {
        "name": "react_to_moot",
        "arguments": {"take": "k3x9qz", "reaction": "heat"},
    }
    # The counts are the tool's, not a guess made before it answered.
    assert "heat 1" in text_of(result)
    assert "Reacted with heat." in text_of(result)


def test_a_reaction_from_a_fan_who_is_not_connected_did_not_land(rendered: Any) -> None:
    sentence = "This tool acts as you on Fanmoot, so it needs your account connected."
    result = rendered(
        "thread",
        TrendingAnswer(spoken="x", takes=[take("Two-nil flatters us.")], count=1),
        acts=[{"click": "react:respect"}],
        toolResult={"isError": True, "content": [{"type": "text", "text": sentence}]},
    )

    assert sentence in text_of(result)
    assert "respect 9" in text_of(result)
    assert "Reacted" not in text_of(result)


# ── Replying to the take you are looking at ──────────────────────────────────
def test_each_take_has_its_own_reply_button(rendered: Any) -> None:
    first = take("First.", handle="ade", name="Ade", short_id="first1")
    second = take("Second.", handle="jo", name="Jo", short_id="second2", id=uuid.uuid4())
    result = rendered(
        "thread",
        TrendingAnswer(spoken="x", takes=[first, second], count=2),
        acts=[
            {"click": "reply:second2"},
            {"submit": "send", "field": "reply", "text": "Spot on, Jo."},
        ],
        toolResult={"structuredContent": {"held": False}, "content": []},
    )

    posted = calls(result, "tools/call")
    assert posted[0]["params"]["arguments"] == {"take": "second2", "body": "Spot on, Jo."}
    assert "Replying to Jo (@jo)" in text_of(result)


def test_a_posted_reply_appears_on_the_card_straight_away(rendered: Any) -> None:
    reply = take(
        "Rubbish, he was superb.", handle="me", name="Me", short_id="mine01", id=uuid.uuid4()
    )
    result = rendered(
        "thread",
        thread_payload(take("Saka was the only one who turned up.")),
        act={"submit": "send", "field": "reply", "text": "Rubbish, he was superb."},
        toolResult={
            "structuredContent": {"held": False, "take": reply.model_dump(mode="json")},
            "content": [],
        },
    )

    assert "Posted." in text_of(result)
    assert "Rubbish, he was superb." in text_of(result)
    assert "@me" in text_of(result)


# ── The moot view: what was just posted ──────────────────────────────────────
def test_a_live_take_says_so_and_shows_the_take(rendered: Any) -> None:
    result = rendered("moot", PostAnswer(spoken="Posted.", take=rich_take()))

    assert "Your take is live" in text_of(result)
    assert "Saka was the only one who turned up." in text_of(result)
    assert "Posted on Fanmoot under @demo_fan." in text_of(result)


def test_a_held_take_is_never_called_live(rendered: Any) -> None:
    """``S5-2``: Fanmoot's own reason, word for word, and no "live" anywhere."""
    reason = "This looks like it is about something other than football."
    result = rendered(
        "moot", PostAnswer(spoken="Held.", take=rich_take(), held=True, gate_reason=reason)
    )

    assert "held for review" in text_of(result)
    assert reason in text_of(result)
    assert "is live" not in text_of(result)
    assert "Posted" not in text_of(result)


def test_a_reply_is_called_a_reply(rendered: Any) -> None:
    result = rendered(
        "moot", PostAnswer(spoken="Posted.", take=rich_take(), replying_to=uuid.uuid4())
    )

    assert "Your reply is live" in text_of(result)


def test_your_own_take_is_not_folded_or_reacted_to(rendered: Any) -> None:
    long = take("x" * 900)
    result = rendered("moot", PostAnswer(spoken="Posted.", take=long))

    assert "Show more" not in text_of(result)
    assert not [
        n for n in result["nodes"] if n["attributes"].get("data-act", "").startswith("react")
    ]


# ── The fan view ─────────────────────────────────────────────────────────────
def arsenal_card() -> ClubCard:
    return ClubCard(
        slug="arsenal",
        name="Arsenal",
        short_name="Arsenal",
        nickname="The Gunners",
        competition="Premier League",
        stadium="Emirates Stadium",
        crest_url="https://fanmoot.test/static/badges/premier-league/arsenal.webp",
        colour="#EF0107",
        member_count=1204,
        url="https://fanmoot.test/clubs/arsenal",
    )


def fan(**over: Any) -> FanAnswer:
    return FanAnswer(
        spoken="You are @gooner_jo.",
        handle="gooner_jo",
        name="Jo",
        club=over.pop("club", arsenal_card()),
        tenure_days=over.pop("tenure_days", 212),
        followers=18,
        following=40,
        url="https://fanmoot.test/u/gooner_jo",
        **over,
    )


def test_the_fan_card_says_whose_name_a_take_goes_out_under(rendered: Any) -> None:
    result = rendered("fan", fan())
    text = text_of(result)

    assert "Jo" in text and "@gooner_jo" in text
    assert "Arsenal" in text and "Premier League" in text
    assert "7 months" in text
    assert "18" in text and "Followers" in text
    assert "Open your profile" in text
    assert "Confirm your email" not in text


def test_a_fan_who_cannot_post_yet_is_told_in_words(rendered: Any) -> None:
    result = rendered("fan", fan(email_verified=False))

    assert "Confirm your email address on Fanmoot before you can post." in text_of(result)


def test_a_fan_without_a_club_is_not_given_one(rendered: Any) -> None:
    result = rendered("fan", fan(club=None, tenure_days=0))

    assert "Not signed to a club yet." in text_of(result)


# ── The club view ────────────────────────────────────────────────────────────
def test_a_club_card_shows_the_club_and_follows_it_as_the_fan(rendered: Any) -> None:
    result = rendered(
        "club",
        ClubAnswer(spoken="Arsenal.", query="the gunners", resolved=True, club=arsenal_card()),
        acts=[{"click": "follow"}],
        toolResult={
            "structuredContent": {"following": True, "follower_count": 1205},
            "content": [],
        },
    )

    text = text_of(result)
    assert "Arsenal" in text and "The Gunners" in text and "Emirates Stadium" in text
    assert "1,204 fans" in text or "1204 fans" in text
    sent = calls(result, "tools/call")
    assert sent[0]["params"] == {
        "name": "follow",
        "arguments": {"target": "arsenal", "kind": "club"},
    }
    assert "You follow Arsenal" in text


def test_which_one_is_answered_as_the_fan_would_type_it(rendered: Any) -> None:
    """The view never picks: the tap is the fan's answer, in the conversation."""
    result = rendered(
        "club",
        ClubAnswer(
            spoken="Arsenal or Aston Villa?",
            query="a",
            resolved=False,
            choices=[
                Choice(slug="arsenal", name="Arsenal", kind="club", hint="Premier League"),
                Choice(slug="aston-villa", name="Aston Villa", kind="club", hint="Premier League"),
            ],
        ),
        acts=[{"click": "choose:aston-villa"}],
    )

    assert "Which one do you mean?" in text_of(result)
    said = calls(result, "ui/message")
    assert said[0]["params"]["content"] == [{"type": "text", "text": "I mean Aston Villa."}]
    assert not calls(result, "tools/call")


def test_club_news_is_a_list_of_headlines(rendered: Any) -> None:
    result = rendered(
        "club",
        NewsAnswer(
            spoken="Two stories.",
            club="Arsenal",
            items=[
                NewsCard(
                    headline="Saka signs on", source="BBC Sport", url="https://www.bbc.co.uk/1"
                ),
                NewsCard(
                    headline="Injury news",
                    source="Arsenal.com",
                    url="https://www.arsenal.com/2",
                    take_url="https://fanmoot.test/m/abc",
                ),
            ],
            count=2,
        ),
    )

    text = text_of(result)
    assert "Arsenal news" in text
    assert "Saka signs on" in text and "Injury news" in text
    assert "See what fans said" in text


# ── The predictions view ─────────────────────────────────────────────────────
def prediction(**over: Any) -> PredictionCard:
    return PredictionCard(
        match_id=over.pop("match_id", MATCH_ID),
        competition="Premier League",
        home="Arsenal",
        away="Spurs",
        home_crest_url="https://fanmoot.test/static/badges/premier-league/arsenal.webp",
        kickoff_at=NOW,
        kickoff_spoken="Saturday at three",
        status=over.pop("status", "scheduled"),
        url="https://fanmoot.test/matches/x",
        **over,
    )


def test_a_settled_call_sits_beside_the_real_score(rendered: Any) -> None:
    result = rendered(
        "predictions",
        PredictionsAnswer(
            spoken="x",
            called=[
                prediction(
                    status="finished",
                    called=True,
                    home_score=2,
                    away_score=1,
                    actual_home=2,
                    actual_away=1,
                    points=3,
                    exact=True,
                )
            ],
            count=1,
        ),
    )

    text = text_of(result)
    assert "Your call 2–1" in text and "finished 2–1" in text
    assert "Exact · 3 pts" in text
    assert not [n for n in result["nodes"] if n["tag"] == "form"]


def test_an_open_fixture_is_called_from_the_card(rendered: Any) -> None:
    called = prediction(called=True, home_score=2, away_score=1)
    result = rendered(
        "predictions",
        PredictionsAnswer(spoken="x", open=[prediction()], open_count=1),
        acts=[
            {
                "fill": {f"home:{MATCH_ID}": "2", f"away:{MATCH_ID}": "1"},
                "click": f"predict:{MATCH_ID}",
            }
        ],
        toolResult={
            "structuredContent": {"prediction": called.model_dump(mode="json")},
            "content": [],
        },
    )

    sent = calls(result, "tools/call")
    assert sent[0]["params"] == {
        "name": "predict_match",
        "arguments": {"match": str(MATCH_ID), "home_score": 2, "away_score": 1},
    }
    assert "Called: Arsenal 2–1 Spurs." in text_of(result)
    assert "Your call 2–1" in text_of(result)


def test_a_nonsense_score_is_caught_before_it_is_sent(rendered: Any) -> None:
    result = rendered(
        "predictions",
        PredictionsAnswer(spoken="x", open=[prediction()], open_count=1),
        acts=[
            {
                "fill": {f"home:{MATCH_ID}": "12", f"away:{MATCH_ID}": "1"},
                "click": f"predict:{MATCH_ID}",
            }
        ],
    )

    assert not calls(result, "tools/call")
    assert "Put a score from 0 to 9 for each side." in text_of(result)


def test_a_call_that_was_refused_is_not_shown_as_made(rendered: Any) -> None:
    sentence = "Predictions for that match have closed."
    result = rendered(
        "predictions",
        PredictionsAnswer(spoken="x", open=[prediction()], open_count=1),
        acts=[
            {
                "fill": {f"home:{MATCH_ID}": "2", f"away:{MATCH_ID}": "1"},
                "click": f"predict:{MATCH_ID}",
            }
        ],
        toolResult={"isError": True, "content": [{"type": "text", "text": sentence}]},
    )

    assert sentence in text_of(result)
    assert "Called:" not in text_of(result)


def test_predict_match_confirms_the_one_call(rendered: Any) -> None:
    result = rendered(
        "predictions",
        PredictionAnswer(
            spoken="Called.", prediction=prediction(called=True, home_score=1, away_score=1)
        ),
    )

    assert "Your call is in" in text_of(result)
    assert "Your call 1–1" in text_of(result)
