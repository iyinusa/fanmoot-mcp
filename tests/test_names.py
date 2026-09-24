"""Name resolution, over a labelled set of what fans actually say.

Three kinds of case, and the third is the one that matters most:

* **Resolves** — a nickname, an initialism, a half-said name, a misspelling.
* **Refuses** — a word that is not a club at all.
* **Asks** — a form that genuinely names more than one club. Answering
  "Manchester United" to a Newcastle fan who said "united" is worse than asking,
  so an ambiguous query must come back with candidates and no match (HAD-7).

The catalogue is the real one, built from the recorded ``/v1/clubs`` pages, so
a club renamed upstream fails here rather than in a demo.
"""

from __future__ import annotations

import pytest

from app.fanmoot.aliases import CLUB_ALIASES, COMPETITION_ALIASES
from app.fanmoot.names import Catalogue, normalise

# ── Clubs that must resolve, and to what ─────────────────────────────────────
RESOLVES: list[tuple[str, str]] = [
    # Fanmoot's own nicknames — these cost no curation at all.
    ("the gunners", "arsenal"),
    ("The Gunners", "arsenal"),
    ("the magpies", "newcastle-united"),
    ("the toffees", "everton"),
    ("the cherries", "bournemouth"),
    ("the villans", "aston-villa"),
    ("los blancos", "real-madrid"),
    ("los colchoneros", "atletico-madrid"),
    ("nerazzurri", "inter"),
    ("rossoneri", "ac-milan"),
    ("die roten", "bayern-munich"),
    # Spoken forms the data does not carry.
    ("spurs", "tottenham-hotspur"),
    ("tottenham", "tottenham-hotspur"),
    ("psg", "paris-saint-germain"),
    ("man u", "manchester-united"),
    ("man utd", "manchester-united"),
    ("man city", "manchester-city"),
    ("west ham", "west-ham-united"),
    ("wolves", "wolverhampton-wanderers"),
    ("barca", "barcelona"),
    ("atleti", "atletico-madrid"),
    ("juve", "juventus"),
    ("bvb", "borussia-dortmund"),
    ("inter milan", "inter"),
    ("hibs", "hibernian"),
    ("hearts", "heart-of-midlothian"),
    ("leeds", "leeds-united"),
    ("forest", "nottingham-forest"),
    ("newcastle", "newcastle-united"),
    ("brighton", "brighton-hove-albion"),
    # The full name with its "FC" picks one club where the bare name is two:
    # "Andorra" is also the national team (found by the demo's W4 run).
    ("FC Andorra", "fc-andorra"),
    ("fc andorra", "fc-andorra"),
    # Accents and punctuation a voice transcript will not carry.
    ("atletico madrid", "atletico-madrid"),
    ("Atlético Madrid", "atletico-madrid"),
    ("koln", "koln"),
    ("borussia monchengladbach", "borussia-monchengladbach"),
    ("brighton & hove albion", "brighton-hove-albion"),
    ("brighton and hove albion", "brighton-hove-albion"),
    # Club-type tokens, which say nothing about which club.
    ("arsenal fc", "arsenal"),
    ("fc barcelona", "barcelona"),
    ("liverpool fc", "liverpool"),
    # Misspellings and mishearings — the fuzzy pass.
    ("arsnal", "arsenal"),
    ("arsenel", "arsenal"),
    ("totenham", "tottenham-hotspur"),
    ("liverpul", "liverpool"),
    ("chelsae", "chelsea"),
    ("evrton", "everton"),
    ("juventis", "juventus"),
    ("barcalona", "barcelona"),
    ("real madird", "real-madrid"),
    ("borrussia dortmund", "borussia-dortmund"),
    ("wolverhamton wanderers", "wolverhampton-wanderers"),
    ("nottingham forrest", "nottingham-forest"),
    ("crystal palce", "crystal-palace"),
    ("aston villla", "aston-villa"),
    ("newcastle utd", "newcastle-united"),
]

#: Genuinely more than one club. Must ask, never guess.
ASKS: list[str] = ["united", "city", "rovers", "wanderers", "town", "county", "andorra"]

#: Not a football club in any reading.
REFUSES: list[str] = ["the weather", "qqqqqqqq", "", "   "]

