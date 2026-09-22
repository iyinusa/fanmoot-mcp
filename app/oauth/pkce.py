"""PKCE, and the refusal of everything weaker than S256.

`S4-4`. Two reasons this is a compliance item rather than a nicety:

* **OAuth 2.1 makes PKCE mandatory for every client**, public or confidential.
  There is no path through ``/authorize`` here without a challenge.
* **Alexa's deploy fails** if ``code_challenge_methods_supported`` does not
  include ``S256`` (`S4-3`), and a server that advertised S256 and then accepted
  ``plain`` would be worse than one that did neither: it would pass the deploy
  check and silently accept the downgraded flow.

So ``plain`` is refused. It is *in* RFC 7636, and that is exactly why it has to
be named and rejected rather than merely unimplemented — a client that omits
``code_challenge_method`` is specified to mean ``plain``, so silence is a
downgrade attempt whether or not anybody intended one.

The attack S256 closes: the authorization code travels back through a redirect,
which on a device means a URL other software can see. With PKCE the code is
useless without the verifier, which never leaves the client that generated it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

#: The only method this server accepts.
S256 = "S256"

#: Named so it can be refused explicitly. RFC 7636's default when a client sends
#: no method, which is why an absent method is treated as this and rejected.
PLAIN = "plain"

SUPPORTED_METHODS: tuple[str, ...] = (S256,)

#: RFC 7636 §4.1: 43–128 characters of unreserved ASCII. Enforced on the way in
#: so a malformed challenge is an ``invalid_request`` on ``/authorize`` rather
#: than a verification that can never succeed at ``/token``.
_ALLOWED = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")


def valid_challenge(challenge: str | None) -> bool:
    """Whether this is a well-formed S256 challenge."""
    return bool(challenge and _ALLOWED.match(challenge))


def valid_verifier(verifier: str | None) -> bool:
    """Whether this is a well-formed verifier. Same alphabet and length as above."""
    return bool(verifier and _ALLOWED.match(verifier))


def challenge_for(verifier: str) -> str:
    """The S256 challenge for a verifier: base64url(sha256(verifier)), unpadded.

    Used by the tests to play the client side of the dance, and by ``verify``.
    Unpadded because RFC 7636 says so, and a padded comparison would fail
    against every real client.
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify(*, verifier: str, challenge: str, method: str = S256) -> bool:
    """Whether ``verifier`` produces ``challenge`` under ``method``.

    Anything but ``S256`` is False — including ``plain``, even when the verifier
    and the challenge are identical and the check would "pass". A downgraded
    method is not a verification this server performs.
    """
    if method != S256 or not valid_verifier(verifier):
        return False
    return hmac.compare_digest(challenge_for(verifier), challenge)
