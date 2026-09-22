"""Mixins every table here shares, and the one time-handling rule they all obey.

Lifted from ``fanmoot``'s ``app/models/base.py`` so a reviewer moving between
the two repos reads one shape: a UUID primary key generated in Python, and
``created_at`` / ``updated_at`` maintained by the database.

**Why the ids are generated in Python.** A server-side ``gen_random_uuid()``
default means the id is unknown until after the INSERT, and half the code in
``app.oauth`` needs it *before*: an authorization code is hashed against the row
that will hold it, and a refresh-token family is named after its first member.
Generating in Python makes that a straight line rather than a flush in the
middle of it.

**Why every read of a deadline goes through ``as_utc``.** These columns are
``DateTime(timezone=True)`` and PostgreSQL — the only database this service runs
against (HAD-3) — hands them back timezone-aware. The test suite is the
exception: it substitutes SQLite so the OAuth flow can be exercised end to end
without a database service in CI, and SQLite has no timezone type, so the same
column reads back **naive**. A naive datetime compared against an aware one
raises, which would turn "has this code expired?" into a 500 in the one place
that must never guess. ``as_utc`` is therefore not defensive clutter: it is the
seam that lets the tests exercise the real models, and it is called on every
expiry comparison in this package.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    """Now, timezone-aware, in UTC. The only clock this package reads."""
    return datetime.now(UTC)


def as_utc(moment: datetime | None) -> datetime | None:
    """A stored datetime as an aware UTC one, whatever the driver returned.

    See the module docstring: PostgreSQL returns aware, SQLite returns naive,
    and a comparison between the two raises. A naive value is *known* to be UTC
    because ``utcnow`` is the only thing that ever writes one.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def expired(deadline: datetime | None, *, at: datetime | None = None) -> bool:
    """Whether ``deadline`` has passed. An absent deadline never expires."""
    moment = as_utc(deadline)
    if moment is None:
        return False
    return moment <= (at or utcnow())


class UUIDPrimaryKey:
    """A UUID primary key, generated here rather than by the database."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Timestamps:
    """``created_at`` and ``updated_at``, both maintained server-side.

    ``server_default`` rather than a Python default so a row written by a
    migration or by psql carries the same stamps as one written by the ORM.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
