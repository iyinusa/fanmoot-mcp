"""Settings — the refusals, which are the part that has to hold."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.settings import Settings


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://u:p@localhost:5432/fanmoot_mcp",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_database_url_is_required() -> None:
    """Empty is refused, not defaulted. Compose injects `${FMCP_DATABASE_URL:-}`,
    so an unset variable arrives as "" rather than absent — and a server that
    boots without the database holding its grants fails later, further from the
    cause."""
    with pytest.raises(ValidationError, match="FMCP_DATABASE_URL is required"):
        Settings(database_url="")  # type: ignore[call-arg]


def test_database_url_must_be_asyncpg() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+asyncpg driver"):
        Settings(database_url="sqlite+aiosqlite:///./dev.db")  # type: ignore[call-arg]


def test_base_urls_lose_their_trailing_slash() -> None:
    """A stray slash propagates into the OAuth discovery documents, where an
    issuer differing by one character is an authorization failure with no
    useful error."""
    settings = _settings(
        public_base_url="https://mcp.fanmoot.com/",
        fanmoot_api_base_url="https://fanmoot.com/",
    )
    assert settings.public_base_url == "https://mcp.fanmoot.com"
    assert settings.url_for("/authorize") == "https://mcp.fanmoot.com/authorize"
    assert settings.fanmoot_url_for("v1/clubs") == "https://fanmoot.com/v1/clubs"


def test_blank_env_var_falls_back_to_the_default() -> None:
    """Dokploy's UI writes "" for a variable added but not filled in, and a
    typed field cannot parse that."""
    settings = _settings(db_pool_size="", fanmoot_timeout_s="", db_echo="")  # type: ignore[arg-type]
    assert settings.db_pool_size == 5
    assert settings.fanmoot_timeout_s == 8.0
    assert settings.db_echo is False


def test_grant_storage_is_off_without_a_key() -> None:
    """No Fernet key means grants are refused, not stored in plaintext (HAD-3).
    The read tools are unaffected, which is why this is a posture rather than a
    boot failure."""
    assert _settings(secret_encryption_key="").can_store_grants is False
    assert _settings(secret_encryption_key="   ").can_store_grants is False
    assert _settings(secret_encryption_key="a-real-fernet-key").can_store_grants is True
