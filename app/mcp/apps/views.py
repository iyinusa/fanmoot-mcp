"""The views, and which tool shows which (``S6-2`` … ``S6-6``).

A view is one HTML document with its CSS and its bridge inlined, served from
``resources/read`` under a ``ui://`` URI, and named by the tools whose answers
it renders. Nothing here fetches, templates or computes: the document is
assembled once per process from files on disk and handed back verbatim.

**Assembled rather than written out once per view.** ``assets/base.css`` and
``assets/bridge.js`` are shared, and each view names where they go with a
sentinel. The alternative — a copy of the handshake per view — is a place per view for
the spec to be implemented slightly differently, which is the failure mode
``S6-1`` exists to prevent. It is a string substitution rather than a template
engine on purpose: the output has to be auditable as HTML, and Jinja's
autoescaping inside a ``<script>`` block is a footgun with no upside here.

**Which tool gets which view** is stated once, below. A tool gets a card when
a picture beats a sentence: a scoreline, a matchweek, a thread, a mood split —
and, since the moot card, the take a fan has just posted, their own account,
a club, and their predictions. What stays spoken is what a person listens to
rather than looks at: ``find_competition``, ``search_fanmoot`` and ``follow``.
``find_club`` has a card because its answer is either a club — crest,
colours, ground — or a "which one?" a fan can answer with a tap.

**One moot card, shared.** ``assets/card.js`` and ``assets/card.css`` draw a
take the way fanmoot.com does — crest, tier, pictures, link, quote, the chip
back to its match — and are inlined, like the bridge, into every view that
shows one. Two views each drawing a take their own way is two takes that
disagree about what a take looks like.

**What a view may call.** A view acts only through ``tools/call``, proxied by
the host on the connection and grant it already holds. The thread's composer
calls ``reply_to_moot``, every interactive card's reactions call
``react_to_moot``, the club card calls ``follow`` and an open fixture calls
``predict_match``. Those without a card of their own are in ``APP_CALLABLE``.
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

#: Where the shared stylesheets and scripts land in each document.
_BASE_CSS = "__BASE_CSS__"
_BRIDGE_JS = "__BRIDGE_JS__"
_CARD_CSS = "__CARD_CSS__"
_CARD_JS = "__CARD_JS__"

#: Which view renders which tool's answer — the one place it is written down.
#: Read by ``register_views`` to attach ``_meta.ui.resourceUri`` to each tool,
#: and by the tests to assert the two agree.
TOOL_VIEWS: dict[str, str] = {
    "get_match": "scoreboard",
    "live_scores": "scoreboard",
    "list_matches": "fixtures",
    "upcoming_matches": "fixtures",
    "read_match_conversation": "thread",
    "read_thread": "thread",
    "trending_takes": "thread",
    "match_mood": "mood",
    "post_moot": "moot",
    "reply_to_moot": "moot",
    "whoami": "fan",
    "find_club": "club",
    "club_news": "club",
    "predict_match": "predictions",
    "my_predictions": "predictions",
}

#: Tools a view may call that have no view of their own (``S6-3``). A tool in
#: ``TOOL_VIEWS`` is callable by an app already — the visibility is the same.
APP_CALLABLE: tuple[str, ...] = ("react_to_moot", "follow")


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
    View(
        "moot",
        "Your take",
        "The take a fan has just posted, as fanmoot.com shows it. A take Fanmoot holds "
        "for review is shown as held, with Fanmoot's reason, never as live.",
    ),
    View(
        "fan",
        "Your account",
        "The signed-in fan: handle, club and how long they have been signed to it, "
        "followers, and whether they can post yet.",
    ),
    View(
        "club",
        "Club",
        "A club's crest, colours, competition and ground with a Follow button — or the "
        "candidates to choose between — and a club's latest news.",
    ),
    View(
        "predictions",
        "Predictions",
        "The fan's calls beside the real results, and the fixtures still open, which can "
        "be called from the card.",
    ),
)

VIEWS_BY_NAME: dict[str, View] = {view.name: view for view in VIEWS}


@cache
def document(name: str) -> str:
    """One view's HTML, with the shared stylesheets and scripts inlined.

    Cached because the files never change inside a process and a
    ``resources/read`` on a voice turn should not be three disk reads (HAD-6).
    """
    html = _read(f"{name}.html")
    bridge = _read("bridge.js").replace("__PROTOCOL__", contract.SPEC_VERSION)
    bridge = bridge.replace("__VERSION__", __version__)
    return (
        html.replace(_BASE_CSS, _read("base.css"))
        .replace(_CARD_CSS, _read("card.css"))
        .replace(_BRIDGE_JS, bridge)
        .replace(_CARD_JS, _read("card.js"))
    )


def _read(filename: str) -> str:
    return (ASSETS / filename).read_text(encoding="utf-8")


def register_views(server: FastMCP[Any]) -> None:
    """Publish the views and point the tools at them.

    Two halves, and both are needed: a resource nothing references is a
    resource no host will ever read, and a ``resourceUri`` naming a resource
    that does not exist is a tool whose card never renders. The spec says the
    resource MUST exist on the server, so they are registered together.

    The views are listed in ``resources/list`` rather than hidden. The spec
    permits omitting UI-only resources, and the reason not to is the
    acceptance test: MCP Inspector renders what it can enumerate, and a judge
    opening it should find every view rather than have to guess URIs.
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
    ``resourceUri`` sitting in every tool definition would be this mapping
    written out once per tool, and one would eventually disagree with
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
