"""Client ID metadata documents — SEP-991, `S4-17`.

The mechanism ``2025-11-25`` *recommends*: instead of registering, a client
publishes its metadata at a URL and uses that URL as its ``client_id``. The
first time one arrives there is no row for it, so an unknown id that is a URL is
a registration waiting to happen rather than a failure.

Half of what is asserted here is the fence around it. This is an outbound
request to a URL a caller chose, which is server-side request forgery unless it
is fenced: ``https`` only, no loopback or dotless host, redirects not followed,
and a document that must claim the id it was found at — otherwise anybody could
point at somebody else's document and inherit their redirect URIs.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app import db as app_db
from app.models import TIER_METADATA_DOCUMENT, OAuthClient
from app.oauth import clients as client_store
from app.settings import Settings
from tests import oauth

DOCUMENT_URL = "https://client.example/.well-known/oauth-client"


def _document(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_id": DOCUMENT_URL,
        "client_name": "A Self-Describing Client",
        "redirect_uris": ["https://client.example/callback"],
    }
    body.update(overrides)
    return body


@pytest.fixture
def served() -> Any:
    """The client's own metadata, served from its URL."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(DOCUMENT_URL).mock(return_value=httpx.Response(200, json=_document()))
        yield mock


async def test_a_client_identified_by_a_url_is_registered_on_first_use(
    oauth_db: object, oauth_env: Settings, served: Any
) -> None:
    async with app_db.get_sessionmaker()() as session:
        client = await client_store.find_or_resolve(session, DOCUMENT_URL)

    assert client is not None
    assert client.client_id == DOCUMENT_URL
    assert client.tier == TIER_METADATA_DOCUMENT
    assert client.metadata_url == DOCUMENT_URL
    assert client.metadata_refreshed_at is not None
    # Public: a client identified by a URL it controls has no secret to keep.
    assert client.is_public


async def test_authorize_accepts_a_metadata_document_client(
    oauth_http: httpx.AsyncClient, served: Any
) -> None:
    """The point of the mechanism: a client that never registered can still send
    a fan here."""
    _, challenge = oauth.pkce_pair()

    page = await oauth.authorize(
        oauth_http,
        client_id=DOCUMENT_URL,
        challenge=challenge,
        redirect_uri="https://client.example/callback",
    )

    assert page.status_code == 200
    assert "A Self-Describing Client" in page.text


async def test_a_document_that_claims_another_id_is_refused(
    oauth_db: object, oauth_env: Settings
) -> None:
    """Otherwise anybody could point at somebody else's document and inherit
    their redirect URIs."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(DOCUMENT_URL).mock(
            return_value=httpx.Response(
                200, json=_document(client_id="https://someone-else.example/doc")
            )
        )
        async with app_db.get_sessionmaker()() as session:
            client = await client_store.find_or_resolve(session, DOCUMENT_URL)

    assert client is None


async def test_the_second_use_reads_the_row_rather_than_the_network(
    oauth_db: object, oauth_env: Settings, served: Any
) -> None:
    async with app_db.get_sessionmaker()() as session:
        await client_store.find_or_resolve(session, DOCUMENT_URL)
        await client_store.find_or_resolve(session, DOCUMENT_URL)

    assert served.calls.call_count == 1
    async with app_db.get_sessionmaker()() as session:
        rows = (await session.execute(select(OAuthClient))).scalars().all()
    assert len(rows) == 1


@pytest.mark.parametrize(
    "client_id",
    [
        "http://client.example/doc",  # cleartext
        "https://localhost/doc",  # loopback — an internal service, not a client
        "https://intranet/doc",  # dotless host — resolves only inside the deployment
        "not-a-url",
    ],
)
async def test_only_a_public_https_url_is_ever_fetched(
    oauth_db: object, oauth_env: Settings, client_id: str
) -> None:
    """The SSRF fence. A `client_id` that does not pass this is simply an
    unknown client — nothing is fetched at all."""
    with respx.mock(assert_all_called=False) as mock:
        async with app_db.get_sessionmaker()() as session:
            client = await client_store.find_or_resolve(session, client_id)

    assert client is None
    assert mock.calls.call_count == 0


async def test_an_unreachable_document_is_an_unknown_client_not_an_error(
    oauth_db: object, oauth_env: Settings
) -> None:
    """A failure here must never be a stack trace or a retry loop: the caller
    reports an unknown client and the flow stops on a page."""
    with respx.mock(assert_all_called=False) as mock:
        mock.get(DOCUMENT_URL).mock(side_effect=httpx.ConnectError("no route"))
        async with app_db.get_sessionmaker()() as session:
            client = await client_store.find_or_resolve(session, DOCUMENT_URL)

    assert client is None


async def test_the_mechanism_is_off_when_dcr_is_off(
    oauth_db: object, oauth_env: Settings, served: Any
) -> None:
    """Both are ways for a client to arrive without an operator, so one switch
    governs both — and the metadata document says so."""
    import os

    from app.settings import reset_settings_cache

    os.environ["FMCP_OAUTH_DCR_ENABLED"] = "false"
    reset_settings_cache()
    try:
        async with app_db.get_sessionmaker()() as session:
            client = await client_store.find_or_resolve(session, DOCUMENT_URL)
    finally:
        os.environ.pop("FMCP_OAUTH_DCR_ENABLED", None)
        reset_settings_cache()

    assert client is None
    assert served.calls.call_count == 0
