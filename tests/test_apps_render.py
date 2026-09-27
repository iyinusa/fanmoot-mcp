"""``S6-8``, the half a regex cannot reach: each view rendered from a payload.

These run the views' **real** JavaScript — the shared bridge and the view's own
renderer — against a DOM with exactly the surface those four files touch, with
a host on the other end of ``postMessage`` playing SEP-1865's sequence:
``ui/initialize`` → ``McpUiInitializeResult`` → ``ui/notifications/initialized``
→ ``ui/notifications/tool-input`` → ``ui/notifications/tool-result``. The
harness is ``tests/apps/render.mjs``, and what it is not is written at the top
of it: no layout, no CSS, no paint.

So what is asserted here is *what the view did with the answer* — the scoreline
is in the document, the unconfirmed score is not stated as a number, the crest
carries the club's name, the composer's reply goes out as a ``tools/call`` for
``reply_to_moot`` and a held reply is not reported as posted. Where the view
sits on a screen is the demo's problem (``W2``), which loads these same
resources into a real browser.

The payloads are built from the same ``shapes`` models the tools answer with,
so a field renamed in ``app/mcp/tools/shapes.py`` breaks these rather than
quietly rendering nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.mcp.apps import views
from app.mcp.tools.shapes import (
    ConversationAnswer,
    LiveAnswer,
    MatchCard,
    MatchDay,
    MoodAnswer,
    SideCard,
    TakeCard,
    UpcomingAnswer,
)

HARNESS = Path(__file__).resolve().parent / "apps" / "render.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="the view renderer needs node; the contract tests cover the rest",
)

NOW = datetime(2026, 9, 19, 15, 7, tzinfo=UTC)
MATCH_ID = uuid.UUID("dd472da4-8efc-42be-9f7e-188a57aec488")
TAKE_ID = uuid.UUID("b2c1e4d6-7f08-4a1b-8e33-5c9d0a7f1120")


# ── The harness ──────────────────────────────────────────────────────────────
def _render(
    tmp_path: Path, view: str, payload: Any, *, act: dict[str, Any] | None = None, **extra: Any
) -> dict[str, Any]:
    file = tmp_path / f"{view}.html"
    file.write_text(views.document(view), encoding="utf-8")

    body = {
        "payload": json.loads(payload.model_dump_json())
        if hasattr(payload, "model_dump_json")
        else payload
    }
    if act:
        body["act"] = act
    body.update(extra)

    finished = subprocess.run(  # noqa: S603 - a local script, with a fixed argv
        ["node", str(HARNESS), str(file)],  # noqa: S607 - node is resolved by PATH on purpose
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
    result: dict[str, Any] = json.loads(finished.stdout)
    assert not result["errors"], result["errors"]
    return result


@pytest.fixture
def rendered(tmp_path: Path) -> Any:
    def go(view: str, payload: Any, **kwargs: Any) -> dict[str, Any]:
        return _render(tmp_path, view, payload, **kwargs)

    return go


def methods(result: dict[str, Any]) -> list[str]:
    return [message["method"] for message in result["sent"] if "method" in message]


def calls(result: dict[str, Any], method: str) -> list[dict[str, Any]]:
    return [message for message in result["sent"] if message.get("method") == method]


# ── Payload builders, from the tools' own shapes ─────────────────────────────
def side(slug: str, name: str, score: int | None) -> SideCard:
    return SideCard(
        slug=slug,
        name=name,
        short_name=name,
        crest_url=f"https://fanmoot.test/static/badges/premier-league/{slug}.webp",
        colour="#EF0107",
        score=score,
    )


def match(
    *, status: str = "live", minute: int | None = 67, confirmed: bool = True, **over: Any
) -> MatchCard:
    return MatchCard(
        id=MATCH_ID,
        status=status,
        competition="Premier League",
        season="2026-27",
        venue="Emirates Stadium",
        kickoff_at=NOW,
        kickoff_spoken="this afternoon",
        home=over.pop("home", side("arsenal", "Arsenal", 2)),
        away=over.pop("away", side("tottenham-hotspur", "Spurs", 1)),
        minute=minute,
        period="2h",
        score_confirmed=confirmed,
        take_count=41,
        url="https://fanmoot.test/matches/x",
        spoken="Arsenal are two-one up at the Emirates, sixty-seventh minute.",
        **over,
    )


def take(body: str, *, handle: str = "demo_fan", name: str = "Demo Fan", **over: Any) -> TakeCard:
    return TakeCard(
        id=over.pop("id", TAKE_ID),
        short_id=over.pop("short_id", "k3x9qz"),
        handle=handle,
        name=name,
        club="Arsenal",
        body=body,
        posted_at=NOW,
        reply_count=4,
        reaction_count=12,
        reactions={"respect": 9, "offside": 3},
        url="https://fanmoot.test/m/k3x9qz",
        spoken=f"{name} says: {body}",
        **over,
    )


# ── Every view: the handshake (S6-1) ─────────────────────────────────────────
@pytest.mark.parametrize("view", [entry.name for entry in views.VIEWS])
def test_a_view_completes_the_handshake_before_it_renders(rendered: Any, view: str) -> None:
    """The spec's order, and the host is entitled to it: nothing may be sent to
    a View before its ``initialized`` notification."""
    result = rendered(view, LiveAnswer(spoken="Nothing on.", matches=[], count=0))

    order = methods(result)
    assert order[0] == "ui/initialize"
    assert "ui/notifications/initialized" in order
    assert order.index("ui/initialize") < order.index("ui/notifications/initialized")

    handshake = result["sent"][0]
    assert handshake["params"]["protocolVersion"] == "2026-01-26"
    # `appInfo` is what the ext-apps SDK's schema requires, and what a real
    # host validates. `clientInfo` — the spec prose's example — is refused, and
    # a refused handshake is a card that never renders.
    assert handshake["params"]["appInfo"]["name"] == "fanmoot-view"
    assert "clientInfo" not in handshake["params"]
    assert handshake["params"]["appCapabilities"]["availableDisplayModes"] == [
        "inline",
        "fullscreen",
    ]


@pytest.mark.parametrize("view", [entry.name for entry in views.VIEWS])
def test_a_view_answers_the_requests_a_host_sends_it(rendered: Any, view: str) -> None:
    """``ping`` and ``ui/resource-teardown`` are requests: a host waits for the
    reply. Their ids are the host's own numbering and can equal the View's —
    the View must answer them rather than read them as replies to itself."""
    result = rendered(
        view,
        LiveAnswer(spoken="Nothing on.", matches=[], count=0),
        hostRequests=["ping", "ui/resource-teardown", "no/such-method"],
    )

    replies = {reply["id"]: reply for reply in result["answered"]}
    assert replies[1]["result"] == {}
    assert replies[2]["result"] == {}
    assert replies[3]["error"]["code"] == -32601
    # And the View's own first request — id 1 as well — still got its answer.
    assert "ui/notifications/initialized" in methods(result)


@pytest.mark.parametrize("view", [entry.name for entry in views.VIEWS])
def test_a_view_takes_the_hosts_theme(rendered: Any, view: str) -> None:
    """Visual cohesion with the host, per the spec's theming section: the
    host's palette wins, and Floodlit is the fallback underneath it."""
    result = rendered(
        view,
        LiveAnswer(spoken="Nothing on.", matches=[], count=0),
        theme="light",
        variables={"--color-text-primary": "#123456"},
    )

    assert result["theme"] == "light"


