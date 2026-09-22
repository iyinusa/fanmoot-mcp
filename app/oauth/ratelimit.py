"""Rate limiting and lockout for the endpoints where guessing pays (`S4-14`).

Three places, three different failures they prevent:

* **``/token``** — generous, per address. A household behind one NAT with
  several linked clients all refreshing at once is legitimate traffic; what this
  stops is a script grinding through codes or refresh tokens, each attempt a
  database write.
* **``/oauth/register``** — tight, per address. Registration is open by design
  (`S4-16`), which means anybody can create rows, which means a limit is the only
  thing between "usable by any AI" and "a table anyone can fill".
* **The login form** — per address *and* per email address. The per-address limit
  stops one machine working through a password list; the per-email lockout stops
  a distributed attempt against one known account, which the address limit alone
  would never see.

**In process, not in Postgres.** This server runs a single worker (see the
Dockerfile — the MCP session manager's task group requires it), so a
process-local counter is a global counter, and a limiter that wrote a row per
attempt would turn a flood into exactly the load it exists to prevent. The day
this scales to several workers, this module grows a shared backend and its
callers do not change — which is why the limit lives behind a function rather
than inline in the router.

The window is a sliding one rather than fixed buckets, because a fixed bucket
lets twice the limit through across a boundary — and at these limits, "twice"
is the difference between ten password attempts and twenty.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from starlette.requests import Request

#: Nothing is remembered longer than this, whatever the caller's window. Keeps a
#: process that has seen a lot of distinct addresses from holding all of them.
_MAX_WINDOW_S = 3600


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether to allow, and how long to say to wait when not."""

    allowed: bool
    retry_after: int = 0

    def __bool__(self) -> bool:
        return self.allowed


#: bucket -> subject -> the times of recent attempts, oldest first.
_hits: dict[str, dict[str, deque[float]]] = defaultdict(lambda: defaultdict(deque))


def hit(bucket: str, subject: str, *, limit: int, window_s: int) -> Decision:
    """Record an attempt and say whether it is allowed.

    Counting *before* deciding is deliberate: an attempt that is refused still
    counts, so hammering a locked bucket keeps it locked rather than letting it
    drain while the attacker is still knocking.
    """
    now = time.monotonic()
    window = min(window_s, _MAX_WINDOW_S)
    attempts = _hits[bucket][subject]

    cutoff = now - window
    while attempts and attempts[0] < cutoff:
        attempts.popleft()

    attempts.append(now)
    if len(attempts) <= limit:
        return Decision(allowed=True)

    # The oldest attempt in the window is the one that has to age out before
    # there is room again, so that is what the caller is told to wait for.
    retry_after = max(1, int(attempts[0] + window - now) + 1)
    return Decision(allowed=False, retry_after=retry_after)


def peek(bucket: str, subject: str, *, limit: int, window_s: int) -> Decision:
    """Whether an attempt *would* be allowed, without counting one.

    Used by the login form's per-email lockout, which counts failures rather
    than attempts: a fan who signs in correctly on the third try should not be
    locked out by the two wrong ones, and a wrong password should not be
    cheaper than a right one to try again after.
    """
    now = time.monotonic()
    window = min(window_s, _MAX_WINDOW_S)
    attempts = _hits[bucket][subject]
    cutoff = now - window
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    if len(attempts) < limit:
        return Decision(allowed=True)
    retry_after = max(1, int(attempts[0] + window - now) + 1)
    return Decision(allowed=False, retry_after=retry_after)


def forgive(bucket: str, subject: str) -> None:
    """Clear a subject's history — what a successful sign-in does to the
    failure counter for that address."""
    _hits[bucket].pop(subject, None)


def reset() -> None:
    """Test hook: forget every counter, so one test cannot lock out another."""
    _hits.clear()


def client_ip(request: Request) -> str:
    """The caller's address, as far as the deployment can honestly say.

    ``X-Forwarded-For``'s **first** entry, because Traefik appends and the
    client-supplied prefix is the part that matters — with the direct peer as
    the fallback. This is a rate-limiting key, not an identity: a caller who can
    forge the header can spread their attempts, which is why the login form also
    counts per email address.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"
