"""The exchange: an opaque token in, a fresh Fanmoot access token out (`S4-8`).

This module is the reason HAD-3 exists. What a client holds — Alexa, Claude,
anything — is a handle to a row in ``access_grants``. It is **not** a Fanmoot
access token and cannot be used against ``fanmoot.com`` directly. When an
authenticated tool needs to act as the fan, ``fanmoot_token_for`` loads the
grant, decrypts the fan's Fanmoot *refresh* token, spends it for a fresh
15-minute access token, and hands that to the tool.

Three consequences worth being explicit about:

**A leaked client token buys nothing outside our scopes.** It is revocable here,
it expires in an hour, and it is meaningless to Fanmoot.

**The upstream refresh token rotates on every exchange**, because Fanmoot rotates
and revokes the family on a replay. So the exchange re-encrypts and stores what
comes back before returning, and a failure to store is a failure of the whole
exchange rather than a token quietly used once and lost.

**The result is cached in process.** Without it, a three-tool voice turn would
spend three round trips to ``/v1/auth/refresh`` before doing any football, and
HAD-6's budget is gone. The cache is keyed by grant and expires a little inside
the token's real life, so a cached token is never handed out in its last few
seconds and then rejected mid-turn.

**And the exchange is serialised per grant.** Two tool calls landing together on
the same grant would otherwise both see a cache miss, both spend the same
upstream refresh token, and the second presentation would look to Fanmoot
exactly like a replay — revoking the family and signing the fan out of an
integration they were actively using. A lock per grant costs nothing and closes
that race; the process runs one worker (see the Dockerfile), so a lock is enough.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.fanmoot import endpoints
from app.fanmoot.client import FanmootError, FanmootUnauthorized
from app.logging_config import get_logger
from app.models.base import utcnow
from app.models.grant import (
    REVOKED_KEY_RETIRED,
    REVOKED_UNUSED,
    REVOKED_UPSTREAM,
    AccessGrant,
    UpstreamSession,
)
from app.oauth import tokens
from app.settings import Settings, get_settings

log = get_logger("oauth.upstream")


class LinkBroken(Exception):
    """The fan's Fanmoot session is gone and cannot be recovered here.

    Raised when the stored refresh token is refused upstream, or will not
    decrypt because the key has rotated. Carries a sentence written for a fan to
    hear, because that is where it ends up: a tool call that cannot act as the
    fan says so out loud rather than failing silently.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.default)
        self.message = message or self.default

    default = (
        "Your Fanmoot account isn't connected any more. Reconnect it and I can post for you again."
    )


@dataclass(frozen=True, slots=True)
class CachedToken:
    token: str
    #: ``time.monotonic`` rather than wall clock: a container whose clock steps
    #: backwards should not extend a credential's life.
    expires_at: float


#: Keyed by grant id. Small by construction — one entry per fan currently in a
#: conversation — and dropped entirely on shutdown.
_cache: dict[uuid.UUID, CachedToken] = {}

#: One lock per grant, created on demand. See the module docstring: this is what
#: stops two concurrent tool calls spending the same upstream refresh token.
_locks: dict[uuid.UUID, asyncio.Lock] = {}