@pytest.mark.parametrize("view", [entry.name for entry in views.VIEWS])
def test_a_view_tells_the_model_what_is_on_screen(rendered: Any, view: str) -> None:
    """So a follow-up — "what are they saying about it" — does not start from
    nothing. One request, overwriting the last, as the spec describes."""
    result = rendered(view, LiveAnswer(spoken="Nothing on.", matches=[], count=0))

    updates = calls(result, "ui/update-model-context")
    assert len(updates) == 1
    assert updates[0]["params"]["structuredContent"]["showing"] == view


# ── Scoreboard (S6-2) ────────────────────────────────────────────────────────
def test_the_scoreboard_puts_the_scoreline_and_the_minute_on_the_card(rendered: Any) -> None:
    result = rendered(
        "scoreboard", LiveAnswer(spoken="Arsenal are two-one up.", matches=[match()], count=1)
    )

    assert "Arsenal" in result["text"]
    assert "Spurs" in result["text"]
    assert "2" in result["text"] and "1" in result["text"]
    assert "67'" in result["text"]
    assert "Emirates Stadium" in result["text"]


def test_the_scoreboard_never_states_a_score_nobody_confirmed(rendered: Any) -> None:
    """HAD-7's rule, in the view as well as in the voice: an ``assumed``
    scoreline is the 0–0 a clock started the match at, which nobody has looked
    at since."""
    unchecked = match(status="live", confirmed=False, home=side("arsenal", "Arsenal", 0))

    result = rendered("scoreboard", LiveAnswer(spoken="Under way.", matches=[unchecked], count=1))

    assert "Score not confirmed" in result["text"]
    classes = {node["class"] for node in result["nodes"]}
    assert not any(name == "score" for name in classes)


