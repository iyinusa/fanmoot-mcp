"""``S3-3``, ``S3-4`` — fixtures, results, one match, and everything in play.

Two kinds of test here, and the difference is deliberate. The fixture and
result tests replay what fanmoot.com really answered; the live tests state a
moment — "Arsenal two-one up in the sixty-seventh minute" — because the corpus
was recorded out of season and the tools that matter most are the ones that
answer during a match. See ``tests/scenarios.py``.
"""

from __future__ import annotations

from datetime import timedelta

from app import voice
from tests import scenarios
from tests.replay import Replay
from tests.tooling import call, refusal

MATCH_ID = "dd472da4-8efc-42be-9f7e-188a57aec488"
MATCH_PATH = "/matches/premier-league/2026-27/brighton-hove-albion-vs-arsenal-2026-09-19"


# ── list_matches ─────────────────────────────────────────────────────────────
async def test_fixtures_are_read_as_sentences_not_as_a_table(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "list_matches", club="arsenal")

    assert payload["count"] == 5
    assert spoken.startswith("Brighton beat Arsenal three-nil")
    # The first two in full, the rest counted — nobody listens to five fixtures.
    assert spoken.endswith("There are three more fixtures.")


async def test_a_club_is_named_the_way_a_fan_names_it(replay: Replay, mcp_url: str) -> None:
    """``list_matches`` takes "the gunners" because ``find_club`` is behind it
    (⟵ ``S1-5``), so a fan never has to know a slug."""
    _, payload = await call(mcp_url, "list_matches", club="the gunners")

    assert payload["count"] == 5


async def test_an_ambiguous_club_stops_the_call_and_asks(replay: Replay, mcp_url: str) -> None:
    """Every tool but ``find_club`` *raises* the question, because it cannot do
    its job without an answer — and the fan hears it inside the same turn."""
    spoken = await refusal(mcp_url, "list_matches", club="united")

    assert spoken.startswith("Do you mean ")


async def test_a_fixture_card_carries_the_scoreline_it_would_be_read_with(
    replay: Replay, mcp_url: str
) -> None:
    _, payload = await call(mcp_url, "list_matches", club="arsenal")
    first = payload["matches"][0]

    assert first["competition"] == "Premier League"
    assert first["home"]["short_name"] == "Brighton"
    assert first["spoken"].startswith("Brighton beat Arsenal three-nil")
    # `S3-9`: what was left behind, as a fact a client can render.
    assert payload["has_more"] is False


async def test_a_scheduled_fixture_is_not_credited_with_a_score(
    replay: Replay, mcp_url: str
) -> None:
    """Every scheduled Fanmoot fixture carries ``score_state: assumed``: the
    nil-nil it will start at, which nobody has observed."""
    _, payload = await call(mcp_url, "list_matches", club="arsenal")
    scheduled = next(m for m in payload["matches"] if m["status"] == "scheduled")

    assert scheduled["score_confirmed"] is False
    assert "nil" not in scheduled["spoken"]
    assert "play" in scheduled["spoken"]


# ── upcoming_matches ─────────────────────────────────────────────────────────
async def test_upcoming_groups_by_day_and_bounds_the_whole_answer(
    replay: Replay, mcp_url: str
) -> None:
    now = voice.now_utc()
    replay.stub(
        "/v1/matches/upcoming",
        scenarios.match_groups(
            {
                "2026-09-26": [
                    scenarios.match(
                        now=now,
                        match_id="aaaaaaaa-0000-4000-8000-000000000001",
                        status="scheduled",
                        home_score=None,
                        away_score=None,
                        kickoff_at=now + timedelta(days=4),
                    )
                ],
                "2026-09-27": [
                    scenarios.match(
                        now=now,
                        match_id=f"aaaaaaaa-0000-4000-8000-00000000000{n}",
                        status="scheduled",
                        home_score=None,
                        away_score=None,
                        kickoff_at=now + timedelta(days=5),
                    )
                    for n in (2, 3, 4)
                ],
            }
        ),
        params={"days": 14},
    )

    spoken, payload = await call(mcp_url, "upcoming_matches", limit=3)

    assert [day["day"] for day in payload["days"]] == ["2026-09-26", "2026-09-27"]
    assert payload["count"] == 3
    assert payload["has_more"] is True
    # One further down this answer, one past the limit — counted together in a
    # single sentence rather than as two hints in a row.
    assert spoken.endswith("There are two more fixtures.")


async def test_an_empty_week_is_an_answer(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "upcoming_matches")

    assert payload["count"] == 0
    assert spoken == "I cannot find any fixtures for that."


# ── get_match ────────────────────────────────────────────────────────────────
async def test_one_match_by_the_words_a_fan_would_use(replay: Replay, mcp_url: str) -> None:
    """The whole of HAD-7 in one assertion: no fan has ever seen a UUID."""
    spoken, payload = await call(mcp_url, "get_match", match="arsenal vs brighton")

    assert payload["match"]["id"] == MATCH_ID
    assert spoken.startswith("Brighton beat Arsenal three-nil")


