"""Codes, grants and the rotating refresh chain — the state machine of `S4-6`.

One authorization produces one ``AccessGrant``, and everything after that hangs
off it. The rules enforced here are the compliance-critical half of the
milestone, so each one is stated where it is implemented rather than in a README:

* **A code is single-use and lives 60 seconds.** It travels through a browser
  redirect, so a longer life is a longer window for a code sitting in somebody's
  history — and a code that could be spent twice is a code an attacker who saw
  the redirect can spend after the client did.
* **PKCE is verified against the challenge stored with the code**, never against
  one repeated in the token request. S256 only.
* **The redirect URI is compared to the one the code was issued for.** A client
  that authorized against one URI and redeems against another is either broken
  or being used.
* **The resource indicator is honoured on authorize and on token, and is absent
  on refresh** — which is exactly what Alexa does, and is why a refresh reuses
  the grant's stored value rather than requiring one.
* **Refresh tokens rotate, and a replay revokes the family.** Presenting a spent
  token means either the legitimate client retried or somebody stole it, and
  there is no way to tell which from here. Revoking the family makes a stolen
  token worth at most one use, at the cost of asking that fan to link again —
  which is the right side of that trade. ``fanmoot``'s own
  ``rotate_refresh_token`` makes the same call for the same reason.
* **A refresh token is always issued** (`S4-7`). Alexa's docs make it mandatory,
  and a fan re-linking their account every hour is the alternative.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models.base import as_utc, expired, utcnow
from app.models.client import OAuthClient
from app.models.grant import (
    REVOKED_BY_FAN,
    REVOKED_REUSE,
    REVOKED_SUPERSEDED,
    AccessGrant,
    AuthorizationCode,
    RefreshToken,
    UpstreamSession,
)
from app.oauth import pkce, tokens
from app.oauth.errors import InvalidGrant, InvalidTarget
from app.settings import Settings, get_settings

log = get_logger("oauth.grants")


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    """What ``/token`` answers with. The plaintext exists only here and in that
    response — the database holds hashes."""

    access_token: str
    refresh_token: str
    expires_in: int
    scope: str


# ── Authorization codes ──────────────────────────────────────────────────────
async def issue_code(
    session: AsyncSession,
    *,
    client: OAuthClient,
    upstream: UpstreamSession,
    scope: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    resource: str | None,
    settings: Settings | None = None,
) -> str:
    """Mint a code for a consent that has just been given. Returns the plaintext.

    The row stores its SHA-256 and everything ``/token`` will be held to. The
    plaintext is returned once, goes into the redirect, and is never recoverable
    from this database.
    """
    settings = settings or get_settings()
    code = tokens.mint()
    session.add(
        AuthorizationCode(
            code_hash=tokens.fingerprint(code),
            client_id=client.client_id,
            upstream_session_id=upstream.id,
            fanmoot_user_id=upstream.fanmoot_user_id,
            fanmoot_handle=upstream.fanmoot_handle,
            redirect_uri=redirect_uri,
            scope=scope,
            resource=resource,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            expires_at=utcnow() + timedelta(seconds=settings.oauth_code_ttl_s),
        )
    )
    await session.commit()
    return code


async def redeem_code(
    session: AsyncSession,
    *,
    client: OAuthClient,
    code: str,
    redirect_uri: str | None,
    code_verifier: str | None,
    resource: str | None,
    settings: Settings | None = None,
) -> AccessGrant:
    """Spend a code and return the grant it created.

    Every failure below is ``invalid_grant`` with no distinguishing detail, on
    purpose: a caller cannot act differently on "expired" than on "already
    spent", and an attacker probing codes should learn nothing from the
    difference.
    """
    settings = settings or get_settings()

    row = (
        await session.execute(
            select(AuthorizationCode).where(
                AuthorizationCode.code_hash == tokens.fingerprint(code or "")
            )
        )
    ).scalar_one_or_none()

    if row is None:
        raise InvalidGrant("The authorization code is not valid.")

    if row.client_id != client.client_id:
        # A code issued to one client being redeemed by another. Logged at
        # warning because it is never an accident.
        log.warning(
            "oauth_code_wrong_client",
            issued_to=row.client_id,
            presented_by=client.client_id,
        )
        raise InvalidGrant("The authorization code is not valid.")

    if row.is_spent:
        # Replay. The code is gone either way; what is worth doing is revoking
        # anything it already produced, because if this is the attacker's
        # presentation the legitimate client's tokens are the ones at risk.
        log.warning("oauth_code_replayed", client_id=client.client_id)
        await _revoke_grants_from_code(session, row)
        raise InvalidGrant("The authorization code is not valid.")

    if row.is_expired:
        raise InvalidGrant("The authorization code is not valid.")

    # RFC 6749 §4.1.3: the redirect URI must be the one the code was issued for.
    # Compared, never substituted (`S4-5`).
    if redirect_uri is not None and redirect_uri != row.redirect_uri:
        log.warning("oauth_code_redirect_mismatch", client_id=client.client_id)
        raise InvalidGrant("The authorization code is not valid.")

    if not pkce.verify(
        verifier=code_verifier or "",
        challenge=row.code_challenge,
        method=row.code_challenge_method,
    ):
        log.warning("oauth_pkce_failed", client_id=client.client_id)
        raise InvalidGrant("The authorization code is not valid.")

    # RFC 8707: a resource on the token request must be the one authorized. Not
    # required — Alexa sends it, other clients do not — but never allowed to
    # differ, because that would be a token minted for an audience the fan never
    # saw.
    if resource is not None and resource != (row.resource or settings.resource_uri):
        raise InvalidTarget("The resource does not match the one this code was issued for.")

    row.used_at = utcnow()

    upstream = (
        await session.execute(
            select(UpstreamSession).where(UpstreamSession.id == row.upstream_session_id)
        )
    ).scalar_one_or_none()
    if upstream is None or not upstream.is_active:
        # The fan disconnected between consenting and the client redeeming.
        raise InvalidGrant("That Fanmoot session is no longer available.")

    grant = AccessGrant(
        client_id=client.client_id,
        upstream_session_id=upstream.id,
        fanmoot_user_id=row.fanmoot_user_id,
        fanmoot_handle=row.fanmoot_handle,
        scope=row.scope,
        resource=row.resource or settings.resource_uri,
    )
    session.add(grant)
    await session.flush()

    # One live grant per (client, fan). A re-link — including the scope upgrade
    # in `S4-12` — replaces the old one rather than accumulating rows, so the
    # connected-apps page lists each client once and revoking there revokes
    # everything that client holds.
    await _supersede_previous(session, grant)
    await session.commit()

    log.info(
        "oauth_grant_created",
        client_id=client.client_id,
        tier=client.tier,
        scope=grant.scope,
        handle=grant.fanmoot_handle,
    )
    return grant


async def _revoke_grants_from_code(session: AsyncSession, row: AuthorizationCode) -> None:
    """Revoke whatever a replayed code already produced.

    Narrow on purpose: the grants for that client and that fan created at or
    after the code was spent. Revoking every grant the fan holds would punish
    them for an attacker's replay.
    """
    spent_at = as_utc(row.used_at)
    if spent_at is None:  # pragma: no cover - `is_spent` guarantees otherwise
        return
    grants = (
        await session.execute(
            select(AccessGrant).where(
                AccessGrant.client_id == row.client_id,
                AccessGrant.fanmoot_user_id == row.fanmoot_user_id,
                AccessGrant.revoked_at.is_(None),
            )
        )
    ).scalars()
    for grant in grants:
        await revoke_grant(session, grant, reason=REVOKED_REUSE, commit=False)
    await session.commit()


async def _supersede_previous(session: AsyncSession, grant: AccessGrant) -> None:
    previous = (
        await session.execute(
            select(AccessGrant).where(
                AccessGrant.client_id == grant.client_id,
                AccessGrant.fanmoot_user_id == grant.fanmoot_user_id,
                AccessGrant.id != grant.id,
                AccessGrant.revoked_at.is_(None),
            )
        )
    ).scalars()
    for row in previous:
        await revoke_grant(session, row, reason=REVOKED_SUPERSEDED, commit=False)


# ── Access and refresh tokens ────────────────────────────────────────────────
async def issue_tokens(
    session: AsyncSession,
    grant: AccessGrant,
    *,
    family_id: uuid.UUID | None = None,
    settings: Settings | None = None,
) -> IssuedTokens:
    """Mint the pair for a grant, replacing whatever access token it held.

    ``family_id`` is passed on rotation so the successor joins its predecessor's
    family; a fresh authorization starts a family of its own. Both tokens are
    stored as hashes and returned in plaintext exactly once.
    """
    settings = settings or get_settings()
    now = utcnow()

    access = tokens.mint()
    grant.access_token_hash = tokens.fingerprint(access)
    grant.access_token_expires_at = now + timedelta(seconds=settings.oauth_access_token_ttl_s)

    refresh = tokens.mint()
    session.add(
        RefreshToken(
            grant_id=grant.id,
            token_hash=tokens.fingerprint(refresh),
            family_id=family_id or uuid.uuid4(),
            expires_at=now + timedelta(days=settings.oauth_refresh_token_ttl_days),
        )
    )
    await session.commit()

    return IssuedTokens(
        access_token=access,
        refresh_token=refresh,
        expires_in=settings.oauth_access_token_ttl_s,
        scope=grant.scope,
    )


async def rotate_refresh(
    session: AsyncSession,
    *,
    client: OAuthClient,
    refresh_token: str,
    resource: str | None,
    settings: Settings | None = None,
) -> tuple[AccessGrant, IssuedTokens]:
    """Spend a refresh token for its successor, or revoke the family trying.

    The reuse branch is the whole point of the ``family_id`` column. A token that
    has already been used is presented again: either the legitimate client
    retried after a dropped response, or this is a theft. Since the two are
    indistinguishable from here, the family is revoked and the fan links again —
    a stolen token is then worth at most one use.
    """
    settings = settings or get_settings()

    row = (
        await session.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == tokens.fingerprint(refresh_token or "")
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise InvalidGrant("The refresh token is not valid.")

    grant = (
        await session.execute(select(AccessGrant).where(AccessGrant.id == row.grant_id))
    ).scalar_one_or_none()
    if grant is None or grant.client_id != client.client_id:
        raise InvalidGrant("The refresh token is not valid.")

    if row.used_at is not None:
        log.warning(
            "oauth_refresh_reuse_detected",
            client_id=client.client_id,
            family_id=str(row.family_id),
        )
        await revoke_family(session, row.family_id, reason=REVOKED_REUSE)
        raise InvalidGrant("The refresh token is not valid.")

    if row.revoked_at is not None or expired(row.expires_at) or not grant.is_active:
        raise InvalidGrant("The refresh token is not valid.")

    # RFC 8707 §2.2 lets a refresh narrow the audience; Alexa sends nothing here
    # and the grant's own value is used. Anything *different* is refused rather
    # than quietly honoured — a token for another audience is not a refresh.
    if resource is not None and resource != grant.resource:
        raise InvalidTarget("The resource does not match the one this grant was issued for.")

    row.used_at = utcnow()
    issued = await issue_tokens(session, grant, family_id=row.family_id, settings=settings)
    return grant, issued


async def resolve_access_token(session: AsyncSession, token: str) -> AccessGrant | None:
    """The live grant behind an access token, or None.

    On the hot path: every authenticated tool call runs this. One indexed lookup
    on the token's hash, and the expiry and revocation checks in Python — where
    ``as_utc`` can normalise what the driver returned.
    """
    if not token:
        return None
    grant = (
        await session.execute(
            select(AccessGrant).where(AccessGrant.access_token_hash == tokens.fingerprint(token))
        )
    ).scalar_one_or_none()
    if grant is None or not grant.is_active or expired(grant.access_token_expires_at):
        return None
    return grant


# ── Revocation ───────────────────────────────────────────────────────────────
async def revoke_family(
    session: AsyncSession, family_id: uuid.UUID, *, reason: str, commit: bool = True
) -> None:
    """Revoke every refresh token descended from one authorization."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    if commit:
        await session.commit()


