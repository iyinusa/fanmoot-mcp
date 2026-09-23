"""The four views, and which tool shows which (``S6-2`` … ``S6-6``).

A view is one HTML document with its CSS and its bridge inlined, served from
``resources/read`` under a ``ui://`` URI, and named by the tools whose answers
it renders. Nothing here fetches, templates or computes: the document is
assembled once per process from files on disk and handed back verbatim.

**Assembled rather than written out four times.** ``assets/base.css`` and
``assets/bridge.js`` are shared, and each view names where they go with a
sentinel. The alternative — four copies of the handshake — is four places for
the spec to be implemented slightly differently, which is the failure mode
``S6-1`` exists to prevent. It is a string substitution rather than a template
engine on purpose: the output has to be auditable as HTML, and Jinja's
autoescaping inside a ``<script>`` block is a footgun with no upside here.

**Which tool gets which view** is the table in §5 of the plan, and the mapping
is stated once, below. Eight of the twelve read tools have a visual answer;
the four that do not — ``find_club``, ``find_competition``, ``search_fanmoot``,
``club_news`` — are answers a person listens to rather than looks at, and
giving them a card would be giving them one because we could.

**The write tools have no view of their own.** ``reply_to_moot`` appears here
only to be declared callable *by the app*, which is what lets the thread view's
composer post (``S6-3``). It is not a tool whose result is a card: the card is
the thread it just joined.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app import __version__
from app.logging_config import get_logger
from app.mcp.apps import contract

log = get_logger("mcp.apps")

ASSETS = Path(__file__).resolve().parent / "assets"

#: Where the shared stylesheet and the bridge land in each document.
_BASE_CSS = "__BASE_CSS__"
_BRIDGE_JS = "__BRIDGE_JS__"

#: Which view renders which tool's answer — §5's table, as the one place it is
#: written down. Read by ``register_views`` to attach ``_meta.ui.resourceUri``
#: to each tool, and by the tests to assert the two agree.
TOOL_VIEWS: dict[str, str] = {
    "get_match": "scoreboard",
    "live_scores": "scoreboard",
    "list_matches": "fixtures",
    "upcoming_matches": "fixtures",
    "read_match_conversation": "thread",
    "read_thread": "thread",
    "trending_takes": "thread",
    "match_mood": "mood",
}

#: Tools a view may call that have no view of their own (``S6-3``).
APP_CALLABLE: tuple[str, ...] = ("reply_to_moot",)


class View:
    """One ``ui://`` resource: a name, a title, and the document it serves."""

    def __init__(self, name: str, title: str, description: str) -> None:
        self.name = name
        self.title = title
        self.description = description

    @property
    def uri(self) -> str:
        return contract.uri(self.name)

    def html(self) -> str:
        return document(self.name)


VIEWS: tuple[View, ...] = (
    View(
        "scoreboard",
        "Scoreboard",
        "One match or everything in play: crests, the scoreline, and the live minute. "
        "A score Fanmoot has not confirmed is shown as unconfirmed rather than stated.",
    ),
    View(
        "thread",
        "Match thread",
        "What fans are saying, with a composer that posts a reply as the signed-in fan.",
    ),
    View(
        "fixtures",
        "Fixtures",
        "A matchweek grouped by day: who is playing whom, and when or what it finished.",
    ),
    View(
        "mood",
        "Match mood",
        "The sentiment split on a match thread as one bar and three counts, "
        "with the takes behind it.",
    ),
)

VIEWS_BY_NAME: dict[str, View] = {view.name: view for view in VIEWS}


@cache
def document(name: str) -> str:
    """One view's HTML, with the shared stylesheet and bridge inlined.

    Cached because the files never change inside a process and a
    ``resources/read`` on a voice turn should not be three disk reads (HAD-6).
    """
    html = _read(f"{name}.html")
    bridge = _read("bridge.js").replace("__PROTOCOL__", contract.SPEC_VERSION)
    bridge = bridge.replace("__VERSION__", __version__)
    return html.replace(_BASE_CSS, _read("base.css")).replace(_BRIDGE_JS, bridge)


def _read(filename: str) -> str:
    return (ASSETS / filename).read_text(encoding="utf-8")


def register_views(server: FastMCP[Any]) -> None:
    """Publish the four views and point the tools at them.

    Two halves, and both are needed: a resource nothing references is a
    resource no host will ever read, and a ``resourceUri`` naming a resource
    that does not exist is a tool whose card never renders. The spec says the
    resource MUST exist on the server, so they are registered together.

    The views are listed in ``resources/list`` rather than hidden. The spec
    permits omitting UI-only resources, and the reason not to is the
    acceptance test: MCP Inspector renders what it can enumerate, and a judge
    opening it should find four views rather than have to guess four URIs.
    """
    for view in VIEWS:
        _register_one(server, view)

    attached = _attach_meta(server)
    log.info("mcp_apps_registered", views=len(VIEWS), tools=attached)


def _register_one(server: FastMCP[Any], view: View) -> None:
    @server.resource(
        view.uri,
        name=view.name,
        title=view.title,
        description=view.description,
        mime_type=contract.MIME_TYPE,
        meta=contract.resource_meta(),
    )
    def read() -> str:
        return view.html()


def _attach_meta(server: FastMCP[Any]) -> int:
    """Set ``_meta.ui`` on the registered tools, after the fact.

    Deliberately not a decorator argument on each tool. The tools belong to
    ``S3`` and ``S5`` and are about football, not about rendering; a
    ``resourceUri`` sitting in twelve tool definitions would be this mapping
    written out twelve times, and the ninth one would eventually disagree with
    the table above. Doing it here means ``TOOL_VIEWS`` is the only statement
    of it, and a tool named there that does not exist fails loudly at startup
    rather than quietly at render time.
    """
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    missing = (set(TOOL_VIEWS) | set(APP_CALLABLE)) - set(tools)
    if missing:  # pragma: no cover - a typo in the table, caught at boot
        raise RuntimeError(f"no such tool to attach a view to: {sorted(missing)}")

    for name, view in TOOL_VIEWS.items():
        tools[name].meta = contract.tool_meta(view)
    for name in APP_CALLABLE:
        tools[name].meta = contract.tool_meta()
    return len(TOOL_VIEWS) + len(APP_CALLABLE)
