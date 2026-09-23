"""``S5-2``, ``S5-3``, ``S5-4``, ``S5-9`` — posting, replying, reacting as a fan.

Every test here runs the **whole** path: a real OAuth dance produces a real
grant, the MCP client presents the token it got, the gate lets it through, the
tool exchanges it for a Fanmoot access token and the recorded API answers. A
test that inserted a grant would be testing the insert.

What is asserted is mostly the promises that are expensive to break:

* a retried frame posts **once**, and two different takes never collide;
* a take Fanmoot held for review is **never** reported as live;
* a dictated take past the cap comes back as a question, not as a trimmed
  opinion published under somebody's name;
* Fanmoot's own refusal reaches the fan, with the next step attached.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.mcp.tools.acting import MOOT_MAX
from app.oauth import scopes
from tests.replay import Replay, fixture_body
from tests.tooling import call, refusal, session_at

#: The take in ``fixtures/post_v1_moots.json``, so the recorded answer and the
#: thing being posted are the same take.
TAKE = "Saka was the only one who turned up. Two-nil flatters us."
POSTED_MOOT_ID = "b2c1e4d6-7f08-4a1b-8e33-5c9d0a7f1120"
PARENT_ID = "7c9e1183-d0a6-4e9c-af6b-6510836105e3"
SHORT_ID = "HU6yREc6mJ"
HELD = "post_v1_moots_held.json"


def composed(replay: Replay) -> list[httpx.Request]:
    return [c for c in replay.calls if c.method == "POST" and c.url.path == "/v1/moots"]


def keys(replay: Replay) -> list[str]:
    return [dict(c.url.params)["idempotency_key"] for c in composed(replay)]


# ── post_moot (S5-2) ─────────────────────────────────────────────────────────
async def test_a_take_goes_up_under_the_fans_own_handle(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    spoken, payload = await call(mcp_url, "post_moot", token=linked_token, body=TAKE)

    assert payload["held"] is False
    assert payload["take"]["body"] == TAKE
    assert payload["take"]["handle"] == "demo_fan"
    assert spoken == "Posted your take. It's on your profile as @demo_fan."
    # The whole take, not a summary of it: what a fan dictated is what goes up.
    assert composed(replay)[0].content.decode().count(TAKE) == 1


async def test_a_take_about_a_match_is_anchored_to_its_thread(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """ "Post that about the Brighton game" has to reach the Brighton game's
    thread, and a fan has never seen the id of one (HAD-7)."""
    _, payload = await call(
        mcp_url, "post_moot", token=linked_token, body=TAKE, match="arsenal vs brighton"
    )

    assert payload["take"]["id"] == POSTED_MOOT_ID
    assert "dd472da4-8efc-42be-9f7e-188a57aec488" in composed(replay)[0].content.decode()


async def test_a_held_take_is_never_reported_as_live(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """HAD-7's honest refusal. Fanmoot's football-topicality gate can park a
    take for a moderator, and its reason is passed on word for word — it was
    written for the person who wrote the take."""
    held = fixture_body(HELD)
    replay.stub("/v1/moots", held, method="POST", status=201)

    spoken, payload = await call(
        mcp_url, "post_moot", token=linked_token, body="Off topic entirely."
    )

    assert payload["held"] is True
    assert payload["gate_reason"] == held["gate_reason"]
    assert "review" in spoken
    assert held["gate_reason"] in spoken
    assert "Posted" not in spoken


async def test_a_retried_frame_posts_once_and_two_takes_never_collide(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """``S5-2``. A voice client retries for reasons that have nothing to do
    with the fan, and a duplicate opinion under somebody's handle is not
    something an apology fixes.

    Each ``call`` opens its own session, so both send the same JSON-RPC request
    id — which is exactly the shape of a retry. The key must therefore match,
    and must *not* match for a different take.
    """
    await call(mcp_url, "post_moot", token=linked_token, body=TAKE)
    await call(mcp_url, "post_moot", token=linked_token, body=TAKE)
    await call(mcp_url, "post_moot", token=linked_token, body="Different take entirely.")

    first, retry, other = keys(replay)
    assert first == retry
    assert other != first
    # And nothing a fan said travels in a query string.
    assert TAKE not in first


async def test_two_takes_in_one_conversation_are_two_takes(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """The other half of the same rule. A fan who says the same thing twice in
    one sitting meant it twice; only a repeat of the *same frame* is a retry."""
    async with session_at(mcp_url, headers={"Authorization": f"Bearer {linked_token}"}) as session:
        await session.call_tool("post_moot", {"body": TAKE})
        await session.call_tool("post_moot", {"body": TAKE})

    first, second = keys(replay)
    assert first != second


async def test_a_take_past_the_cap_is_refused_rather_than_trimmed(
    linked_token: str, mcp_url: str
) -> None:
    """``S5-2``. Truncating somebody's opinion and publishing it under their
    name is the one failure here that cannot be taken back."""
    spoken = await refusal(mcp_url, "post_moot", token=linked_token, body="a" * (MOOT_MAX + 1))

    assert spoken == (
        f"That take is longer than {MOOT_MAX} characters, "
        "which is as much as Fanmoot takes. Shall I shorten it?"
    )


async def test_an_empty_take_asks_what_to_say(linked_token: str, mcp_url: str) -> None:
    spoken = await refusal(mcp_url, "post_moot", token=linked_token, body="   ")

    assert spoken == "What would you like it to say?"


async def test_the_schema_declares_the_cap_a_client_has_to_respect(
    linked_token: str, mcp_url: str
) -> None:
    """Declared where a client can read it before dictating a paragraph, not
    only where it is enforced after one."""
    from tests.tooling import surface

    listed = await surface(mcp_url)

    assert listed["post_moot"].inputSchema["properties"]["body"]["maxLength"] == MOOT_MAX


# ── The refusals a fan can act on (S5-8) ─────────────────────────────────────
async def test_an_unconfirmed_address_is_told_what_to_do_about_it(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """Fanmoot's own sentence, which is written for a fan, plus the step it had
    no reason to include."""
    replay.stub(
        "/v1/moots",
        {"detail": "Confirm your email address to continue."},
        method="POST",
        status=403,
    )

    spoken = await refusal(mcp_url, "post_moot", token=linked_token, body=TAKE)

    assert spoken.startswith("Confirm your email address to continue.")
    assert "Look for the message from Fanmoot" in spoken


async def test_a_muted_account_hears_fanmoots_own_words_unedited(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """Upstream's mute message already says until when and where to appeal.
    Paraphrasing it here would be this server inventing a moderation policy."""
    muted = "You are muted until 30 Sep 2026, 12:00 UTC, so you cannot post, reply or react."
    replay.stub("/v1/moots", {"detail": muted}, method="POST", status=403)

    spoken = await refusal(mcp_url, "post_moot", token=linked_token, body=TAKE)

    assert spoken == muted


async def test_being_rate_limited_says_how_long_to_wait(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    replay.stub(
        "/v1/moots",
        {"detail": "You are posting too quickly. Take a breath."},
        method="POST",
        status=429,
    )

    spoken = await refusal(mcp_url, "post_moot", token=linked_token, body=TAKE)

    assert "Take a breath." in spoken
    assert "Try again" in spoken


async def test_a_credential_fanmoot_will_not_take_asks_for_a_reconnect(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """A 403 with no sentence of its own is the credential failing rather than
    the act being refused, and those are completely different things to hear."""
    replay.stub("/v1/moots", {}, method="POST", status=401)

    spoken = await refusal(mcp_url, "post_moot", token=linked_token, body=TAKE)

    assert "isn't connected any more" in spoken


# ── reply_to_moot (S5-3) ─────────────────────────────────────────────────────
async def test_a_reply_is_anchored_to_the_take_it_answers(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    _, payload = await call(
        mcp_url, "reply_to_moot", token=linked_token, take=PARENT_ID, body="Rubbish, he was superb."
    )

    assert payload["replying_to"] == PARENT_ID
    assert f'"parent_id":"{PARENT_ID}"' in composed(replay)[0].content.decode()


async def test_a_reply_can_be_aimed_with_the_link_a_fan_has(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """``fanmoot.com/m/HU6yREc6mJ`` is what appears in a share sheet, and the
    only id a fan will ever have to hand."""
    _, payload = await call(
        mcp_url,
        "reply_to_moot",
        token=linked_token,
        take=f"https://fanmoot.com/m/{SHORT_ID}",
        body="Rubbish, he was superb.",
    )

    assert payload["replying_to"] == PARENT_ID


async def test_a_reply_says_it_is_a_reply(linked_token: str, mcp_url: str) -> None:
    spoken, _ = await call(
        mcp_url, "reply_to_moot", token=linked_token, take=PARENT_ID, body="Rubbish."
    )

    assert spoken.startswith("Posted your reply.")


async def test_something_that_is_not_a_take_is_a_question_not_a_404(
    linked_token: str, mcp_url: str
) -> None:
    spoken = await refusal(
        mcp_url, "reply_to_moot", token=linked_token, take="the one about the ref", body="No."
    )

    assert spoken == "That does not look like a Fanmoot take. Say the link again?"


# ── react_to_moot (S5-4) ─────────────────────────────────────────────────────
async def test_a_reaction_is_set_and_read_back(linked_token: str, mcp_url: str) -> None:
    """Fanmoot's five are football-flavoured on purpose: "like" says nothing
    about a tactical argument."""
    spoken, payload = await call(
        mcp_url, "react_to_moot", token=linked_token, take=POSTED_MOOT_ID, reaction="respect"
    )

    assert payload["reaction"] == "respect"
    assert payload["reaction_count"] == 13
    assert payload["reactions"]["respect"] == 10
    assert spoken == "Done — gave it respect. Thirteen reactions on it now."


async def test_clearing_a_reaction_says_so(linked_token: str, mcp_url: str, replay: Replay) -> None:
    """One call sets, changes or clears — reacting again with the one already
    given is how a fan takes it back."""
    replay.stub(
        f"/v1/moots/{POSTED_MOOT_ID}/react",
        {
            "moot_id": POSTED_MOOT_ID,
            "my_reaction": None,
            "reaction_count": 12,
            "reaction_counts": {"respect": 9, "heat": 3},
        },
        method="POST",
    )

    spoken, payload = await call(
        mcp_url, "react_to_moot", token=linked_token, take=POSTED_MOOT_ID, reaction="respect"
    )

    assert payload["reaction"] is None
    assert spoken == "Cleared your reaction on that take."


async def test_a_reaction_outside_the_five_never_reaches_fanmoot(
    linked_token: str, mcp_url: str, replay: Replay
) -> None:
    """The set is a ``Literal`` in the input schema, so a model reads the five
    rather than guessing one."""
    spoken = await refusal(
        mcp_url, "react_to_moot", token=linked_token, take=POSTED_MOOT_ID, reaction="like"
    )

    assert "reaction" in spoken
    assert not [c for c in replay.calls if c.url.path.endswith("/react")]


# ── The gate in front of all of it (S5-9) ────────────────────────────────────
@pytest.mark.parametrize("tool", sorted(scopes.TOOL_SCOPES))
async def test_every_authenticated_tool_is_challenged_without_a_token(
    oauth_env: object, oauth_db: object, mcp_url: str, tool: str
) -> None:
    """The 401 Alexa starts account linking from (`S4-9`), asserted for all
    seven rather than for the one that happened to be written first.

    Posted as a raw frame because the assertion is about the HTTP status and
    the challenge header, and an SDK that turned a 401 into an exception would
    hide exactly that.
    """
    frame: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": {}},
    }
    async with httpx.AsyncClient() as http:
        response = await http.post(
            mcp_url,
            json=frame,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

    assert response.status_code == 401, tool
    challenge = response.headers["www-authenticate"]
    assert "resource_metadata=" in challenge, tool
    assert response.json()["scope"] == scopes.TOOL_SCOPES[tool], tool
