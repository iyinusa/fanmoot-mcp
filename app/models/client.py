"""``oauth_clients`` — every client that may ask a fan for access.

One table for both tiers of HAD-5. Alexa's row is pre-seeded from the
environment; Claude's, ChatGPT's, Cursor's and the Inspector's create
themselves through DCR (`S4-16`) or are resolved from a client ID metadata
document (`S4-17`). A self-registered client is **not** second-class: same
columns, same scopes, same flow. The only difference is ``tier``, which exists
so an operator reading the audit log can tell Alexa's pre-seeded credentials
from something that registered itself thirty seconds ago without guessing from
the shape of the id (`S4-18`).

**The secret is stored as a SHA-256 hash and the plaintext is shown once**
(HAD-3, mirroring ``fanmoot``'s ``models/token.py``). A dump of this table
therefore hands an attacker a list of redirect URIs — which are public
anyway — and nothing they can authenticate with.

**Redirect URIs are a list, and the list is the whole of the policy** (`S4-5`).
Alexa has several, varying by where the device was registered, and the single
most likely cause of a silent account-linking failure is a server that
registered one of them and substituted it for whatever it was sent. So the
presented URI is compared, exactly, against every registered one — and echoed
back unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import Timestamps, UUIDPrimaryKey, expired

#: Pre-seeded from ``FMCP_OAUTH_CLIENTS``. In practice: Alexa, which has no DCR.
TIER_STATIC = "static"
#: Registered itself through RFC 7591 at ``POST /oauth/register``.
TIER_DYNAMIC = "dynamic"
#: Identified by a URL serving its own metadata — SEP-991, the mechanism
#: ``2025-11-25`` recommends (`S4-17`).
TIER_METADATA_DOCUMENT = "metadata_document"

TIERS = frozenset({TIER_STATIC, TIER_DYNAMIC, TIER_METADATA_DOCUMENT})

#: RFC 7591's default when a client says nothing, and what a confidential
#: client such as Alexa uses. ``none`` is the public-client case — Claude
#: Desktop, Cursor and the Inspector hold no secret and lean on PKCE instead,
#: which OAuth 2.1 makes mandatory for everyone anyway.
AUTH_METHOD_SECRET_POST = "client_secret_post"  # noqa: S105 - a method name
AUTH_METHOD_SECRET_BASIC = "client_secret_basic"  # noqa: S105 - a method name
AUTH_METHOD_NONE = "none"

AUTH_METHODS = frozenset({AUTH_METHOD_SECRET_POST, AUTH_METHOD_SECRET_BASIC, AUTH_METHOD_NONE})


class OAuthClient(UUIDPrimaryKey, Timestamps, Base):
    """One registered client."""

    __tablename__ = "oauth_clients"
    __table_args__ = (
        # The reaper's query (`S4-16`): dynamic rows that never completed a
        # grant, oldest first.
        Index("ix_oauth_clients_tier_last_grant", "tier", "last_grant_at"),
    )

    #: What the client sends as ``client_id``. Server-generated for every tier
    #: but ``static``, where it is Amazon's own id for the add-on.
    client_id: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)

    #: SHA-256 of the secret, or NULL for a public client. Never the secret.
    client_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: How this client authenticates at ``/token``. A public client says
    #: ``none``; a confidential one presents its secret in the body or in a
    #: Basic header, and both are accepted because clients differ on which.
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(40), nullable=False, default=AUTH_METHOD_NONE
    )

    #: Shown on the consent screen, so it is the name a fan is being asked to
    #: trust. A DCR client supplies its own and we display it as given — a
    #: client that lies about its name is why the redirect URI, not the name, is
    #: what the flow actually trusts.
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    client_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    logo_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    policy_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tos_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    software_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    software_version: Mapped[str | None] = mapped_column(String(60), nullable=True)

    #: Every registered redirect URI. Matched exactly and echoed back (`S4-5`).
    redirect_uris: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    #: Declared for the discovery documents and enforced at ``/token``.
    #: ``authorization_code`` and ``refresh_token``; never ``implicit``, which
    #: OAuth 2.1 removes and Alexa does not support.
    grant_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    response_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    #: Space-separated, as RFC 7591 sends it. The ceiling on what this client
    #: may ever be granted, not what it currently holds.
    scope: Mapped[str] = mapped_column(String(400), nullable=False, default="")

    tier: Mapped[str] = mapped_column(String(30), nullable=False, default=TIER_DYNAMIC)

    #: SHA-256 of the RFC 7591 registration access token, for a client that
    #: comes back to read or update its own registration.
    registration_access_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: SEP-991: the URL that served this client's metadata, and when it was last
    #: read. Held so a client identified by URL can be refreshed rather than
    #: re-registered, and so the audit log can say where a claim came from.
    metadata_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: When this client last completed a grant. NULL after registration, which
    #: is exactly what the TTL reaper looks for (`S4-16`).
    last_grant_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Set instead of deleting. A revoked client's grants and audit trail keep
    #: their foreign key, and the row stops working.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Registrations expire (`S4-16`); a static client never does. Held as a
    #: column rather than computed so a client can be pinned open by hand.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Derived ──────────────────────────────────────────────────────────────
    @property
    def is_public(self) -> bool:
        """No secret. PKCE is what protects the code, for every client (`S4-4`)."""
        return self.token_endpoint_auth_method == AUTH_METHOD_NONE

    @property
    def is_active(self) -> bool:
        return self.disabled_at is None and not expired(self.expires_at)

    @property
    def allowed_scopes(self) -> set[str]:
        return {part for part in self.scope.split() if part}

    def permits_redirect_uri(self, redirect_uri: str) -> bool:
        """Exact match against the registered set — no prefix, no default (`S4-5`).

        Exact because everything looser has been an exploit: a prefix match
        lets ``https://client.example/cb.attacker.example`` through, and a
        "just take the first one" fallback is how a code gets delivered to the
        wrong Alexa endpoint and the link fails with no error anybody can see.
        """
        return redirect_uri in (self.redirect_uris or [])

    def as_registration_response(self, **extra: Any) -> dict[str, Any]:
        """This client as RFC 7591 describes it back to itself.

        ``client_secret`` is not here: it is known only at the moment of
        registration and is added by the handler that generated it.
        """
        body: dict[str, Any] = {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "redirect_uris": list(self.redirect_uris or []),
            "grant_types": list(self.grant_types or []),
            "response_types": list(self.response_types or []),
            "scope": self.scope,
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
        }
        for key, value in (
            ("client_uri", self.client_uri),
            ("logo_uri", self.logo_uri),
            ("policy_uri", self.policy_uri),
            ("tos_uri", self.tos_uri),
            ("software_id", self.software_id),
            ("software_version", self.software_version),
        ):
            if value:
                body[key] = value
        body.update(extra)
        return body
