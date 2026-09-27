"""MCP Apps: the visual views (``S6``, HAD-8).

The extension that turns a spoken answer into something a person can look at —
a scoreboard, a match thread they can reply in, a matchweek, a mood split —
rendered by the host from resources this server publishes under ``ui://``.

Two modules, and the split is the whole discipline of ``S6-1``:

* ``contract`` holds every literal the specification fixes — the extension
  identifier, the MIME type, the ``_meta`` shapes, the bridge method names —
  read out of ``ext-apps`` at ``2026-01-26`` rather than remembered.
* ``views`` holds the documents and the table saying which tool shows
  which. Nothing in it decides protocol; everything it writes comes from
  ``contract``.

The same ``ui://`` resources are what the web demo loads and renders in its own
sandboxed iframe (HAD-8), which is why they are ordinary resources on the
server rather than anything special: the demo is a second host, not a second
implementation.
"""

from __future__ import annotations

from app.mcp.apps.contract import EXTENSION_ID, MIME_TYPE, SPEC_VERSION
from app.mcp.apps.views import TOOL_VIEWS, VIEWS, register_views

__all__ = [
    "EXTENSION_ID",
    "MIME_TYPE",
    "SPEC_VERSION",
    "TOOL_VIEWS",
    "VIEWS",
    "register_views",
]
