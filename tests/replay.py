"""Replaying ``fixtures/`` as if it were fanmoot.com.

The suite drives the **real** ``FanmootClient`` — real timeouts, real retry
policy, real error mapping — against an ``httpx`` transport that serves the
recorded bodies (HAD-14). Only the socket is fake.

Two things this does that a plain stub would not:

* **It honours ``If-None-Match``.** A fixture carries the ETag Fanmoot really
  sent, so a conditional request gets a real ``304`` with no body — which is
  the path ``cache.py`` exists for and the one worth actually testing.
* **It counts.** ``Replay.calls`` is what lets a cache test assert the
  interesting thing: not "the value was right" but "the network was not
  touched a second time".
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@dataclass
class Recorded:
    file: str
    method: str
    path: str
    params: dict[str, Any]
    status: int
    etag: str | None
    synthetic: bool

    @property
    def body(self) -> Any:
        return json.loads((FIXTURES / self.file).read_text())

    def key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return _key(self.method, self.path, self.params)


def _key(
    method: str, path: str, params: dict[str, Any] | None
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    cleaned = tuple(sorted((k, str(v)) for k, v in (params or {}).items() if v is not None))
    return method.upper(), path, cleaned


def load_manifest() -> list[Recorded]:
    entries = json.loads((FIXTURES / "manifest.json").read_text())
    return [
        Recorded(
            file=e["file"],
            method=e["method"],
            path=e["path"],
            params=e.get("params") or {},
            status=e.get("status", 200),
            etag=e.get("etag"),
            synthetic=e.get("synthetic", False),
        )
        for e in entries
    ]


@dataclass
class Replay:
    """A transport over the recorded corpus."""

    recordings: dict[tuple[str, str, tuple[tuple[str, str], ...]], Recorded] = field(
        default_factory=dict
    )
    calls: list[httpx.Request] = field(default_factory=list)
    #: Paths that should fail, and how — set by a test that wants to exercise
    #: the retry policy or an error mapping rather than a happy path.
    faults: dict[str, list[int | Exception]] = field(default_factory=dict)

    @classmethod
    def from_fixtures(cls) -> Replay:
        return cls(recordings={r.key(): r for r in load_manifest()})

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def paths(self) -> list[str]:
        return [r.request.url.path if hasattr(r, "request") else "" for r in []] or [
            str(c.url.path) for c in self.calls
        ]

    def fail(self, path: str, *outcomes: int | Exception) -> None:
        """Queue failures for one path. Each call consumes one; when the queue
        empties the recorded response is served, which is exactly the shape of
        "fails once, then succeeds"."""
        self.faults[path] = list(outcomes)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path

        queued = self.faults.get(path)
        if queued:
            outcome = queued.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return httpx.Response(outcome, json={"detail": "injected"}, request=request)

        params = dict(request.url.params)
        recorded = self.recordings.get(_key(request.method, path, params))
        if recorded is None and request.method == "POST":
            recorded = self._best_post(path, params)
        if recorded is None:
            # Loud on purpose. A test that silently got an empty body because
            # its URL was a character out is a test that proves nothing.
            raise AssertionError(
                f"No fixture for {request.method} {path} {params or ''}. "
                f"Run `make record`, or add it to fixtures/manifest.json."
            )

        headers = {"Content-Type": "application/json"}
        if recorded.etag:
            headers["ETag"] = recorded.etag
            if _matches(request.headers.get("if-none-match", ""), recorded.etag):
                return httpx.Response(304, headers=headers, request=request)

        return httpx.Response(recorded.status, json=recorded.body, headers=headers, request=request)

    def _best_post(self, path: str, params: dict[str, Any]) -> Recorded | None:
        """Match a POST on its path, tolerating query parameters it did not
        record.

        A GET's query string *is* its identity, so those are matched exactly.
        A POST's is not: ``/v1/moots`` carries an ``idempotency_key`` derived
        from the MCP request id, which is different on every call by design —
        exact matching there would mean a fixture per request id.

        So a recorded POST matches when everything it *did* record is present
        in the request, and the most specific recording wins. That is what
        lets one fixture be the ordinary post and another, keyed on a chosen
        idempotency key, be the one the football gate held.
        """
        best: Recorded | None = None
        for recorded in self.recordings.values():
            if recorded.method != "POST" or recorded.path != path:
                continue
            if any(params.get(k) != str(v) for k, v in recorded.params.items()):
                continue
            if best is None or len(recorded.params) > len(best.params):
                best = recorded
        return best


def _matches(if_none_match: str, etag: str) -> bool:
    """RFC 9110 weak comparison, the same rule Fanmoot's own middleware uses."""
    if not if_none_match:
        return False
    wanted = etag.removeprefix("W/")
    return any(
        part.strip().removeprefix("W/") in (wanted, "*") for part in if_none_match.split(",")
    )
