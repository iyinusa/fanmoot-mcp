"""``S3-1`` — the one voice this server speaks in.

These are the assertions that keep a scoreline from turning back into a table.
Every one of them is a sentence somebody would say out loud, and the tests are
written as those sentences rather than as field comparisons, because that is
what is actually being checked: not that the data arrived, but that it can be
*heard*.
"""

from __future__ import annotations

from datetime import timedelta

from app import voice
from app.fanmoot.models import MatchOut, MootOut
from tests import scenarios

NOW = scenarios.NOW


def line(**overrides: object) -> str:
    match = MatchOut.model_validate(scenarios.match(**overrides))  # type: ignore[arg-type]
    return voice.match_line(match, now=NOW)


# ── Numbers ──────────────────────────────────────────────────────────────────
def test_a_goal_tally_says_nil() -> None:
    """Football does not say zero, and a model reading "zero" out loud is the
    single clearest tell that nobody wrote this for the ear."""
    assert voice.score_word(0) == "nil"
    assert voice.score_word(1) == "one"
    assert voice.score_word(3) == "three"


def test_numbers_and_ordinals_are_words_up_to_ninety_nine() -> None:
    assert voice.number_words(67) == "sixty-seven"
    assert voice.number_words(40) == "forty"
    assert voice.ordinal_words(67) == "sixty-seventh"
    assert voice.ordinal_words(30) == "thirtieth"
    assert voice.ordinal_words(1) == "first"
    # Past ninety-nine, digits — a number a fan will not remember either way.
    assert voice.number_words(112) == "112"


def test_counts_agree_with_their_noun() -> None:
    assert voice.count_words(1, "fixture") == "one fixture"
    assert voice.count_words(4, "fixture") == "four fixtures"
    assert voice.count_words(2, "reply", "replies") == "two replies"


def test_a_spoken_list_uses_and_not_a_comma() -> None:
    assert voice.join_words(["Arsenal", "Aston Villa", "Athletic Club"]) == (
        "Arsenal, Aston Villa and Athletic Club"
    )


# ── The clock ────────────────────────────────────────────────────────────────
def test_the_minute_is_computed_from_the_clocks_origin() -> None:
    """Fanmoot sends where the period started, not what the clock reads —
    which is why its live payload is identical between goals (HAD-6)."""
    reading = voice.clock_reading(
        _clock(scenarios.clock("2h", base_minute=45, minutes_in=22)), now=NOW
    )

    assert reading.minute == 67
    assert reading.spoken == "sixty-seventh minute"
    assert reading.running is True


def test_past_the_cap_the_clock_speaks_added_time() -> None:
    """A server-computed clock cannot know what the fourth official gave, so it
    holds at ninety and counts the extra separately."""
    reading = voice.clock_reading(
        _clock(scenarios.clock("2h", base_minute=45, minutes_in=48)), now=NOW
    )

    assert (reading.minute, reading.extra) == (90, 3)
    assert reading.spoken == "ninety plus three"


def test_half_time_is_a_stopped_clock_not_a_minute() -> None:
    reading = voice.clock_reading(_clock(scenarios.clock("ht")), now=NOW)

    assert reading.spoken == "half time"
    assert reading.running is False


def test_a_stopped_clock_says_nothing_rather_than_freezing() -> None:
    """A period with no start is a clock stopped on purpose: the computed
    timeline ran out and nobody confirmed what happened. Showing its last
    minute would be a stopped clock pretending to run."""
    stopped = scenarios.clock("2h")
    stopped["started_at"] = None
    stopped["running"] = False

    reading = voice.clock_reading(_clock(stopped), now=NOW)

    assert reading.minute is None
    assert reading.spoken == ""


# ── Scorelines ───────────────────────────────────────────────────────────────
def test_a_live_scoreline_is_spoken_from_the_leaders_side() -> None:
    assert line() == "Arsenal are two-one up at the Emirates, sixty-seventh minute."


def test_a_draw_is_two_all_and_a_goalless_one_is_nil_nil() -> None:
    assert "are two-all" in line(home_score=2, away_score=2)
    assert "finished nil-nil" in line(
        status="finished", home_score=0, away_score=0, with_clock=False
    )


def test_half_time_leads_with_the_break_then_the_score() -> None:
    match = scenarios.match()
    match["clock"] = scenarios.clock("ht")

    spoken = voice.match_line(MatchOut.model_validate(match), now=NOW)

    assert spoken == "It's half time at the Emirates: Arsenal are two-one up."


def test_an_unobserved_score_is_never_stated() -> None:
    """``score_state == "assumed"`` is the nil-nil a server-side clock started
    the match at, which nobody has looked at since. Saying "it's nil-nil" then
    is saying something nobody checked."""
    spoken = line(score_state="assumed", home_score=0, away_score=0)

    assert "nil" not in spoken
    assert "under way" in spoken
    assert "nobody has confirmed a score yet" in spoken


def test_a_result_is_spoken_as_a_result() -> None:
    spoken = line(status="finished", home_score=3, away_score=0, with_clock=False)

    assert spoken.startswith("Arsenal beat Spurs three-nil")


def test_a_postponed_fixture_says_so_and_stops() -> None:
    assert line(status="postponed") == "Arsenal against Spurs is postponed."


