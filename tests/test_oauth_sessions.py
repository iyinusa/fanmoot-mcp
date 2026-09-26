"""The pages a fan sees: sign in, sign up, "not you?", and connected apps.

`S4-10`, `S4-11`, `S4-13` and `S4-14`. These are the parts of the milestone a
judge can look at, and the parts a fan can get wrong, so what is asserted here is
mostly behaviour under mistakes: a wrong password, a taken email address, the
wrong person signed in on the tablet, an app that should not be connected any
more.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app import db as app_db
from app.models import AccessGrant, ConsentEvent, UpstreamSession
from app.settings import Settings
from tests import oauth
from tests.conftest import STATIC_CLIENT_SECRET
from tests.replay import Replay


# ── Sign in (S4-10) ──────────────────────────────────────────────────────────
async def test_the_password_goes_to_fanmoot_and_is_not_stored_here(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """HAD-4: this server is not an identity provider. What it keeps is the
    refresh token, encrypted — never the password."""
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)

    await oauth.sign_in(oauth_http, page)

    login_call = next(call for call in fanmoot_auth.calls if call.url.path == "/v1/auth/login")
    assert oauth.PASSWORD in login_call.content.decode()

    async with app_db.get_sessionmaker()() as session:
        row = (await session.execute(select(UpstreamSession))).scalar_one()
    assert oauth.PASSWORD not in row.refresh_token_encrypted
    # Fernet ciphertext, not the token — the one credential here that cannot be
    # hashed because it has to be used (HAD-3).
    assert "upstream-refresh-1" not in row.refresh_token_encrypted


async def test_a_wrong_password_shows_fanmoots_own_sentence(
    oauth_http: httpx.AsyncClient, static_client: str, replay: Replay
) -> None:
    """Fanmoot's message is written for a fan and is better than anything this
    server could invent."""
    replay.stub(
        "/v1/auth/login",
        {"detail": "Email or password is incorrect."},
        method="POST",
        params={"in_body": "true"},
        status=401,
    )
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)

    again = await oauth.sign_in(oauth_http, page)

    assert again.status_code == 400
    assert "Email or password is incorrect." in again.text
    # And the form is still usable: the pending authorization survives.
    assert oauth.flow_token(again.text)


async def test_repeated_failures_lock_the_form(
    oauth_http: httpx.AsyncClient,
    static_client: str,
    replay: Replay,
    oauth_env: Settings,
) -> None:
    """`S4-14`. Counted per address *and* per email, because a distributed
    attempt against one known account is invisible to the address limit."""
    replay.stub(
        "/v1/auth/login",
        {"detail": "Email or password is incorrect."},
        method="POST",
        params={"in_body": "true"},
        status=401,
    )
    _, challenge = oauth.pkce_pair()

    for _ in range(oauth_env.oauth_login_limit + 1):
        page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
        last = await oauth.sign_in(oauth_http, page)

    assert "Too many attempts" in last.text


async def test_a_second_authorization_skips_the_password(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """The browser session cookie. A fan linking two clients in one sitting
    should not type their password twice."""
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
    await oauth.sign_in(oauth_http, page)

    second = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)

    assert "Allow Alexa+ to use your Fanmoot account?" in second.text
    assert "&#64;thegunner" in second.text


async def test_not_you_forgets_the_session_and_keeps_the_request(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """A shared living-room tablet is the normal case for a football account,
    not the exotic one (`S4-10`)."""
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
    consent = await oauth.sign_in(oauth_http, page)

    switched = await oauth_http.get(
        "/oauth/switch", params={"flow_token": oauth.flow_token(consent.text)}
    )

    assert "Sign in to Fanmoot" in switched.text and "Alexa+" in switched.text
    # The request survives: signing in again lands back on consent, not on an
    # expired flow.
    back = await oauth.sign_in(oauth_http, switched)
    assert back.status_code == 200
    assert "Allow Alexa+" in back.text


async def test_a_tampered_flow_token_is_refused(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """The whole reason the pending authorization is signed: none of its
    parameters can be edited between the page that validated them and the page
    that acts on them."""
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
    carried = oauth.flow_token(page.text)

    refused = await oauth_http.post(
        "/oauth/login",
        data={"flow": carried[:-4] + "AAAA", "email": oauth.EMAIL, "password": oauth.PASSWORD},
    )

    assert refused.status_code == 400
    assert "start again" in refused.text.lower()


# ── Sign up inside the flow (S4-11) ──────────────────────────────────────────
async def test_a_brand_new_fan_signs_up_and_carries_straight_on(
    oauth_http: httpx.AsyncClient, static_client: str, replay: Replay
) -> None:
    """The creator's explicit requirement: "Alexa, post to Fanmoot" to a live
    account without leaving the flow."""
    replay.stub(
        "/v1/auth/register",
        {
            "user": {
                "id": "9a7c5d21-4b3e-4f10-8d22-77e1c0a4b912",
                "email": "new@example.com",
                "handle": "newfan",
                "name": "New Fan",
                "email_verified": False,
            },
            "tokens": {
                "access_token": "upstream-access-1",
                "token_type": "Bearer",
                "expires_in": 900,
                "refresh_token": "upstream-refresh-1",
            },
        },
        method="POST",
        params={"in_body": "true"},
    )
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)

    consent = await oauth_http.post(
        "/oauth/signup",
        data={
            "flow": oauth.flow_token(page.text),
            "email": "new@example.com",
            "password": "correct horse battery staple",
            "name": "New Fan",
            "date_of_birth": "1990-08-14",
            "country_code": "GB-ENG",
        },
    )

    assert consent.status_code == 200
    assert "Allow Alexa+ to use your Fanmoot account?" in consent.text
    # And the fan is told about the verification email, rather than wondering
    # later where their first take went.
    assert "Check your email" in consent.text

    granted = await oauth.approve(oauth_http, consent)
    assert granted.status_code == 302
    assert "code" in oauth.query_of(granted)


async def test_the_signup_page_is_not_the_dcr_endpoint(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """HAD-5's naming collision, as a test: ``/oauth/register`` is RFC 7591 and
    answers JSON; the fan-facing page is ``/oauth/signup``."""
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)

    fan_page = await oauth_http.get(
        "/oauth/signup", params={"flow_token": oauth.flow_token(page.text)}
    )
    dcr = await oauth_http.post(
        "/oauth/register", json={"redirect_uris": ["https://ok.example/cb"]}
    )

    assert "Create your Fanmoot account" in fan_page.text
    assert dcr.headers["content-type"].startswith("application/json")


async def test_prompt_create_opens_the_flow_on_the_signup_form(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """A client that knows the fan is new — the demo's "New here" — asks for
    ``prompt=create`` and the fan lands on Register, not on a password form."""
    _, challenge = oauth.pkce_pair()

    page = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, prompt="create"
    )

    assert page.status_code == 200
    assert "Create your Fanmoot account" in page.text
    # The flow token is carried, so the sign-up posts into the same request.
    assert oauth.flow_token(page.text)


async def test_prompt_create_is_not_skipped_for_a_browser_already_signed_in(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """Asked to create an account, the server must not walk the fan to consent
    as whoever last signed in on this tablet."""
    _, challenge = oauth.pkce_pair()
    first = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
    await oauth.sign_in(oauth_http, first)

    again = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)
    fresh = await oauth.authorize(
        oauth_http, client_id=static_client, challenge=challenge, prompt="create"
    )

    assert "Allow Alexa+ to use your Fanmoot account?" in again.text
    assert "Create your Fanmoot account" in fresh.text


async def test_fanmoots_own_refusal_is_shown_on_the_signup_form(
    oauth_http: httpx.AsyncClient, static_client: str, replay: Replay
) -> None:
    replay.stub(
        "/v1/auth/register",
        {"detail": "That email address cannot be used."},
        method="POST",
        params={"in_body": "true"},
        status=409,
    )
    _, challenge = oauth.pkce_pair()
    page = await oauth.authorize(oauth_http, client_id=static_client, challenge=challenge)

    refused = await oauth_http.post(
        "/oauth/signup",
        data={
            "flow": oauth.flow_token(page.text),
            "email": "taken@example.com",
            "password": "correct horse battery staple",
            "name": "Someone",
            "date_of_birth": "1990-08-14",
        },
    )

    assert "That email address cannot be used." in refused.text
    # The form keeps what was typed, minus the password.
    assert "taken@example.com" in refused.text
    assert "correct horse battery staple" not in refused.text


# ── Connected apps and revocation (S4-13) ────────────────────────────────────
async def test_the_connected_apps_page_lists_every_client_alike(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """Alexa and something that registered itself two minutes ago appear the
    same way, because to a fan they are the same thing."""
    await oauth.linked(oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET)
    registered = (
        await oauth_http.post(
            "/oauth/register",
            json={"client_name": "Claude", "redirect_uris": ["http://localhost:9/cb"]},
        )
    ).json()
    await oauth.linked(
        oauth_http,
        client_id=registered["client_id"],
        client_secret=None,
        redirect_uri="http://localhost:9/cb",
    )

    page = await oauth_http.get("/oauth/connected")

    assert page.status_code == 200
    assert "Alexa+" in page.text
    assert "Claude" in page.text
    assert "self-registered" in page.text


async def test_disconnecting_revokes_the_grant_and_records_it(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    await oauth.linked(oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET)
    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()

    done = await oauth_http.post("/oauth/connected/disconnect", data={"grant_id": str(grant.id)})

    assert done.status_code == 302
    async with app_db.get_sessionmaker()() as session:
        after = (await session.execute(select(AccessGrant))).scalar_one()
        events = (await session.execute(select(ConsentEvent))).scalars().all()
    assert after.revoked_at is not None
    assert after.access_token_hash is None
    assert "revoked" in {event.action for event in events}


async def test_disconnecting_somebody_elses_grant_does_nothing(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """A grant id is not a secret. Without the ownership check this endpoint
    would disconnect anybody's app for anybody."""
    import uuid as uuid_module

    await oauth.linked(oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET)

    await oauth_http.post(
        "/oauth/connected/disconnect", data={"grant_id": str(uuid_module.uuid4())}
    )

    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()
    assert grant.revoked_at is None


