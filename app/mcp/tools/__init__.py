"""The surface: twelve tools anyone may call, seven that act as a fan.

One module per group, exactly as §5 of the plan tables them.

**Read, no authentication** (``S3``):

* ``clubs``        — ``find_club``, ``find_competition`` (``S3-2``)
* ``matches``      — ``list_matches``, ``upcoming_matches``, ``get_match``,
                     ``live_scores`` (``S3-3``, ``S3-4``)
* ``conversation`` — ``read_match_conversation``, ``read_thread`` (``S3-5``)
* ``discovery``    — ``search_fanmoot``, ``trending_takes``, ``club_news``
                     (``S3-6``)
* ``mood``         — ``match_mood`` (``S3-7``)

**Acting as the fan, behind an OAuth scope** (``S5``):

* ``fan``         — ``whoami`` (``S5-1``), ``follow`` (``S5-6``)
* ``compose``     — ``post_moot``, ``reply_to_moot``, ``react_to_moot``
                    (``S5-2`` … ``S5-4``)
* ``predictions`` — ``predict_match``, ``my_predictions`` (``S5-5``)

Each module exposes ``register(server)`` and nothing else, so the surface is a
list of eight calls rather than a decorator scattered across the package — and
so a test can build a server with no tools on it at all.

**The split is registered in two functions rather than one.** Which of the two
halves a tool is in is not a property of the tool object: the gate in
``app.oauth.bearer`` decides from ``TOOL_SCOPES``, before the tool exists in
the request. Keeping the registration in two named calls is what makes the two
lists comparable — a test asserts that the tools ``register_write_tools``
installs are exactly the ones the gate guards, and a write tool that reached
this file without reaching ``TOOL_SCOPES`` would be one anybody could call.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.mcp.tools import (
    clubs,
    compose,
    conversation,
    discovery,
    fan,
    matches,
    mood,
    predictions,
)


def register_read_tools(server: FastMCP[Any]) -> None:
    """Install every unauthenticated tool on ``server`` (``S3``)."""
    clubs.register(server)
    matches.register(server)
    conversation.register(server)
    discovery.register(server)
    mood.register(server)


def register_write_tools(server: FastMCP[Any]) -> None:
    """Install every tool that acts as a signed-in fan (``S5``).

    Registered second, and separately, so ``tools/list`` reads read-first —
    which is the order a model should consider them in, and the order the
    server instructions describe them in.
    """
    fan.register(server)
    compose.register(server)
    predictions.register(server)