async def revoke_grant(
    session: AsyncSession, grant: AccessGrant, *, reason: str, commit: bool = True
) -> None:
    """Take a link away: the grant, its access token, and every refresh token.

    The access token hash is cleared rather than left in place, so the unique
    index cannot be held by a credential that no longer works — and so a
    resolution that somehow raced this finds nothing rather than a revoked row.
    """
    now = utcnow()
    grant.revoked_at = now
    grant.revoked_reason = reason
    grant.access_token_hash = None
    grant.access_token_expires_at = None
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.grant_id == grant.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now, revoked_reason=reason)
    )
    if commit:
        await session.commit()
    log.info("oauth_grant_revoked", client_id=grant.client_id, reason=reason)


async def revoke_presented_token(session: AsyncSession, *, client: OAuthClient, token: str) -> bool:
    """RFC 7009 — revoke whichever kind of token this is. Returns whether one matched.

    Both kinds are accepted without a hint, because RFC 7009 requires a server to
    cope with a wrong or missing ``token_type_hint`` anyway, and looking in both
    places is cheaper than getting the hint wrong. A token belonging to another
    client is ignored: the endpoint answers 200 regardless, so a caller cannot
    use it to discover whether somebody else's token is live.
    """
    digest = tokens.fingerprint(token or "")

    grant = (
        await session.execute(select(AccessGrant).where(AccessGrant.access_token_hash == digest))
    ).scalar_one_or_none()
    if grant is not None and grant.client_id == client.client_id:
        await revoke_grant(session, grant, reason=REVOKED_BY_FAN)
        return True

    row = (
        await session.execute(select(RefreshToken).where(RefreshToken.token_hash == digest))
    ).scalar_one_or_none()
    if row is None:
        return False
    owner = (
        await session.execute(select(AccessGrant).where(AccessGrant.id == row.grant_id))
    ).scalar_one_or_none()
    if owner is None or owner.client_id != client.client_id:
        return False
    await revoke_grant(session, owner, reason=REVOKED_BY_FAN)
    return True


