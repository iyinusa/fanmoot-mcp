"""Request correlation — the thread that ties this server's logs to the demo's
trace panel (HAD-12)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app import health


@pytest.fixture(autouse=True)
def _dependencies_answer(monkeypatch: pytest.MonkeyPatch) -> Any:
    async def _ping(timeout_s: float = 2.0) -> bool:
        return True

    monkeypatch.setattr(health, "ping", _ping)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://fanmoot.test/healthz").mock(return_value=httpx.Response(200))
        yield mock


async def test_an_inbound_request_id_is_honoured(client: httpx.AsyncClient) -> None:
    """So a trace can be followed across a proxy — and across the boundary
    between the demo's trace panel and this server."""
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-me-0001"})

    assert response.headers["X-Request-ID"] == "trace-me-0001"


async def test_one_is_minted_when_the_client_sends_none(client: httpx.AsyncClient) -> None:
    first = await client.get("/healthz")
    second = await client.get("/healthz")

    assert first.headers["X-Request-ID"]
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