# ── Time ─────────────────────────────────────────────────────────────────────
def test_a_kick_off_is_relative_while_it_is_close() -> None:
    assert _kickoff(minutes=8) == "in eight minutes"
    assert _kickoff(minutes=37) == "in about thirty-five minutes"
    assert _kickoff(minutes=95) == "in just over an hour"
    assert _kickoff(minutes=150) == "in about two hours"


def test_a_kick_off_further_out_is_a_day_and_a_clock_time() -> None:
    assert _kickoff(days=4, minutes=30) == "Saturday at half past four"
    assert _kickoff(days=1, minutes=-90) == "tomorrow at half past two"
    assert _kickoff(days=20) == "on the twelfth of October"


def test_clock_times_are_said_the_way_a_kick_off_is_said() -> None:
    assert voice.clock_time(NOW.replace(hour=14, minute=0)) == "three o'clock"
    assert voice.clock_time(NOW.replace(hour=11, minute=30)) == "half past twelve"
    assert voice.clock_time(NOW.replace(hour=18, minute=45)) == "quarter to eight"
    assert voice.clock_time(NOW.replace(hour=16, minute=20)) == "twenty past five"


def test_a_day_is_named_before_it_is_dated() -> None:
    assert voice.day_phrase(NOW, now=NOW) == "today"
    assert voice.day_phrase(NOW + timedelta(days=1), now=NOW) == "tomorrow"
    assert voice.day_phrase(NOW + timedelta(days=3), now=NOW) == "Friday"
    assert voice.day_phrase(NOW - timedelta(days=3), now=NOW) == "last Saturday"
    assert voice.day_phrase(NOW + timedelta(days=30), now=NOW) == "the twenty-second of October"


# ── Lists ────────────────────────────────────────────────────────────────────
def test_a_bounded_list_says_how_much_it_left_out() -> None:
    """``S3-9``. A bounded list that does not say it is bounded is a list that
    lies about what is on this weekend."""
    assert voice.more_hint(4, "fixture") == "There are four more fixtures."
    assert voice.more_hint(1, "reply", "replies") == "There is one more reply."
    assert voice.more_hint(0, "fixture") == ""


def test_an_empty_fixture_list_is_an_answer_not_a_blank() -> None:
    assert voice.fixtures_line([]) == "I cannot find any fixtures for that."
    assert voice.live_line([]) == "Nothing is being played right now."


# ── The conversation ─────────────────────────────────────────────────────────
def test_a_take_is_attributed_to_whoever_said_it() -> None:
    """The instructions tell the model these are strangers talking. A quote
    read without a name attached is one the fan hears as the assistant's own."""
    spoken = voice.take(_moot("The second goal was offside, all day."))

    assert spoken == "Jo says: The second goal was offside, all day."


def test_a_long_take_is_trimmed_at_a_word() -> None:
    spoken = voice.take(_moot("blah " * 100))

    assert len(spoken) < 220
    assert spoken.endswith("…")


def test_a_link_inside_a_take_is_not_read_out() -> None:
    spoken = voice.take(_moot("Read this https://example.com/very/long/path it's damning"))

    assert "http" not in spoken
    assert spoken == "Jo says: Read this it's damning"


def test_the_room_is_summarised_before_anybody_is_quoted() -> None:
    takes = [_moot("one"), _moot("two"), _moot("three")]

    spoken = voice.conversation_line(takes, subject="the derby", remaining=12)

    assert spoken.startswith("Three takes on the derby.")
    assert spoken.endswith("There are twelve more takes.")


def test_a_quiet_thread_says_it_is_quiet() -> None:
    assert voice.conversation_line([], subject="the derby") == (
        "Nobody has said anything about the derby yet."
    )


# ── Mood ─────────────────────────────────────────────────────────────────────
def test_the_mood_leads_with_the_split_it_measured() -> None:
    spoken = voice.mood_line(
        positive=12, negative=3, neutral=2, subject="the derby", summary="Mostly about the penalty."
    )

    assert spoken.startswith("The mood on the derby is happy:")
    assert "twelve happy, three furious and two neither" in spoken
    assert spoken.endswith("Mostly about the penalty.")


def test_a_furious_thread_is_called_furious() -> None:
    spoken = voice.mood_line(positive=1, negative=9, neutral=1, subject="the derby")

    assert "is furious" in spoken


def test_an_even_split_is_called_split() -> None:
    spoken = voice.mood_line(positive=5, negative=4, neutral=1, subject="the derby")

    assert "is split" in spoken


# ── Asking rather than guessing ──────────────────────────────────────────────
def test_two_candidates_become_a_question() -> None:
    """HAD-7: answering "Manchester United" to a Newcastle fan who said
    "united" is worse than asking."""
    assert voice.ask_which(["Arsenal", "Aston Villa", "Athletic Club"]) == (
        "Do you mean Arsenal, Aston Villa or Athletic Club?"
    )
    assert voice.ask_which(["Arsenal"]) == "Do you mean Arsenal?"
    assert voice.ask_which([], kind="competition") == "I cannot find that competition on Fanmoot."


# ── Helpers ──────────────────────────────────────────────────────────────────
def _clock(payload: dict[str, object]) -> voice.ClockOut:
    return voice.ClockOut.model_validate(payload)


def _moot(body: str) -> MootOut:
    return MootOut.model_validate(scenarios.moot(body))


def _kickoff(*, days: int = 0, minutes: int = 0) -> str:
    return voice.kickoff_phrase(NOW + timedelta(days=days, minutes=minutes), now=NOW)
