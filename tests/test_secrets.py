"""``S7-4``: key rotation without a reconnect, and no credential in a log.

Rotation is the part with a sharp edge. The rewrap runs beside a live server
that is rewriting the same column, so the tests assert the conservative
outcome: what cannot be opened is reported and left alone unless asked, and a
revoked link loses its ciphertext.
"""

from __future__ import annotations

import io
import sys
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncEngine

from app import db as app_db
from app.logging_config import REDACTED, configure_logging, get_logger, redact
from app.models import UpstreamSession
from app.models.audit import ToolCall
from app.models.grant import REVOKED_KEY_RETIRED
from app.oauth import tokens, upstream
from app.settings import Settings, reset_settings_cache
from tests.replay import Replay

OLD, NEW = Fernet.generate_key().decode(), Fernet.generate_key().decode()


def _keys(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FMCP_SECRET_ENCRYPTION_KEY", value)
    reset_settings_cache()
    tokens.reset_cache()


# ── The envelope ─────────────────────────────────────────────────────────────
def test_a_second_key_still_opens_what_the_first_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    _keys(monkeypatch, OLD)
    sealed = tokens.encrypt("upstream-refresh")

    _keys(monkeypatch, f"{NEW},{OLD}")
    assert tokens.decrypt(sealed) == "upstream-refresh"


def test_rotate_moves_a_secret_onto_the_newest_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _keys(monkeypatch, OLD)
    sealed = tokens.encrypt("upstream-refresh")

    _keys(monkeypatch, f"{NEW},{OLD}")
    moved = tokens.rotate(sealed)

    _keys(monkeypatch, NEW)
    assert tokens.decrypt(moved) == "upstream-refresh"
    with pytest.raises(tokens.SecretUnreadable):
        tokens.decrypt(sealed)


def test_a_bad_key_is_named_by_position_never_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    _keys(monkeypatch, f"{NEW},not-a-key")
    with pytest.raises(tokens.SecretsUnavailable) as refused:
        tokens.encrypt("x")

    assert "Key 2" in str(refused.value)
    assert "not-a-key" not in str(refused.value)
    assert NEW not in str(refused.value)


def test_only_the_first_key_signs_the_browser_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    _keys(monkeypatch, NEW)
    alone = tokens.signing_key()
    _keys(monkeypatch, f"{NEW},{OLD}")

    assert tokens.signing_key() == alone


# ── Re-wrapping the store ────────────────────────────────────────────────────
async def _session_under(key: str, monkeypatch: pytest.MonkeyPatch) -> UpstreamSession:
    _keys(monkeypatch, key)
    async with app_db.get_sessionmaker()() as session:
        row = UpstreamSession(
            fanmoot_user_id=uuid.uuid4(),
            fanmoot_handle="thegunner",
            refresh_token_encrypted=tokens.encrypt("upstream-refresh-1"),
        )
        session.add(row)
        await session.commit()
        return row


async def test_every_stored_session_is_rewrapped(
    oauth_env: Settings, oauth_db: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = await _session_under(OLD, monkeypatch)
    _keys(monkeypatch, f"{NEW},{OLD}")

    async with app_db.get_sessionmaker()() as session:
        report = await upstream.rewrap_sessions(session)
    assert (report.rewrapped, report.unreadable) == (1, 0)

    _keys(monkeypatch, NEW)
    async with app_db.get_sessionmaker()() as session:
        stored = await session.get(UpstreamSession, row.id)
        assert stored is not None
        assert tokens.decrypt(stored.refresh_token_encrypted) == "upstream-refresh-1"


async def test_a_session_nothing_opens_is_left_alone_unless_asked(
    oauth_env: Settings,
    oauth_db: AsyncEngine,
    fanmoot_auth: Replay,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = await _session_under(OLD, monkeypatch)
    _keys(monkeypatch, NEW)

    async with app_db.get_sessionmaker()() as session:
        report = await upstream.rewrap_sessions(session)
    assert (report.unreadable, report.revoked) == (1, 0)

    async with app_db.get_sessionmaker()() as session:
        report = await upstream.rewrap_sessions(session, revoke_unreadable=True)
        assert report.revoked == 1
        stored = await session.get(UpstreamSession, row.id)
        assert stored is not None
        assert stored.revoked_reason == REVOKED_KEY_RETIRED
        # The dead credential is not kept as ciphertext either.
        assert stored.refresh_token_encrypted == ""


# ── Unused sessions (S7-12) ──────────────────────────────────────────────────
async def test_a_session_no_app_uses_is_ended_after_its_grace(
    oauth_env: Settings, oauth_db: AsyncEngine, fanmoot_auth: Replay, signed_in: UpstreamSession
) -> None:
    async with app_db.get_sessionmaker()() as session:
        assert await upstream.end_unused_sessions(session, grace_s=3600) == 0
        assert await upstream.end_unused_sessions(session, grace_s=-60) == 1
        stored = await session.get(UpstreamSession, signed_in.id)
        assert stored is not None
        assert stored.revoked_at is not None
        assert stored.refresh_token_encrypted == ""
    # And fanmoot.com was told, so the session ends there as well.
    assert "/v1/auth/logout" in fanmoot_auth.paths()


async def test_a_session_an_app_still_uses_is_kept(
    oauth_db: AsyncEngine, linked_token: str
) -> None:
    async with app_db.get_sessionmaker()() as session:
        assert await upstream.end_unused_sessions(session, grace_s=-60) == 0


# ── Logs ─────────────────────────────────────────────────────────────────────
def test_credentials_are_struck_out_of_an_event() -> None:
    event = redact(
        None,
        "info",
        {
            "event": "token_issued",
            "access_token": "abc",
            "body": {"refresh_token": "def", "scope": "fan.read"},
            "error": "upstream said: Authorization: Bearer eyJhbGciOi.x-y_z",
            "status": 200,
        },
    )

    assert event["access_token"] == REDACTED
    assert event["body"] == {"refresh_token": REDACTED, "scope": "fan.read"}
    assert "eyJhbGciOi" not in event["error"]
    assert event["status"] == 200


def test_the_configured_pipeline_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Through structlog as the server configures it, not the processor alone."""
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    configure_logging("production")
    get_logger("test.secrets").info("grant_linked", refresh_token="plaintext-refresh")

    assert "plaintext-refresh" not in out.getvalue()
    assert REDACTED in out.getvalue()
    # Back to the real stdout before reconfiguring, so later tests do not log
    # into this buffer.
    monkeypatch.undo()
    configure_logging("test")


def test_a_tool_call_record_has_nowhere_to_put_a_body() -> None:
    assert {column.name for column in ToolCall.__table__.columns} == {
        "id",
        "tool",
        "duration_ms",
        "status",
        "authenticated",
        "request_id",
        "grant_id",
        "created_at",
    }
