"""Replay the recordings against production and fail if a shape drifted (`S7-8`).

    make verify-live
    make verify-live ARGS="--base-url https://staging.fanmoot.com -v"

Every non-synthetic entry in ``fixtures/manifest.json`` is requested again from
the live API, with the same path and parameters. See ``app/fanmoot/drift.py``
for the two tests each answer is put to.

Exit status:

* **0**: every answer parses, and nothing the recordings had is missing.
* **1**: an answer no longer parses into the mirror its wrapper uses, or a
  request that answered 200 now answers something else. A tool will break;
  fix the mirror before the next deploy.
* **3**: everything parses but fields have moved. Nothing is broken yet, but
  re-record with ``make record``.

A recording of one particular match or moot that now answers 404 is reported
as *gone* and does not fail the run: that moment has passed, and a re-record
picks a new one.

Read-only GETs, one at a time, with this server's own User-Agent, so it is safe
to run as often as you like. It runs locally, not in CI, so it costs no
Action minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fanmoot.drift import names_one_thing, shape_drift, validator_for  # noqa: E402

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


async def verify(base_url: str, verbose: bool) -> int:
    entries = [
        e
        for e in json.loads((FIXTURES / "manifest.json").read_text())
        if not e.get("synthetic") and e["method"] == "GET"
    ]
    broken: list[str] = []
    drifted: list[str] = []
    gone: list[str] = []

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=20.0,
        headers={"User-Agent": "fanmoot-mcp/verify-live", "Accept": "application/json"},
    ) as client:
        for entry in entries:
            path, params = entry["path"], entry.get("params") or {}
            label = path + (f" {params}" if params else "")
            response = await client.get(path, params=params or None)

            if response.status_code != entry["status"]:
                if response.status_code == 404 and names_one_thing(path):
                    gone.append(label)
                    if verbose:
                        print(f"  gone     {label}")
                    continue
                broken.append(f"{label}: was {entry['status']}, now {response.status_code}")
                continue

            try:
                live: Any = response.json()
            except ValueError:
                broken.append(f"{label}: answered with something that is not JSON")
                continue

            validate = validator_for(path)
            if validate is not None:
                try:
                    validate(live)
                except ValidationError as exc:
                    fields = ", ".join(".".join(map(str, e["loc"])) for e in exc.errors()[:5])
                    broken.append(f"{label}: no longer parses ({fields})")
                    continue

            recorded = json.loads((FIXTURES / entry["file"]).read_text())
            moved = shape_drift(recorded, live)
            if moved:
                drifted.append(f"{label}: " + "; ".join(moved[:5]))
            if verbose:
                print(f"  {'drifted' if moved else 'ok':<8} {label}")

    print(f"\n{len(entries)} recordings replayed against {base_url}")
    for title, rows in (("BROKEN", broken), ("DRIFTED", drifted), ("GONE", gone)):
        if rows:
            print(f"\n{title} ({len(rows)})")
            for row in rows:
                print(f"  {row}")
    if broken:
        return 1
    if drifted:
        return 3
    print("\nevery shape this server reads is still there.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--base-url", default="https://fanmoot.com")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    return asyncio.run(verify(args.base_url.rstrip("/"), args.verbose))


if __name__ == "__main__":
    sys.exit(main())
