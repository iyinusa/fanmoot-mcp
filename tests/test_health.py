"""``/healthz`` — the four facts, and the promise that it answers 200 anyway."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app import __version__, health


@pytest.fixture
def upstream_ok() -> Any:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://fanmoot.test/healthz").mock(
            return_value=httpx.Response(200, json={"status": "ok"})
        )
        yield mock


@pytest.fixture
def db_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ping(timeout_s: float = 2.0) -> bool:
        return True

    monkeypatch.setattr(health, "ping", _ping)


@pytest.fixture
def db_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ping(timeout_s: float = 2.0) -> bool:
        raise OSError("connection refused")

    monkeypatch.setattr(health, "ping", _ping)


async def test_reports_version_protocol_and_both_dependencies(
    client: httpx.AsyncClient, upstream_ok: Any, db_ok: None
) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "fanmoot-mcp"
    assert body["version"] == __version__
    assert body["checks"]["fanmoot_api"] == "ok"
    assert body["checks"]["database"] == "ok"


async def test_serves_the_handshake_era_protocol_version(
    client: httpx.AsyncClient, upstream_ok: Any, db_ok: None
) -> None:
    """The number the Alexa+ track turns on (HAD-2). Read from the SDK, so a
    dependency bump onto the modern era (`2026-07-28`) fails here rather than in
    a judge's handshake. `S2-4` asserts the *negotiated* version; this asserts
    the one we are built against."""
    body = (await client.get("/healthz")).json()

    assert body["mcp_protocol_version"] == "2025-11-25"


async def test_an_unreachable_upstream_is_degraded_not_dead(
    client: httpx.AsyncClient, db_ok: None
) -> None:
    """Restarting this container does not fix fanmoot.com. Docker and Traefik
    act on the status code, so dependency health lives in the body — otherwise
    a recoverable upstream failure becomes a restart loop."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://fanmoot.test/healthz").mock(side_effect=httpx.ConnectError("refused"))
        response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["fanmoot_api"] == "unreachable"


async def test_an_unreachable_database_is_degraded_not_dead(
    client: httpx.AsyncClient, upstream_ok: Any, db_down: None
) -> None:
    response = await client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "unavailable"


async def test_missing_grant_storage_is_a_posture_not_a_fault(
    client: httpx.AsyncClient, upstream_ok: Any, db_ok: None
) -> None:
    """No Fernet key closes the authenticated half of the surface and leaves the
    read tools alone, so it is reported without degrading the service."""
    body = (await client.get("/healthz")).json()

    assert body["checks"]["grant_storage"] == "disabled"
    assert body["status"] == "ok"
