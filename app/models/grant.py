"""The four tables one account link actually consists of.

Read in the order a flow creates them:

1. **``upstream_sessions``** — the fan signed in to Fanmoot on our consent page,
   and we are holding the **Fanmoot** refresh token that came back, encrypted.
   This is the load-bearing table of HAD-3.
2. **``authorization_codes``** — they pressed Allow, and a 60-second single-use
   code is on its way back to the client through a browser redirect.
3. **``access_grants``** — the code was spent. This row *is* the link: which
   client, which fan, which scopes, which resource, and the hash of the access
   token currently outstanding.
4. **``refresh_tokens``** — a rotating chain hanging off the grant, all members
   sharing a ``family_id`` so a replay is detectable.

**Every credential is stored as a SHA-256 hash, never as itself** (`S4-1`,
HAD-3, and the same rule as ``fanmoot``'s ``models/token.py``). The exception is
the fan's upstream Fanmoot refresh token, which cannot be hashed because it has
to be *used* — so it is encrypted with Fernet instead, and a server with no key
declines to store one at all rather than storing it in the clear.

**Why an Alexa access token is not a Fanmoot access token.** Fanmoot's own
access tokens live 15 minutes and cannot be revoked; Alexa needs a credential
that survives months and that a fan can take away on demand. So what Alexa
holds is an opaque handle to ``access_grants``, and the exchange in
``app.oauth.upstream`` turns it into a fresh Fanmoot token per turn (`S4-8`). A
leaked Alexa token buys an attacker nothing outside the scopes on that row.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import Timestamps, UUIDPrimaryKey, expired

#: Why something was revoked. Read by an operator after the fact, so the values
#: are sentences-in-a-word rather than codes.
REVOKED_BY_FAN = "fan_disconnected"
REVOKED_REUSE = "refresh_reuse_detected"
REVOKED_SUPERSEDED = "superseded_by_new_grant"
REVOKED_UPSTREAM = "upstream_session_ended"
REVOKED_BY_CLIENT = "client_revoked"


class UpstreamSession(UUIDPrimaryKey, Timestamps, Base):
    """The fan's Fanmoot session, held on their behalf.

    One row per sign-in on our consent page. It holds a **Fanmoot** refresh
    token encrypted at rest with Fernet, and nothing else about the fan beyond
    the id and handle needed to name them on a "connected apps" page — no email
    address, no password, no name. ``app.oauth.upstream`` is the only module
    that decrypts it.

    Rotation is expected, not exceptional: Fanmoot rotates refresh tokens on
    every exchange and revokes the family on a replay, so this row is rewritten
    each time the exchange runs and ``rotated_at`` is when that last happened.
    A fan who signs out of Fanmoot everywhere therefore breaks this link — which
    is correct, and is why the spoken failure says "reconnect your account"
    rather than "something went wrong".
    """

    __tablename__ = "upstream_sessions"
    __table_args__ = (Index("ix_upstream_sessions_user_active", "fanmoot_user_id", "revoked_at"),)

    fanmoot_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: For display on the consent screen and the connected-apps page. A handle
    #: is public on Fanmoot; the email address the fan typed to get here is not
    #: stored anywhere in this database.
    fanmoot_handle: Mapped[str] = mapped_column(String(60), nullable=False)

    #: Fernet ciphertext. NOT a hash — this one has to be usable (HAD-3).
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class AccessGrant(UUIDPrimaryKey, Timestamps, Base):
    """One live link between a client and a fan. The row the whole flow exists for.

    ``access_token_hash`` lives here rather than in a table of its own, and that
    is a deliberate consequence of one grant holding one outstanding access
    token at a time: a refresh mints the next one and the previous stops
    working. Alexa holds exactly one, every other client holds exactly one, and
    a table of historical access tokens would be a table of dead rows with a
    90-day retention question attached.

    ``resource`` is RFC 8707's indicator, stored as it was presented on
    ``/authorize`` so ``/token`` can be held to it and the mount can refuse a
    token minted for something else (`S4-6`).
    """

    __tablename__ = "access_grants"
    __table_args__ = (
        # "Which clients hold a grant for this fan" — the connected-apps page.
        Index("ix_access_grants_user_active", "fanmoot_user_id", "revoked_at"),
        # "Does this client already have a grant for this fan" — the consent
        # screen, and the scope upgrade in `S4-12`.
        Index("ix_access_grants_client_user", "client_id", "fanmoot_user_id"),
    )

    client_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False
    )
    upstream_session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("upstream_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    fanmoot_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fanmoot_handle: Mapped[str] = mapped_column(String(60), nullable=False)

    #: Space-separated, exactly what the fan consented to — never what the
    #: client asked for.
    scope: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    resource: Mapped[str | None] = mapped_column(String(500), nullable=True)

    access_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Last time a tool call resolved this grant. What makes a connected-apps
    #: page say "last used yesterday" instead of only "connected in March".
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def scopes(self) -> set[str]:
        return {part for part in self.scope.split() if part}

    def covers(self, scope: str) -> bool:
        return scope in self.scopes


class AuthorizationCode(UUIDPrimaryKey, Timestamps, Base):
    """A 60-second, single-use code in flight through a browser redirect.

    It carries everything ``/token`` has to hold the client to: the exact
    ``redirect_uri`` presented on ``/authorize``, the PKCE challenge, the scopes
    the fan actually approved, and the ``resource`` indicator. Checking those at
    redemption rather than trusting what the token request repeats is the whole
    of the authorization-code protection (`S4-6`).

    ``used_at`` is set on redemption and a second presentation is refused. It is
    not deleted, because "this code was already spent" and "this code never
    existed" are different events and only the first one is worth an alert.
    """

    __tablename__ = "authorization_codes"
    __table_args__ = (Index("ix_authorization_codes_client", "client_id", "expires_at"),)

    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    client_id: Mapped[str] = mapped_column(
        String(200), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False
    )
    upstream_session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("upstream_sessions.id", ondelete="CASCADE"), nullable=False
    )

    fanmoot_user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fanmoot_handle: Mapped[str] = mapped_column(String(60), nullable=False)

    #: Echoed back, never substituted (`S4-5`). Held so ``/token`` can require
    #: the same value and refuse a different one.
    redirect_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    scope: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    resource: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: PKCE. ``S256`` only — ``plain`` is refused on ``/authorize`` and the
    #: column exists so the refusal can be logged with what was attempted.
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(10), nullable=False, default="S256")

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_spent(self) -> bool:
        return self.used_at is not None

    @property
    def is_expired(self) -> bool:
        return expired(self.expires_at)


class RefreshToken(UUIDPrimaryKey, Timestamps, Base):
    """One link in a rotating refresh chain, exactly as ``fanmoot`` does it.

    Refresh tokens rotate: spending one issues its successor and marks this one
    used. Every token descended from a single grant shares a ``family_id``, and
    that is what makes theft detectable — if a spent token is presented again,
    either the legitimate client replayed it or somebody stole it, and there is
    no way to tell which from here. The safe answer is to revoke the whole
    family and make the fan link again, so a stolen token is worth at most one
    use (`S4-6`).
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_family", "family_id"),
        Index("ix_refresh_tokens_grant_active", "grant_id", "revoked_at"),
    )

    grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("access_grants.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)

    @property
    def is_spendable(self) -> bool:
        return self.used_at is None and self.revoked_at is None and not expired(self.expires_at)
