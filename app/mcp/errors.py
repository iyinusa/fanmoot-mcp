"""An exception, turned into a sentence a model can read aloud.

``S2-6``, and the clarification in ``2025-11-25`` that makes it a compliance
item rather than a nicety: **a bad argument is a tool execution error, not a
protocol error.** A JSON-RPC error tells the model the call itself was
malformed and there is nothing to do about it. A ``CallToolResult`` with
``isError: true`` tells it *what* was wrong, in words, while the turn is still
open — so "I know Arsenal and Aston Villa, which did you mean?" comes back to
the fan instead of a 422 (HAD-7).

The SDK already declines to turn a raised exception into a protocol error, so
the compliance half is free. What is not free is the *message*. FastMCP wraps
everything a tool raises as ``ToolError("Error executing tool get_match:
1 validation error for get_matchArguments ...")``, and a model asked to speak
that aloud will. So every exception is unwrapped here and mapped to one
sentence, written for the ear.

The mapping is by **cause**, not by string: ``Tool.run`` re-raises with
``from e``, so the exception the tool actually raised is still attached, and
``app.fanmoot.client`` has already translated every upstream failure into an
error type carrying a speakable ``message``. That is the whole reason those
types exist.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.fanmoot.client import (
    FanmootError,
    FanmootNotFound,
    FanmootRateLimited,
    FanmootUnauthorized,
)
from app.logging_config import get_logger

log = get_logger("mcp.errors")

#: What a caller is told when nothing more specific is known. Deliberately
#: vague about the cause and specific about what to do next: the detail is in
#: the log line, where an operator can read it and a fan cannot.
_GENERIC = "Something went wrong reaching Fanmoot. Try that again in a moment."

#: An unknown tool is a client-side mistake, not a fan-side one. The model is
#: the one that can act on it, so the sentence is addressed to the model.
_UNKNOWN_TOOL = "There is no tool called {name} on this server."

#: A limit on how much of a validation complaint is spoken. A model reading a
#: list of eight field names out loud is worse than one asking a question.
_MAX_SPOKEN_FIELDS = 3


class ToolInputError(Exception):
    """Something the fan said that a tool could not act on, already in words.

    Raised by the read tools in ``S3`` when a name resolves to three clubs, a
    match cannot be found between two of them, or a moot id is not a moot id.
    It carries the sentence to speak — "Do you mean Arsenal, Aston Villa or
    Athletic Club?" — because the tool is the only layer that knows which
    question to ask.

    It is an *execution* error, not a protocol one: the model hears the
    question while the turn is still open and can ask the fan (``S2-6``,
    HAD-7).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def unwrap(exc: BaseException) -> BaseException:
    """The exception a tool actually raised, past FastMCP's wrapper.

    ``ToolError`` is FastMCP's envelope and never carries information of its
    own beyond the text; ``__cause__`` is the real failure. One level is
    enough — the envelope is applied exactly once, in ``Tool.run``.
    """
    return exc.__cause__ or exc


def speakable(exc: BaseException, *, tool: str) -> str:
    """One sentence describing ``exc``, written to be spoken.

    Never contains a status code, a URL, a stack, a field path or the word
    "exception". A caller may hand the result straight to a model.
    """
    cause = unwrap(exc)

    # A tool that already knows what to say says it. This is the disambiguation
    # question, and it is the most common non-happy path on the whole surface.
    if isinstance(cause, ToolInputError):
        return cause.message

    if isinstance(cause, ValidationError):
        return _from_validation_error(cause)

    # Everything from `app.fanmoot.client` already carries a spoken sentence —
    # that boundary is where an HTTP status becomes something sayable.
    if isinstance(cause, FanmootRateLimited):
        return cause.message
    if isinstance(cause, FanmootUnauthorized | FanmootNotFound | FanmootError):
        return cause.message

    text = str(exc)
    if text.startswith("Unknown tool: "):
        return _UNKNOWN_TOOL.format(name=text.removeprefix("Unknown tool: "))

    # Unrecognised: the fan gets the generic sentence and the operator gets the
    # truth. Logging it here rather than re-raising is the point — the turn
    # carries on, and a tool that failed once does not end the conversation.
    log.exception("tool_failed_unexpectedly", tool=tool, error_type=type(cause).__name__)
    return _GENERIC


def _from_validation_error(exc: ValidationError) -> str:
    """Pydantic's complaint, rewritten as a question.

    Pydantic reports every problem with a location, a type and a literal input.
    A voice surface wants none of that — it wants the *names* of what is
    missing, so the model can ask for them.
    """
    missing: list[str] = []
    wrong: list[str] = []
    for error in exc.errors():
        field = ".".join(str(part) for part in error["loc"]) or "input"
        if error["type"] == "missing":
            missing.append(field)
        elif error["type"].startswith(("greater_than", "less_than")):
            # A bound, not a type: the model asked for fifty takes and the cap
            # is twenty (HAD-7, `S3-9`). Saying the bound is what lets it ask
            # again correctly, where "I could not make sense of limit" would
            # have it guess.
            return _from_bound(field, error)
        else:
            wrong.append(field)

    if missing and not wrong:
        return f"I still need {_join(missing)} for that."
    if wrong and not missing:
        return f"I could not make sense of {_join(wrong)}. Say that again?"
    return f"I still need {_join(missing)}, and I could not make sense of {_join(wrong)}."


def _from_bound(field: str, error: Any) -> str:
    """A range violation, as the sentence that teaches the bound."""
    limit = (error.get("ctx") or {}).get("le") or (error.get("ctx") or {}).get("ge")
    spoken = field.replace("_", " ")
    if error["type"].startswith("less_than"):
        return f"I can manage {spoken} up to {limit} at a time. Ask for fewer?"
    return f"{spoken.capitalize()} has to be at least {limit}."


def _join(fields: list[str]) -> str:
    """``["a", "b", "c"]`` → ``"a, b and c"``, truncated to what is worth saying."""
    spoken = [field.replace("_", " ") for field in fields[:_MAX_SPOKEN_FIELDS]]
    if len(fields) > _MAX_SPOKEN_FIELDS:
        spoken.append("a couple more things")
    if len(spoken) == 1:
        return spoken[0]
    return f"{', '.join(spoken[:-1])} and {spoken[-1]}"
