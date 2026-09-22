"""``S2-3`` — ``Origin`` validation, and the three cases that are easy to get wrong.

A ``2025-11-25`` MUST, and the defence against DNS rebinding: same-origin
policy stops an attacker's page *reading* this server's answers, which is no
comfort at all when the request itself posts a moot under a fan's name.
"""

from __future__ import annotations

import httpx
import pytest

from app.mcp.security import OriginValidation, normalise_origin
from app.settings import Settings


async def _post_with_origin(url: str, origin: str | None) -> httpx.Response:
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if origin is not None:
        headers["origin"] = origin
    async with httpx.AsyncClient() as http:
        return await http.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers=headers,
        )


async def test_a_foreign_browser_origin_is_refused_with_403(mcp_url: str) -> None:
    response = await _post_with_origin(mcp_url, "https://evil.example")

    assert response.status_code == 403


async def test_a_client_that_sends_no_origin_is_served(mcp_url: str) -> None:
    """Alexa+, Claude Desktop, Cursor and curl are not browsers.

    Refusing them would close the server to every client it exists for while
    stopping nothing: the header's whole value is that a browser cannot omit
    it.
    """
    response = await _post_with_origin(mcp_url, None)

    assert response.status_code == 200


async def test_loopback_is_served_in_development(mcp_url: str) -> None:
    """The MCP Inspector runs in a browser on localhost (``S2-7``)."""
    response = await _post_with_origin(mcp_url, "http://localhost:6274")

    assert response.status_code == 200


def _policy(*, production: bool, configured: str = "") -> OriginValidation:
    settings = Settings(
        env="production" if production else "development",
        public_base_url="https://mcp.fanmoot.com",
        mcp_allowed_origins=configured,
        database_url="postgresql+asyncpg://u:p@h:5432/d",
    )

    async def _nothing(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        return None

    return OriginValidation(
        _nothing,  # type: ignore[arg-type]
        settings.mcp_allowed_origin_list,
        allow_loopback=not settings.is_production,
    )


def test_loopback_is_not_served_in_production() -> None:
    """A production deployment reached from a page on someone's laptop is the
    rebinding case itself, not a developer being convenient."""
    assert _policy(production=True).permits("http://localhost:6274") is False


def test_null_origin_is_refused() -> None:
    """What a sandboxed iframe or a ``file://`` page sends — a browser declining
    to say where it came from. Treating it as "not a browser" would hand an
    attacker the one string that gets past the check."""
    assert _policy(production=True).permits("null") is False


def test_this_servers_own_pages_are_always_allowed() -> None:
    """The consent and sign-up screens ``S4`` serves live on this origin. An
    operator should not have to list their own host."""
    assert _policy(production=True).permits("https://mcp.fanmoot.com") is True


def test_the_demo_origin_is_allowed_once_configured() -> None:
    policy = _policy(production=True, configured="https://amazonplus.fanmoot.com")

    assert policy.permits("https://amazonplus.fanmoot.com") is True
    assert policy.permits("https://fanmoot.com") is False


def test_a_configured_origin_survives_being_typed_by_a_person() -> None:
    """A trailing slash or a capital letter in ``FMCP_MCP_ALLOWED_ORIGINS``
    would otherwise refuse the very client it was added for."""
    policy = _policy(production=True, configured=" HTTPS://AmazonPlus.Fanmoot.com/ ")

    assert policy.permits("https://amazonplus.fanmoot.com") is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.com/", "https://example.com"),
        ("https://example.com:8443", "https://example.com:8443"),
        ("null", "null"),
    ],
)
def test_origins_are_compared_in_one_spelling(raw: str, expected: str) -> None:
    assert normalise_origin(raw) == expected
