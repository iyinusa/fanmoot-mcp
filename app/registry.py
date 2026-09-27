"""``/.well-known/mcp-registry-auth``: proving to the MCP registry that we own this host (`S7-11`).

The registry lets a publisher claim a reverse-DNS namespace by serving a public
key from the domain it names. Serving it from ``mcp.fanmoot.com`` claims
``com.fanmoot.mcp/*``, which is where ``server.json`` publishes. That needs no
DNS change on fanmoot.com and no deploy of fanmoot.

The value is the one line ``mcp-publisher`` asks for
(``v=MCPv1; k=ed25519; p=<public key>``) and comes from
``FMCP_REGISTRY_AUTH``. It is a public key, so there is nothing secret to leak.
Unset, the path is a plain 404, and the server claims nothing.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

from app.settings import get_settings

router = APIRouter(tags=["registry"])


@router.get("/.well-known/mcp-registry-auth", include_in_schema=False)
async def registry_auth() -> Response:
    proof = get_settings().registry_auth.strip()
    if not proof:
        return PlainTextResponse("Not Found", status_code=404)
    return PlainTextResponse(proof + "\n", headers={"Cache-Control": "no-store"})


@router.get("/.well-known/openai-apps-challenge", include_in_schema=False)
async def openai_apps_challenge() -> Response:
    """The same proof for ChatGPT's app directory: the submission form's MCP tab
    issues a token and checks this host serves it. From
    ``FMCP_OPENAI_APPS_CHALLENGE``; unset, a 404."""
    token = get_settings().openai_apps_challenge.strip()
    if not token:
        return PlainTextResponse("Not Found", status_code=404)
    return PlainTextResponse(token, headers={"Cache-Control": "no-store"})
