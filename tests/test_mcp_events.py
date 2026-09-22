"""``S2-2`` — the event store behind ``Last-Event-ID``.

A fan's SSE stream drops when their phone changes network. Resumability is what
turns that into a reconnect instead of a lost goal.
"""

from __future__ import annotations

from mcp.server.streamable_http import EventMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

from app.mcp.events import InMemoryEventStore


def _notification(name: str) -> JSONRPCMessage:
    return JSONRPCMessage(
        JSONRPCNotification(jsonrpc="2.0", method="notifications/message", params={"data": name})
    )


async def _replay(store: InMemoryEventStore, after: str) -> tuple[str | None, list[str]]:
    seen: list[str] = []

    async def collect(event: EventMessage) -> None:
        params = event.message.root.params or {}
        seen.append(str(params.get("data")))

    stream = await store.replay_events_after(after, collect)
    return stream, seen


async def test_replays_only_what_came_after_the_clients_last_event() -> None:
    store = InMemoryEventStore()
    await store.store_event("s1", _notification("first"))
    anchor = await store.store_event("s1", _notification("second"))
    await store.store_event("s1", _notification("third"))
    await store.store_event("s1", _notification("fourth"))

    stream, seen = await _replay(store, anchor)

    assert stream == "s1"
    assert seen == ["third", "fourth"]


async def test_one_streams_events_are_not_replayed_onto_another() -> None:
    store = InMemoryEventStore()
    anchor = await store.store_event("s1", _notification("mine"))
    await store.store_event("s2", _notification("someone elses"))
    await store.store_event("s1", _notification("also mine"))

    stream, seen = await _replay(store, anchor)

    assert stream == "s1"
    assert seen == ["also mine"]


async def test_a_priming_cursor_is_a_position_not_a_message() -> None:
    """The transport stores an empty event before dispatching, so a client has
    something to quote even if the stream drops before any reply. Replaying it
    as a frame would send the client an empty message it cannot parse."""
    store = InMemoryEventStore()
    priming = await store.store_event("s1", None)
    await store.store_event("s1", _notification("after"))

    stream, seen = await _replay(store, priming)

    assert stream == "s1"
    assert seen == ["after"]


async def test_an_unknown_event_id_is_answered_honestly() -> None:
    """``None`` tells the transport to let the client start a fresh session.
    Pretending to have caught up would leave it believing it had."""
    store = InMemoryEventStore()
    await store.store_event("s1", _notification("first"))

    stream, seen = await _replay(store, "an-id-from-a-previous-process")

    assert stream is None
    assert seen == []


async def test_a_stream_is_bounded_and_evicted_ids_stop_resolving() -> None:
    """Memory a client controls has to have a ceiling. The eviction must take
    the index with it — an id left pointing at a dropped event would resume a
    client at a position that no longer exists."""
    store = InMemoryEventStore(max_events_per_stream=3)
    oldest = await store.store_event("s1", _notification("one"))
    for name in ("two", "three", "four"):
        await store.store_event("s1", _notification(name))

    assert store.stored == 3
    assert await _replay(store, oldest) == (None, [])


async def test_forgetting_a_stream_forgets_its_events() -> None:
    store = InMemoryEventStore()
    anchor = await store.store_event("s1", _notification("one"))
    await store.store_event("s1", _notification("two"))

    store.forget_stream("s1")

    assert store.stored == 0
    assert await _replay(store, anchor) == (None, [])
