"""The cache — hit, miss, 304, and the boundary that must never move.

HAD-6 calls this a functional requirement rather than an optimisation, so what
is asserted here is not "the value was right" but "the network was not touched",
which is the property the latency budget actually rests on.
"""

from __future__ import annotations

from app.fanmoot import cache as cache_module
from app.fanmoot import endpoints
from app.fanmoot.cache import TtlCache, key_for
from tests.replay import Replay


async def test_a_miss_fetches_and_a_hit_does_not(replay: Replay) -> None:
    first = await endpoints.list_leagues()
    second = await endpoints.list_leagues()

    assert replay.call_count == 1
    assert [league.slug for league in second] == [league.slug for league in first]


async def test_a_hit_returns_the_same_parsed_object(replay: Replay) -> None:
    """What is cached is the *model*, not the JSON. A second call skips pydantic
    as well as the network — on a live digest polled every ten seconds that is
    most of the cost."""
    first = await endpoints.live_digest()
    second = await endpoints.live_digest()

    assert second is first


async def test_an_expired_entry_revalidates_and_a_304_refreshes_it(replay: Replay) -> None:
    """The case the whole module exists for: Fanmoot answers `304`, no body
    crosses the wire, nothing is re-validated, and the TTL moves forward."""
    cache = cache_module.get_cache()
    first = await endpoints.list_leagues()

    # Expire it without waiting an hour.
    entry = next(iter(cache._entries.values()))
    assert entry.etag, "the recorded /v1/leagues carries a real ETag"
    entry.expires_at = 0.0

    second = await endpoints.list_leagues()

    assert replay.call_count == 2
    assert replay.calls[1].headers["if-none-match"] == entry.etag
    assert cache.stats.not_modified == 1
    assert second is first

    # And the refreshed TTL means the next call is a plain hit again.
    await endpoints.list_leagues()
    assert replay.call_count == 2


async def test_an_expired_entry_without_an_etag_just_refetches() -> None:
    """Not every upstream response carries a validator. Missing one is a slower
    path, not a broken one."""
    cache = TtlCache()
    calls: list[str | None] = []

    async def load(etag: str | None) -> tuple[object, str | None, bool]:
        calls.append(etag)
        return {"n": len(calls)}, None, False

    first = await cache.fetch("k", ttl=60, token=None, load=load)
    cache._entries["k"].expires_at = 0.0
    second = await cache.fetch("k", ttl=60, token=None, load=load)

    assert calls == [None, None]
    assert first == {"n": 1}
    assert second == {"n": 2}


async def test_a_signed_in_read_is_never_cached(replay: Replay) -> None:
    """The boundary that is a security property, not tuning. Fanmoot's
    authenticated reads are per-fan; one fan's response in a shared dictionary
    is one fan reading another's."""
    await endpoints.me(token="fan-a")
    await endpoints.me(token="fan-b")

    assert replay.call_count == 2
    assert len(cache_module.get_cache()) == 0


async def test_a_signed_in_read_does_not_poison_the_public_one(replay: Replay) -> None:
    """And nothing a token fetched is left behind for an anonymous caller to
    find."""
    await endpoints.search("arsenal", limit=5, token="fan-a")

    assert len(cache_module.get_cache()) == 0

    await endpoints.search("arsenal", limit=5)
    assert replay.call_count == 2
    assert len(cache_module.get_cache()) == 1


async def test_parameter_order_does_not_split_one_entry_in_two(replay: Replay) -> None:
    """Tools build their query dictionaries in whatever order reads best."""
    assert key_for("/v1/matches", {"club": "arsenal", "limit": 5}) == key_for(
        "/v1/matches", {"limit": 5, "club": "arsenal"}
    )
    assert key_for("/v1/matches", {"club": "arsenal", "league": None}) == key_for(
        "/v1/matches", {"club": "arsenal"}
    )


async def test_ttls_follow_how_fast_the_data_moves() -> None:
    """§5's table, asserted rather than left as prose: a live score is not a
    club badge and must not be cached like one."""
    assert cache_module.TTL_LIVE < cache_module.TTL_MATCH
    assert cache_module.TTL_MATCH <= cache_module.TTL_THREAD
    assert cache_module.TTL_THREAD < cache_module.TTL_FIXTURES
    assert cache_module.TTL_FIXTURES < cache_module.TTL_CATALOGUE
    assert cache_module.TTL_LIVE == 10.0
    assert cache_module.TTL_CATALOGUE == 3600.0


async def test_the_cache_is_bounded() -> None:
    """A long tail of one-off search queries must evict rather than grow: this
    lives in a process that is meant to stay up for the whole hackathon."""
    cache = TtlCache(max_entries=4)

    async def load(etag: str | None) -> tuple[object, str | None, bool]:
        return "x", None, False

    for i in range(10):
        await cache.fetch(f"k{i}", ttl=60, token=None, load=load)

    assert len(cache) == 4


async def test_eviction_drops_the_least_recently_used() -> None:
    cache = TtlCache(max_entries=2)

    async def load(etag: str | None) -> tuple[object, str | None, bool]:
        return "x", None, False

    await cache.fetch("a", ttl=60, token=None, load=load)
    await cache.fetch("b", ttl=60, token=None, load=load)
    await cache.fetch("a", ttl=60, token=None, load=load)  # `a` is now the recent one
    await cache.fetch("c", ttl=60, token=None, load=load)

    assert set(cache._entries) == {"a", "c"}
