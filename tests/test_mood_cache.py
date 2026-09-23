"""``S7-1``: a thread is read by the model once per five minutes, not once per question.

A Bedrock call costs most of a second, four times the latency budget. These
tests stand a counting fake in for the model, since the suite never calls
Bedrock, and assert when it is reached and when it is not.
"""

from __future__ import annotations

import pytest

from app import mood
from app.fanmoot.models import MootOut
from app.settings import reset_settings_cache
from tests import scenarios


def _moots(*bodies: str) -> list[MootOut]:
    return [MootOut.model_validate(scenarios.moot(body)) for body in bodies]


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """A Bedrock stand-in that labels everything positive and remembers each call."""
    monkeypatch.setenv("FMCP_BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    reset_settings_cache()
    calls: list[list[str]] = []

    def label(bodies: list[str]) -> tuple[list[str], str]:
        calls.append(bodies)
        return ["positive"] * len(bodies), "Everyone is happy."

    monkeypatch.setattr(mood, "_label_with_bedrock", label)
    return calls


async def test_the_same_thread_is_labelled_once(model: list[list[str]]) -> None:
    thread = _moots("Get in.", "Deserved.")

    first = await mood.read_mood(thread, subject="x")
    second = await mood.read_mood(thread, subject="x")

    assert len(model) == 1
    assert first == second
    assert first.method == "bedrock"


async def test_a_new_take_is_read_at_once(model: list[list[str]]) -> None:
    await mood.read_mood(_moots("Get in."), subject="x")
    await mood.read_mood(_moots("Get in.", "Offside, surely."), subject="x")

    assert len(model) == 2


async def test_a_failed_model_call_is_not_cached(
    model: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lexicon standing in for a timed-out model should not stand in for five minutes."""

    def down(bodies: list[str]) -> tuple[list[str], str]:
        raise TimeoutError

    thread = _moots("Get in.")
    monkeypatch.setattr(mood, "_label_with_bedrock", down)
    assert (await mood.read_mood(thread, subject="x")).method == "lexicon"

    monkeypatch.setattr(mood, "_label_with_bedrock", lambda b: (["negative"] * len(b), "Gloom."))
    assert (await mood.read_mood(thread, subject="x")).method == "bedrock"
