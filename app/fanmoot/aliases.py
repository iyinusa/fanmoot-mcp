"""Spoken names Fanmoot's own data does not carry.

Fanmoot stores one official nickname per club — "The Gunners", "Los Blancos",
"The Magpies" — and ``names.py`` matches against those for free. This file is
for the rest: the forms a fan actually says out loud that are neither the club's
name nor its nickname, and that fuzzy matching would get wrong rather than
merely miss.

The distinction matters, because the failure modes differ:

* "spurs" is not a substring of "Tottenham Hotspur" and is nobody's nickname in
  the data. Fuzzy matching scores it closer to *Sparta Rotterdam*.
* "psg" is an initialism. Edit distance has nothing to work with.
* "united" and "city" are genuinely ambiguous and must **not** resolve — they
  belong in ``AMBIGUOUS`` below, which forces a question rather than a guess.

Keys are already normalised (``names.normalise``): lowercase, unaccented, no
punctuation, no leading "the". Values are slugs that must exist in Fanmoot's
catalogue — ``test_names.py`` asserts exactly that against the recorded
fixtures, so an alias pointing at a club that was renamed or dropped fails the
build rather than silently resolving to nothing.
"""

from __future__ import annotations

#: Spoken form → club slug.
CLUB_ALIASES: dict[str, str] = {
    # ── England ──────────────────────────────────────────────────────────────
    "spurs": "tottenham-hotspur",
    "tottenham": "tottenham-hotspur",
    "man u": "manchester-united",
    "man utd": "manchester-united",
    "man united": "manchester-united",
    "united": "manchester-united",  # see AMBIGUOUS — only reached with a hint
    "red devils": "manchester-united",
    "man city": "manchester-city",
    "citizens": "manchester-city",
    "gunners": "arsenal",
    "reds": "liverpool",
    "blues": "chelsea",
    "toffees": "everton",
    "magpies": "newcastle-united",
    "newcastle": "newcastle-united",
    "toon": "newcastle-united",
    "hammers": "west-ham-united",
    "west ham": "west-ham-united",
    "irons": "west-ham-united",
    "wolves": "wolverhampton-wanderers",
    "villa": "aston-villa",
    "villans": "aston-villa",
    "seagulls": "brighton-hove-albion",
    "brighton": "brighton-hove-albion",
    "albion": "brighton-hove-albion",
    "cherries": "bournemouth",
    "eagles": "crystal-palace",
    "palace": "crystal-palace",
    "bees": "brentford",
    "clarets": "burnley",
    "cottagers": "fulham",
    "forest": "nottingham-forest",
    "tricky trees": "nottingham-forest",
    "whites": "leeds-united",
    "leeds": "leeds-united",
    "black cats": "sunderland",
    "lilywhites": "tottenham-hotspur",
    # ── Scotland ─────────────────────────────────────────────────────────────
    "hoops": "celtic",
    "gers": "rangers",
    "hearts": "heart-of-midlothian",
    "hibs": "hibernian",
    "dons": "aberdeen",
    # ── Spain ────────────────────────────────────────────────────────────────
    "los blancos": "real-madrid",
    "madrid": "real-madrid",
    "real": "real-madrid",
    "merengues": "real-madrid",
    "barca": "barcelona",
    "blaugrana": "barcelona",
    "fc barcelona": "barcelona",
    "atleti": "atletico-madrid",
    "atletico": "atletico-madrid",
    "colchoneros": "atletico-madrid",
    "athletic bilbao": "athletic-club",
    "bilbao": "athletic-club",
    "betis": "real-betis",
    "la real": "real-sociedad",
    # ── Italy ────────────────────────────────────────────────────────────────
    "inter milan": "inter",
    "internazionale": "inter",
    "nerazzurri": "inter",
    "milan": "ac-milan",
    "rossoneri": "ac-milan",
    "juve": "juventus",
    "old lady": "juventus",
    "bianconeri": "juventus",
    "verona": "hellas-verona",
    # ── Germany ──────────────────────────────────────────────────────────────
    "bayern": "bayern-munich",
    "die roten": "bayern-munich",
    "fc bayern": "bayern-munich",
    "dortmund": "borussia-dortmund",
    "bvb": "borussia-dortmund",
    "gladbach": "borussia-monchengladbach",
    "leverkusen": "bayer-leverkusen",
    "leipzig": "rb-leipzig",
    "cologne": "koln",
    "frankfurt": "eintracht-frankfurt",
    # ── France ───────────────────────────────────────────────────────────────
    "psg": "paris-saint-germain",
    "paris": "paris-saint-germain",
    "les parisiens": "paris-saint-germain",
    "om": "marseille",
    "ol": "lyon",
    # ── Netherlands & Portugal ───────────────────────────────────────────────
    "psv eindhoven": "psv",
    "sporting": "sporting-cp",
    "sporting lisbon": "sporting-cp",
}

#: Things a fan says that genuinely name more than one club, listed so they are
#: answered with a question rather than a guess (HAD-7: "I know Arsenal and
#: Aston Villa — which did you mean?" beats a confident wrong answer).
#:
#: ``united`` and ``city`` appear in ``CLUB_ALIASES`` as well. That is not a
#: contradiction: a bare "united" is refused here, and the alias is only
#: reached when a caller has already narrowed the field — say, to the clubs in
#: one match. The entry exists so that narrowing has something to land on.
AMBIGUOUS: frozenset[str] = frozenset(
    {
        "united",
        "city",
        "rovers",
        "rangers fc",
        "athletic",
        "real sociedad or real madrid",
        "borussia",
        "town",
        "county",
        "wanderers",
        "albion fc",
    }
)

#: Spoken form → competition slug.
COMPETITION_ALIASES: dict[str, str] = {
    "prem": "premier-league",
    "premiership": "premier-league",
    "epl": "premier-league",
    "english premier league": "premier-league",
    "pl": "premier-league",
    "top flight": "premier-league",
    "laliga": "la-liga",
    "spanish league": "la-liga",
    "primera division": "la-liga",
    "seriea": "serie-a",
    "italian league": "serie-a",
    "german league": "bundesliga",
    "french league": "ligue-1",
    "ligue one": "ligue-1",
    "ucl": "uefa-champions-league",
    "champions league": "uefa-champions-league",
    "european cup": "uefa-champions-league",
    "uel": "uefa-europa-league",
    "europa": "uefa-europa-league",
    "europa league": "uefa-europa-league",
    "conference league": "uefa-conference-league",
    "uecl": "uefa-conference-league",
    "the cup": "fa-cup",
    "facup": "fa-cup",
    "scottish prem": "scottish-premiership",
    "spfl": "scottish-premiership",
    "mls": "mls",
    "major league soccer": "mls",
    "npfl": "npfl",
    "nigerian league": "npfl",
    "saudi league": "saudi-pro-league",
    "spl": "saudi-pro-league",
    "dutch league": "eredivisie",
    "portuguese league": "primeira-liga",
    "efl championship": "championship",
    "the championship": "championship",
}
