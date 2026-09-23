"""Has fanmoot.com's API moved out from under the recordings? (`S7-8`, HAD-14)

The suite replays ``fixtures/``, so it cannot see Fanmoot change. This is the
other half: the same requests, sent to production, with the answers judged on
two tests.

* **Does it still parse?** Each path is matched to the mirror its wrapper in
  ``endpoints.py`` validates into. The mirrors ignore unknown fields, so a
  failure here means a field this server *reads* was removed, renamed or
  retyped, and a tool is going to break. That is the failure that matters.
* **Did the shape move?** A field present in the recording and missing live,
  or present with another JSON type, is reported even when the mirror does not
  read it. It is often the first sign of a rename, and it means the recordings
  are going stale and should be re-captured with ``make record``.

A new field upstream is not drift. Fanmoot adding things is expected, and the
mirrors are built to ignore them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import TypeAdapter

from app.fanmoot.models import (
    ClubNewsPage,
    ClubOut,
    ClubPage,
    LeagueDetail,
    LeagueOut,
    LiveDigest,
    MatchDetail,
    MatchGroup,
    MatchPage,
    MootPage,
    SearchResponse,
    ThreadOut,
)

Validate = Callable[[Any], Any]

_SEG = r"[^/]+"

#: Path pattern → the mirror its wrapper validates into. Order matters: the
#: specific ``/v1/matches/live`` must be tried before ``/v1/matches/{id}``.
MIRRORS: list[tuple[re.Pattern[str], Validate]] = [
    (re.compile(r"^/v1/leagues$"), TypeAdapter(list[LeagueOut]).validate_python),
    (re.compile(rf"^/v1/leagues/{_SEG}$"), LeagueDetail.model_validate),
    (re.compile(r"^/v1/clubs$"), ClubPage.model_validate),
    (re.compile(rf"^/v1/clubs/{_SEG}/news$"), ClubNewsPage.model_validate),
    (re.compile(rf"^/v1/clubs/{_SEG}$"), ClubOut.model_validate),
    (re.compile(r"^/v1/updates$"), ClubNewsPage.model_validate),
    (re.compile(r"^/v1/matches$"), MatchPage.model_validate),
    (re.compile(r"^/v1/matches/upcoming$"), TypeAdapter(list[MatchGroup]).validate_python),
    (re.compile(r"^/v1/matches/live$"), LiveDigest.model_validate),
    (re.compile(r"^/v1/matches/by-path/.+$"), MatchDetail.model_validate),
    (re.compile(rf"^/v1/matches/{_SEG}/moots$"), MootPage.model_validate),
    (re.compile(rf"^/v1/matches/{_SEG}$"), MatchDetail.model_validate),
    (re.compile(rf"^/v1/feed/{_SEG}$"), MootPage.model_validate),
    (re.compile(rf"^/v1/moots/{_SEG}/replies$"), MootPage.model_validate),
    (re.compile(rf"^/v1/moots/{_SEG}$"), ThreadOut.model_validate),
    (re.compile(rf"^/v1/m/{_SEG}$"), ThreadOut.model_validate),
    (re.compile(r"^/v1/search$"), SearchResponse.model_validate),
]

#: A path naming one particular thing: a match, a moot, a fixture's slug. What
#: it named in the recording may simply be gone now, and that is a stale
#: recording rather than a changed API.
_NAMES_ONE_THING = re.compile(
    r"/(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|by-path/.+)"
)


def validator_for(path: str) -> Validate | None:
    for pattern, validate in MIRRORS:
        if pattern.match(path):
            return validate
    return None


def names_one_thing(path: str) -> bool:
    return bool(_NAMES_ONE_THING.search(path))


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def shape_drift(recorded: Any, live: Any, *, at: str = "$") -> list[str]:
    """Where ``live`` has lost or retyped something ``recorded`` had.

    Lists are compared by their first element, since a page of matches is one
    shape repeated. An empty list on either side says nothing about the shape,
    so it is skipped rather than reported: an empty live list out of season is
    not drift. ``null`` against a value is skipped for the same reason, because
    an optional field can be either on any given day.
    """
    if recorded is None or live is None:
        return []
    kind_then, kind_now = _kind(recorded), _kind(live)
    if kind_then != kind_now:
        return [f"{at}: was {kind_then}, now {kind_now}"]
    if isinstance(recorded, dict):
        found: list[str] = []
        for key, value in recorded.items():
            if key not in live:
                found.append(f"{at}.{key}: missing")
            else:
                found.extend(shape_drift(value, live[key], at=f"{at}.{key}"))
        return found
    if isinstance(recorded, list):
        if not recorded or not live:
            return []
        return shape_drift(recorded[0], live[0], at=f"{at}[0]")
    return []
