"""``S7-11``: the MCP registry entry, and the proof that we own its namespace."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import httpx
import pytest

from app import __version__
from app.settings import reset_settings_cache

ROOT = Path(__file__).resolve().parent.parent
SERVER = json.loads((ROOT / "server.json").read_text())


def test_the_entry_fits_the_registry_schema() -> None:
    """The ``2025-12-11`` schema's own limits, checked here so a publish is not
    the first thing to find out."""
    assert SERVER["$schema"].endswith("/2025-12-11/server.schema.json")
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", SERVER["name"])
    assert 1 <= len(SERVER["description"]) <= 100
    assert 1 <= len(SERVER["title"]) <= 100
    assert SERVER["remotes"] == [{"type": "streamable-http", "url": "https://mcp.fanmoot.com/mcp"}]


def test_the_namespace_is_the_host_that_proves_it() -> None:
    """HTTP verification on mcp.fanmoot.com grants com.fanmoot.mcp/*, and nothing else."""
    assert SERVER["name"].startswith("com.fanmoot.mcp/")


def test_the_version_is_the_one_being_shipped() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert SERVER["version"] == __version__ == pyproject["project"]["version"]


def test_the_icon_is_one_this_server_serves() -> None:
    src = SERVER["icons"][0]["src"]
    assert src.startswith("https://mcp.fanmoot.com/static/")
    assert (ROOT / "app" / src.removeprefix("https://mcp.fanmoot.com/")).is_file()


async def test_no_proof_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/.well-known/mcp-registry-auth")).status_code == 404


async def test_the_proof_is_served_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    proof = "v=MCPv1; k=ed25519; p=MCowBQYDK2VwAyEAexamplepublickey="
    monkeypatch.setenv("FMCP_REGISTRY_AUTH", proof)
    reset_settings_cache()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/.well-known/mcp-registry-auth")

    assert response.status_code == 200
    assert response.text.strip() == proof
