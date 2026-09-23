"""Structured football, turned into a sentence somebody can say out loud.

``S3-1``, and the reason every read tool is three lines long: a tool fetches,
shapes and then calls in here for the one thing it will actually be *heard*
saying. Every ``content[0].text`` on this server comes from this module, so the
server has **one** voice rather than twelve (HAD-7).

What that means concretely, and what the tests below assert:

* **Scores are spoken, not printed.** "Arsenal are two-one up at the Emirates,
  sixty-seventh minute" — never "Arsenal 2 - 1 Tottenham, 67'". Nil, not zero.
  Two-all, not two-two.
* **Times are relative and local.** "In about two hours", "on Saturday at half
  past three". An ISO timestamp read aloud in a kitchen is noise.
* **The clock is arithmetic.** Fanmoot sends a clock's *origin* — the period it
  is in and when that period started — and the reading is computed wherever it
  is rendered. ``clock_reading`` mirrors ``app/services/match_clock.derive``
  and ``web/src/hooks/useLiveClock.ts`` upstream, and must keep mirroring them:
  a rule added on one side and not the other is this server telling a fan a
  different minute from the one on the screen in front of them.
* **An unobserved score is not spoken.** ``score_state == "assumed"`` is the
  0–0 a server-side clock started the match at, which nobody has looked at
  since. Saying "it's nil-nil" then is saying something nobody checked, so the
  lines below say the match is under way and that the score is unconfirmed.

Nothing here fetches, and nothing here knows about MCP. It takes the mirrors
from ``app.fanmoot.models`` and returns strings, which is what makes the voice
of this server testable without a server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import untrusted
from app.fanmoot.models import ClockOut, MatchOut, MootOut
from app.logging_config import get_logger
from app.settings import get_settings

log = get_logger("voice")

# ── The clock ────────────────────────────────────────────────────────────────
#: The minute each period counts up to before it is into added time. Mirrors
#: ``PERIOD_CAP`` in Fanmoot's ``app/models/match.py``.
_PERIOD_CAP = {"1h": 45, "ht": 45, "2h": 90, "et": 120, "pen": 120}

#: Match lifecycle, mirrored from upstream. Four values, and the clock's period
#: is deliberately *not* one of them: half-time is unambiguously ``live`` with
#: a stopped clock.
LIVE = "live"
SCHEDULED = "scheduled"
FINISHED = "finished"
POSTPONED = "postponed"

#: A score nobody has observed. See the module docstring.
ASSUMED = "assumed"

# ── Numbers ──────────────────────────────────────────────────────────────────
_CARDINALS = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
]

_TENS = {
    2: "twenty",
    3: "thirty",
    4: "forty",
    5: "fifty",
    6: "sixty",
    7: "seventy",
    8: "eighty",
    9: "ninety",
}

_ORDINALS = [
    "zeroth",
    "first",
    "second",
    "third",
    "fourth",
    "fifth",
    "sixth",
    "seventh",
    "eighth",
    "ninth",
    "tenth",
    "eleventh",
    "twelfth",
    "thirteenth",
    "fourteenth",
    "fifteenth",
    "sixteenth",
    "seventeenth",
    "eighteenth",
    "nineteenth",
]

_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def number_words(value: int) -> str:
    """``67`` → ``"sixty-seven"``.

    Falls back to digits above ninety-nine, which no scoreline, minute or
    result count on this server reaches — and where digits are what a model
    would say anyway ("a hundred and twelve replies" is not an improvement on
    "112 replies" when it is a number a fan will not remember either way).
    """
    if value < 0:
        return str(value)
    if value < len(_CARDINALS):
        return _CARDINALS[value]
    if value < 100:
        tens, unit = divmod(value, 10)
        return _TENS[tens] if unit == 0 else f"{_TENS[tens]}-{_CARDINALS[unit]}"
    return str(value)


def ordinal_words(value: int) -> str:
    """``67`` → ``"sixty-seventh"``. Used for the minute and for a date."""
    if value < 0:
        return str(value)
    if value < len(_ORDINALS):
        return _ORDINALS[value]
    if value < 100:
        tens, unit = divmod(value, 10)
        if unit == 0:
            return _TENS[tens].removesuffix("y") + "ieth"
        return f"{_TENS[tens]}-{_ORDINALS[unit]}"
    return str(value)


def score_word(value: int | None) -> str:
    """A goal tally as football says it: ``0`` is **nil**, never zero."""
    if value is None:
        return "no"
    return "nil" if value == 0 else number_words(value)


def count_words(value: int, singular: str, plural: str | None = None) -> str:
    """``4, "fixture"`` → ``"four fixtures"``; ``1`` → ``"one fixture"``."""
    noun = singular if value == 1 else (plural or f"{singular}s")
    return f"{number_words(value)} {noun}"


def sentence(text: str) -> str:
    """A line that begins with a counted noun, made to begin like a sentence.

    "five takes on Fanmoot right now" is built out of ``count_words``, which
    has no way of knowing whether it is starting a sentence or sitting inside
    one. This is the one place that decides.
    """
    return f"{text[0].upper()}{text[1:]}" if text else text


def join_words(parts: list[str]) -> str:
    """``["a", "b", "c"]`` → ``"a, b and c"`` — the shape of a spoken question."""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} and {parts[-1]}"


# ── Time ─────────────────────────────────────────────────────────────────────
def now_utc() -> datetime:
    return datetime.now(UTC)


def _zone() -> ZoneInfo:
    """The zone times are spoken in.

    A voice surface has to say a kick-off in *some* zone, and an MCP request
    carries no hint of the fan's: there is no locale in ``initialize`` and no
    timezone in a tool call. So one is configured (``FMCP_VOICE_TIMEZONE``,
    Europe/London by default, which is where Fanmoot's fixtures are listed),
    and saying which one it is beats inventing a per-request guess that would
    be wrong in a way nobody could see.
    """
    name = get_settings().voice_timezone
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - misconfiguration
        log.warning("voice_timezone_unknown", timezone=name)
        return ZoneInfo("UTC")


def local(moment: datetime) -> datetime:
    """A UTC instant, in the zone this server speaks."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_zone())


def clock_time(moment: datetime) -> str:
    """``15:30`` → ``"half past three"``; ``19:45`` → ``"quarter to eight"``.

    Kick-offs land on the quarter hour almost without exception, so the
    quarter-hour forms carry nearly every real case and the rest degrades to
    "seven twenty-two" rather than to something elaborate nobody says.
    """
    at = local(moment)
    hour = at.hour % 12 or 12
    next_hour = (at.hour + 1) % 12 or 12
    minute = at.minute

    if minute == 0:
        return f"{number_words(hour)} o'clock"
    if minute == 15:
        return f"quarter past {number_words(hour)}"
    if minute == 30:
        return f"half past {number_words(hour)}"
    if minute == 45:
        return f"quarter to {number_words(next_hour)}"
    if minute % 5 == 0:
        if minute < 30:
            return f"{number_words(minute)} past {number_words(hour)}"
        return f"{number_words(60 - minute)} to {number_words(next_hour)}"
    return f"{number_words(hour)} {number_words(minute)}"


def day_phrase(moment: datetime, *, now: datetime | None = None) -> str:
    """``"today"``, ``"tomorrow"``, ``"Saturday"``, ``"the fourteenth of October"``."""
    at = local(moment)
    today = local(now or now_utc()).date()
    days = (at.date() - today).days

    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days == -1:
        return "yesterday"
    if 1 < days < 7:
        return at.strftime("%A")
    if -7 < days < -1:
        return f"last {at.strftime('%A')}"
    return f"the {ordinal_words(at.day)} of {_MONTHS[at.month - 1]}"


def _part_of_day(moment: datetime) -> str:
    hour = local(moment).hour
    if hour < 12:
        return "this morning"
    if hour < 17:
        return "this afternoon"
    return "tonight"


def kickoff_phrase(kickoff: datetime, *, now: datetime | None = None) -> str:
    """When a match starts, in the terms a fan uses.

    "In about two hours" for something imminent, a clock time for later today,
    a day and a time for the rest of the week. Never a date a fan would have to
    convert.
    """
    moment = now or now_utc()
    minutes = round((kickoff - moment).total_seconds() / 60)

    if minutes < 0:
        return _past_phrase(kickoff, now=moment)
    if minutes < 2:
        return "any moment now"
    if minutes < 15:
        return f"in {count_words(minutes, 'minute')}"
    if minutes < 60:
        return f"in about {count_words(5 * round(minutes / 5), 'minute')}"
    if minutes < 100:
        return "in just over an hour"
    if minutes < 6 * 60:
        return f"in about {count_words(round(minutes / 60), 'hour')}"

    day = day_phrase(kickoff, now=moment)
    if day == "today":
        return f"at {clock_time(kickoff)} {_part_of_day(kickoff)}"
    if day.startswith("the "):
        return f"on {day}"
    return f"{day} at {clock_time(kickoff)}"


def _past_phrase(moment: datetime, *, now: datetime) -> str:
    day = day_phrase(moment, now=now)
    if day == "today":
        return f"{_part_of_day(moment)}"
    return day if day.startswith(("last", "the")) else day


# ── The match clock ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ClockReading:
    """What minute it is, and whether there is a minute to give at all.

    ``minute`` is ``None`` at half-time-with-no-reading, in penalties, and on a
    clock that stopped without a result being confirmed. ``spoken`` is empty in
    exactly those cases, so a caller can append it unconditionally.
    """

    minute: int | None
    extra: int | None
    spoken: str
    running: bool


def clock_reading(clock: ClockOut | None, *, now: datetime | None = None) -> ClockReading:
    """Fanmoot's clock origin → the minute, computed here.

    Mirrors ``derive`` upstream, including the two rules that matter most:
    a period with no start is a clock stopped on purpose and shows nothing
    rather than freezing on its last minute, and a period past its cap shows
    added time — "forty-five plus two" — because nothing here can know how much
    the fourth official gave.
    """
    if clock is None:
        return ClockReading(None, None, "", False)

    if clock.period == "ht":
        return ClockReading(45, None, "half time", False)
    if clock.period == "pen":
        return ClockReading(None, None, "penalties", False)

    if clock.started_at is None or not clock.running:
        return ClockReading(None, None, "", False)

    elapsed = int((now or now_utc()).timestamp() - clock.started_at.timestamp()) // 60
    played = clock.base_minute + max(0, elapsed)
    cap = _PERIOD_CAP.get(clock.period, 90)

    if played <= cap:
        return ClockReading(played, None, f"{ordinal_words(played)} minute", True)

    extra = played - cap
    spoken = f"{number_words(cap)} plus {number_words(extra)}"
    return ClockReading(cap, extra, spoken, True)


# ── Matches ──────────────────────────────────────────────────────────────────
def club_name(match: MatchOut, side: str) -> str:
    """The name to say for one side.

    ``short_name`` — "Brighton", "Spurs", "Barça" — is what a fan says, and
    what a scoreline with two long names needs to stay inside one breath.
    """
    club = match.home if side == "home" else match.away
    if club is None:
        return "someone"
    return club.short_name or club.name


def scoreline(match: MatchOut) -> str:
    """The score, from the leader's point of view.

    "Arsenal are two-one up", "it's two-all", "Brighton beat Arsenal three-nil".
    Which of those you get depends on the status; the arithmetic is the same.
    """
    home, away = club_name(match, "home"), club_name(match, "away")
    home_score, away_score = match.home_score or 0, match.away_score or 0
    finished = match.status == FINISHED

    if home_score == away_score:
        level = "nil-nil" if home_score == 0 else f"{score_word(home_score)}-all"
        if finished:
            return f"{home} and {away} finished {level}"
        return f"{home} and {away} are {level}"

    leader, trailer = (home, away) if home_score > away_score else (away, home)
    high, low = max(home_score, away_score), min(home_score, away_score)
    if finished:
        return f"{leader} beat {trailer} {score_word(high)}-{score_word(low)}"
    return f"{leader} are {score_word(high)}-{score_word(low)} up"


def _at_venue(match: MatchOut) -> str:
    return f" at {match.venue}" if match.venue else ""


def match_line(match: MatchOut, *, now: datetime | None = None) -> str:
    """One match, as one sentence — whatever state it is in."""
    moment = now or now_utc()
    home, away = club_name(match, "home"), club_name(match, "away")

    if match.status == POSTPONED:
        return f"{home} against {away} is postponed."

    if match.status == SCHEDULED:
        when = kickoff_phrase(match.kickoff_at, now=moment)
        # The comma belongs after a venue and nowhere else: "Arsenal play Leeds
        # at the Emirates, in about two hours" reads as one breath, and
        # "Arsenal play Leeds, on Saturday" reads as two.
        if match.venue:
            return f"{home} play {away} at {match.venue}, {when}."
        return f"{home} play {away} {when}."

    if match.status == FINISHED:
        if match.score_state == ASSUMED:
            return f"{home} against {away} is over, but the result is not confirmed yet."
        day = day_phrase(match.kickoff_at, now=moment)
        return f"{scoreline(match)}{_at_venue(match)}, {day}."

    # Live. An unobserved score is the one thing this module will not say.
    if match.score_state == ASSUMED:
        return (
            f"{home} and {away} are under way{_at_venue(match)}, "
            "but nobody has confirmed a score yet."
        )

    reading = clock_reading(match.clock, now=moment)
    if reading.spoken == "half time":
        return f"It's half time{_at_venue(match)}: {scoreline(match)}."
    if reading.spoken:
        return f"{scoreline(match)}{_at_venue(match)}, {reading.spoken}."
    return f"{scoreline(match)}{_at_venue(match)}."


def fixtures_line(
    matches: list[MatchOut], *, remaining: int = 0, now: datetime | None = None
) -> str:
    """A short list of fixtures, read the way a person reads a list: the first
    couple in full, the rest counted."""
    if not matches:
        return "I cannot find any fixtures for that."

    moment = now or now_utc()
    spoken = [match_line(match, now=moment) for match in matches[:2]]

    # Two read in full, and everything else counted in *one* sentence — the
    # ones further down this answer and the ones past the limit together. Two
    # hints in a row ("there is one other too; there is one more fixture") is
    # accurate and unlistenable, and a fan cannot hear the distinction anyway.
    rest = max(0, len(matches) - 2) + max(0, remaining)
    if rest:
        spoken.append(more_hint(rest, "fixture"))
    return " ".join(spoken)


def live_line(matches: list[MatchOut], *, remaining: int = 0, now: datetime | None = None) -> str:
    """Everything in play, or the honest answer that nothing is."""
    if not matches:
        return "Nothing is being played right now."

    moment = now or now_utc()
    spoken = [match_line(match, now=moment) for match in matches[:3]]
    if remaining > 0:
        spoken.append(more_hint(remaining, "match", "matches"))
    return " ".join(spoken)


def more_hint(remaining: int, singular: str, plural: str | None = None) -> str:
    """The ``has_more`` hint, said out loud (``S3-9``).

    A voice surface cannot read fifty fixtures, so it says how many it did not
    read. "There are four more fixtures" is the sentence that keeps a bounded
    list honest rather than silently truncated.
    """
    if remaining <= 0:
        return ""
    verb = "is" if remaining == 1 else "are"
    noun = singular if remaining == 1 else (plural or f"{singular}s")
    return f"There {verb} {number_words(remaining)} more {noun}."


# ── The conversation ─────────────────────────────────────────────────────────
#: How much of a take is worth reading aloud before it stops being a quote and
#: starts being a monologue.
QUOTE_LIMIT = 180

#: A URL in a take. Fans paste them constantly, and the server instructions say
#: never to read one aloud — so a quote drops the link and reads the words
#: around it.
_LINK = re.compile(r"https?://\S+")


def take(moot: MootOut) -> str:
    """One moot, attributed and trimmed.

    Attribution is not decoration: the instructions tell the model these are
    strangers talking, and a quoted opinion read without a name attached is an
    opinion the fan will hear as the assistant's own.
    """
    body = untrusted.clean(_LINK.sub("", moot.body))
    if not body:
        # A take that was nothing but a link still has to say something.
        return f"{untrusted.quotable(moot.author.name or moot.author.handle)} shared a link."
    if len(body) > QUOTE_LIMIT:
        # Trimmed at a word, with the punctuation that word was carrying
        # dropped: "…on the pitch.…" is a sentence that ended twice.
        body = body[: QUOTE_LIMIT - 1].rsplit(" ", 1)[0].rstrip(".,;:!? ") + "…"
    # Quoted, and the quote cannot be closed from inside (`S7-3`): what a
    # stranger typed reaches the model as something somebody said, never as a
    # sentence of this server's.
    return untrusted.quote(moot.author.name or moot.author.handle, body)


def conversation_line(moots: list[MootOut], *, subject: str = "this", remaining: int = 0) -> str:
    """The room, then one or two voices from it.

    Deliberately a summary *count* plus a couple of quotes rather than a list:
    a thread read out in full is unlistenable, and the model is told to
    summarise the mood and quote at most one or two takes.
    """
    if not moots:
        return f"Nobody has said anything about {subject} yet."

    spoken = [sentence(f"{count_words(len(moots), 'take')} on {subject}.")]
    spoken.extend(take(moot) for moot in moots[:2])
    if remaining > 0:
        spoken.append(more_hint(remaining, "take"))
    return " ".join(spoken)


def thread_line(root: MootOut, replies: list[MootOut], *, remaining: int = 0) -> str:
    """One take and what came back at it."""
    spoken = [take(root)]
    if not replies:
        spoken.append("Nobody has replied yet.")
        return " ".join(spoken)

    spoken.append(sentence(f"{count_words(len(replies), 'reply', 'replies')} so far."))
    spoken.append(take(replies[0]))
    if remaining > 0:
        spoken.append(more_hint(remaining, "reply", "replies"))
    return " ".join(spoken)


#: How a sentiment label sounds when it is spoken rather than tabulated. The
#: structured payload keeps the neutral machine words; only the ear gets these.
MOOD_WORDS = {"positive": "happy", "negative": "furious", "neutral": "unmoved"}


def mood_line(
    *, positive: int, negative: int, neutral: int, subject: str, summary: str = ""
) -> str:
    """The sentiment split on a thread, as a sentence.

    The counts come first because they are what was measured; the model's
    one-line summary follows, when there is one.
    """
    total = positive + negative + neutral
    if total == 0:
        return f"The thread on {subject} is empty, so there is no mood to read."

    lead = "split"
    if positive > negative * 2:
        lead = "happy"
    elif negative > positive * 2:
        lead = "furious"

    counts = join_words(
        [
            f"{number_words(positive)} happy",
            f"{number_words(negative)} furious",
            f"{number_words(neutral)} neither",
        ]
    )
    spoken = f"The mood on {subject} is {lead}: out of {count_words(total, 'take')}, {counts}."
    return f"{spoken} {summary.strip()}".strip() if summary else spoken


# ── Catalogue ────────────────────────────────────────────────────────────────
def club_line(name: str, *, competition: str = "", nickname: str = "", members: int = 0) -> str:
    """A club, placed. "Arsenal, the Gunners, in the Premier League." """
    parts = [name]
    if nickname:
        parts.append(f"the {nickname}" if not nickname.lower().startswith("the") else nickname)
    line = ", ".join(parts)
    if competition:
        joiner = ", in the" if len(parts) > 1 else " play in the"
        line = f"{line}{joiner} {competition}"
    if members:
        counted = count_words(members, "fan")
        line = f"{line}. {counted[0].upper()}{counted[1:]} on Fanmoot"
    return f"{line}."


def ask_which(candidates: list[str], *, kind: str = "club") -> str:
    """The question a resolver asks instead of guessing (HAD-7).

    "Arsenal, Aston Villa or Athletic Club?" — the one sentence that turns a
    422 into a conversation.
    """
    if not candidates:
        return f"I cannot find that {kind} on Fanmoot."
    if len(candidates) == 1:
        return f"Do you mean {candidates[0]}?"
    return f"Do you mean {', '.join(candidates[:-1])} or {candidates[-1]}?"


def search_line(
    *,
    query: str,
    clubs: list[str],
    competitions: list[str],
    people: int,
    takes: int,
    matches: int,
) -> str:
    """What one search turned up, in the order a fan would want it.

    Names first — a fan searching "arsenal" almost always means the club — then
    the volume of everything else as counts, because reading five headlines and
    five takes aloud is not an answer to "search for X".
    """
    found: list[str] = []
    if clubs:
        found.append(f"{count_words(len(clubs), 'club')}, {join_words(clubs[:3])}")
    if competitions:
        counted = count_words(len(competitions), "competition")
        found.append(f"{counted}, {join_words(competitions[:2])}")
    if matches:
        found.append(count_words(matches, "match", "matches"))
    if takes:
        found.append(count_words(takes, "take"))
    if people:
        found.append(count_words(people, "fan"))

    if not found:
        return f"Nothing on Fanmoot matches {query}."
    return f"For {query}: {join_words(found)}."


def news_line(headlines: list[str], *, club: str = "", remaining: int = 0) -> str:
    """Club news, read as a bulletin rather than as a list of links."""
    subject = f"{club}" if club else "Fanmoot"
    if not headlines:
        return f"No recent news for {subject}."

    spoken = [sentence(f"{count_words(len(headlines), 'story', 'stories')} for {subject}.")]
    spoken.extend(headline.rstrip(".") + "." for headline in headlines[:2])
    if remaining > 0:
        spoken.append(more_hint(remaining, "story", "stories"))
    return " ".join(spoken)


# ── Acting as the fan (S5) ───────────────────────────────────────────────────
#: How a reaction sounds when it is said rather than tapped. Fanmoot's set is
#: football-flavoured on purpose — "like" says nothing about a tactical
#: argument — so the spoken form keeps the flavour rather than flattening all
#: five into "reacted".
REACTION_PHRASES = {
    "respect": "gave it respect",
    "heat": "called it heat",
    "tactics": "marked it tactics",
    "banter": "marked it banter",
    "offside": "flagged it offside",
}


def fan_line(
    *,
    handle: str,
    club: str = "",
    tenure_days: int = 0,
    followers: int = 0,
    unverified: bool = False,
) -> str:
    """Who the fan is, as they would introduce themselves.

    The handle is said with "at" in front of it because that is how a fan reads
    one out, and the club comes before the numbers because on Fanmoot the club
    *is* the identity — the follower count is trivia next to it.
    """
    opening = f"You are @{handle}"
    if club:
        opening = (
            f"{opening}, an {club} fan"
            if club[0].lower() in "aeiou"
            else (f"{opening}, a {club} fan")
        )
        if tenure_days > 0:
            opening = f"{opening} for {count_words(tenure_days, 'day')}"
    spoken = [f"{opening}."]
    if followers:
        counted = count_words(followers, "fan")
        spoken.append(f"{sentence(counted)} follow you.")
    if unverified:
        # Said out loud rather than discovered when a take goes missing: an
        # unconfirmed address cannot post, and that is worth knowing before
        # dictating a paragraph (`S5-8`).
        spoken.append("Your email address is not confirmed yet, so you cannot post until it is.")
    return " ".join(spoken)


def posted_line(
    moot: MootOut, *, held: bool = False, gate_reason: str = "", replying: bool = False
) -> str:
    """What just went to Fanmoot, honestly (HAD-7, ``S5-2``).

    A held moot is **never** reported as live. Fanmoot's football-topicality
    gate can hold a take for a moderator, and the reason it gives is passed on
    word for word — it was written for the person who wrote the take, and
    paraphrasing it here would be this server inventing a moderation policy.
    """
    what = "reply" if replying else "take"
    if held:
        opening = f"Your {what} is with Fanmoot for review rather than live."
        return f"{opening} {gate_reason}".rstrip() if gate_reason else opening
    where = f" It's on your profile as @{moot.author.handle}." if moot.author.handle else ""
    return f"Posted your {what}.{where}"


def reaction_line(kind: str | None, *, total: int = 0) -> str:
    """A reaction set, changed or cleared — the same call does all three."""
    if kind is None:
        return "Cleared your reaction on that take."
    phrase = REACTION_PHRASES.get(kind, f"marked it {kind}")
    counted = f" {sentence(count_words(total, 'reaction'))} on it now." if total else ""
    return f"Done — {phrase}.{counted}"


def prediction_line(
    *, home: str, away: str, home_score: int, away_score: int, kickoff: str = ""
) -> str:
    """A scoreline called before kickoff, read back for confirmation."""
    called = f"{home} {score_word(home_score)}, {away} {score_word(away_score)}"
    when = f" {kickoff}" if kickoff else ""
    return f"Called it: {called}{when}."


def predictions_line(*, called: int, open_matches: int) -> str:
    """What the fan has called, and what is still open to them."""
    if not called and not open_matches:
        return "You have not called anything, and nothing is open to predict right now."
    parts: list[str] = []
    if called:
        parts.append(sentence(f"{count_words(called, 'prediction')} on the go."))
    else:
        parts.append("You have not called anything yet.")
    if open_matches:
        parts.append(f"{sentence(count_words(open_matches, 'match', 'matches'))} still open.")
    return " ".join(parts)


def follow_line(*, name: str, kind: str = "club", followers: int = 0) -> str:
    """A follow, confirmed.

    No "already following" variant, and that is upstream's shape rather than an
    omission: ``POST /v1/follow`` is idempotent and answers with the state, not
    with whether this call is what created it. So the sentence says what is
    true either way, which is also what a fan who asked twice wants to hear.
    """
    what = {"user": "fan", "league": "competition"}.get(kind, kind)
    counted = f" {sentence(count_words(followers, 'follower'))} now." if followers else ""
    return f"Following {name}, the {what}.{counted}"
