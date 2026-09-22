"""The read surface: twelve tools, no authentication (``S3``).

One module per group, exactly as §5 of the plan tables them:

* ``clubs``        — ``find_club``, ``find_competition`` (``S3-2``)
* ``matches``      — ``list_matches``, ``upcoming_matches``, ``get_match``,
                     ``live_scores`` (``S3-3``, ``S3-4``)
* ``conversation`` — ``read_match_conversation``, ``read_thread`` (``S3-5``)
* ``discovery``    — ``search_fanmoot``, ``trending_takes``, ``club_news``
                     (``S3-6``)
* ``mood``         — ``match_mood`` (``S3-7``)

Each module exposes ``register(server)`` and nothing else, so the surface is a
list of five calls rather than a decorator scattered across the package — and
so a test can build a server with no tools on it at all.

Everything here reads. Nothing takes a credential, nothing writes, and every
tool is annotated ``readOnlyHint`` to say so. The authenticated half of the
surface — posting, replying, reacting, predicting — arrives with ``S5``, behind
the OAuth server ``S4`` builds.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.mcp.tools import clubs, conversation, discovery, matches, mood


def register_read_tools(server: FastMCP[Any]) -> None:
    """Install every unauthenticated tool on ``server``."""
    clubs.register(server)
    matches.register(server)
    conversation.register(server)
    discovery.register(server)
    mood.register(server)
