"""``S7-8``: telling a changed API from a stale recording."""

from __future__ import annotations

import json

import pytest

from app.fanmoot.drift import names_one_thing, shape_drift, validator_for
from tests.replay import FIXTURES, load_manifest

LIVE_READS = [r for r in load_manifest() if not r.synthetic and r.method == "GET"]


@pytest.mark.parametrize("recorded", LIVE_READS, ids=lambda r: r.file)
def test_every_recorded_read_has_a_mirror_and_parses(recorded: object) -> None:
    """``make verify-live`` judges each path by its mirror, so a path with none
    would be judged by shape alone and a broken field could pass."""
    validate = validator_for(recorded.path)  # type: ignore[attr-defined]
    assert validate is not None, recorded.path  # type: ignore[attr-defined]
    validate(json.loads((FIXTURES / recorded.file).read_text()))  # type: ignore[attr-defined]


def test_the_live_digest_is_not_read_as_a_match() -> None:
    assert validator_for("/v1/matches/live") is not validator_for("/v1/matches/abc")


def test_a_removed_or_retyped_field_is_drift() -> None:
    then = {"items": [{"id": "x", "score": 1, "venue": "Emirates"}], "next": None}
    now = {"items": [{"id": "x", "score": "1"}], "next": "c2"}

    assert shape_drift(then, now) == [
        "$.items[0].score: was number, now string",
        "$.items[0].venue: missing",
    ]


def test_new_fields_empty_lists_and_nulls_are_not_drift() -> None:
    then = {"items": [{"id": "x"}], "clock": {"minute": 67}, "venue": None}
    now = {"items": [], "clock": None, "venue": "Emirates", "added": True}

    assert shape_drift(then, now) == []


@pytest.mark.parametrize(
    ("path", "gone"),
    [
        ("/v1/matches/dd472da4-8efc-42be-9f7e-188a57aec488", True),
        ("/v1/matches/by-path/premier-league/2026-27/a-vs-b", True),
        ("/v1/clubs/arsenal", False),
        ("/v1/matches/live", False),
    ],
)
def test_only_a_path_naming_one_thing_can_simply_be_gone(path: str, gone: bool) -> None:
    assert names_one_thing(path) is gone
