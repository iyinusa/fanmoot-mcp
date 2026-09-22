"""Turning what a fan says into a club or a competition.

A tool that demands a UUID the fan cannot possibly know forces a wasted turn
(HAD-7). So "the gunners", "spurs", "los blancos" and "arsnal" all have to land,
and the ones that genuinely do not — "united" on its own — have to come back as
**a question, not a guess**. Answering "Manchester United" to a Newcastle fan
who said "united" is worse than asking.

Four passes, in order, stopping at the first that is decisive:

1. **Exact**, on the normalised slug, name, short name or nickname. Fanmoot
   carries one official nickname per club, so "the gunners" resolves here with
   no curation at all.
2. **Alias**, from ``aliases.py`` — the spoken forms the data does not carry.
   Initialisms ("psg") and substitutions ("spurs") live here because neither
   edit distance nor substring matching can reach them.
3. **Containment**, for the partial name a fan half-says: "wolverhampton",
   "borussia". One hit resolves; several become candidates.
4. **Fuzzy**, for misspellings and mishearings: "arsnal", "totenham",
   "liverpul". A single clear leader resolves; a close pack becomes candidates.

Nothing here calls Bedrock or a model. It is a normaliser, a dictionary and
``difflib``, which is what makes it fast enough to sit in front of every tool
and cheap enough to run twice in one turn.
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.fanmoot import cache as cache_module
from app.fanmoot import endpoints
from app.fanmoot.aliases import AMBIGUOUS, CLUB_ALIASES, COMPETITION_ALIASES
from app.fanmoot.client import FanmootClient
from app.fanmoot.models import ClubOut, LeagueOut

#: Tokens that say "this is a football club" and nothing about *which* one.
#: Dropped so "FC Barcelona", "Barcelona FC" and "Barcelona" are one key.
#: ``real`` is deliberately absent: it distinguishes Real Madrid from Madrid's
#: other clubs and Real Sociedad from Sociedad, so dropping it would merge
#: things a fan keeps apart.
_CLUB_WORDS = frozenset({"fc", "afc", "cf", "sc", "sv", "bk", "cd", "ud", "ac", "as", "ss"})

#: Above this, a fuzzy match is good enough to act on when it is also clearly
#: ahead of the next one.
_FUZZY_ACCEPT = 0.82
#: Below this, a fuzzy match is not worth offering as a candidate at all.
_FUZZY_OFFER = 0.62
#: How far ahead of the runner-up the leader must be to resolve without asking.
#: A small gap means two plausible readings, which is a question.
_FUZZY_MARGIN = 0.08

#: A word match is weaker evidence than a whole-name match, and the discount is
#: what separates them when both land. "arsnal" scores 0.92 against the *name*
#: Arsenal and 0.86 against the *word* "Insurance" in Bendel Insurance — close
#: enough to force a question, until the word score is held slightly cheaper.
_WORD_MATCH_DISCOUNT = 0.95

#: A voice surface cannot read a list of ten. Three is what fits in a spoken
#: question: "Arsenal, Aston Villa or Athletic Club?"
_MAX_CANDIDATES = 3


def normalise(text: str) -> str:
    """The form everything is compared in.

    Lowercased, unaccented, punctuation-free, "the" dropped from the front, and
    club-type tokens removed. ``Atlético Madrid``, ``atletico madrid`` and
    ``Club Atletico de Madrid`` all reduce to the same string, which is what
    lets a dictionary do most of this module's work.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    # Full stops close up rather than split: "F.C." is one token, and turning
    # it into "f c" leaves two single letters that no club-word list can catch.
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", stripped.lower().replace(".", ""))
    words = [w for w in cleaned.split() if w]
    if words and words[0] == "the":
        words = words[1:]
    kept = [w for w in words if w not in _CLUB_WORDS]
    # Everything was a club word ("FC", "AC") — keep the original rather than
    # reduce a query to nothing.
    return " ".join(kept or words)


@dataclass(frozen=True)
class Candidate:
    """One thing the fan might have meant, with enough context to ask about it.

    ``hint`` is what makes a spoken question answerable: "Rangers in the
    Scottish Premiership, or Queens Park Rangers?" needs the competition, not
    just the name.
    """

    slug: str
    name: str
    kind: str
    hint: str = ""
    score: float = 1.0


#: ``(slug, score) -> Candidate``. The two resolvers differ only in how they
#: describe a hit, so the passes below take the describer rather than branching
#: on "is this a club or a competition" five times.
Describe = Callable[[str, float], Candidate]


@dataclass
class Resolution:
    """What one lookup decided.

    Either ``match`` is set — we are confident — or ``candidates`` is non-empty
    and the caller must ask. Never both, and a resolution with neither means
    Fanmoot has nothing by that name at all.
    """

    query: str
    match: Candidate | None = None
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.match is not None

    @property
    def ambiguous(self) -> bool:
        return self.match is None and bool(self.candidates)

    @property
    def unknown(self) -> bool:
        return self.match is None and not self.candidates


