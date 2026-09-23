"""``S7-12``: the privacy page says what the code does, and the code does it.

The figures on ``/privacy`` are rendered from the constants that enforce them,
so these tests assert both halves: the page names them, and ``prune`` deletes
on them.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app import audit
from app import db as app_db
from app.models.audit import ConsentEvent, ToolCall
from app.models.base import utcnow
from app.settings import Settings


async def test_the_privacy_page_states_the_real_retention(client: httpx.AsyncClient) -> None:
    page = await client.get("/privacy")

    assert page.status_code == 200
    assert f"Tool records: {audit.TOOL_CALL_RETENTION_DAYS} days" in page.text
    assert f"{audit.CONSENT_RETENTION_DAYS} days" in page.text
    assert "support@fanmoot.com" in page.text
    assert "never stored here" in page.text


async def test_the_terms_are_served_and_link_back(client: httpx.AsyncClient) -> None:
    page = await client.get("/terms")

    assert page.status_code == 200
    assert 'href="/privacy"' in page.text


async def test_every_oauth_page_links_both(
    oauth_env: Settings, oauth_http: httpx.AsyncClient
) -> None:
    page = await oauth_http.get("/oauth/connected")

    assert 'href="/privacy"' in page.text
    assert 'href="/terms"' in page.text


async def test_prune_keeps_what_is_young_and_deletes_what_is_not(
    oauth_env: Settings, oauth_db: AsyncEngine
) -> None:
    now = utcnow()
    old_call = now - timedelta(days=audit.TOOL_CALL_RETENTION_DAYS + 1)
    old_consent = now - timedelta(days=audit.CONSENT_RETENTION_DAYS + 1)

    async with app_db.get_sessionmaker()() as session:
        for at in (old_call, now):
            session.add(ToolCall(tool="live_scores", duration_ms=5, status="ok", created_at=at))
        for at in (old_consent, now - timedelta(days=30)):
            session.add(
                ConsentEvent(
                    client_id="c",
                    fanmoot_user_id=uuid.uuid4(),
                    action="granted",
                    created_at=at,
                )
            )
        await session.commit()

        removed = await audit.prune(session)
        assert removed["tool_calls"] == 1
        assert removed["consent_events"] == 1
        assert await session.scalar(select(func.count()).select_from(ToolCall)) == 1
        assert await session.scalar(select(func.count()).select_from(ConsentEvent)) == 1
