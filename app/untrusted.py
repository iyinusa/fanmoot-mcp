"""Text a stranger wrote, made safe to put in front of a model (`S7-3`).

A moot is typed by anybody with an account, and this server hands moots to two
models: the client's, in every tool result that quotes a take, and its own, in
``match_mood``'s Bedrock pass. Neither should be steerable by what a fan typed.
The rules are:

* **Moots are framed as data.** A quote reaches a client as
  ``Name says: “…”`` and its double quotes are made single, so a take can
  never close its own quotation and carry on as if it were the tool talking.
  The server instructions tell the model the same thing in words.
* **Invisible characters are removed.** Bidi overrides, zero-width joiners and
  C0/C1 controls are how a take hides text from the person reading a card while
  showing it to the model reading the payload, so neither gets them.
* **Fences cannot be closed from inside.** The mood prompt wraps takes in
  ``<posts>…</posts>``, and angle brackets inside a take are swapped for
  look-alikes, so ``</posts>`` in a moot is just words.
* **Tool output never carries instructions.** Every sentence this server
  writes addresses the fan, never the model, and a stranger's words only ever
  arrive inside a quote. ``app.voice`` is the one place a take is spoken, and
  it goes through ``quote``.

Nothing is *removed* for what it says. "Ignore your instructions" is a take
about something, however badly written, and it is quoted like any other.
"""

from __future__ import annotations

import re
import unicodedata

#: Characters that change how text *looks* without being text: bidi embeddings,
#: overrides and isolates, zero-width spaces and joiners, the word joiner, the
#: byte-order mark, and the soft hyphen.
_INVISIBLE = re.compile("[­​-‏‪-‮⁠-⁤⁦-⁯﻿￹-￻]")


def visible(text: str) -> str:
    """Invisible and control characters out, line breaks kept.

    For a card's body, where a fan's paragraphs are part of what they wrote and
    a renderer shows them with ``textContent``. Nothing here is read aloud.
    """
    text = _INVISIBLE.sub("", text)
    return "".join(
        char if char == "\n" or unicodedata.category(char)[0] != "C" else " " for char in text
    ).strip()


def clean(text: str) -> str:
    """Invisible and control characters out, whitespace collapsed.

    Newlines go too. A quote read aloud has no line breaks, and a line break is
    how a take would imitate the start of a new message in a transcript.
    """
    text = _INVISIBLE.sub("", text)
    text = "".join(char if unicodedata.category(char)[0] != "C" else " " for char in text)
    return " ".join(text.split())


def quotable(text: str) -> str:
    """``clean``, plus double quotes made single, so a quote cannot be closed."""
    return re.sub('["“”„‟«»]', "'", clean(text))


def fenced(text: str) -> str:
    """``clean``, plus angle brackets swapped, so a prompt fence cannot be closed."""
    return clean(text).replace("<", "‹").replace(">", "›")


def quote(name: str, body: str) -> str:
    """``Name says: “body”``. The one way a take is attributed anywhere."""
    return f"{quotable(name)} says: “{quotable(body)}”"