def test_the_scoreboard_names_every_crest_it_shows(rendered: Any) -> None:
    """``S6-7``. "Arsenal badge" is the match being read out; "image" is not."""
    result = rendered("scoreboard", LiveAnswer(spoken="x", matches=[match()], count=1))

    assert result["images"]
    for image in result["images"]:
        assert image["alt"].endswith(" badge"), image
        assert image["src"].startswith("https://fanmoot.test/"), image


def test_the_scoreboard_renders_a_whole_round_not_just_one_match(rendered: Any) -> None:
    """``live_scores`` and ``get_match`` are the same card seen several times
    or once."""
    result = rendered(
        "scoreboard",
        LiveAnswer(
            spoken="Three on.",
            matches=[match(), match(minute=12), match(status="finished", minute=None)],
            count=3,
            has_more=True,
        ),
    )

    cards = [node for node in result["nodes"] if "match" in node["class"].split()]
    assert len(cards) == 3
    assert "Full time" in result["text"]
    assert "More than this" in result["text"]


def test_an_empty_scoreboard_says_the_tools_own_sentence(rendered: Any) -> None:
    """A blank card is a bug; "nothing is on" is an answer."""
    result = rendered(
        "scoreboard", LiveAnswer(spoken="Nothing is on right now.", matches=[], count=0)
    )

    assert "Nothing is on right now." in result["text"]


# ── Thread (S6-3) ────────────────────────────────────────────────────────────
def thread_payload(*takes: TakeCard) -> ConversationAnswer:
    return ConversationAnswer(
        spoken="Forty-one takes on Arsenal against Spurs.",
        match=match(),
        view="top",
        takes=list(takes),
        count=len(takes),
    )


def test_the_thread_attributes_every_take_to_the_fan_who_posted_it(rendered: Any) -> None:
    """The instructions say a moot is something a stranger said. A quote with
    no name on it is one the reader hears as the assistant's."""
    result = rendered(
        "thread",
        thread_payload(
            take("Saka was the only one who turned up.", handle="ade", name="Ade"),
            take("Nonsense, the midfield was fine.", handle="jo", name="Jo"),
        ),
    )

    assert "Saka was the only one who turned up." in result["text"]
    assert "@ade" in result["text"]
    assert "Jo" in result["text"]


def test_the_thread_shows_reactions_as_words_and_counts(rendered: Any) -> None:
    """``S6-7``: never an emoji alone, and never a colour alone."""
    result = rendered("thread", thread_payload(take("Two-nil flatters us.")))

    assert "respect 9" in result["text"]
    assert "offside 3" in result["text"]


def test_the_composer_posts_through_the_host(rendered: Any) -> None:
    """``S6-3``, and the whole reason this view exists rather than a picture of
    a thread: the reply goes out as a ``tools/call`` the host proxies, on the
    connection and the grant the host already holds."""
    result = rendered(
        "thread",
        thread_payload(take("Saka was the only one who turned up.")),
        act={"submit": "composer", "field": "reply", "text": "Rubbish, he was superb."},
        toolResult={"structuredContent": {"held": False}, "content": []},
    )

    posted = calls(result, "tools/call")
    assert len(posted) == 1
    assert posted[0]["params"]["name"] == "reply_to_moot"
    assert posted[0]["params"]["arguments"]["body"] == "Rubbish, he was superb."
    # Aimed at the take on screen, by the id a fan never has to read out.
    assert posted[0]["params"]["arguments"]["take"] == "k3x9qz"
    assert "Posted." in result["text"]


def test_a_held_reply_is_not_reported_as_posted(rendered: Any) -> None:
    """``S5-2`` again, one layer up. Fanmoot's own reason, word for word."""
    reason = "This looks like it is about something other than football."
    result = rendered(
        "thread",
        thread_payload(take("Saka was the only one who turned up.")),
        act={"submit": "composer", "field": "reply", "text": "Off topic entirely."},
        toolResult={"structuredContent": {"held": True, "gate_reason": reason}, "content": []},
    )

    assert reason in result["text"]
    assert "Posted." not in result["text"]


def test_a_refused_reply_says_the_tools_own_sentence(rendered: Any) -> None:
    """A fan whose account is not connected is told that, not "error"."""
    sentence = "This tool acts as you on Fanmoot, so it needs your account connected."
    result = rendered(
        "thread",
        thread_payload(take("Saka was the only one who turned up.")),
        act={"submit": "composer", "field": "reply", "text": "Rubbish."},
        toolResult={"isError": True, "content": [{"type": "text", "text": sentence}]},
    )

    assert sentence in result["text"]
    assert "Posted." not in result["text"]


