"""``GET /healthz`` — what this server is, and whether the two things it needs
are answering.

Four facts, because four things can be wrong and each one fails differently:

* **version** — which build is actually deployed.
* **MCP protocol version** — read from the SDK, never hard-coded. This is the
  number the track turns on (HAD-2): a dependency bump that quietly moved us to
  the modern era would show up here, and in ``S2-4``'s test, rather than in a
  judge's failed handshake.
* **upstream Fanmoot** — this server is a client of ``fanmoot.com/v1`` (HAD-1).
  When that is unreachable there is nothing to say about football, and the
  cause is not in this repo.
* **database** — this server's own OAuth store (HAD-3). When it is down the
  read tools still work and only the authenticated half is affected.

**It always answers 200.** Docker's HEALTHCHECK and Traefik act on the status
code, and restarting this container does not fix an unreachable ``fanmoot.com``
— it turns a recoverable dependency failure into a restart loop. So liveness is
the status code and dependency health is the body: ``status`` is ``degraded``
when something is wrong, and an operator or a smoke test reads the field.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter
from mcp.types import LATEST_PROTOCOL_VERSION

from app import __version__
from app.db import ping
from app.logging_config import get_logger
from app.settings import get_settings

router = APIRouter(tags=["health"])
log = get_logger("health")

#: Bounded hard. The probe runs on a schedule and must never be the reason a
#: request is slow, so it gives up well inside Docker's 5-second timeout.
_UPSTREAM_PROBE_TIMEOUT_S = 2.0


async def _upstream_reachable(base_url: str) -> bool:
    """Whether Fanmoot's API answers.

    Its own ``/healthz`` is the right target: it is liveness on that side too,
    touches none of its dependencies, and is exempt from its canonical-host
    redirect — so this probe reads *that* service's health rather than the
    reverse proxy's opinion of it.

    A short-lived client rather than the shared one from ``S1-1``, which does
    not exist yet. When it does, this becomes a call through it so the probe
    exercises the same connection pool the tools use.
    """
    try:
        async with httpx.AsyncClient(timeout=_UPSTREAM_PROBE_TIMEOUT_S) as client:
            response = await client.get(f"{base_url}/healthz")
        return response.status_code == httpx.codes.OK
    except Exception as exc:
        # The message names the failure and nothing else; the base URL is
        # already public and no credential is involved in this call.
        log.warning("health_upstream_unreachable", error=str(exc))
        return False


@router.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, Any]:
    settings = get_settings()
    checks: dict[str, str] = {}

    upstream_up = await _upstream_reachable(settings.fanmoot_api_base_url)
    checks["fanmoot_api"] = "ok" if upstream_up else "unreachable"

    try:
        await ping()
        checks["database"] = "ok"
    except Exception as exc:
        # Says what failed and nothing a caller could not learn by trying to
        # connect themselves. The connection string, which carries the
        # password, is never included.
        log.warning("health_database_unreachable", error=str(exc))
        checks["database"] = "unavailable"

    # Not a dependency — a posture. Without the Fernet key the read tools are
    # unaffected and the write tools cannot accept a grant (HAD-3), which is a
    # thing to see on a dashboard rather than discover from a failed sign-in.
    checks["grant_storage"] = "ok" if settings.can_store_grants else "disabled"

    degraded = any(state not in ("ok", "disabled") for state in checks.values())
    return {
        "status": "degraded" if degraded else "ok",
        "service": settings.service_name,
        "version": __version__,
        "env": settings.env,
        "mcp_protocol_version": LATEST_PROTOCOL_VERSION,
        "checks": checks,
    }
