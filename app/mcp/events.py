"""Resumability: the events a client can ask for again after its stream drops.

``S2-2``. Streamable HTTP delivers server messages over SSE, and SSE
connections die — a phone changes network, a proxy times out an idle stream, a
laptop sleeps. ``2025-11-25`` handles that with ``Last-Event-ID``: the client
reconnects quoting the last event it saw and the server replays what came
after. Without an event store the SDK still tracks sessions, but a dropped
stream loses whatever was in flight, silently.

It also unlocks the other half of SEP-1699 — a server may close a standalone
GET stream at will, because the client knows how to come back. That is what
makes it reasonable for this server to hang up on an idle listener rather than
hold a socket open across a fan's whole evening.

**In memory, and that is a deployment decision, not a shortcut.** An event
belongs to a session, a session lives in one process's memory, and this service
runs a single worker for exactly that reason (see the Dockerfile's ``CMD``).
Moving to several workers means moving sessions out of process too — Redis
behind this same interface — and that is an ``S7`` decision taken with the
latency numbers in hand.

Bounded per stream, because it is memory a client controls. A session that
never reconnects still costs at most ``max_events_per_stream`` messages, and
the oldest are dropped first: a client that has been gone that long has lost
the thread anyway and is better off starting a new session.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from uuid import uuid4

from mcp.server.streamable_http import (
    EventCallback,
    EventId,
    EventMessage,
    EventStore,
    StreamId,
)
from mcp.types import JSONRPCMessage

from app.logging_config import get_logger

log = get_logger("mcp.events")

#: Per stream, not per session. A POST's response stream carries a handful of
#: messages; the standalone GET stream carries notifications for as long as it
#: is open. 256 is generous for the first and a sane ceiling for the second.
DEFAULT_MAX_EVENTS_PER_STREAM = 256


@dataclass(frozen=True, slots=True)
class _Stored:
    event_id: EventId
    stream_id: StreamId
    #: ``None`` for a *priming* event — the cursor the transport stores before
    #: dispatching a request so the client has something to quote even if the
    #: stream drops before any message is sent. It has an id and no payload,
    #: so replay skips it rather than sending an empty frame.
    message: JSONRPCMessage | None


class InMemoryEventStore(EventStore):
    """Per-stream ring buffers, with an id index for ``Last-Event-ID`` lookup.

    Two structures rather than one because the two operations want different
    shapes: storing appends to a stream, replaying starts from an *event* and
    has to find its stream. The index is kept in step with the rings — an event
    evicted from its ring is removed from the index in the same breath, so a
    client quoting an id that has aged out is told "unknown" rather than handed
    a stale position.
    """

    def __init__(self, max_events_per_stream: int = DEFAULT_MAX_EVENTS_PER_STREAM) -> None:
        if max_events_per_stream <= 0:
            raise ValueError("max_events_per_stream must be a positive number of events")
        self._max_events = max_events_per_stream
        self._streams: dict[StreamId, deque[_Stored]] = {}
        self._index: dict[EventId, _Stored] = {}

    async def store_event(self, stream_id: StreamId, message: JSONRPCMessage | None) -> EventId:
        """Remember one event and return the id the client will quote back."""
        event = _Stored(event_id=uuid4().hex, stream_id=stream_id, message=message)

        ring = self._streams.get(stream_id)
        if ring is None:
            ring = deque(maxlen=self._max_events)
            self._streams[stream_id] = ring

        # `deque(maxlen=...)` drops silently on append, so the eviction is read
        # off the front *before* appending. Doing it after would leave the
        # dropped event's id in the index, pointing at nothing.
        if len(ring) == self._max_events:
            self._index.pop(ring[0].event_id, None)

        ring.append(event)
        self._index[event.event_id] = event
        return event.event_id

    async def replay_events_after(
        self,
        last_event_id: EventId,
        send_callback: EventCallback,
    ) -> StreamId | None:
        """Send everything that followed ``last_event_id`` on its stream.

        Returns the stream's id so the transport can re-attach the live tail to
        the reconnected client, or ``None`` when the id is unknown — expired,
        from a previous process, or invented. ``None`` is the honest answer
        there: the client then knows to start a fresh session rather than
        believing it has caught up.
        """
        anchor = self._index.get(last_event_id)
        if anchor is None:
            log.info("event_replay_unknown_id")
            return None

        ring = self._streams.get(anchor.stream_id)
        if ring is None:  # pragma: no cover - an indexed event always has a ring
            return None

        replayed = 0
        seen_anchor = False
        for event in list(ring):
            if not seen_anchor:
                seen_anchor = event.event_id == last_event_id
                continue
            if event.message is None:
                # A priming cursor. It marks a position and carries nothing to
                # deliver.
                continue
            await send_callback(EventMessage(event.message, event.event_id))
            replayed += 1

        log.info("event_replay", stream=anchor.stream_id, events=replayed)
        return anchor.stream_id

    def forget_stream(self, stream_id: StreamId) -> None:
        """Drop one stream's events. Used when a session ends for good."""
        for event in self._streams.pop(stream_id, ()):
            self._index.pop(event.event_id, None)

    @property
    def stored(self) -> int:
        """How many events are held, across every stream. For tests and health."""
        return len(self._index)