def _lock_for(grant_id: uuid.UUID) -> asyncio.Lock:
    lock = _locks.get(grant_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[grant_id] = lock
    return lock


# ── Signing a fan in, on our own consent page (S4-10, S4-11) ─────────────────
async def sign_in(session: AsyncSession, *, email: str, password: str) -> UpstreamSession:
    """Authenticate against Fanmoot and store the session that comes back.

    The password is used once, in the request this makes, and is never written
    anywhere in this service. What is stored is the refresh token, encrypted —
    and only if there is a key to encrypt it with: ``tokens.encrypt`` raises
    ``SecretsUnavailable`` rather than falling back to plaintext (HAD-3).
    """
    result = await endpoints.login(email=email, password=password)
    return await _store(session, result.user.id, result.user.handle, result.tokens.refresh_token)


async def sign_up(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    name: str,
    date_of_birth: str,
    country_code: str | None = None,
    handle: str | None = None,
    club_slug: str | None = None,
) -> tuple[UpstreamSession, bool]:
    """Create a Fanmoot account inside the flow (`S4-11`).

    Returns the session and whether the address still needs verifying, which the
    page after this says out loud: a brand new fan can link and post, and
    Fanmoot will hold their first take until the email is confirmed. Telling them
    that here is the difference between a working flow and a fan wondering where
    their take went.
    """
    result = await endpoints.register(
        email=email,
        password=password,
        name=name,
        date_of_birth=date_of_birth,
        country_code=country_code,
        handle=handle,
        club_slug=club_slug,
    )
    upstream = await _store(
        session, result.user.id, result.user.handle, result.tokens.refresh_token
    )
    return upstream, not result.user.email_verified


async def _store(
    session: AsyncSession,
    fanmoot_user_id: uuid.UUID,
    handle: str,
    refresh_token: str | None,
) -> UpstreamSession:
    if not refresh_token:
        # Only reachable if Fanmoot stops honouring `in_body=true`, which would
        # leave this server holding a 15-minute credential and no way to renew
        # it. Better to refuse the link than to create one that dies at lunch.
        raise LinkBroken("Fanmoot did not return a session this server can hold on to.")

    upstream = UpstreamSession(
        fanmoot_user_id=fanmoot_user_id,
        fanmoot_handle=handle,
        refresh_token_encrypted=tokens.encrypt(refresh_token),
    )
    session.add(upstream)
    await session.commit()
    log.info("upstream_session_created", handle=handle)
    return upstream


# ── The exchange itself (S4-8) ───────────────────────────────────────────────
async def fanmoot_token_for(
    session: AsyncSession, grant: AccessGrant, *, settings: Settings | None = None
) -> str:
    """A live Fanmoot access token for this grant. The hot path of `S5`.

    Cached, serialised per grant, and self-healing in one direction only: if
    Fanmoot refuses the stored refresh token, the upstream session and every
    grant hanging off it are revoked here, because a link that cannot be renewed
    is not a link — and leaving it in place would mean failing every tool call
    for that fan for the next 90 days.
    """
    settings = settings or get_settings()

    cached = _cache.get(grant.id)
    if cached is not None and cached.expires_at > time.monotonic():
        return cached.token

    async with _lock_for(grant.id):
        # Re-read under the lock: the call that was holding it may have just
        # filled the cache, in which case there is nothing to do.
        cached = _cache.get(grant.id)
        if cached is not None and cached.expires_at > time.monotonic():
            return cached.token

        upstream = (
            await session.execute(
                select(UpstreamSession).where(UpstreamSession.id == grant.upstream_session_id)
            )
        ).scalar_one_or_none()
        if upstream is None or not upstream.is_active:
            raise LinkBroken()

        try:
            stored = tokens.decrypt(upstream.refresh_token_encrypted)
        except tokens.SecretUnreadable:
            # The key that wrapped it has been retired (`S7-4`) — dropped
            # before `make rotate-keys` re-wrapped this row, or on purpose
            # after a leak. This fan reconnects and nothing leaked.
            log.warning("upstream_session_unreadable", handle=upstream.fanmoot_handle)
            raise LinkBroken() from None

        try:
            issued = await endpoints.refresh_session(refresh_token=stored)
        except FanmootUnauthorized:
            log.warning("upstream_session_refused", handle=upstream.fanmoot_handle)
            # `notify_upstream=False`: Fanmoot has just said this session is
            # gone, so calling its logout endpoint would be a second round trip
            # inside a voice turn to tell it something it told us.
            await end_session(session, upstream, reason=REVOKED_UPSTREAM, notify_upstream=False)
            raise LinkBroken() from None
        except FanmootError:
            # Fanmoot is unreachable or unhappy. Not the fan's link failing —
            # the caller's error mapping already has a sentence for this, and
            # revoking a working grant over a 502 would be the wrong repair.
            raise

        if issued.refresh_token:
            # Fanmoot rotates. Storing the successor before returning is what
            # keeps the next turn working; committing here rather than leaving
            # it to the caller is deliberate, because a token spent and not
            # recorded is a link that breaks on the next call.
            upstream.refresh_token_encrypted = tokens.encrypt(issued.refresh_token)
            upstream.rotated_at = utcnow()
            await session.commit()

        # A little inside the real expiry, so a token is never handed out in its
        # last seconds and rejected mid-turn.
        ttl = min(settings.oauth_upstream_access_ttl_s, max(issued.expires_in - 60, 30))
        _cache[grant.id] = CachedToken(token=issued.access_token, expires_at=time.monotonic() + ttl)
        return issued.access_token


async def end_session(
    session: AsyncSession,
    upstream: UpstreamSession,
    *,
    reason: str,
    notify_upstream: bool = True,
) -> None:
    """Revoke an upstream session and every grant that depends on it.

    Best-effort upstream: Fanmoot's ``/v1/auth/logout`` is called so the session
    stops existing over there too, but a failure there does not stop the
    revocation here. The local state is what this server is responsible for.

    ``notify_upstream=False`` is for the case where Fanmoot has *already* refused
    the session — there is nothing left over there to end, and a voice turn does
    not spend a round trip telling it something it just told us.
    """
    from app.oauth import grants as grants_module

    try:
        stored = tokens.decrypt(upstream.refresh_token_encrypted)
    except tokens.SecretUnreadable:
        stored = ""
    if stored and notify_upstream:
        try:
            await endpoints.logout(refresh_token=stored)
        except FanmootError as exc:
            log.info("upstream_logout_failed", error=str(exc))

    upstream.revoked_at = utcnow()
    upstream.revoked_reason = reason
    # The credential goes with the link (`S7-4`). It has just been logged out
    # upstream, or Fanmoot refused it, so it is dead either way. A dead
    # credential kept as ciphertext is still something a leaked key would open.
    upstream.refresh_token_encrypted = ""

    dependent = (
        await session.execute(
            select(AccessGrant).where(
                AccessGrant.upstream_session_id == upstream.id,
                AccessGrant.revoked_at.is_(None),
            )
        )
    ).scalars()
    for grant in dependent:
        forget(grant.id)
        await grants_module.revoke_grant(session, grant, reason=reason, commit=False)
    await session.commit()


@dataclass
class Rewrapped:
    """What ``rewrap_sessions`` did, for ``make rotate-keys`` to print."""

    rewrapped: int = 0
    #: Rewritten by a live exchange between our read and our write. Already
    #: under the new key, because the exchange encrypts with the first one.
    moved_on: int = 0
    unreadable: int = 0
    revoked: int = 0


async def rewrap_sessions(session: AsyncSession, *, revoke_unreadable: bool = False) -> Rewrapped:
    """Re-wrap every active upstream session under the first key (`S7-4`).

    Runs beside a live server, so each write is **conditional on the ciphertext
    it read**. The exchange above rewrites this column every time Fanmoot
    rotates a refresh token. An unconditional write here could put back the
    predecessor, and presenting a spent refresh token to Fanmoot looks exactly
    like a replay: it revokes the family and signs the fan out. A row that moved
    on in between was written under the first key anyway, so skipping it is
    correct.

    A row no configured key opens is counted and left alone unless
    ``revoke_unreadable`` is set. Revoking it then ends the link cleanly, so the
    fan's client is asked to reconnect, rather than failing on the next call.
    """
    report = Rewrapped()
    rows = (
        await session.execute(
            select(UpstreamSession.id, UpstreamSession.refresh_token_encrypted).where(
                UpstreamSession.revoked_at.is_(None)
            )
        )
    ).all()

    for row_id, ciphertext in rows:
        try:
            fresh = tokens.rotate(ciphertext)
        except tokens.SecretUnreadable:
            report.unreadable += 1
            if revoke_unreadable:
                upstream = await session.get(UpstreamSession, row_id)
                if upstream is not None:
                    await end_session(
                        session, upstream, reason=REVOKED_KEY_RETIRED, notify_upstream=False
                    )
                    report.revoked += 1
            continue

        written = await session.execute(
            update(UpstreamSession)
            .where(
                UpstreamSession.id == row_id,
                UpstreamSession.refresh_token_encrypted == ciphertext,
            )
            .values(refresh_token_encrypted=fresh)
        )
        if getattr(written, "rowcount", 0):
            report.rewrapped += 1
        else:
            report.moved_on += 1
        await session.commit()

    log.info(
        "upstream_sessions_rewrapped",
        rewrapped=report.rewrapped,
        moved_on=report.moved_on,
        unreadable=report.unreadable,
        revoked=report.revoked,
    )
    return report


async def end_unused_sessions(session: AsyncSession, *, grace_s: int) -> int:
    """End every Fanmoot session no app is linked to any more (`S7-12`).

    A session outlives its last grant in two ordinary ways. The fan
    disconnected every app that used it, or signed in and then pressed "Not
    now". Either way it is a working Fanmoot credential with no purpose, so it
    is logged out upstream and its ciphertext cleared.

    ``grace_s`` spares the young ones. A fan who has just signed in holds a
    browser session that can still link an app to this row, and ending it under
    them would fail their next consent screen for no reason.
    """
    cutoff = utcnow() - timedelta(seconds=grace_s)
    linked = select(AccessGrant.id).where(
        AccessGrant.upstream_session_id == UpstreamSession.id,
        AccessGrant.revoked_at.is_(None),
    )
    unused = (
        (
            await session.execute(
                select(UpstreamSession).where(
                    UpstreamSession.revoked_at.is_(None),
                    UpstreamSession.created_at < cutoff,
                    ~linked.exists(),
                )
            )
        )
        .scalars()
        .all()
    )
    for upstream in unused:
        await end_session(session, upstream, reason=REVOKED_UNUSED)
    if unused:
        log.info("upstream_sessions_unused_ended", count=len(unused))
    return len(unused)


def forget(grant_id: uuid.UUID) -> None:
    """Drop a grant's cached Fanmoot token — called the moment it is revoked.

    Without this a revoked grant would keep working for up to fourteen minutes,
    which is exactly the window "disconnect this app" exists to close.
    """
    _cache.pop(grant_id, None)
    _locks.pop(grant_id, None)


def reset_cache() -> None:
    """Test hook, and the lifespan's: forget every cached upstream token."""
    _cache.clear()
    _locks.clear()