COMPETITIONS: list[tuple[str, str]] = [
    ("the prem", "premier-league"),
    ("premier league", "premier-league"),
    ("epl", "premier-league"),
    ("la liga", "la-liga"),
    ("laliga", "la-liga"),
    ("serie a", "serie-a"),
    ("the bundesliga", "bundesliga"),
    ("ligue 1", "ligue-1"),
    ("champions league", "uefa-champions-league"),
    ("ucl", "uefa-champions-league"),
    ("europa league", "uefa-europa-league"),
    ("fa cup", "fa-cup"),
    ("mls", "mls"),
    ("eredivisie", "eredivisie"),
]


@pytest.mark.parametrize(("said", "slug"), RESOLVES, ids=[s for s, _ in RESOLVES])
async def test_a_club_a_fan_named_resolves(catalogue: Catalogue, said: str, slug: str) -> None:
    result = catalogue.resolve_club(said)

    assert result.resolved, f"{said!r} did not resolve; candidates={result.candidates}"
    assert result.match is not None
    assert result.match.slug == slug


@pytest.mark.parametrize("said", ASKS)
async def test_an_ambiguous_club_asks_rather_than_guesses(catalogue: Catalogue, said: str) -> None:
    result = catalogue.resolve_club(said)

    assert not result.resolved, f"{said!r} resolved to {result.match} — it should have asked"
    assert result.ambiguous or result.unknown


@pytest.mark.parametrize("said", REFUSES)
async def test_something_that_is_not_a_club_resolves_to_nothing(
    catalogue: Catalogue, said: str
) -> None:
    result = catalogue.resolve_club(said)

    assert not result.resolved


@pytest.mark.parametrize(("said", "slug"), COMPETITIONS, ids=[s for s, _ in COMPETITIONS])
async def test_a_competition_a_fan_named_resolves(
    catalogue: Catalogue, said: str, slug: str
) -> None:
    result = catalogue.resolve_competition(said)

    assert result.resolved, f"{said!r} did not resolve; candidates={result.candidates}"
    assert result.match is not None
    assert result.match.slug == slug


async def test_the_labelled_set_is_the_size_the_milestone_asked_for() -> None:
    """S1-7 says roughly sixty nicknames and misspellings. Counted, so the set
    cannot quietly shrink to the cases that happen to pass."""
    assert len(RESOLVES) + len(ASKS) + len(COMPETITIONS) >= 60


async def test_candidates_carry_enough_to_ask_a_spoken_question(
    catalogue: Catalogue,
) -> None:
    """ "Which did you mean?" needs more than two identical names — a competition
    or a country is what makes the question answerable out loud."""
    result = catalogue.resolve_club("united")

    assert result.candidates
    assert all(candidate.name for candidate in result.candidates)
    assert any(candidate.hint for candidate in result.candidates)
    # A voice surface cannot read a list of ten.
    assert len(result.candidates) <= 3


async def test_every_curated_alias_points_at_a_club_that_still_exists(
    catalogue: Catalogue,
) -> None:
    """A club renamed or dropped upstream fails the build here, rather than
    silently resolving to nothing in front of a judge."""
    missing = [slug for slug in set(CLUB_ALIASES.values()) if catalogue.club(slug) is None]

    assert not missing, f"aliases point at clubs Fanmoot no longer has: {sorted(missing)}"


async def test_every_curated_competition_alias_still_exists(catalogue: Catalogue) -> None:
    missing = [
        slug for slug in set(COMPETITION_ALIASES.values()) if catalogue.competition(slug) is None
    ]

    assert not missing, f"aliases point at competitions Fanmoot no longer has: {sorted(missing)}"


async def test_the_catalogue_covers_every_page(catalogue: Catalogue) -> None:
    """Keyset paging at 100 against a catalogue of roughly a thousand clubs. A
    loader that stopped at the first page would pass most of this file and fail
    on every Spanish club."""
    assert len(catalogue.clubs) > 900
    assert catalogue.club("real-madrid") is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("The Gunners", "gunners"),
        ("Atlético Madrid", "atletico madrid"),
        ("Brighton & Hove Albion", "brighton hove albion"),
        ("Arsenal F.C.", "arsenal"),
        ("  Spurs  ", "spurs"),
        ("AC", "ac"),
    ],
)
async def test_normalise(raw: str, expected: str) -> None:
    """Including the degenerate case: a query that is *only* club-type tokens
    keeps them, rather than reducing to an empty string that matches everything."""
    assert normalise(raw) == expected
