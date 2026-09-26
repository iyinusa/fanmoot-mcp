"""``GET /``: the front door lists what the server actually serves.

The tool cards are rendered from ``tools/list``, so these tests assert against
the server rather than against a number typed here: a tool added to the
surface should appear on the page without anyone touching the template.
"""

from __future__ import annotations

import httpx

from app import audit
from app.mcp.apps.views import VIEWS
from app.mcp.server import get_server
from app.oauth.scopes import SCOPES, TOOL_SCOPES
from app.settings import get_settings


async def test_the_root_is_a_page_not_a_404(client: httpx.AsyncClient) -> None:
    page = await client.get("/")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert f'<code class="endpoint__url">{get_settings().resource_uri}</code>' in page.text


async def test_every_tool_is_listed_in_the_right_half(client: httpx.AsyncClient) -> None:
    page = (await client.get("/")).text
    fan_half = page.split("Act as you", 1)[1]

    for tool in await get_server().list_tools():
        assert f'<code class="tool__name">{tool.name}</code>' in page
        in_fan_half = f'<code class="tool__name">{tool.name}</code>' in fan_half
        assert in_fan_half == (tool.name in TOOL_SCOPES), tool.name


async def test_the_views_and_scopes_come_from_their_registries(
    client: httpx.AsyncClient,
) -> None:
    page = (await client.get("/")).text

    for view in VIEWS:
        assert view.uri in page
    for scope in SCOPES:
        assert f"<code>{scope.name}</code>" in page


async def test_the_page_runs_no_script_and_keeps_the_html_policy(
    client: httpx.AsyncClient,
) -> None:
    page = await client.get("/")

    assert "<script" not in page.text
    assert " style=" not in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]


async def test_privacy_and_terms_open_as_modals_with_the_real_text(
    client: httpx.AsyncClient,
) -> None:
    """Popovers, so no script is needed to open them — and the same partials as
    the pages, so the modal cannot promise something the page does not."""
    page = (await client.get("/")).text

    for name in ("privacy", "terms"):
        assert f'popovertarget="{name}"' in page
        assert f'id="{name}" class="modal" popover' in page
    assert f"Tool records: {audit.TOOL_CALL_RETENTION_DAYS} days" in page


async def test_only_public_discovery_documents_are_linked(client: httpx.AsyncClient) -> None:
    """The discovery documents are public by specification. `/healthz` is an
    operator's endpoint — it says which dependency is down — so the page does
    not advertise it."""
    page = (await client.get("/")).text

    assert 'href="/.well-known/oauth-authorization-server"' in page
    assert 'href="/healthz"' not in page


async def test_the_stylesheet_and_fonts_are_served(client: httpx.AsyncClient) -> None:
    assert (await client.get("/static/home.css")).status_code == 200
    for weight in (400, 700, 900):
        assert (await client.get(f"/static/fonts/lato-{weight}.woff2")).status_code == 200


async def test_head_is_answered(client: httpx.AsyncClient) -> None:
    assert (await client.head("/")).status_code == 200
