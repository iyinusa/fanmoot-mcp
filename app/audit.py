"""Writing the two durable logs, without ever failing the thing being logged.

``app.models.audit`` declares the tables; this is the only module that writes to
them, and it writes **best-effort**. A consent event that cannot be recorded must
not stop a fan linking their account, and a tool-call row that cannot be written
must not fail a tool call — the in-process ring in ``app.mcp.telemetry`` is the
copy that always exists, and this is the copy that survives a redeploy.

The tool-call write-through is installed by the application lifespan and by
nothing else. That is what keeps the test suite honest about its own dependency:
the suite never installs it, so ``S3``'s tool tests need no database, exactly as
they did before this milestone (see the note in ``.github/workflows/check.yml``).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_sessionmaker
from app.logging_config import get_logger
from app.mcp import telemetry
from app.models.audit import ConsentEvent, ToolCall
from app.models.base import utcnow
from app.models.grant import AuthorizationCode

log = get_logger("audit")

#: Strong references to in-flight write-through tasks. Without this the event
#: loop is the only holder and the garbage collector may take one mid-insert.
_pending: set[asyncio.Task[None]] = set()


async def consent(
    session: AsyncSession,
    *,
    client_id: str,
    client_name: str,
    fanmoot_user_id: uuid.UUID,
    fanmoot_handle: str,
    action: str,
    scope: str = "",
    ip: str | None = None,
    user_agent: str | None = None,
    commit: bool = True,
) -> None:
    """Record one decision a fan made about one client.

    Written in the caller's session so it commits with the grant it describes:
    a grant that exists with no consent event behind it is the one state this
    table must never be in.
    """
    session.add(
        ConsentEvent(
            client_id=client_id,
            client_name=client_name[:200],
            fanmoot_user_id=fanmoot_user_id,
            fanmoot_handle=fanmoot_handle,
            action=action,
            scope=scope,
            ip=ip,
            user_agent=(user_agent or "")[:300] or None,
        )
    )
    if commit:
        await session.commit()
    log.info(
        "consent_event",
        action=action,
        client_id=client_id,
        handle=fanmoot_handle,
        scope=scope,
    )


def install_tool_call_sink() -> None:
    """Start writing every recorded tool call through to ``tool_calls``.

    Called from the lifespan. ``telemetry.record`` is synchronous and runs
    inside a tool call, so the insert is scheduled rather than awaited — a voice
    turn does not wait for a latency row to be written.
    """
    telemetry.set_sink(_write_through)


def _write_through(call: telemetry.ToolCall) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - record() is always called on the loop
        return
    task = loop.create_task(_insert(call))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _insert(call: telemetry.ToolCall) -> None:
    try:
        async with get_sessionmaker()() as session:
            session.add(
                ToolCall(
                    tool=call.tool,
                    duration_ms=call.duration_ms,
                    status=call.status,
                    authenticated=call.authenticated,
                    request_id=call.request_id,
                    grant_id=call.grant_id,
                    created_at=call.at,
                )
            )
            await session.commit()
    except Exception as exc:
        # Swallowed on purpose, and logged once with the type rather than a
        # stack: if the database is down, every tool call would otherwise print
        # a traceback and the real failure would be buried in them.
        log.warning("tool_call_not_persisted", error_type=type(exc).__name__, error=str(exc))


def pending_writes() -> set[asyncio.Task[Any]]:
    """The write-throughs still in flight — awaited by the lifespan on shutdown."""
    return set(_pending)


# ── Retention (S7-12) ────────────────────────────────────────────────────────
#: How long each log is kept. These are the numbers `/privacy` states, and
#: ``tests/test_legal.py`` checks the page against them, so the two cannot
#: drift apart.
TOOL_CALL_RETENTION_DAYS = 90
CONSENT_RETENTION_DAYS = 365
#: An authorization code lives sixty seconds. A day past expiry, its row is
#: only a record that somebody once started to sign in.
CODE_RETENTION_DAYS = 1


async def prune(session: AsyncSession) -> dict[str, int]:
    """Delete what has outlived its retention. Run daily by the lifespan."""
    now = utcnow()
    removed: dict[str, int] = {}
    for label, statement in (
        (
            "tool_calls",
            delete(ToolCall).where(
                ToolCall.created_at < now - timedelta(days=TOOL_CALL_RETENTION_DAYS)
            ),
        ),
        (
            "consent_events",
            delete(ConsentEvent).where(
                ConsentEvent.created_at < now - timedelta(days=CONSENT_RETENTION_DAYS)
            ),
        ),
        (
            "authorization_codes",
            delete(AuthorizationCode).where(
                AuthorizationCode.expires_at < now - timedelta(days=CODE_RETENTION_DAYS)
            ),
        ),
    ):
        result = cast(CursorResult[Any], await session.execute(statement))
        removed[label] = result.rowcount or 0
    await session.commit()
    if any(removed.values()):
        log.info("retention_pruned", **removed)
    return removed