def test_a_thread_with_nothing_in_it_offers_no_composer(rendered: Any) -> None:
    """There is nothing to reply to, and a form aimed at nothing would post to
    nothing."""
    result = rendered("thread", thread_payload())

    composer = next(node for node in result["nodes"] if node["id"] == "composer")
    assert composer["hidden"] is True
    assert "Nobody has said anything yet." in result["text"]


# ── Fixtures (S6-4) ──────────────────────────────────────────────────────────
def test_the_fixtures_view_keeps_the_days_the_tool_grouped(rendered: Any) -> None:
    result = rendered(
        "fixtures",
        UpcomingAnswer(
            spoken="Six on this weekend.",
            days=[
                MatchDay(
                    day="2026-09-19",
                    day_spoken="Saturday",
                    matches=[match(status="scheduled", minute=None)],
                ),
                MatchDay(
                    day="2026-09-20",
                    day_spoken="Sunday",
                    matches=[match(status="scheduled", minute=None)],
                ),
            ],
            count=2,
        ),
    )

    assert "Saturday" in result["text"]
    assert "Sunday" in result["text"]
    days = [node for node in result["nodes"] if node["class"] == "day"]
    assert len(days) == 2


def test_a_finished_fixture_shows_the_score_and_a_scheduled_one_the_time(
    rendered: Any,
) -> None:
    """Two rows, two different answers, one shape. A result with a kick-off
    time beside it would be a list nobody can read at a glance."""
    result = rendered(
        "fixtures",
        UpcomingAnswer(
            spoken="x",
            days=[
                MatchDay(
                    day="2026-09-19",
                    day_spoken="Saturday",
                    matches=[
                        match(status="finished", minute=None),
                        match(status="scheduled", minute=None),
                    ],
                )
            ],
            count=2,
        ),
    )

    assert "2–1" in result["text"]
    assert "this afternoon" in result["text"]


def test_an_empty_week_is_an_answer_rather_than_a_blank_card(rendered: Any) -> None:
    result = rendered("fixtures", UpcomingAnswer(spoken="Nothing on.", days=[], count=0))

    assert "Nothing on." in result["text"]


# ── Mood (S6-5) ──────────────────────────────────────────────────────────────
def mood(**over: Any) -> MoodAnswer:
    return MoodAnswer(
        spoken="The room is furious.",
        subject="Arsenal against Spurs",
        match=match(),
        positive=over.pop("positive", 6),
        negative=over.pop("negative", 30),
        neutral=over.pop("neutral", 4),
        sampled=40,
        method=over.pop("method", "bedrock"),
        summary="Most of them think the second goal was offside.",
        takes=over.pop("takes", [take("That was never a goal.")]),
        **over,
    )


def test_the_mood_bar_is_three_labelled_counts_not_three_colours(rendered: Any) -> None:
    """``S6-7``. A reader who cannot tell the colours apart still gets the
    split, because the numbers and the words are under it."""
    result = rendered("mood", mood())

    assert "Happy" in result["text"]
    assert "Furious" in result["text"]
    assert "Unmoved" in result["text"]
    assert "30" in result["text"]

    bar = next(node for node in result["nodes"] if node["id"] == "bar")
    assert "30 furious" in bar["attributes"]["aria-label"]


def test_the_mood_bar_is_drawn_in_proportion(rendered: Any) -> None:
    result = rendered("mood", mood(positive=25, negative=25, neutral=0))

    widths = [
        node["style"]["width"]
        for node in result["nodes"]
        if node["class"] in {"positive", "negative", "neutral"} and node["style"].get("width")
    ]
    assert widths == ["50.0%", "50.0%"]


def test_the_mood_says_what_read_it(rendered: Any) -> None:
    """``S3-7``'s honesty, carried into the card: a reader deciding how much to
    trust a split should be told whether a model or a lexicon produced it."""
    by_model = rendered("mood", mood(method="bedrock"))
    by_lexicon = rendered("mood", mood(method="lexicon"))

    assert "read by a model" in by_model["text"]
    assert "read by lexicon" in by_lexicon["text"]


def test_a_quiet_thread_has_no_mood_to_show(rendered: Any) -> None:
    result = rendered("mood", mood(positive=0, negative=0, neutral=0, takes=[]))

    assert "Nobody has said anything yet." in result["text"]