async def test_the_connected_page_asks_for_a_sign_in_first(
    oauth_http: httpx.AsyncClient, oauth_db: object
) -> None:
    page = await oauth_http.get("/oauth/connected")

    assert page.status_code == 200
    assert "Sign in to manage connected apps" in page.text


async def test_signing_out_leaves_connected_apps_alone(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """Worth the distinction on a page about revoking access."""
    await oauth.linked(oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET)

    out = await oauth_http.get("/oauth/signout")
    after = await oauth_http.get("/oauth/connected")

    assert "still connected" in out.text
    assert "Sign in to manage connected apps" in after.text
    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()
    assert grant.revoked_at is None


# ── RFC 7009 (S4-13) ─────────────────────────────────────────────────────────
async def test_a_client_can_revoke_its_own_token(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    from app.oauth import grants as grant_store

    body = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )

    revoked = await oauth_http.post(
        "/oauth/revoke",
        data={
            "token": body["access_token"],
            "client_id": static_client,
            "client_secret": STATIC_CLIENT_SECRET,
        },
    )

    assert revoked.status_code == 200
    async with app_db.get_sessionmaker()() as session:
        assert await grant_store.resolve_access_token(session, body["access_token"]) is None


async def test_revoking_a_refresh_token_takes_the_whole_grant(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    body = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )

    await oauth_http.post(
        "/oauth/revoke",
        data={
            "token": body["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": static_client,
            "client_secret": STATIC_CLIENT_SECRET,
        },
    )

    rotated = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=body["refresh_token"],
    )
    assert rotated.status_code == 400


async def test_revoking_an_unknown_token_still_answers_200(
    oauth_http: httpx.AsyncClient, static_client: str
) -> None:
    """RFC 7009 says so, and it is also right: a caller must not be able to use
    this endpoint to discover whether a token is live."""
    response = await oauth_http.post(
        "/oauth/revoke",
        data={
            "token": "never-existed",
            "client_id": static_client,
            "client_secret": STATIC_CLIENT_SECRET,
        },
    )

    assert response.status_code == 200


async def test_a_client_cannot_revoke_another_clients_token(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    body = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )
    other = (
        await oauth_http.post(
            "/oauth/register",
            json={"client_name": "Nosy", "redirect_uris": ["http://localhost:9/cb"]},
        )
    ).json()

    response = await oauth_http.post(
        "/oauth/revoke", data={"token": body["access_token"], "client_id": other["client_id"]}
    )

    assert response.status_code == 200
    async with app_db.get_sessionmaker()() as session:
        grant = (await session.execute(select(AccessGrant))).scalar_one()
    assert grant.revoked_at is None


# ── One live grant per client and fan ────────────────────────────────────────
async def test_re_linking_supersedes_the_previous_grant(
    oauth_http: httpx.AsyncClient, static_client: str, fanmoot_auth: Replay
) -> None:
    """So the connected-apps page lists each client once, and disconnecting
    there takes away everything that client holds."""
    first = await oauth.linked(
        oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET
    )
    await oauth.linked(oauth_http, client_id=static_client, client_secret=STATIC_CLIENT_SECRET)

    async with app_db.get_sessionmaker()() as session:
        grants = (await session.execute(select(AccessGrant))).scalars().all()
        live = [grant for grant in grants if grant.revoked_at is None]

    assert len(grants) == 2
    assert len(live) == 1
    assert {g.revoked_reason for g in grants if g.revoked_at} == {"superseded_by_new_grant"}

    stale = await oauth.refresh(
        oauth_http,
        client_id=static_client,
        client_secret=STATIC_CLIENT_SECRET,
        refresh_token=first["refresh_token"],
    )
    assert stale.status_code == 400


@pytest.mark.parametrize("path", ["/authorize", "/oauth/connected", "/oauth/signout"])
async def test_every_page_refuses_to_be_cached_or_framed(
    oauth_http: httpx.AsyncClient, static_client: str, path: str
) -> None:
    """A consent screen in someone's browser cache, or in an invisible iframe on
    another site, is the same mistake twice."""
    response = await oauth_http.get(path, params={"client_id": static_client})

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
