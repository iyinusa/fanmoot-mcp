"""The HTTP surface of the authorization server.

``/authorize``, ``/token``, ``/oauth/revoke``, and the four pages a fan actually
sees: sign in, sign up, consent, and the list of apps they have connected.

Read the flow in order and the shape of this module follows:

1. **``GET /authorize``** validates everything the client sent, and the order it
   validates in is the security-relevant part. The ``client_id`` and
   ``redirect_uri`` come first, and a failure in either renders a page **here** —
   never a redirect, because the only place to redirect to is a URI the caller
   supplied, and a server that bounced errors to an unvalidated URI is an open
   redirector that will forward a code next (`S4-5`). Everything after that is
   redirected back to the client with ``error`` and ``state``, as RFC 6749 says.
2. **Sign in, or sign up** (`S4-10`, `S4-11`). This server is not an identity
   provider: it posts the fan's credentials to Fanmoot's own ``/v1/auth/login``,
   or creates the account with ``/v1/auth/register``, and holds the refresh token
   that comes back encrypted (HAD-3, HAD-4). A brand new fan can go from "Alexa,
   post to Fanmoot" to a live account without leaving the flow — the creator's
   explicit requirement.
3. **Consent** names exactly what the app may do, per scope, with a checkbox
   each. Narrowing is real: the grant records what was ticked, not what was
   asked for, and a client that later needs more is upgraded through `S4-12`
   rather than failing.
4. **``POST /token``** redeems the code or rotates the refresh token, and is the
   one endpoint here with no HTML at all.

The pending authorization is carried between steps as a signed token in a hidden
field (``app.oauth.flow``), so none of those parameters can be edited between the
page that validated them and the page that acts on them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from app import __version__
from app.audit import consent as record_consent
from app.db import get_session
from app.fanmoot.client import FanmootError, FanmootRateLimited, FanmootUnauthorized
from app.logging_config import get_logger
from app.models.audit import (
    CONSENT_DENIED,
    CONSENT_GRANTED,
    CONSENT_REVOKED,
    CONSENT_SIGNED_UP,
    CONSENT_UPGRADED,
)
from app.models.client import OAuthClient
from app.models.grant import REVOKED_BY_FAN, UpstreamSession
from app.oauth import clients as client_store
from app.oauth import flow, pkce, ratelimit, scopes, tokens, upstream
from app.oauth import grants as grant_store
from app.oauth.errors import (
    NO_STORE,
    AccessDenied,
    FlowRefusal,
    InvalidRequest,
    InvalidScope,
    OAuthError,
    TemporarilyUnavailable,
    UnauthorizedClient,
    UnsupportedGrantType,
    redirect_error,
)
from app.settings import Settings, get_settings

router = APIRouter(tags=["oauth"])
log = get_logger("oauth.router")

#: The four pages a fan sees, plus the two notices. Rendered from this package
#: rather than from a shared template root: they are part of the OAuth surface,
#: and a server with no OAuth would not have them.
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

#: Applied to every HTML page here. These pages carry a form that posts a
#: password and a signed flow token; a cached copy on a shared machine is a
#: consent screen somebody else can submit.
_HTML_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


# ── Rendering ────────────────────────────────────────────────────────────────
def _page(
    request: Request, template: str, context: dict[str, Any], *, status: int = 200
) -> Response:
    """One page, with the headers every page here needs."""
    return templates.TemplateResponse(
        request,
        template,
        {"version": __version__, **context},
        status_code=status,
        headers=dict(_HTML_HEADERS),
    )


async def flow_refusal_handler(request: Request, exc: Exception) -> Response:
    """Render a refusal that must not be redirected. Registered in ``app.main``."""
    refusal = exc if isinstance(exc, FlowRefusal) else FlowRefusal("Something went wrong", str(exc))
    return _page(
        request,
        "notice.html",
        {"headline": refusal.headline, "detail": refusal.detail, "tone": "warn"},
        status=refusal.status,
    )


def _set_session_cookie(response: Response, session_token: str, settings: Settings) -> None:
    response.set_cookie(
        flow.SESSION_COOKIE,
        session_token,
        max_age=flow.SESSION_TTL_S,
        httponly=True,
        secure=settings.is_production,
        # Lax, not Strict: a fan arrives here by a cross-site navigation from
        # Alexa or from their client, and Strict would drop the cookie on
        # arrival — signing them out at exactly the wrong moment. Lax still
        # refuses to attach it to a cross-site POST, which is the CSRF case and
        # the reason the disconnect form needs nothing else.
        samesite="lax",
        path="/",
    )


# ── GET /authorize ───────────────────────────────────────────────────────────
@router.get("/authorize", include_in_schema=False)
async def authorize(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """The start of every link. Validates, then shows sign-in or consent."""
    settings = get_settings()
    params = request.query_params
    state = params.get("state")

    if not settings.can_store_grants:
        # No Fernet key: this server will not hold a fan's Fanmoot credentials
        # at all (HAD-3), so there is nothing honest to offer here. The read
        # tools are unaffected, which is why the server is up at all.
        raise FlowRefusal(
            "Account linking is switched off",
            "This server has no encryption key configured, so it cannot hold a Fanmoot "
            "session on your behalf. Football questions still work without linking.",
            status=503,
        )

    client_id = (params.get("client_id") or "").strip()
    client = await client_store.find_or_resolve(session, client_id) if client_id else None
    if client is None or not client.is_active:
        log.warning("oauth_authorize_unknown_client", client_id=client_id[:120])
        raise FlowRefusal(
            "We do not recognise that app",
            "The app that sent you here is not registered with Fanmoot, or its registration "
            "has lapsed. Nothing has been shared. Try connecting again from the app itself.",
        )

    redirect_uri = (params.get("redirect_uri") or "").strip()
    if not redirect_uri:
        raise FlowRefusal(
            "That app did not say where to send you back",
            "A redirect_uri is required. This server never guesses one, because guessing is "
            "how a sign-in ends up delivered to the wrong place.",
        )
    if not client.permits_redirect_uri(redirect_uri):
        # The single most likely cause of a silent account-linking failure
        # (`S4-5`), so it is logged with enough to fix it and shown as a page
        # rather than forwarded to the URI in question.
        log.warning(
            "oauth_redirect_uri_rejected",
            client_id=client.client_id,
            registered=len(client.redirect_uris or []),
        )
        raise FlowRefusal(
            "That return address is not registered",
            f"{client.client_name} asked to be sent back to an address Fanmoot has not "
            "registered for it. Nothing has been shared. If you run this app, register the "
            "exact address with the Fanmoot MCP server.",
        )

    # Past this point the client and its redirect URI are established, so a
    # failure goes back to the client as RFC 6749 specifies.
    if params.get("response_type") != "code":
        return redirect_error(
            redirect_uri,
            error="unsupported_response_type",
            description="This server issues authorization codes only (response_type=code).",
            state=state,
        )

    method = params.get("code_challenge_method") or pkce.PLAIN
    challenge = params.get("code_challenge") or ""
    if method != pkce.S256:
        # An absent method *means* `plain` per RFC 7636, so silence is refused
        # here too — see app.oauth.pkce on why that is a downgrade rather than
        # an omission.
        return redirect_error(
            redirect_uri,
            error="invalid_request",
            description="PKCE is required: send code_challenge with code_challenge_method=S256.",
            state=state,
        )
    if not pkce.valid_challenge(challenge):
        return redirect_error(
            redirect_uri,
            error="invalid_request",
            description="code_challenge must be 43-128 unreserved characters (RFC 7636 §4.1).",
            state=state,
        )

    raw_scope = params.get("scope")
    if unknown := scopes.unknown(raw_scope):
        return redirect_error(
            redirect_uri,
            error="invalid_scope",
            description=f"Unknown scope(s) {unknown}. Supported: {scopes.SUPPORTED}.",
            state=state,
        )
    # A client that asks for nothing is offered everything it registered for, and
    # the fan narrows it below. Asking for more than the registration allows is
    # trimmed to the registration rather than refused — the ceiling is the
    # registration, and a client cannot raise it by asking.
    asked = scopes.parse(raw_scope) or sorted(client.allowed_scopes)
    offered = [name for name in scopes.parse(" ".join(asked)) if name in client.allowed_scopes]
    if not offered:
        return redirect_error(
            redirect_uri,
            error="invalid_scope",
            description=f"This client is registered for: {client.scope or '(nothing)'}.",
            state=state,
        )

    resource = params.get("resource")
    if resource is not None and resource.rstrip("/") != settings.resource_uri.rstrip("/"):
        return redirect_error(
            redirect_uri,
            error="invalid_target",
            description=f"This server issues tokens for {settings.resource_uri} only.",
            state=state,
        )

    pending = flow.Pending(
        client_id=client.client_id,
        client_name=client.client_name,
        redirect_uri=redirect_uri,
        scope=" ".join(offered),
        code_challenge=challenge,
        code_challenge_method=pkce.S256,
        state=state,
        resource=resource,
    )

    # Already signed in on this browser? Then skip the password and go straight
    # to consent — with a "not you?" escape, because a shared device is the
    # normal case for a football account.
    browser = flow.read_session(request.cookies.get(flow.SESSION_COOKIE))
    if browser is not None:
        live = await _live_upstream(session, browser.upstream_session_id)
        if live is not None:
            pending = pending.signed_in_as(upstream_session_id=live.id, handle=live.fanmoot_handle)
            return await _consent_page(request, session, pending)

    return _login_page(request, pending)


async def _live_upstream(
    session: AsyncSession, upstream_session_id: uuid.UUID
) -> UpstreamSession | None:
    row = (
        await session.execute(
            select(UpstreamSession).where(UpstreamSession.id == upstream_session_id)
        )
    ).scalar_one_or_none()
    return row if row is not None and row.is_active else None


def _login_page(
    request: Request, pending: flow.Pending, *, error: str = "", email: str = ""
) -> Response:
    """The sign-in page. The app's name comes from the signed flow token rather
    than from a query: it was validated at ``/authorize`` and cannot have been
    edited since, which is the whole point of carrying it signed."""
    carried = flow.issue(pending)
    return _page(
        request,
        "login.html",
        {
            "flow": carried,
            "error": error,
            "email": email,
            "client_name": pending.client_name or "an app",
            "signup_url": "/oauth/signup?" + urlencode({"flow_token": carried}),
        },
        status=400 if error else 200,
    )


async def _consent_page(
    request: Request,
    session: AsyncSession,
    pending: flow.Pending,
    *,
    error: str = "",
) -> Response:
    """The screen that names exactly what the app may do (`S4-10`)."""
    client = await client_store.find(session, pending.client_id)
    if client is None:  # pragma: no cover - validated on the way in
        raise FlowRefusal("We do not recognise that app", "Try connecting again from the app.")

    requested = scopes.describe(scopes.parse(pending.scope))
    existing = None
    if pending.upstream_session_id is not None:
        live = await _live_upstream(session, pending.upstream_session_id)
        if live is not None:
            existing = await grant_store.active_grant(
                session, client_id=client.client_id, fanmoot_user_id=live.fanmoot_user_id
            )

    return _page(
        request,
        "consent.html",
        {
            "flow": flow.issue(pending),
            "client": client,
            "handle": pending.handle or "",
            "scopes": requested,
            # A re-link says so, and names what is being *added*. A returning fan
            # presented with an identical first-time form learns nothing from it.
            "already_granted": sorted(existing.scopes) if existing is not None else [],
            "fresh_account": pending.fresh_account,
            "error": error,
        },
        status=400 if error else 200,
    )


# ── Sign in (S4-10) ──────────────────────────────────────────────────────────
@router.post("/oauth/login", include_in_schema=False)
async def login(
    request: Request,
    flow_token: str = Form(alias="flow"),
    email: str = Form(""),
    password: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Authenticate the fan against Fanmoot, then show consent."""
    settings = get_settings()
    try:
        pending = flow.read(flow_token)
    except flow.FlowExpired:
        return _expired(request)

    address = ratelimit.client_ip(request)
    normalised = email.strip().lower()

    # Two limits, because they catch different attacks: per address stops one
    # machine working through a password list, per email stops a distributed
    # attempt against one known account that the address limit would never see.
    for bucket, subject in (("oauth.login", address), ("oauth.login.email", normalised)):
        if not subject:
            continue
        if not _login_guard(settings).peek(bucket, subject).allowed:
            return _login_page(
                request,
                pending,
                error="Too many attempts. Wait a minute and try again.",
                email=email,
            )

    if not normalised or not password:
        return _login_page(
            request, pending, error="Enter your Fanmoot email and password.", email=email
        )

    try:
        session_row = await upstream.sign_in(session, email=normalised, password=password)
    except FanmootUnauthorized as exc:
        # Counted as a failure against both buckets, and Fanmoot's own sentence
        # is shown: it is written for a fan and is better than anything invented
        # here ("Email or password is incorrect", not "authentication failed").
        guard = _login_guard(settings)
        guard.hit("oauth.login", address)
        guard.hit("oauth.login.email", normalised)
        return _login_page(request, pending, error=exc.message, email=email)
    except FanmootRateLimited as exc:
        return _login_page(request, pending, error=exc.message, email=email)
    except FanmootError as exc:
        return _login_page(request, pending, error=exc.message, email=email)
    except tokens.SecretsUnavailable:
        raise FlowRefusal(
            "Account linking is switched off",
            "This server has no encryption key configured, so it cannot hold a Fanmoot "
            "session on your behalf.",
            status=503,
        ) from None

    # A successful sign-in clears the address's failure history, so a fan who
    # mistyped twice is not one attempt from a lockout for the next 15 minutes.
    ratelimit.forgive("oauth.login", address)
    ratelimit.forgive("oauth.login.email", normalised)

    pending = pending.signed_in_as(
        upstream_session_id=session_row.id, handle=session_row.fanmoot_handle
    )
    response = await _consent_page(request, session, pending)
    _set_session_cookie(
        response,
        flow.issue_session(
            flow.BrowserSession(
                upstream_session_id=session_row.id,
                fanmoot_user_id=session_row.fanmoot_user_id,
                handle=session_row.fanmoot_handle,
            )
        ),
        settings,
    )
    return response


