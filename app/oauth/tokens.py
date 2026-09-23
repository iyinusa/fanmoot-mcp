"""Minting credentials, hashing them, and the one envelope that is not a hash.

Three jobs, and the reason they share a module is that they share a rule: **this
service never stores a credential it could read back.**

* ``mint`` makes an opaque token from the OS random source. Not a JWT: an Alexa
  access token is a *handle* to a grant row (HAD-3), and a self-describing token
  would be a token this server could not revoke.
* ``fingerprint`` is the SHA-256 that actually goes in the database, and
  ``matches`` is the constant-time comparison that finds it again. Same pattern
  as ``fanmoot``'s ``models/token.py``, for the same reason: a dump of this
  schema should hand an attacker nothing usable.
* ``encrypt``/``decrypt`` are the Fernet envelope around the one credential that
  cannot be hashed, because it has to be *used* — the fan's upstream Fanmoot
  refresh token.

**Why a plain SHA-256 and not a password hash.** Argon2 exists in this repo's
dependencies and is the right answer for a *password*, which is short, chosen by
a human and guessable. These tokens are 256 bits from ``secrets``; there is
nothing to guess, and a per-lookup Argon2 verification would add tens of
milliseconds to every tool call for no attacker cost. A fast digest is correct
here and slow hashing is correct there.

**Rotation without a reconnect** (`S7-4`). The key setting takes a
comma-separated list and the envelope is a ``MultiFernet``: the first key wraps
everything written, every key opens what it wrapped. ``rotate`` re-wraps one
ciphertext under the first key, and ``scripts/rotate_keys.py`` runs it over every
stored session — so an old key can be retired without signing a single fan out.
The exchange in ``app.oauth.upstream`` rewrites a session each time Fanmoot
rotates its refresh token, so an active link migrates on its own within a
quarter of an hour; the script is for the ones nobody has used since.

**Why the server refuses rather than degrades without a key.** ``can_store_grants``
is False when ``FMCP_SECRET_ENCRYPTION_KEY`` is empty, and every path that would
write a fan's upstream credentials raises ``SecretsUnavailable``. The read tools
are untouched, so the server keeps answering football questions and declines to
link an account — which is the safe direction, and is reported by ``/healthz``
so an operator reads it rather than discovering it from a failed sign-in.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.settings import get_settings

#: 32 bytes of OS randomness, base64url — 43 characters. Long enough that the
#: hash is the only attack surface, short enough to survive a redirect, a header
#: and whatever a client writes it into.
_TOKEN_BYTES = 32

#: A client id is not a secret, but it is an identifier that ends up in logs and
#: config files, so it is short and readable rather than long and opaque.
_CLIENT_ID_BYTES = 12


class SecretsUnavailable(RuntimeError):
    """No Fernet key, so a fan's upstream credentials cannot be stored.

    Raised rather than falling back to plaintext. The authenticated half of the
    surface is closed until a key is configured; the read tools do not care.
    """


class SecretUnreadable(RuntimeError):
    """The stored ciphertext will not open with the current key.

    In practice: a key was retired before ``make rotate-keys`` re-wrapped what
    it held, or it was dropped on purpose after a suspected leak. Every session
    it wrapped becomes unreadable and those fans reconnect, which is the
    *designed* consequence (nothing leaks) rather than a fault — so this is
    translated into "reconnect your Fanmoot account" wherever a fan can see it.
    """


def mint() -> str:
    """One opaque credential: authorization code, access token or refresh token."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def mint_client_id(prefix: str = "fmcp") -> str:
    """A server-generated ``client_id`` for a self-registering client.

    Prefixed so an operator reading a log line or a consent event can tell one
    of ours from Amazon's, which are ``amzn1.application-oa2-client.…``. RFC 7591
    forbids a client choosing its own id, and the prefix is what makes that
    visible rather than merely true.
    """
    return f"{prefix}_{secrets.token_urlsafe(_CLIENT_ID_BYTES)}"


def fingerprint(token: str) -> str:
    """The SHA-256 hex digest that goes in the database. Never the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def matches(presented: str, stored_hash: str | None) -> bool:
    """Whether ``presented`` hashes to ``stored_hash``, compared in constant time.

    ``compare_digest`` rather than ``==`` because the comparison is on a secret
    and a short-circuiting compare leaks how much of it was right. An absent
    hash is False, never True — a public client has no secret and must not be
    able to authenticate by sending none.
    """
    if not stored_hash or not presented:
        return False
    return hmac.compare_digest(fingerprint(presented), stored_hash)


@lru_cache
def _box() -> MultiFernet:
    keys = get_settings().encryption_keys
    if not keys:
        raise SecretsUnavailable(
            "FMCP_SECRET_ENCRYPTION_KEY is not set, so this server will not store a fan's "
            "Fanmoot credentials. Generate one with: python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    boxes: list[Fernet] = []
    for position, key in enumerate(keys, start=1):
        try:
            boxes.append(Fernet(key.encode("utf-8")))
        except (ValueError, TypeError) as exc:
            # Which one, by position — never the key itself, which is the
            # secret this message would otherwise print into a log.
            raise SecretsUnavailable(
                f"Key {position} of FMCP_SECRET_ENCRYPTION_KEY is not a valid Fernet key "
                "(32 url-safe base64-encoded bytes). Generate one with: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from exc
    return MultiFernet(boxes)


def encrypt(plaintext: str) -> str:
    """Wrap a fan's upstream refresh token for storage (HAD-3)."""
    return _box().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Unwrap one. Raises ``SecretUnreadable`` if the key has moved on."""
    try:
        return _box().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise SecretUnreadable(
            "A stored Fanmoot session will not decrypt with the current key."
        ) from exc


def rotate(ciphertext: str) -> str:
    """Re-wrap one stored secret under the current first key.

    Opens with whichever key wrapped it and seals with the newest, so running it
    twice is harmless. Raises ``SecretUnreadable`` when no configured key opens
    it — the key that did has already been dropped.
    """
    try:
        return _box().rotate(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError) as exc:
        raise SecretUnreadable(
            "A stored Fanmoot session will not decrypt with any configured key."
        ) from exc


def signing_key() -> bytes:
    """A key for signing the short-lived browser-leg state (``app.oauth.flow``).

    Derived from the Fernet key rather than configured separately, because a
    second secret is a second thing to forget and this one is already required
    for the flow to be able to store anything at all. Domain-separated by a
    fixed label so the signing key and the encryption key are not the same bytes
    doing two jobs.

    Only the **first** key signs. A rotation therefore invalidates browser legs
    that are in flight at the moment of the deploy — a fan half way through a
    consent screen starts it again — which is fifteen minutes of exposure, and
    far simpler than a signature that has to be checked against every key.
    """
    settings = get_settings()
    if not settings.can_store_grants:
        raise SecretsUnavailable(
            "FMCP_SECRET_ENCRYPTION_KEY is not set, so the authorization flow cannot sign its "
            "own state and account linking is closed."
        )
    return hashlib.sha256(
        b"fanmoot-mcp/oauth-flow/v1|" + settings.encryption_keys[0].encode("utf-8")
    ).digest()


def reset_cache() -> None:
    """Test hook: drop the cached Fernet so a new key takes effect."""
    _box.cache_clear()