# ── Queries the consent screen and the connected-apps page run ───────────────
async def active_grant(
    session: AsyncSession, *, client_id: str, fanmoot_user_id: uuid.UUID
) -> AccessGrant | None:
    """The live grant this client already holds for this fan, if any.

    Read by the consent screen so a re-link says "already connected" and lists
    what is being *added*, rather than presenting a returning fan with a form
    that looks identical to the first one.
    """
    return (
        await session.execute(
            select(AccessGrant).where(
                AccessGrant.client_id == client_id,
                AccessGrant.fanmoot_user_id == fanmoot_user_id,
                AccessGrant.revoked_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def grants_for_fan(
    session: AsyncSession, fanmoot_user_id: uuid.UUID
) -> list[tuple[AccessGrant, OAuthClient | None]]:
    """Every live grant a fan holds, with the client that holds it (`S4-13`).

    Alexa and a self-registered client alike — the page that lists these is the
    fan's, and a client that registered itself two minutes ago should be exactly
    as disconnectable as the one an operator seeded.
    """
    rows = (
        await session.execute(
            select(AccessGrant, OAuthClient)
            .outerjoin(OAuthClient, OAuthClient.client_id == AccessGrant.client_id)
            .where(
                AccessGrant.fanmoot_user_id == fanmoot_user_id,
                AccessGrant.revoked_at.is_(None),
            )
            .order_by(AccessGrant.created_at.desc())
        )
    ).all()
    return [(grant, client) for grant, client in rows]


async def touch(session: AsyncSession, grant: AccessGrant, *, at: datetime | None = None) -> None:
    """Record that this grant was just used, without a commit of its own.

    Called on the tool-call path, which is why it does not commit: the caller is
    already inside a session that will, and a second round trip per tool call is
    a tenth of the latency budget spent on a timestamp.
    """
    grant.last_used_at = at or utcnow()