@router.get("/oauth/signin", include_in_schema=False)
async def signin_page(request: Request, flow_token: str = "") -> Response:
    """The sign-in page for a flow already in progress — the Sign in tab on the
    sign-up form. Distinct from ``/oauth/switch``, which also forgets whoever this
    browser was signed in as."""
    try:
        pending = flow.read(flow_token)
    except flow.FlowExpired:
        return _expired(request)
    return _login_page(request, pending)


@router.get("/oauth/switch", include_in_schema=False)
async def switch(request: Request, flow_token: str = "") -> Response:
    """The "not you?" escape (`S4-10`): forget this browser's session, sign in again.

    Keeps the pending authorization, because the *request* is still valid — it is
    only the fan that was wrong. A shared living-room tablet is the normal case
    for this, not the exotic one.
    """
    try:
        pending = flow.read(flow_token)
    except flow.FlowExpired:
        return _expired(request)
    response = _login_page(request, pending.signed_out())
    response.delete_cookie(flow.SESSION_COOKIE, path="/")
    return response


# ── Sign up inside the flow (S4-11) ──────────────────────────────────────────
@router.get("/oauth/signup", include_in_schema=False)
async def signup_page(request: Request, flow_token: str = "") -> Response:
    """The Register tab. Note the path: ``/oauth/register`` belongs to DCR."""
    try:
        pending = flow.read(flow_token)
    except flow.FlowExpired:
        return _expired(request)
    return _signup_page(request, pending)


