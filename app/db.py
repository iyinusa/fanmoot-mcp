"""Async SQLAlchemy engine, session factory and the FastAPI session dependency.

This database holds **OAuth state and nothing else** (HAD-3): clients, codes,
grants, refresh tokens, the encrypted upstream sessions, consent events and the
tool-call log. Fanmoot's own schema is reached over its public API and is not
represented here at all (HAD-1).

The engine is built lazily rather than at import time. Two reasons: the settings
it needs are read from the environment, which tests mutate before importing the
app; and a module-level ``create_async_engine`` would open a connection pool
inside Alembic and inside every worker fork, whether or not that process ever
runs a query.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.settings import get_settings


class Base(DeclarativeBase):
    """Declarative base. Every model subclasses this, and Alembic's autogenerate
    compares the database against ``Base.metadata`` — so a model that is never
    imported is a table that never gets migrated. ``app/models/__init__.py``
    exists to import them all, and arrives with ``S4``."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    kwargs: dict[str, Any] = {
        # Checks a pooled connection is alive before handing it out. Not
        # optional against a *remote* Postgres: the network, the provider's idle
        # timeout or a failover will silently close pooled sockets, and without
        # this the first query after an idle spell fails.
        "pool_pre_ping": True,
        "echo": settings.db_echo,
        "future": True,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        # Retire connections below any sane proxy or provider idle cut-off, so
        # we close them before the far end does it for us.
        "pool_recycle": 1800,
    }
    return create_async_engine(settings.database_url, **kwargs)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            class_=AsyncSession,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session per request.

    The session is closed on the way out but **not** committed: callers commit
    explicitly, so a handler that raises half-way through a multi-step write
    leaves nothing behind.
    """
    async with get_sessionmaker()() as session:
        yield session


async def ping(timeout_s: float = 2.0) -> bool:
    """Whether the database answers, within a bounded wait.

    The timeout is the point. This backs ``/healthz``, which Docker curls every
    30 seconds with a 5-second timeout of its own; an unbounded connect against
    an unreachable host would hang the probe rather than report the problem.
    """
    from sqlalchemy import text

    async def _probe() -> bool:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    return await asyncio.wait_for(_probe(), timeout=timeout_s)


async def dispose_engine() -> None:
    """Close the pool on shutdown so workers exit without dangling sockets."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def reset_engine_for_tests() -> None:
    """Test hook: drop the cached engine so a new ``FMCP_DATABASE_URL`` takes effect."""
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None
