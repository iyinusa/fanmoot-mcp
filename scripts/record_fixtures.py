"""Capture live ``/v1`` responses into ``fixtures/``.

    make record            # the public read surface
    make record ARGS=-v    # …and say what each one returned

The test suite runs against these rather than against the network (HAD-14), so
it is fast, offline, deterministic — and still asserting against shapes the real
API actually produced. That last part is the whole point: a hand-written stub
asserts what we *believe* Fanmoot returns, which is exactly the belief that
goes stale.

Two things this script does deliberately:

* **It chains.** A match id, a match path, a moot id — none of these can be
  written down in advance, so each is read out of the response before it. A
  recording therefore describes one real moment on Fanmoot rather than a set of
  URLs somebody guessed.
* **It records misses.** A ``404`` or an empty list is a fixture too. Out of
  season, ``/v1/matches/live`` is empty, and a suite that only ever replays a
  busy Saturday never exercises the "nothing is on" path a fan will hit on a
  Tuesday morning.

The authenticated endpoints are not recorded here: doing so would need a real
fan's credentials and would write their email address and handle into a file in
this repository. Their fixtures are hand-built from Fanmoot's own schemas and
marked ``synthetic`` in the manifest, and ``--token`` exists for a maintainer
who wants to re-record them against a throwaway account.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
MANIFEST = FIXTURES / "manifest.json"


def fixture_name(method: str, path: str, params: dict[str, Any] | None) -> str:
    """A stable, readable filename for one call.

    The readable part is the path, so ``ls fixtures`` tells you what was
    recorded. The hash suffix is the parameters, because two calls to
    ``/v1/matches`` with different filters are two different fixtures and a
    filename cannot carry a query string on every filesystem.
    """
    slug = path.strip("/").replace("/", "_").replace("-", "_")
    query = urlencode(sorted((k, str(v)) for k, v in (params or {}).items() if v is not None))
    if not query:
        return f"{method.lower()}_{slug}.json"
    digest = hashlib.sha256(query.encode()).hexdigest()[:8]
    return f"{method.lower()}_{slug}_{digest}.json"


class Recorder:
    def __init__(self, base_url: str, token: str | None, verbose: bool) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verbose = verbose
        self.entries: list[dict[str, Any]] = []

    async def capture(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        response = await client.get(path, params=clean or None, headers=headers)

        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = None

        name = fixture_name("GET", path, clean)
        (FIXTURES / name).write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
        self.entries.append(
            {
                "file": name,
                "method": "GET",
                "path": path,
                "params": clean,
                "status": response.status_code,
                # Recorded so the suite can replay a real conditional request:
                # a `304` path that never saw a real ETag is not testing much.
                "etag": response.headers.get("etag"),
                "cache_control": response.headers.get("cache-control"),
                "recorded_at": datetime.now(UTC).isoformat(),
                "synthetic": False,
            }
        )
        if self.verbose:
            size = len(json.dumps(body)) if body is not None else 0
            print(f"  {response.status_code}  {path:<52} {size:>7} B  → {name}")
        return body if response.status_code == httpx.codes.OK else None

    def write_manifest(self) -> None:
        kept = {e["file"]: e for e in _existing_synthetic()}
        for entry in self.entries:
            kept[entry["file"]] = entry
        MANIFEST.write_text(
            json.dumps(sorted(kept.values(), key=lambda e: e["file"]), indent=1) + "\n"
        )


def _existing_synthetic() -> list[dict[str, Any]]:
    """Hand-built fixtures survive a re-record.

    The authenticated endpoints have no live recording, so a fresh run must add
    to the manifest rather than replace it.
    """
    if not MANIFEST.exists():
        return []
    entries = json.loads(MANIFEST.read_text())
    return [e for e in entries if e.get("synthetic")]


async def record(base_url: str, token: str | None, verbose: bool) -> int:
    FIXTURES.mkdir(exist_ok=True)
    rec = Recorder(base_url, token, verbose)

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=20.0,
        headers={"User-Agent": "fanmoot-mcp/record", "Accept": "application/json"},
    ) as client:
        print(f"recording {base_url}/v1 …")

        # ── Catalogue ────────────────────────────────────────────────────────
        await rec.capture(client, "/v1/leagues")
        await rec.capture(client, "/v1/leagues/premier-league")
        await rec.capture(client, "/v1/clubs/arsenal")

        # Every page, because name resolution is indexed over the whole
        # catalogue and a suite that only sees the first hundred clubs would
        # pass while "los blancos" resolved to nothing.
        cursor: str | None = None
        while True:
            page = await rec.capture(client, "/v1/clubs", {"cursor": cursor, "limit": 100})
            if not isinstance(page, dict):
                break
            cursor = page.get("next_cursor")
            if not cursor or not page.get("items"):
                break

        # ── Matches ──────────────────────────────────────────────────────────
        await rec.capture(client, "/v1/matches/live")
        await rec.capture(client, "/v1/matches/upcoming", {"days": 14})
        matches = await rec.capture(client, "/v1/matches", {"club": "arsenal", "limit": 5})

        match_id: str | None = None
        match_url: str | None = None
        if isinstance(matches, dict) and matches.get("items"):
            first = matches["items"][0]
            match_id = first.get("id")
            match_url = first.get("url")

        if match_id:
            await rec.capture(client, f"/v1/matches/{match_id}")
            await rec.capture(client, f"/v1/matches/{match_id}/moots", {"limit": 5})
            await rec.capture(client, "/v1/feed/match", {"match_id": match_id, "limit": 5})
        if match_url:
            # `/m/{league}/{season}/{slug}` on the site is
            # `/v1/matches/by-path/{league}/{season}/{slug}` on the API.
            parts = [p for p in match_url.split("/") if p]
            if len(parts) >= 4:
                league_slug, season, match_slug = parts[-3], parts[-2], parts[-1]
                await rec.capture(
                    client, f"/v1/matches/by-path/{league_slug}/{season}/{match_slug}"
                )

        # ── Conversation ─────────────────────────────────────────────────────
        trending = await rec.capture(client, "/v1/feed/trending", {"limit": 5})
        if isinstance(trending, dict) and trending.get("items"):
            moot_id = trending["items"][0].get("id")
            if moot_id:
                await rec.capture(client, f"/v1/moots/{moot_id}")
                await rec.capture(client, f"/v1/moots/{moot_id}/replies")

        await rec.capture(client, "/v1/search", {"q": "arsenal", "limit": 5})

        # ── Club news ────────────────────────────────────────────────────────
        await rec.capture(client, "/v1/updates")
        await rec.capture(client, "/v1/clubs/arsenal/news")

    rec.write_manifest()
    total = sum(f.stat().st_size for f in FIXTURES.glob("*.json"))
    print(f"\n{len(rec.entries)} responses recorded · {total // 1024} KB in {FIXTURES}/")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="https://fanmoot.com")
    parser.add_argument(
        "--token",
        default=None,
        help="A Fanmoot access token. Only for re-recording the authenticated "
        "endpoints, and only ever from a throwaway account.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return asyncio.run(record(args.base_url, args.token, args.verbose))


if __name__ == "__main__":
    sys.exit(main())