class Catalogue:
    """Every club and competition Fanmoot knows, indexed for lookup.

    Built from ``/v1/clubs`` and ``/v1/leagues``, both of which sit behind the
    one-hour catalogue TTL — so this is rebuilt roughly hourly at a cost of a
    couple of conditional requests, not per call.
    """

    def __init__(self, clubs: list[ClubOut], leagues: list[LeagueOut]) -> None:
        self.clubs = clubs
        self.leagues = leagues
        self._clubs_by_slug = {c.slug: c for c in clubs}
        self._leagues_by_slug = {league.slug: league for league in leagues}
        self._club_index = _index_clubs(clubs)
        self._league_index = _index_leagues(leagues)

    def club(self, slug: str) -> ClubOut | None:
        return self._clubs_by_slug.get(slug)

    def competition(self, slug: str) -> LeagueOut | None:
        return self._leagues_by_slug.get(slug)

    def resolve_club(self, query: str) -> Resolution:
        return _resolve(
            query,
            index=self._club_index,
            aliases=CLUB_ALIASES,
            known=set(self._clubs_by_slug),
            kind="club",
            describe=self._describe_club,
        )

    def resolve_competition(self, query: str) -> Resolution:
        return _resolve(
            query,
            index=self._league_index,
            aliases=COMPETITION_ALIASES,
            known=set(self._leagues_by_slug),
            kind="competition",
            describe=self._describe_competition,
        )

    def _describe_club(self, slug: str, score: float) -> Candidate:
        club = self._clubs_by_slug[slug]
        hint = club.league.name if club.league else (club.country_code or "")
        return Candidate(slug=slug, name=club.name, kind="club", hint=hint, score=score)

    def _describe_competition(self, slug: str, score: float) -> Candidate:
        league = self._leagues_by_slug[slug]
        return Candidate(
            slug=slug,
            name=league.name,
            kind="competition",
            hint=league.country_code or league.kind,
            score=score,
        )


def _index_clubs(clubs: list[ClubOut]) -> dict[str, set[str]]:
    """Normalised spoken form → the slugs that form could mean.

    A set, not a slug: "rangers" is a club in Scotland and part of several
    names elsewhere, and collapsing that to one answer at index time is exactly
    the guess this module exists to avoid.
    """
    index: dict[str, set[str]] = {}
    for club in clubs:
        for surface in (club.slug.replace("-", " "), club.name, club.short_name, club.nickname):
            if surface:
                index.setdefault(normalise(surface), set()).add(club.slug)
    return index