async def test_one_match_by_id_and_by_link(replay: Replay, mcp_url: str) -> None:
    by_id = await call(mcp_url, "get_match", match=MATCH_ID)
    by_link = await call(mcp_url, "get_match", match=f"https://fanmoot.com{MATCH_PATH}")

    assert by_id[1]["match"]["id"] == by_link[1]["match"]["id"] == MATCH_ID


async def test_a_match_that_is_not_on_says_so_rather_than_guessing(
    replay: Replay, mcp_url: str
) -> None:
    """Both clubs' fixture windows are looked in before this is said: a match a
    fortnight out can sit inside the busier club's five and outside the
    quieter one's."""
    replay.stub("/v1/matches", scenarios.match_page([]), params={"club": "chelsea", "limit": 5})

    spoken = await refusal(mcp_url, "get_match", match="arsenal vs chelsea")

    assert spoken == "I cannot find a match between Arsenal and Chelsea around now."


async def test_a_busy_thread_is_mentioned_with_the_match(replay: Replay, mcp_url: str) -> None:
    now = voice.now_utc()
    live = scenarios.match(now=now, moot_count=40)
    replay.stub(f"/v1/matches/{live['id']}", live)

    spoken, payload = await call(mcp_url, "get_match", match=live["id"])

    assert payload["match"]["take_count"] == 40
    assert spoken.endswith("Forty takes on it.")


async def test_goals_and_cards_come_back_with_the_club_that_got_them(
    replay: Replay, mcp_url: str
) -> None:
    now = voice.now_utc()
    live = scenarios.match(
        now=now,
        events=[
            {
                "id": "bbbbbbbb-0000-4000-8000-000000000001",
                "kind": "goal",
                "minute": 63,
                "extra_minute": None,
                "club_id": scenarios.ARSENAL["id"],
                "player_name": "Saka",
                "detail": None,
            }
        ],
    )
    replay.stub(f"/v1/matches/{live['id']}", live)

    _, payload = await call(mcp_url, "get_match", match=live["id"])

    assert payload["events"] == [
        {
            "kind": "goal",
            "minute": 63,
            "added": None,
            "club": "Arsenal",
            "player": "Saka",
            "detail": None,
        }
    ]


# ── live_scores ──────────────────────────────────────────────────────────────
async def test_nothing_on_is_said_plainly(replay: Replay, mcp_url: str) -> None:
    spoken, payload = await call(mcp_url, "live_scores")

    assert payload["matches"] == []
    assert spoken == "Nothing is being played right now."


async def test_a_live_score_joins_the_thin_digest_to_the_match_record(
    replay: Replay, mcp_url: str
) -> None:
    """``/v1/matches/live`` carries ids, scores and clocks and no names — that
    thinness is what makes it cacheable for everybody at once (HAD-6). The
    names come from the match record, and the *score* comes from the digest."""
    now = voice.now_utc()
    record = scenarios.match(now=now, home_score=0, away_score=0)
    moved_on = scenarios.match(now=now, home_score=2, away_score=1)
    replay.stub(f"/v1/matches/{record['id']}", record)
    replay.stub("/v1/matches/live", scenarios.live_digest([moved_on]))

    spoken, payload = await call(mcp_url, "live_scores")

    assert spoken == "Arsenal are two-one up at the Emirates, sixty-seventh minute."
    assert payload["matches"][0]["home"]["score"] == 2
    assert payload["matches"][0]["minute"] == 67


async def test_a_live_score_nobody_has_confirmed_is_not_spoken(
    replay: Replay, mcp_url: str
) -> None:
    now = voice.now_utc()
    unobserved = scenarios.match(now=now, home_score=0, away_score=0, score_state="assumed")
    replay.stub(f"/v1/matches/{unobserved['id']}", unobserved)
    replay.stub("/v1/matches/live", scenarios.live_digest([unobserved]))

    spoken, payload = await call(mcp_url, "live_scores")

    assert "nil" not in spoken
    assert "nobody has confirmed a score yet" in spoken
    assert payload["matches"][0]["score_confirmed"] is False


async def test_live_scores_are_bounded_and_say_what_was_left(replay: Replay, mcp_url: str) -> None:
    now = voice.now_utc()
    playing = [
        scenarios.match(now=now, match_id=f"cccccccc-0000-4000-8000-00000000000{n}")
        for n in range(1, 5)
    ]
    for match in playing:
        replay.stub(f"/v1/matches/{match['id']}", match)
    replay.stub("/v1/matches/live", scenarios.live_digest(playing))

    spoken, payload = await call(mcp_url, "live_scores", limit=2)

    assert payload["count"] == 2
    assert payload["has_more"] is True
    assert spoken.endswith("There are two more matches.")