def _signup_page(
    request: Request,
    pending: flow.Pending,
    *,
    error: str = "",
    values: dict[str, str] | None = None,
) -> Response:
    carried = flow.issue(pending)
    return _page(
        request,
        "signup.html",
        {
            "flow": carried,
            "error": error,
            "values": values or {},
            "login_url": "/oauth/signin?" + urlencode({"flow_token": carried}),
        },
        status=400 if error else 200,
    )


@router.post("/oauth/signup", include_in_schema=False)
async def signup(
    request: Request,
    flow_token: str = Form(alias="flow"),
    email: str = Form(""),
    password: str = Form(""),
    name: str = Form(""),
    date_of_birth: str = Form(""),
    country_code: str = Form(""),
    handle: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Create a Fanmoot account and carry straight on to consent."""
    settings = get_settings()
    try:
        pending = flow.read(flow_token)
    except flow.FlowExpired:
        return _expired(request)

    values = {
        "email": email,
        "name": name,
        "date_of_birth": date_of_birth,
        "country_code": country_code,
        "handle": handle,
    }

    decision = ratelimit.hit(
        "oauth.signup",
        ratelimit.client_ip(request),
        limit=settings.oauth_register_limit,
        window_s=settings.oauth_register_window_s,
    )
    if not decision.allowed:
        return _signup_page(
            request, pending, error="Too many sign-ups from here. Try again shortly.", values=values
        )

    missing = [
        label
        for label, value in (
            ("your email", email),
            ("a password", password),
            ("your name", name),
            ("your date of birth", date_of_birth),
        )
        if not value.strip()
    ]
    if missing:
        return _signup_page(
            request, pending, error=f"Fanmoot needs {_join(missing)}.", values=values
        )
    try:
        date.fromisoformat(date_of_birth.strip())
    except ValueError:
        return _signup_page(
            request, pending, error="Date of birth should look like 1990-08-14.", values=values
        )

    try:
        session_row, needs_verification = await upstream.sign_up(
            session,
            email=email.strip().lower(),
            password=password,
            name=name.strip(),
            date_of_birth=date_of_birth.strip(),
            country_code=country_code.strip() or None,
            handle=handle.strip() or None,
        )
    except FanmootError as exc:
        # Fanmoot's own refusal, verbatim: "That handle is taken", "That email
        # address cannot be used", the age rule for the country given. Every one
        # of those is better than a generic sentence and is already written for
        # a fan.
        return _signup_page(request, pending, error=exc.message, values=values)

    await record_consent(
        session,
        client_id=pending.client_id,
        client_name=pending.client_name,
        fanmoot_user_id=session_row.fanmoot_user_id,
        fanmoot_handle=session_row.fanmoot_handle,
        action=CONSENT_SIGNED_UP,
        ip=ratelimit.client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    pending = pending.signed_in_as(
        upstream_session_id=session_row.id,
        handle=session_row.fanmoot_handle,
        fresh=needs_verification,
    )
    response = await _consent_page(request, session, pending)
    _set_session_cookie(
        response,
        flow.issue_session(
            flow.BrowserSession(
                upstream_session_id=session_row.id,
                fanmoot_user_id=session_row.fanmoot_user_id,
                handle=session_row.fanmoot_handle,
            )
        ),
        settings,
    )
    return response


# ── Consent ──────────────────────────────────────────────────────────────────
@router.post("/oauth/consent", include_in_schema=False)
async def consent(
    request: Request,
    flow_token: str = Form(alias="flow"),
    decision: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Turn a fan's answer into a code, or into ``access_denied``."""
    settings = get_settings()
    try:
        pending = flow.read(flow_token)
    except flow.FlowExpired:
        return _expired(request)

    if pending.upstream_session_id is None:
        # Consent without a sign-in. Not reachable through the pages; reachable
        # by hand, and the answer is to start again rather than to guess who
        # this is.
        return _expired(request)

    client = await client_store.find(session, pending.client_id)
    upstream_row = await _live_upstream(session, pending.upstream_session_id)
    if client is None or not client.is_active or upstream_row is None:
        raise FlowRefusal(
            "That link cannot be completed",
            "The app or the Fanmoot session behind this request is no longer available. "
            "Start again from the app.",
        )

    address = ratelimit.client_ip(request)
    requested = scopes.parse(pending.scope)

    if decision != "allow":
        await record_consent(
            session,
            client_id=client.client_id,
            client_name=client.client_name,
            fanmoot_user_id=upstream_row.fanmoot_user_id,
            fanmoot_handle=upstream_row.fanmoot_handle,
            action=CONSENT_DENIED,
            scope=" ".join(requested),
            ip=address,
            user_agent=request.headers.get("user-agent"),
        )
        return redirect_error(
            pending.redirect_uri,
            error=AccessDenied.error,
            description="The fan declined.",
            state=pending.state,
        )

    # What was ticked, intersected with what was asked for. The form cannot widen
    # the request: a checkbox naming a scope the client never asked for is
    # dropped here rather than granted.
    form = await request.form()
    ticked = {str(value) for value in form.getlist("scope")}
    approved = [name for name in requested if name in ticked]
    if not approved:
        return await _consent_page(
            request,
            session,
            pending,
            error=(
                "Nothing was selected, so nothing was shared. Tick at least one, or press Not now."
            ),
        )

    existing = await grant_store.active_grant(
        session, client_id=client.client_id, fanmoot_user_id=upstream_row.fanmoot_user_id
    )
    code = await grant_store.issue_code(
        session,
        client=client,
        upstream=upstream_row,
        scope=" ".join(approved),
        redirect_uri=pending.redirect_uri,
        code_challenge=pending.code_challenge,
        code_challenge_method=pending.code_challenge_method,
        resource=pending.resource,
        settings=settings,
    )
    await record_consent(
        session,
        client_id=client.client_id,
        client_name=client.client_name,
        fanmoot_user_id=upstream_row.fanmoot_user_id,
        fanmoot_handle=upstream_row.fanmoot_handle,
        # An existing grant gaining scopes is an upgrade, not a first consent
        # (`S4-12`), and the audit log says which.
        action=CONSENT_UPGRADED if existing is not None else CONSENT_GRANTED,
        scope=" ".join(approved),
        ip=address,
        user_agent=request.headers.get("user-agent"),
    )

    # RFC 9207: `iss` on the redirect, so a client with several authorization
    # servers configured cannot be tricked into redeeming this code at another.
    query = [("code", code), ("iss", settings.public_base_url)]
    if pending.state is not None:
        query.append(("state", pending.state))
    return RedirectResponse(
        _with_query(pending.redirect_uri, query), status_code=302, headers=dict(NO_STORE)
    )


# ── POST /token ──────────────────────────────────────────────────────────────
@router.post("/token", include_in_schema=False)
async def token(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Redeem a code, or rotate a refresh token (`S4-6`, `S4-7`)."""
    settings = get_settings()

    decision = ratelimit.hit(
        "oauth.token",
        ratelimit.client_ip(request),
        limit=settings.oauth_token_limit,
        window_s=settings.oauth_token_window_s,
    )
    if not decision.allowed:
        return TemporarilyUnavailable(
            "Too many token requests from this address.", retry_after=decision.retry_after
        ).response()

    try:
        form = await request.form()
    except Exception:
        return InvalidRequest(
            "The token endpoint takes application/x-www-form-urlencoded."
        ).response()

    def field(name: str) -> str | None:
        value = form.get(name)
        return str(value) if value is not None else None

    grant_type = field("grant_type") or ""

    try:
        client = await client_store.authenticate(
            session,
            client_id=field("client_id"),
            client_secret=field("client_secret"),
            authorization_header=request.headers.get("authorization"),
        )
        if grant_type not in (client.grant_types or []):
            raise UnauthorizedClient(f"This client is not registered for {grant_type!r}.")

        if grant_type == "authorization_code":
            issued = await _exchange_code(session, client, field, settings)
        elif grant_type == "refresh_token":
            issued = await _rotate(session, client, field, settings)
        else:
            raise UnsupportedGrantType(
                "Supported: authorization_code, refresh_token. "
                "This server does not implement the implicit or password grants."
            )
    except OAuthError as exc:
        log.info("oauth_token_refused", error=exc.error, grant_type=grant_type or "(none)")
        return exc.response()

    await client_store.note_grant(session, client)
    await session.commit()

    return JSONResponse(
        {
            "access_token": issued.access_token,
            "token_type": "Bearer",
            "expires_in": issued.expires_in,
            # Always. Mandatory per Alexa's docs (`S4-7`), and without it a fan
            # re-links every hour.
            "refresh_token": issued.refresh_token,
            "scope": issued.scope,
        },
        headers=dict(NO_STORE),
    )


async def _exchange_code(
    session: AsyncSession,
    client: OAuthClient,
    field: Callable[[str], str | None],
    settings: Settings,
) -> grant_store.IssuedTokens:
    code = field("code")
    if not code:
        raise InvalidRequest("code is required for grant_type=authorization_code.")
    verifier = field("code_verifier")
    if not verifier:
        raise InvalidRequest("code_verifier is required: this server requires PKCE (S256).")

    grant = await grant_store.redeem_code(
        session,
        client=client,
        code=code,
        redirect_uri=field("redirect_uri"),
        code_verifier=verifier,
        resource=field("resource"),
        settings=settings,
    )
    return await grant_store.issue_tokens(session, grant, settings=settings)


async def _rotate(
    session: AsyncSession,
    client: OAuthClient,
    field: Callable[[str], str | None],
    settings: Settings,
) -> grant_store.IssuedTokens:
    presented = field("refresh_token")
    if not presented:
        raise InvalidRequest("refresh_token is required for grant_type=refresh_token.")

    # RFC 8707: Alexa sends `resource` on authorize and token and **not** on
    # refresh, which is exactly what this allows — absent means "the one this
    # grant was issued for".
    grant, issued = await grant_store.rotate_refresh(
        session,
        client=client,
        refresh_token=presented,
        resource=field("resource"),
        settings=settings,
    )

    requested = field("scope")
    if requested:
        # RFC 6749 §6 permits narrowing on refresh and forbids widening. Nobody
        # sends this, and refusing a widening attempt is cheaper than reasoning
        # about a token whose scope does not match its grant.
        asked = set(scopes.parse(requested))
        if not asked or not asked.issubset(grant.scopes):
            raise InvalidScope("A refresh may narrow scopes, never add them.")
    return issued


# ── Revocation and the connected-apps page (S4-13) ───────────────────────────
@router.post("/oauth/revoke", include_in_schema=False)
async def revoke(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """RFC 7009. Answers 200 whether or not anything matched.

    That is the specification and it is also the right behaviour: a client
    cleaning up on sign-out should not have to care, and a caller must not be
    able to use this endpoint to discover whether a token is live.
    """
    try:
        form = await request.form()
    except Exception:
        return InvalidRequest("Send application/x-www-form-urlencoded.").response()

    presented = str(form.get("token") or "")
    try:
        client = await client_store.authenticate(
            session,
            client_id=str(form.get("client_id") or "") or None,
            client_secret=str(form.get("client_secret") or "") or None,
            authorization_header=request.headers.get("authorization"),
        )
    except OAuthError as exc:
        return exc.response()

    if presented:
        matched = await grant_store.revoke_presented_token(session, client=client, token=presented)
        log.info("oauth_revocation", client_id=client.client_id, matched=matched)
    return Response(status_code=200, headers=dict(NO_STORE))


@router.get("/oauth/connected", include_in_schema=False)
async def connected(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Every app holding a grant for this fan, and a button to disconnect each.

    Alexa and a self-registered client appear alike, because to a fan they are
    alike: something that can post as them. Getting here needs a Fanmoot sign-in
    on this origin — the same form the consent flow uses, with no pending
    authorization behind it.
    """
    browser = flow.read_session(request.cookies.get(flow.SESSION_COOKIE))
    if browser is None:
        return _page(request, "signin.html", {"error": ""})

    rows = await grant_store.grants_for_fan(session, browser.fanmoot_user_id)
    return _page(
        request,
        "connected.html",
        {
            "handle": browser.handle,
            "grants": [
                {
                    "id": str(grant.id),
                    "name": client.client_name if client is not None else grant.client_id,
                    "tier": client.tier if client is not None else "unknown",
                    "scopes": scopes.describe(sorted(grant.scopes)),
                    "connected_at": grant.created_at,
                    "last_used_at": grant.last_used_at,
                }
                for grant, client in rows
            ],
        },
    )


@router.post("/oauth/connected/signin", include_in_schema=False)
async def connected_signin(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Sign in to manage connected apps — no authorization request involved."""
    settings = get_settings()
    address = ratelimit.client_ip(request)
    if not _login_guard(settings).peek("oauth.login", address).allowed:
        return _page(
            request, "signin.html", {"error": "Too many attempts. Wait a minute."}, status=429
        )

    try:
        session_row = await upstream.sign_in(
            session, email=email.strip().lower(), password=password
        )
    except FanmootError as exc:
        _login_guard(settings).hit("oauth.login", address)
        return _page(request, "signin.html", {"error": exc.message}, status=400)
    except tokens.SecretsUnavailable:
        raise FlowRefusal(
            "Account linking is switched off",
            "This server has no encryption key configured.",
            status=503,
        ) from None

    response = RedirectResponse("/oauth/connected", status_code=302, headers=dict(NO_STORE))
    _set_session_cookie(
        response,
        flow.issue_session(
            flow.BrowserSession(
                upstream_session_id=session_row.id,
                fanmoot_user_id=session_row.fanmoot_user_id,
                handle=session_row.fanmoot_handle,
            )
        ),
        settings,
    )
    return response


@router.post("/oauth/connected/disconnect", include_in_schema=False)
async def disconnect(
    request: Request,
    grant_id: str = Form(""),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Take one app's access away.

    The session cookie is ``SameSite=Lax``, so a cross-site POST does not carry
    it and this form needs no separate CSRF token. The grant is checked against
    the signed-in fan before anything is revoked — a grant id is not a secret,
    and without that check this would disconnect anybody's app for anybody.
    """
    browser = flow.read_session(request.cookies.get(flow.SESSION_COOKIE))
    if browser is None:
        return RedirectResponse("/oauth/connected", status_code=302)

    try:
        target = uuid.UUID(grant_id)
    except ValueError:
        return RedirectResponse("/oauth/connected", status_code=302)

    rows = await grant_store.grants_for_fan(session, browser.fanmoot_user_id)
    for grant, client in rows:
        if grant.id != target:
            continue
        upstream.forget(grant.id)
        await grant_store.revoke_grant(session, grant, reason=REVOKED_BY_FAN, commit=False)
        await record_consent(
            session,
            client_id=grant.client_id,
            client_name=client.client_name if client is not None else "",
            fanmoot_user_id=grant.fanmoot_user_id,
            fanmoot_handle=grant.fanmoot_handle,
            action=CONSENT_REVOKED,
            scope=grant.scope,
            ip=ratelimit.client_ip(request),
            user_agent=request.headers.get("user-agent"),
            commit=False,
        )
        await session.commit()
        break

    return RedirectResponse("/oauth/connected", status_code=302, headers=dict(NO_STORE))


@router.get("/oauth/signout", include_in_schema=False)
async def signout(request: Request) -> Response:
    """Forget this browser's Fanmoot session. Grants are untouched.

    Worth the distinction on a page about revoking access: signing out of *this*
    site does not disconnect anything, and the page says so.
    """
    response = _page(
        request,
        "notice.html",
        {
            "headline": "Signed out of Fanmoot on this device",
            "detail": "Apps you connected are still connected. Sign in again to manage them.",
            "tone": "ok",
        },
    )
    response.delete_cookie(flow.SESSION_COOKIE, path="/")
    return response


# ── Small shared pieces ──────────────────────────────────────────────────────
class _Guard:
    """One configured limit, applied to whichever bucket a caller names.

    A tiny wrapper, and it exists because the login form checks two buckets with
    the same numbers and the alternative is four call sites each repeating the
    same two settings lookups — which is how the per-email limit ends up with the
    per-address window after somebody tunes one of them.
    """

    __slots__ = ("limit", "window_s")

    def __init__(self, limit: int, window_s: int) -> None:
        self.limit = limit
        self.window_s = window_s

    def peek(self, bucket: str, subject: str) -> ratelimit.Decision:
        return ratelimit.peek(bucket, subject, limit=self.limit, window_s=self.window_s)

    def hit(self, bucket: str, subject: str) -> ratelimit.Decision:
        return ratelimit.hit(bucket, subject, limit=self.limit, window_s=self.window_s)


def _login_guard(settings: Settings) -> _Guard:
    """The login limit: counted per address *and* per email address (`S4-14`)."""
    return _Guard(settings.oauth_login_limit, settings.oauth_login_window_s)


def _expired(request: Request) -> Response:
    """What a stale, missing or tampered flow token gets.

    One page for all three: the fix is identical (start again from the app), and
    distinguishing "expired" from "altered" would tell somebody probing which
    one they achieved.
    """
    return _page(
        request,
        "notice.html",
        {
            "headline": "That took a little too long",
            "detail": "Start the connection again from the app you were using. Nothing was shared.",
            "tone": "warn",
        },
        status=400,
    )


def _with_query(url: str, pairs: list[tuple[str, str]]) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(pairs)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), parts.fragment)
    )


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


__all__ = ["flow_refusal_handler", "router"]
