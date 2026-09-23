"""``/privacy`` and ``/terms`` (`S7-12`).

This server runs an OAuth authorization server that holds fans' Fanmoot
sessions, so it owes them a plain statement of what it keeps, for how long, and
how to take it back. Not because a store listing asks for one — the Alexa+
manifest that would have was cut (§2.0) — but because the consent screen asks a
fan to trust it, and links here.

The retention figures on the page are read from ``app.audit`` and the token
lifetimes from ``Settings``, rather than typed into the template. So the page
cannot promise ninety days while the code keeps a year.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app import __version__, audit
from app.oauth import flow
from app.settings import get_settings

router = APIRouter(tags=["legal"])

#: The OAuth pages' templates, because these pages wear the same shell.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "oauth" / "templates"))

#: When the wording last changed. Bump it with the text.
UPDATED = "23 September 2026"

#: Public pages, so a short cache is fine, and nothing on them is personal.
_HEADERS = {"Cache-Control": "public, max-age=3600"}


def _context() -> dict[str, object]:
    settings = get_settings()
    return {
        "version": __version__,
        "updated": UPDATED,
        "support_email": settings.support_email,
        "public_base_url": settings.public_base_url,
        "access_hours": settings.oauth_access_token_ttl_s // 3600,
        "refresh_days": settings.oauth_refresh_token_ttl_days,
        "dcr_days": settings.oauth_dcr_client_ttl_days,
        "session_minutes": flow.SESSION_TTL_S // 60,
        "tool_call_days": audit.TOOL_CALL_RETENTION_DAYS,
        "consent_days": audit.CONSENT_RETENTION_DAYS,
        "mood_uses_bedrock": settings.mood_uses_bedrock,
    }


@router.get("/privacy", include_in_schema=False)
async def privacy(request: Request) -> Response:
    return templates.TemplateResponse(request, "privacy.html", _context(), headers=_HEADERS)


@router.get("/terms", include_in_schema=False)
async def terms(request: Request) -> Response:
    return templates.TemplateResponse(request, "terms.html", _context(), headers=_HEADERS)
