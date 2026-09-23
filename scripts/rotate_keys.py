"""Re-wrap every stored Fanmoot session under the newest encryption key.

    make rotate-keys                          # re-wrap; report anything unreadable
    make rotate-keys ARGS=--revoke-unreadable # …and end the links nothing can open

Step three of the rotation in docs/runbook.md. By then ``FMCP_SECRET_ENCRYPTION_KEY``
holds ``new,old``: the server already writes under the new key and still reads
under both. This moves every row it has not yet rewritten itself onto the new key,
so the old one can then be dropped without signing anybody out.

Safe to run beside the live server and safe to run twice (see
``app.oauth.upstream.rewrap_sessions``). Reads ``FMCP_DATABASE_URL`` and
``FMCP_SECRET_ENCRYPTION_KEY`` from the environment or ``.env``, exactly as
the server does. Inside the container: ``docker compose exec mcp python
scripts/rotate_keys.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import dispose_engine, get_sessionmaker  # noqa: E402
from app.oauth import upstream  # noqa: E402
from app.settings import get_settings  # noqa: E402


async def run(revoke_unreadable: bool) -> int:
    keys = get_settings().encryption_keys
    if not keys:
        print("FMCP_SECRET_ENCRYPTION_KEY is empty: there is nothing to rotate to.")
        return 2
    if len(keys) == 1:
        print("One key configured. Everything is already written under it; nothing to do.")
        print("To rotate, set FMCP_SECRET_ENCRYPTION_KEY=<new>,<old> first. See docs/runbook.md.")

    try:
        async with get_sessionmaker()() as session:
            report = await upstream.rewrap_sessions(session, revoke_unreadable=revoke_unreadable)
    finally:
        await dispose_engine()

    print(f"re-wrapped under key 1   {report.rewrapped}")
    print(f"already moved on         {report.moved_on}")
    print(f"unreadable by any key    {report.unreadable}")
    if revoke_unreadable:
        print(f"revoked (fan reconnects) {report.revoked}")
    elif report.unreadable:
        print("\nSome sessions open with no configured key. Put the retired key back, or")
        print("re-run with --revoke-unreadable to end those links cleanly.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revoke-unreadable", action="store_true")
    args = parser.parse_args()
    return asyncio.run(run(args.revoke_unreadable))


if __name__ == "__main__":
    sys.exit(main())
