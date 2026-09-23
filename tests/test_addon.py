"""``S8-1``: the Alexa+ manifest, checked against the schema Amazon documents.

The toolkit that would validate it is partner-gated, so these tests are the
only validation it gets. The limits are the MCP QuickStart's field table.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import httpx
import pytest

from app.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
ADDON = json.loads((ROOT / "alexa" / "addon.json").read_text())
LISTING = ADDON["storeListing"]
LOCALE = LISTING["locales"]["en-US"]
MEDIA = LOCALE["mediaAssets"]
ORIGIN = "https://mcp.fanmoot.com"

ICON_SIZES = {"72x72", "64x64", "88x88", "126x126", "180x180", "241x241"}


def _png_size(path: Path) -> str:
    """``WxH`` from the IHDR chunk, so the test needs no imaging library."""
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", head[16:24])
    return f"{width}x{height}"


def _local(uri: str) -> Path:
    """The file under ``app/`` that the server mounts at ``uri``."""
    assert uri.startswith(f"{ORIGIN}/static/"), uri
    return ROOT / "app" / uri.removeprefix(f"{ORIGIN}/")


def test_the_listing_fits_the_documented_limits() -> None:
    assert ADDON["manifestVersion"] == "1.0"
    assert LISTING["distributionCountries"] == ["US"]
    assert set(LISTING["locales"]) == {"en-US"}
    assert 1 <= len(LOCALE["name"]["value"]) <= 30
    assert 1 <= len(LOCALE["shortDescription"]) <= 123
    assert 1 <= len(LOCALE["fullDescription"]) <= 4000
    assert 3 <= len(LOCALE["examplePhrases"]) <= 4
    assert all(1 <= len(phrase) <= 200 for phrase in LOCALE["examplePhrases"])


def test_the_endpoint_is_the_resource_the_metadata_names() -> None:
    """Alexa requires the PRM ``resource`` to match this URL exactly. It is the
    MCP endpoint, not the host, so a manifest naming the host would never link."""
    (integration,) = ADDON["integrations"]
    assert integration["type"] == "MCP"
    endpoint = integration["config"]["endpoints"]["default"]
    assert endpoint["type"] == "HTTPS"
    assert endpoint["uri"] == Settings(public_base_url=ORIGIN).resource_uri  # type: ignore[call-arg]

    server = json.loads((ROOT / "server.json").read_text())
    assert endpoint["uri"] == server["remotes"][0]["url"]


def test_every_image_is_served_here_at_its_stated_size() -> None:
    light = MEDIA["icons"]["light"]
    assert {icon["size"] for icon in light} == ICON_SIZES
    assert len(light) == len(ICON_SIZES)
    assert MEDIA["icons"]["dark"] == []

    carousel = MEDIA["carouselImages"]
    assert len(carousel) >= 1
    assert all(image["size"] == "600x900" for image in carousel)
    assert all(1 <= len(image["altText"]) <= 250 for image in carousel)

    for image in [*light, *carousel]:
        assert _png_size(_local(image["uri"])) == image["size"]


@pytest.mark.parametrize("field", ["privacyPolicyUrl", "termsOfUseUrl"])
async def test_the_policy_pages_are_ones_this_server_serves(
    client: httpx.AsyncClient, field: str
) -> None:
    url = LOCALE["privacyAndCompliance"][field]
    assert url.startswith(f"{ORIGIN}/")
    assert (await client.get(url.removeprefix(ORIGIN))).status_code == 200


async def test_the_images_are_mounted(client: httpx.AsyncClient) -> None:
    uri = MEDIA["icons"]["light"][0]["uri"]
    response = await client.get(uri.removeprefix(ORIGIN))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
