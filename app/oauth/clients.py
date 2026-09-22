"""The client store — both tiers of HAD-5, one table, one set of rules.

**Tier 1, static.** ``FMCP_OAUTH_CLIENTS`` is a JSON array upserted by
``client_id`` at every boot (`S4-5`). Upsert rather than insert, because a
redeploy that orphaned Alexa's credentials would break account linking for every
fan who had already linked, and Alexa cannot re-register — it has no DCR.

**Tier 2, self-registering.** ``register`` creates a row from RFC 7591 metadata
(`S4-16`) and ``resolve_metadata_document`` creates one from a URL that serves
its own metadata (SEP-991, `S4-17`). Both land in the same table with the same
scopes; only ``tier`` differs, and it differs so an operator can tell them apart
in the audit log (`S4-18`).

Three rules hold across both tiers:

**Redirect URIs are validated on the way in and matched exactly on the way out**
(`S4-5`). ``https`` only, with ``http://localhost`` and ``http://127.0.0.1``
carved out — not as a convenience but because Claude Desktop, Cursor and the MCP
Inspector genuinely have nowhere else to put a loopback callback, and a server
that refused them would be a server only Alexa could use.

**A client never chooses its own id.** RFC 7591 says the server generates it, and
``mint_client_id`` prefixes ours so a log line shows at a glance whether a
request came from Amazon's client or from something that registered itself.

**Open registration is guarded, not trusted.** Per-IP rate limiting lives in the
router; the TTL here is the other half — a registration that never completes a
grant is reaped (`S4-16`), so anyone can create a row and nobody can leave litter.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
from sqlalchemy import CursorResult, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models.base import utcnow
from app.models.client import (
    AUTH_METHOD_NONE,
    AUTH_METHOD_SECRET_POST,
    AUTH_METHODS,
    TIER_DYNAMIC,
    TIER_METADATA_DOCUMENT,
    TIER_STATIC,
    OAuthClient,
)
from app.oauth import scopes as scope_module
from app.oauth import tokens
from app.settings import Settings, get_settings

log = get_logger("oauth.clients")

#: What every client here may do. No ``implicit``, no client credentials: this
#: server issues tokens to a *fan*, and a grant with no fan behind it has nobody
#: to post as.
GRANT_TYPES = ["authorization_code", "refresh_token"]
RESPONSE_TYPES = ["code"]

#: Loopback, in the spellings a desktop client actually registers. ``::1`` is
#: included because a client on a v6-first host will use it.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Guards on fetching a client ID metadata document (SEP-991). The request is to
#: a URL somebody else chose, which makes it server-side request forgery unless
#: it is fenced: https only, no loopback or link-local host, one redirect never
#: followed, a two-second budget and a body that cannot be large.
_METADATA_TIMEOUT_S = 2.0
_METADATA_MAX_BYTES = 64 * 1024


class RegistrationRefused(Exception):
    """Client metadata this server will not register.

    ``error`` is one of RFC 7591's registration error codes, so the handler can
    return it verbatim: ``invalid_redirect_uri`` and ``invalid_client_metadata``
    are the two that ever come up.
    """

    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


# ── Lookup ───────────────────────────────────────────────────────────────────
async def find(session: AsyncSession, client_id: str) -> OAuthClient | None:
    """One client by the id it sent, or None."""
    if not client_id:
        return None
    result = await session.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    return result.scalar_one_or_none()


async def find_or_resolve(session: AsyncSession, client_id: str) -> OAuthClient | None:
    """``find``, and if that misses, try the id *as a URL* (SEP-991, `S4-17`).

    A client identified by a metadata document has an ``https`` URL for a
    ``client_id``, and the first time it arrives there is no row for it. So an
    unknown id that is a URL is a registration waiting to happen rather than a
    failure — and an unknown id that is *not* a URL is simply unknown.
    """
    client = await find(session, client_id)
    if client is not None:
        return client
    if not _is_metadata_document_url(client_id):
        return None
    return await resolve_metadata_document(session, client_id)


async def authenticate(
    session: AsyncSession,
    *,
    client_id: str | None,
    client_secret: str | None,
    authorization_header: str | None = None,
) -> OAuthClient:
    """The client behind a token request, authenticated as its method requires.

    Accepts the secret from the body (``client_secret_post``) or from an HTTP
    Basic header (``client_secret_basic``) because clients differ on which, and
    RFC 6749 permits both. A public client sends neither and is identified by its
    id alone — which is safe only because PKCE is mandatory here, and is the
    whole reason OAuth 2.1 requires it of everybody.

    Every failure raises the same ``InvalidClient``: distinguishing "no such
    client" from "wrong secret" would turn this endpoint into an oracle for which
    client ids exist.
    """
    from app.oauth.errors import InvalidClient

    basic_id, basic_secret = _basic_credentials(authorization_header)
    if basic_id is not None:
        if client_id and client_id != basic_id:
            # Two different ids in one request. RFC 6749 §2.3 forbids presenting
            # credentials twice; a mismatch is a confused client at best.
            raise InvalidClient("client_id in the body does not match the Basic credentials.")
        client_id = basic_id
        client_secret = basic_secret

    client = await find(session, client_id or "")
    if client is None or not client.is_active:
        raise InvalidClient("Unknown or inactive client.")

    if client.is_public:
        # Nothing to verify. A public client that sends a secret anyway is not
        # refused for it — Inspector has been known to — but the secret means
        # nothing and PKCE is doing the work.
        return client

    if not tokens.matches(client_secret or "", client.client_secret_hash):
        log.warning("oauth_client_secret_rejected", client_id=client.client_id, tier=client.tier)
        raise InvalidClient("Client authentication failed.")
    return client


def _basic_credentials(header: str | None) -> tuple[str | None, str | None]:
    """``Authorization: Basic …`` split into id and secret, or ``(None, None)``.

    Both halves are percent-decoded per RFC 6749 §2.3.1, which is not pedantry:
    Amazon's client secrets are base64-ish and a ``+`` in one would otherwise
    arrive as a space.
    """
    import base64
    import binascii
    from urllib.parse import unquote

    if not header or not header.lower().startswith("basic "):
        return None, None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None, None
    client_id, _, secret = decoded.partition(":")
    return unquote(client_id), unquote(secret)


# ── Static seeding (tier 1) ──────────────────────────────────────────────────
async def seed_static(session: AsyncSession, settings: Settings | None = None) -> int:
    """Upsert every client in ``FMCP_OAUTH_CLIENTS``. Returns how many.

    Called from the lifespan. A malformed entry is **skipped and logged loudly**
    rather than raising: one bad client in the array must not stop the server
    booting and take the other one — and the read surface — down with it. The
    log line names the id, which is what an operator needs to fix it.

    A secret is only written when the entry supplies one, so removing
    ``client_secret`` from the environment after the first boot does not silently
    turn Alexa's confidential client into a public one.
    """
    settings = settings or get_settings()
    seeded = 0

    for spec in settings.oauth_client_specs:
        client_id = str(spec.get("client_id") or "").strip()
        if not client_id:
            log.error("oauth_static_client_skipped", reason="no client_id")
            continue

        raw_uris = spec.get("redirect_uris") or []
        if not isinstance(raw_uris, list) or not raw_uris:
            log.error("oauth_static_client_skipped", client_id=client_id, reason="no redirect_uris")
            continue
        try:
            redirect_uris = validate_redirect_uris([str(uri) for uri in raw_uris])
        except RegistrationRefused as exc:
            log.error(
                "oauth_static_client_skipped",
                client_id=client_id,
                reason=exc.error,
                detail=exc.description,
            )
            continue

        secret = spec.get("client_secret")
        method = str(
            spec.get("token_endpoint_auth_method")
            or (AUTH_METHOD_SECRET_POST if secret else AUTH_METHOD_NONE)
        )
        if method not in AUTH_METHODS:
            log.error("oauth_static_client_skipped", client_id=client_id, reason="auth_method")
            continue

        # `scope` and `scopes` both accepted: the environment is written by a
        # person, and RFC 7591 says `scope` while everything about this service
        # says `scopes`. Unknown scopes are dropped rather than refused.
        asked = spec.get("scope") or spec.get("scopes") or scope_module.SUPPORTED
        granted = scope_module.parse(asked if isinstance(asked, str) else " ".join(asked))

        client = await find(session, client_id)
        if client is None:
            client = OAuthClient(client_id=client_id)
            session.add(client)

        client.client_name = str(spec.get("client_name") or "Alexa+")
        client.redirect_uris = redirect_uris
        client.grant_types = list(GRANT_TYPES)
        client.response_types = list(RESPONSE_TYPES)
        client.scope = " ".join(granted)
        client.token_endpoint_auth_method = method
        client.tier = TIER_STATIC
        client.client_uri = _optional(spec.get("client_uri"))
        client.logo_uri = _optional(spec.get("logo_uri"))
        client.policy_uri = _optional(spec.get("policy_uri"))
        client.tos_uri = _optional(spec.get("tos_uri"))
        # A static client is pinned open: it is the operator's, and reaping the
        # one client that cannot re-register would be the worst possible bug.
        client.expires_at = None
        client.disabled_at = None
        if isinstance(secret, str) and secret.strip():
            client.client_secret_hash = tokens.fingerprint(secret.strip())

        seeded += 1

    if seeded:
        await session.commit()
    log.info("oauth_static_clients_seeded", count=seeded)
    return seeded


def _optional(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


# ── Dynamic registration (tier 2) ────────────────────────────────────────────
def validate_redirect_uris(uris: list[str]) -> list[str]:
    """The registered set, or a refusal naming what was wrong with it.

    The policy, in full:

    * ``https`` anywhere, or ``http`` **only** for ``localhost``, ``127.0.0.1``
      and ``::1`` — the loopback callbacks Claude Desktop, Cursor and Inspector
      need. A plain-``http`` URI on a public host is refused: a code delivered
      over cleartext is a code on the wire.
    * No fragment. RFC 6749 forbids one in a redirect URI, and a client that
      registers ``#/callback`` will lose the query the code arrives in.
    * At least one, at most sixteen. Alexa has several, varying by where the
      device was registered; nobody has sixteen, and an unbounded list is a
      place to store data.
    """
    if not uris:
        raise RegistrationRefused("invalid_redirect_uri", "At least one redirect_uri is required.")
    if len(uris) > 16:
        raise RegistrationRefused("invalid_redirect_uri", "Too many redirect URIs (16 at most).")

    checked: list[str] = []
    for uri in uris:
        candidate = uri.strip()
        parts = urlsplit(candidate)
        if not parts.scheme or not parts.netloc:
            raise RegistrationRefused(
                "invalid_redirect_uri", f"{candidate!r} is not an absolute URI."
            )
        if parts.fragment:
            raise RegistrationRefused(
                "invalid_redirect_uri", f"{candidate!r} has a fragment, which RFC 6749 forbids."
            )
        host = (parts.hostname or "").lower()
        loopback = parts.scheme == "http" and host in _LOOPBACK_HOSTS
        if parts.scheme != "https" and not loopback:
            raise RegistrationRefused(
                "invalid_redirect_uri",
                f"{candidate!r} must use https, or http on localhost for a loopback callback.",
            )
        if candidate not in checked:
            checked.append(candidate)
    return checked


async def register(
    session: AsyncSession,
    metadata: dict[str, Any],
    *,
    settings: Settings | None = None,
    tier: str = TIER_DYNAMIC,
    client_id: str | None = None,
) -> tuple[OAuthClient, str | None, str]:
    """Create a client from RFC 7591 metadata.

    Returns ``(client, client_secret, registration_access_token)`` — the secret
    is ``None`` for a public client, and both plaintext values are the only time
    they exist: what is stored is their SHA-256.

    ``token_endpoint_auth_method`` decides whether a secret is issued at all, and
    it defaults to ``none``. That is the opposite of RFC 7591's default and it is
    deliberate: every MCP client that self-registers here is a public client with
    a loopback callback, PKCE is mandatory regardless, and a secret written into
    a desktop client's config file is a secret in a file rather than a secret.
    """
    settings = settings or get_settings()

    raw_uris = metadata.get("redirect_uris")
    if not isinstance(raw_uris, list):
        raise RegistrationRefused(
            "invalid_redirect_uri", "redirect_uris is required and must be an array."
        )
    redirect_uris = validate_redirect_uris([str(uri) for uri in raw_uris])

    method = str(metadata.get("token_endpoint_auth_method") or AUTH_METHOD_NONE)
    if method not in AUTH_METHODS:
        raise RegistrationRefused(
            "invalid_client_metadata",
            f"token_endpoint_auth_method {method!r} is not supported. "
            "Use none, client_secret_post or client_secret_basic.",
        )

    for field, allowed in (("grant_types", GRANT_TYPES), ("response_types", RESPONSE_TYPES)):
        asked = metadata.get(field)
        if asked is None:
            continue
        if not isinstance(asked, list):
            raise RegistrationRefused("invalid_client_metadata", f"{field} must be an array.")
        unsupported = sorted({str(item) for item in asked} - set(allowed))
        if unsupported:
            raise RegistrationRefused(
                "invalid_client_metadata",
                f"{field} {unsupported} not supported; this server does {allowed}.",
            )

    # A client that asks for nothing gets everything this server issues, and the
    # *fan* narrows it on the consent screen. That is the right place for the
    # decision: a client cannot know which scopes a person will agree to, and a
    # registration that guessed low would send every fan through a second
    # authorization the first time it tried to post (`S4-12`).
    granted = scope_module.parse(str(metadata.get("scope") or scope_module.SUPPORTED))
    if not granted:
        raise RegistrationRefused(
            "invalid_client_metadata",
            f"None of the requested scopes are issued here. Supported: {scope_module.SUPPORTED}.",
        )

    secret: str | None = None
    client = OAuthClient(
        client_id=client_id or tokens.mint_client_id(),
        client_name=str(metadata.get("client_name") or "Unnamed MCP client")[:200],
        redirect_uris=redirect_uris,
        grant_types=list(GRANT_TYPES),
        response_types=list(RESPONSE_TYPES),
        scope=" ".join(granted),
        token_endpoint_auth_method=method,
        tier=tier,
        client_uri=_optional(metadata.get("client_uri")),
        logo_uri=_optional(metadata.get("logo_uri")),
        policy_uri=_optional(metadata.get("policy_uri")),
        tos_uri=_optional(metadata.get("tos_uri")),
        software_id=_optional(metadata.get("software_id")),
        software_version=_optional(metadata.get("software_version")),
        # The TTL from `S4-16`: reaped if it never completes a grant. Cleared by
        # `note_grant` the moment one does, so a client in real use never
        # expires.
        expires_at=utcnow() + timedelta(days=settings.oauth_dcr_client_ttl_days),
    )
    if method != AUTH_METHOD_NONE:
        secret = tokens.mint()
        client.client_secret_hash = tokens.fingerprint(secret)

    registration_token = tokens.mint()
    client.registration_access_token_hash = tokens.fingerprint(registration_token)

    session.add(client)
    await session.commit()
    # `created_at` is a server default, so it is unset on the object until it is
    # read back — and an async session cannot lazily refresh it later without
    # raising. The registration response carries `client_id_issued_at`, so it is
    # fetched here rather than discovered as a MissingGreenlet in the handler.
    await session.refresh(client)
    log.info(
        "oauth_client_registered",
        client_id=client.client_id,
        tier=tier,
        name=client.client_name,
        redirect_uris=len(redirect_uris),
    )
    return client, secret, registration_token


# ── Client ID metadata documents (SEP-991, S4-17) ────────────────────────────
def _is_metadata_document_url(client_id: str) -> bool:
    """Whether this ``client_id`` is a URL we would be willing to fetch.

    ``https`` and a hostname with a dot in it. The dot is what keeps this from
    being a fetch of ``https://localhost/...`` or of an internal name that
    resolves only inside the deployment — the first half of the SSRF fence.
    """
    parts = urlsplit(client_id)
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and bool(parts.path)
        and "." in host
        and host not in _LOOPBACK_HOSTS
        and not parts.fragment
    )


async def resolve_metadata_document(
    session: AsyncSession, client_id: str, *, settings: Settings | None = None
) -> OAuthClient | None:
    """Fetch a client's own metadata document and register it (SEP-991).

    The mechanism ``2025-11-25`` *recommends*: instead of registering, a client
    publishes its metadata at a URL and uses that URL as its ``client_id``. The
    document is RFC 7591 client metadata and must claim the same ``client_id`` it
    was found at — otherwise anybody could point at somebody else's document and
    inherit their redirect URIs.

    Fenced, because this is an outbound request to a URL a caller chose:
    ``https`` only, no loopback or dotless host, redirects **not** followed (a
    redirect is how an ``https`` URL becomes an internal one), a two-second
    budget, and a bounded body. A failure returns None and the caller reports an
    unknown client — never a stack trace, and never a retry loop.
    """
    settings = settings or get_settings()
    if not settings.oauth_dcr_enabled or not _is_metadata_document_url(client_id):
        return None

    try:
        async with httpx.AsyncClient(timeout=_METADATA_TIMEOUT_S, follow_redirects=False) as http:
            response = await http.get(client_id, headers={"Accept": "application/json"})
        if response.status_code != httpx.codes.OK:
            log.warning(
                "oauth_client_metadata_unreadable", url=client_id, status=response.status_code
            )
            return None
        if len(response.content) > _METADATA_MAX_BYTES:
            log.warning("oauth_client_metadata_too_large", url=client_id)
            return None
        document = response.json()
    except Exception as exc:
        log.warning("oauth_client_metadata_failed", url=client_id, error=str(exc))
        return None

    if not isinstance(document, dict) or str(document.get("client_id") or "") != client_id:
        log.warning("oauth_client_metadata_mismatch", url=client_id)
        return None

    try:
        client, _, _ = await register(
            session,
            {**document, "token_endpoint_auth_method": AUTH_METHOD_NONE},
            settings=settings,
            tier=TIER_METADATA_DOCUMENT,
            client_id=client_id,
        )
    except RegistrationRefused as exc:
        log.warning("oauth_client_metadata_refused", url=client_id, reason=exc.error)
        return None

    client.metadata_url = client_id
    client.metadata_refreshed_at = utcnow()
    await session.commit()
    return client


# ── Housekeeping ─────────────────────────────────────────────────────────────
async def note_grant(session: AsyncSession, client: OAuthClient) -> None:
    """Record that this client completed a grant, and stop it expiring.

    The second half is the point of the TTL in `S4-16`: registration is open, so
    the table fills with clients that tried once and vanished, and a client in
    real use must never be swept up with them.
    """
    client.last_grant_at = utcnow()
    client.expires_at = None


async def reap_stale(session: AsyncSession) -> int:
    """Delete self-registered clients that expired without completing a grant.

    Static clients are untouched — they have no ``expires_at`` — and so is
    anything that ever completed a grant, because ``note_grant`` cleared it.
    Deleting rather than disabling: a registration nobody used is not history
    worth keeping, and its consent events (if it somehow has any) are not
    foreign-keyed to it.
    """
    now = utcnow()
    # Cast because `AsyncSession.execute` is typed for the SELECT case; a DELETE
    # really does return a CursorResult, and `rowcount` is how many rows went.
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(OAuthClient).where(
                OAuthClient.tier != TIER_STATIC,
                OAuthClient.last_grant_at.is_(None),
                OAuthClient.expires_at.is_not(None),
                OAuthClient.expires_at < now,
            )
        ),
    )
    reaped = result.rowcount or 0
    if reaped:
        await session.commit()
        log.info("oauth_clients_reaped", count=reaped)
    return reaped


__all__ = [
    "GRANT_TYPES",
    "RESPONSE_TYPES",
    "RegistrationRefused",
    "authenticate",
    "find",
    "find_or_resolve",
    "note_grant",
    "reap_stale",
    "register",
    "resolve_metadata_document",
    "seed_static",
    "validate_redirect_uris",
]
