"""``GET /`` — the page a person sees when they open this server in a browser.

Until now the root answered ``{"detail": "Not Found"}``, which is what a judge,
a developer pasting the URL into a tab, or a fan following a link from a
connector list all saw first. This is the front door instead: what the server
is, the one URL to paste into a client, and what it can do once connected.

**The surface on the page is read from the server, not typed into it.** Tools,
their titles and icons come from ``tools/list``; which scope each one needs
from ``app.oauth.scopes``; the views from ``app.mcp.apps.views``. The same rule
``/privacy`` follows (`S7-12`): a page that lists twelve tools while the server
serves thirteen is a page that lies, so this one cannot.

Like every other HTML page here it runs no script and draws only its own
stylesheets, icons and fonts, so the strict policy in ``app.middleware``
applies. Everything that moves is CSS, and all of it stops under
``prefers-reduced-motion``. Privacy and terms open as popovers — the HTML
``popover`` attribute needs no script — rendered from the same partials as
``/privacy`` and ``/terms``, which stay as pages because the consent screen and
the Alexa manifest link to them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from mcp.types import LATEST_PROTOCOL_VERSION

from app import __version__, legal
from app.mcp.apps.views import VIEWS
from app.mcp.instructions import SERVER_DESCRIPTION
from app.mcp.server import get_server
from app.oauth import scopes
from app.settings import get_settings

router = APIRouter(tags=["home"])

#: The OAuth pages' templates, so the front door and the sign-in page share a
#: directory and a palette.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "oauth" / "templates"))

ICONS_DIR = Path(__file__).resolve().parent / "static" / "icons"

#: Public, and identical for every visitor. Short, because a deploy that adds a
#: tool should show on the page within minutes.
_HEADERS = {"Cache-Control": "public, max-age=300"}

#: The icons are drawn with an accessible name for a client's tool list. On
#: this page the tool's title sits beside each one, so they are decoration.
_ICON_A11Y = re.compile(r'\s(?:role|aria-label)="[^"]*"')


#: The ticker: things fans actually say, and the tool that answers each. An
#: entry naming a tool the server does not serve is dropped rather than shown.
ASKS: tuple[tuple[str, str], ...] = (
    ("what's the score?", "live_scores"),
    ("who are the gunners playing?", "list_matches"),
    ("anything on tonight?", "upcoming_matches"),
    ("what are fans saying about it?", "read_match_conversation"),
    ("are spurs fans happy?", "match_mood"),
    ("what's everyone arguing about?", "trending_takes"),
    ("any transfer news?", "club_news"),
    ("call it two-one", "predict_match"),
    ("post that it was never offside", "post_moot"),
    ("what's happening in the Brighton game?", "get_match"),
)


@dataclass(frozen=True, slots=True)
class ToolCard:
    name: str
    title: str
    summary: str
    icon: str
    scope: str | None


@cache
def _icon(stem: str) -> str:
    path = ICONS_DIR / f"{stem}.svg"
    if not path.is_file():
        return ""
    svg = _ICON_A11Y.sub("", path.read_text(encoding="utf-8").strip())
    return svg.replace("<svg ", '<svg aria-hidden="true" focusable="false" ', 1)


def _first_sentence(description: str | None) -> str:
    text = " ".join((description or "").split())
    head, dot, _ = text.partition(". ")
    return head + "." if dot else text


async def _tool_cards() -> list[ToolCard]:
    cards = []
    for tool in await get_server().list_tools():
        stem = Path(urlparse(str(tool.icons[0].src)).path).stem if tool.icons else ""
        cards.append(
            ToolCard(
                name=tool.name,
                title=tool.title or tool.name,
                summary=_first_sentence(tool.description),
                icon=_icon(stem) if stem else "",
                scope=scopes.required_for(tool.name),
            )
        )
    return cards


@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def home(request: Request) -> Response:
    settings = get_settings()
    cards = await _tool_cards()
    served = {card.name for card in cards}
    context = {
        **legal.context(),
        "version": __version__,
        "year": datetime.now(UTC).year,
        "protocol_version": LATEST_PROTOCOL_VERSION,
        "description": SERVER_DESCRIPTION,
        "mcp_url": settings.resource_uri,
        "public_base_url": settings.public_base_url,
        "support_email": settings.support_email,
        "read_tools": [card for card in cards if card.scope is None],
        "fan_tools": [card for card in cards if card.scope is not None],
        "scopes": scopes.SCOPES,
        "views": VIEWS,
        "asks": [(ask, tool) for ask, tool in ASKS if tool in served],
    }
    return templates.TemplateResponse(request, "home.html", context, headers=_HEADERS)
