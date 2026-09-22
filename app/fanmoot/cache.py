"""A read-through TTL cache with conditional revalidation.

**This is a functional requirement, not an optimisation** (HAD-6). Alexa+ wants
an answer inside 500 ms, and a cold call to ``fanmoot.com/v1/matches/live``
plus JSON shaping will not reliably clear that. So every read goes through
here.

How a read resolves, in order:

1. **Fresh hit** — inside its TTL. Returned with no network at all. This is the
   case the latency budget is built on.
2. **Stale hit with an ETag** — a conditional ``GET`` carrying
   ``If-None-Match``. Fanmoot serves ``ETag`` on these endpoints, and a ``304``
   refreshes the TTL and returns *the object already parsed*. No body crosses
   the wire and no JSON is re-validated, which on a live digest polled every
   ten seconds is most of the cost.
3. **Miss, or a ``200`` on revalidation** — parsed, stored, returned.

Two boundaries that are security properties, not tuning:

* **A request carrying a bearer token is never cached and never served from
  cache.** Fanmoot's authenticated reads are per-fan — a feed carries *your*
  reactions, ``/v1/me`` is you — so one fan's response in a shared dictionary
  is one fan reading another's. The bypass is unconditional and has no setting.
* **The cache is per-process and in-memory.** It holds public football data
  that Fanmoot already serves to anybody, it dies with the worker, and it is
  bounded, so a long tail of one-off queries evicts rather than grows.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar
from urllib.parse import urlencode

from app.logging_config import get_logger

log = get_logger("fanmoot.cache")

T = TypeVar("T")

#: Per-endpoint TTLs, in seconds, chosen from how fast the data actually moves
#: (§5's table). These are the numbers, in one place, rather than scattered
#: through the wrappers — so "why is a fixture list five minutes stale" has an
#: answer you can read.
TTL_LIVE = 10.0
TTL_MATCH = 30.0
TTL_THREAD = 30.0
TTL_SEARCH = 60.0
TTL_TRENDING = 60.0
TTL_FIXTURES = 300.0
TTL_NEWS = 300.0
TTL_MOOD = 300.0
TTL_CATALOGUE = 3600.0

#: Entries, not bytes. Every value here is a parsed page of football — tens of
#: kilobytes at the outside — and the tail that needs bounding is one-off
#: search queries, which is a count.
_MAX_ENTRIES = 512


@dataclass
class _Entry:
    value: Any
    etag: str | None
    expires_at: float


@dataclass
class Stats:
    """Enough to answer "is the cache doing its job" in ``/healthz`` and in
    ``make latency`` (S7), without a metrics dependency."""

    hits: int = 0
    misses: int = 0
    revalidations: int = 0
    not_modified: int = 0


class TtlCache:
    """Bounded, in-process, least-recently-used.

    Not thread-safe and does not need to be: one event loop per worker, and
    every method here runs to completion without awaiting. Two coroutines can
    still race to fill the same key — both will fetch, the second overwrites —
    which costs one duplicated request and avoids a lock on the hot path.
    """

    def __init__(self, *, max_entries: int = _MAX_ENTRIES) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._max_entries = max_entries
        self.stats = Stats()

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        self._entries.clear()
        self.stats = Stats()

    async def fetch(
        self,
        key: str,
        *,
        ttl: float,
        token: str | None,
        load: Callable[[str | None], Awaitable[tuple[Any, str | None, bool]]],
    ) -> Any:
        """Resolve one read.

        ``load`` is given the ETag to revalidate against (or ``None`` for an
        unconditional fetch) and returns ``(value, etag, not_modified)``. The
        caller owns parsing, so on a ``304`` nothing is parsed twice.
        """
        # A signed-in read belongs to one fan. Straight to the network, and
        # nothing is written back.
        if token is not None:
            return (await load(None))[0]

        now = time.monotonic()
        entry = self._entries.get(key)

        if entry is not None and entry.expires_at > now:
            self._entries.move_to_end(key)
            self.stats.hits += 1
            return entry.value

        if entry is not None and entry.etag:
            self.stats.revalidations += 1
            value, etag, not_modified = await load(entry.etag)
            if not_modified:
                # The whole point: the TTL moves forward and the already-parsed
                # object is handed back untouched.
                self.stats.not_modified += 1
                entry.expires_at = now + ttl
                if etag:
                    entry.etag = etag
                self._entries.move_to_end(key)
                return entry.value
            self._store(key, value, etag, now + ttl)
            return value

        self.stats.misses += 1
        value, etag, _ = await load(None)
        self._store(key, value, etag, now + ttl)
        return value

    def _store(self, key: str, value: Any, etag: str | None, expires_at: float) -> None:
        self._entries[key] = _Entry(value=value, etag=etag, expires_at=expires_at)
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            evicted, _ = self._entries.popitem(last=False)
            log.debug("cache_evicted", key=evicted)


def key_for(path: str, params: dict[str, Any] | None = None) -> str:
    """A stable key for one upstream read.

    Parameters are sorted, so ``?club=arsenal&limit=5`` and
    ``?limit=5&club=arsenal`` are one entry rather than two — which matters
    because the tools build their query dictionaries in whatever order reads
    best, not in a canonical one.
    """
    if not params:
        return path
    cleaned = sorted((k, str(v)) for k, v in params.items() if v is not None)
    if not cleaned:
        return path
    return f"{path}?{urlencode(cleaned)}"


_cache: TtlCache | None = None


def get_cache() -> TtlCache:
    global _cache
    if _cache is None:
        _cache = TtlCache()
    return _cache


def reset_cache() -> None:
    """Test hook, and the lifespan's: start from cold."""
    global _cache
    _cache = None