def _index_leagues(leagues: list[LeagueOut]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for league in leagues:
        for surface in (league.slug.replace("-", " "), league.name, league.short_name):
            if surface:
                index.setdefault(normalise(surface), set()).add(league.slug)
    return index


def _resolve(
    query: str,
    *,
    index: dict[str, set[str]],
    aliases: dict[str, str],
    known: set[str],
    kind: str,
    describe: Describe,
) -> Resolution:
    key = normalise(query)
    result = Resolution(query=query)
    if not key:
        return result

    # A form that names more than one club is a question, always — checked
    # before anything else so no later pass can talk itself into an answer.
    # The candidates come from word containment rather than an exact lookup:
    # no club is *named* "united", which is precisely why the question needs
    # asking, and "Manchester United, Newcastle United or Leeds United?" is
    # only answerable if we can name three of them.
    if key in AMBIGUOUS:
        result.candidates = _offer(_containing(index, key), known, describe, 1.0)
        return result

    # 1 — exact, on a name, short name, slug or Fanmoot's own nickname.
    exact = index.get(key)
    if exact and len(exact) == 1:
        result.match = describe(next(iter(exact)), 1.0)
        return result

    # 2 — a curated spoken form. Deliberately ahead of an *ambiguous* exact
    # match, because that is what curation is for: Fanmoot has a club literally
    # slugged `magpies` as well as Newcastle, and Pisa share `Nerazzurri` with
    # Inter. A fan saying either means the famous one, and saying so once here
    # beats asking a question nobody thinks is a question.
    #
    # Only trusted if the slug still exists, so a club renamed upstream
    # degrades to the passes below rather than to nothing.
    aliased = aliases.get(key)
    if aliased and aliased in known:
        result.match = describe(aliased, 1.0)
        return result

    if exact:
        result.candidates = _offer(exact, known, describe, 1.0)
        return result

    # 3 — containment, for a half-said name.
    contained = {
        slug
        for surface, slugs in index.items()
        for slug in slugs
        if _contains_word(surface, key) or _contains_word(key, surface)
    }
    if len(contained) == 1:
        result.match = describe(next(iter(contained)), 0.9)
        return result
    if contained:
        result.candidates = _offer(contained, known, describe, 0.9)
        return result

    # 4 — fuzzy, for a misspelling or a mishearing.
    #
    # Scored against each surface *and*, for a one-word query, against each
    # word in it. "totenham" against the whole of "tottenham hotspur" scores
    # 0.64 — length alone drags it under — while against the word "tottenham"
    # it is 0.94. Fans say one word of a two-word club constantly, and a
    # misspelling of that word is the commonest failure a voice transcript
    # produces. Word scoring is held to single-token queries so that a
    # multi-word query cannot match on one shared word.
    single_word = " " not in key
    scored = sorted(
        (
            (_best_ratio(key, surfaces, words=single_word), slug)
            for slug, surfaces in _by_slug(index).items()
        ),
        reverse=True,
    )
    offers = [(score, slug) for score, slug in scored if score >= _FUZZY_OFFER]
    if not offers:
        return result

    best_score, best_slug = offers[0]
    runner_up = offers[1][0] if len(offers) > 1 else 0.0
    if best_score >= _FUZZY_ACCEPT and best_score - runner_up >= _FUZZY_MARGIN:
        result.match = describe(best_slug, best_score)
        return result

    result.candidates = [
        describe(slug, score) for score, slug in offers[:_MAX_CANDIDATES] if slug in known
    ]
    return result


def _by_slug(index: dict[str, set[str]]) -> dict[str, list[str]]:
    """Invert the index: slug → every normalised surface that names it."""
    inverted: dict[str, list[str]] = {}
    for surface, slugs in index.items():
        for slug in slugs:
            inverted.setdefault(slug, []).append(surface)
    return inverted


def _offer(slugs: set[str], known: set[str], describe: Describe, score: float) -> list[Candidate]:
    """Candidates, in a stable order a spoken question can rely on."""
    return [describe(slug, score) for slug in sorted(slugs) if slug in known][:_MAX_CANDIDATES]


def _containing(index: dict[str, set[str]], word: str) -> set[str]:
    """Every slug whose name contains this word."""
    return {
        slug for surface, slugs in index.items() if _contains_word(surface, word) for slug in slugs
    }


def _best_ratio(key: str, surfaces: list[str], *, words: bool) -> float:
    best = max(_ratio(key, surface) for surface in surfaces)
    if not words:
        return best
    for surface in surfaces:
        for word in surface.split():
            if len(word) >= 4:
                best = max(best, _ratio(key, word) * _WORD_MATCH_DISCOUNT)
    return best


def _contains_word(haystack: str, needle: str) -> bool:
    """Whole-word containment.

    Substring matching would make "inter" a hit inside "Internazionale" — which
    is wanted — and also inside "Winterslag", which is not. Word boundaries are
    the difference, and a one-word query is where it bites.
    """
    if not needle or len(needle) < 3:
        return False
    return f" {needle} " in f" {haystack} "


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


async def load_catalogue(*, client: FanmootClient | None = None) -> Catalogue:
    """Fetch the catalogue, paging clubs to the end.

    Fanmoot's ``/v1/clubs`` is keyset-paged at 100, and it has around a
    thousand clubs, so this is a handful of requests every hour — all of them
    served from cache or a ``304`` after the first.
    """
    leagues = await endpoints.list_leagues(client=client)

    clubs: list[ClubOut] = []
    cursor: str | None = None
    while True:
        page = await endpoints.list_clubs(cursor=cursor, limit=100, client=client)
        clubs.extend(page.items)
        cursor = page.next_cursor
        if not cursor or not page.items:
            break

    return Catalogue(clubs=clubs, leagues=leagues)


_catalogue: Catalogue | None = None
_catalogue_built_at = 0.0


async def get_catalogue(*, client: FanmootClient | None = None) -> Catalogue:
    """The process's catalogue, built on first use and rebuilt hourly.

    ``load_catalogue`` is cheap in *requests* — every page is behind the
    one-hour TTL and answers from cache or a ``304`` — but it still normalises
    a thousand club names into four indexes each time it is called, and it sits
    in front of every tool that takes a club name. Once an hour is the right
    number of times to do that; once a tool call is not.

    The rebuild window matches ``TTL_CATALOGUE`` deliberately: a club that
    appears upstream becomes findable here on the same clock as it becomes
    visible to ``/v1/clubs``.
    """
    global _catalogue, _catalogue_built_at

    now = time.monotonic()
    if _catalogue is None or now - _catalogue_built_at > cache_module.TTL_CATALOGUE:
        _catalogue = await load_catalogue(client=client)
        _catalogue_built_at = now
    return _catalogue


def reset_catalogue() -> None:
    """Test hook, and the lifespan's: forget the indexed catalogue.

    Reset alongside the HTTP cache, or a test that replays a different Fanmoot
    would resolve names against the one the previous test recorded.
    """
    global _catalogue, _catalogue_built_at
    _catalogue = None
    _catalogue_built_at = 0.0
