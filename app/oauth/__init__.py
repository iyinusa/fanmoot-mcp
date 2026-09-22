"""The OAuth 2.1 authorization server (`S4`, HAD-4).

``mcp.fanmoot.com`` is its own authorization server. It hosts ``/authorize``,
``/token``, the fan-facing login, sign-up and consent pages, RFC 7591 dynamic
client registration, revocation, and all three discovery documents — and it
authenticates a fan by calling Fanmoot's public ``/v1/auth/*`` endpoints rather
than by owning any part of their identity.

The modules, in the order a flow touches them:

* ``scopes``    — the four scopes, what each one lets a client do, and which
                  tool needs which (the contract `S5` fills in).
* ``metadata``  — the three discovery documents (`S4-2`, `S4-3`).
* ``clients``   — both tiers of client registration (`S4-5`, `S4-16`, `S4-18`).
* ``dcr``       — RFC 7591 registration and SEP-991 metadata documents.
* ``flow``      — the pending authorization, carried across the browser leg.
* ``pkce``      — S256 verification, and the refusal of anything weaker.
* ``tokens``    — token minting, SHA-256 hashing, the Fernet envelope.
* ``grants``    — codes, grants, rotating refresh tokens, revocation.
* ``upstream``  — the exchange: an opaque token in, a fresh Fanmoot token out.
* ``bearer``    — the ``/mcp`` mount's token verification and 401 challenge.
* ``ratelimit`` — the limits on ``/token``, ``/oauth/register`` and the login form.
* ``router``    — the HTTP surface that ties them together.
"""
