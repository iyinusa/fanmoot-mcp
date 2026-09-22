"""The two durable logs: what a fan agreed to, and what got called.

Both are append-only event tables — no ``updated_at``, because an event is not a
record that changes — and both are written best-effort by ``app.audit``: a
failure to log must never fail the thing being logged.

**``consent_events`` is the one that has to exist.** "Did this fan agree to let
this client post as them, and when?" is a question that gets asked after
something has gone wrong, and the answer cannot be "the grant row says the scope
is there now". So every grant, refusal, scope upgrade and disconnection is
recorded with the address and client it came from, and revoking a grant does not
erase its history.

**``tool_calls`` is the durable half of ``app.mcp.telemetry``.** The in-process
ring buffer is what the demo's trace panel and ``make latency`` read, because
neither can afford a round trip to Postgres inside a voice turn; this table is
what survives a redeploy. Same rule as the ring: **no bodies, ever** (`S7-4`).
A tool's name, how long it took, whether it worked, and — when there was one —
which grant it ran under. Never an argument, never a result: ``post_moot``'s
argument is a fan's opinion.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import UUIDPrimaryKey

#: A fan pressed Allow on the consent screen.
CONSENT_GRANTED = "granted"
#: A fan pressed the other button. Recorded, because a client that is refused
#: repeatedly is worth looking at.
CONSENT_DENIED = "denied"
#: An existing grant gained a scope without a fresh link — SEP-835 (`S4-12`).
CONSENT_UPGRADED = "upgraded"
#: The fan disconnected it, from the connected-apps page or by revoking a token.
CONSENT_REVOKED = "revoked"
#: A new Fanmoot account was created inside the flow (`S4-11`).
CONSENT_SIGNED_UP = "signed_up"


class ConsentEvent(UUIDPrimaryKey, Base):
    """One thing a fan decided about one client."""

    __tablename__ = "consent_events"
    __table_args__ = (
        Index("ix_consent_events_user_time", "fanmoot_user_id", "created_at"),
        Index("ix_consent_events_client_time", "client_id", "created_at"),
    )

    #: Not a foreign key. A consent event outlives the client row it refers to —
    #: a reaped DCR registration should not take the record of what it was
    #: granted with it.
    client_id: Mapped[str] = mapped_column(String(200), nullable=False)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    fanmoot_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fanmoot_handle: Mapped[str] = mapped_column(String(60), nullable=False, default="")

    action: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(400), nullable=False, default="")

    #: Who asked, as far as the network can say. Kept for an investigation, and
    #: for nothing else: it is never shown to another fan.
    ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class ToolCall(UUIDPrimaryKey, Base):
    """One ``tools/call``, durably.

    The columns match ``app.mcp.telemetry.ToolCall``'s fields, so the
    write-through is an insert of that record rather than a translation of it.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (
        # "What was slow, and when" — the only query `make latency` runs against
        # this table rather than against the ring.
        Index("ix_tool_calls_tool_time", "tool", "created_at"),
    )

    tool: Mapped[str] = mapped_column(String(60), nullable=False)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    authenticated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: The HTTP request id from ``RequestIdMiddleware``, so a slow tool call in
    #: this table can be joined to the log lines around it by grep.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: Which link it ran under, when it ran under one. ``SET NULL`` rather than
    #: cascade: a fan disconnecting an app should not rewrite the latency
    #: history of the calls it made.
    grant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("access_grants.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
