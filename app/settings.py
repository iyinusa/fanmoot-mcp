"""Configuration for the Fanmoot MCP server.

Every value is read from the environment with the ``FMCP_`` prefix (so
``fanmoot_api_base_url`` comes from ``FMCP_FANMOOT_API_BASE_URL``) and falls
back to the default declared here. A bare ``docker run`` with nothing set boots
into a *development* posture; production is configured through the Dokploy UI,
which passes environment variables rather than writing a file.

The prefix is deliberately not ``FM_``: this service and ``fanmoot`` are
deployed side by side on the same host, and a shared prefix would let one
service's variable silently configure the other.

**Defaults are safe, not convenient.** Anything dangerous when forgotten
defaults to off or empty — most importantly ``secret_encryption_key``: without
it the server refuses to store a fan's upstream Fanmoot credentials at all,
rather than storing them in plaintext (HAD-3).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FMCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Environment & identity ───────────────────────────────────────────────
    env: str = "development"
    service_name: str = "fanmoot-mcp"

    # Where *this* server lives. The single source of truth for every absolute
    # URL it emits: the OAuth discovery documents, the `issuer` claim, the
    # redirect back from the consent screen, and the resource identifier a
    # client checks its token against. No trailing slash.
    public_base_url: str = "http://localhost:8080"

    # ── Upstream Fanmoot (HAD-1) ─────────────────────────────────────────────
    # The public API this server is a client of. It is never a database URL:
    # there is no path from here into Fanmoot's schema, by design.
    fanmoot_api_base_url: str = "https://fanmoot.com"
    # A voice turn has a budget measured in a couple of seconds, so the upstream
    # timeout is short and the caller is told the truth rather than left waiting.
    fanmoot_timeout_s: float = 8.0

    # ── This server's own database (OAuth state only — HAD-3) ────────────────
    # PostgreSQL only, in every environment, and **required**. Seven tables,
    # none of them Fanmoot's. `validate_default=True` matters: pydantic skips
    # validating defaults, so without it an *absent* FMCP_DATABASE_URL would
    # slip through as "" and fail later with an opaque engine error.
    #
    #   postgresql+asyncpg://user:pass@host:5432/fanmoot_mcp?ssl=require
    database_url: str = Field(
        default="",
        validate_default=True,
        description="Async SQLAlchemy URL for this server's OAuth database. PostgreSQL only.",
    )
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # ── Secrets ──────────────────────────────────────────────────────────────
    # The Fernet key that wraps a fan's upstream Fanmoot refresh token at rest
    # (HAD-3). Empty is a *working* configuration for everything that does not
    # need a grant — the read tools, which take no authentication at all — and a
    # refusal for everything that does. That is the safe direction: a server
    # missing its key serves football answers and declines to post, rather than
    # writing credentials somewhere a leak would matter.
    #
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    secret_encryption_key: str = ""

    # ── Validators ───────────────────────────────────────────────────────────
    @field_validator(
        "fanmoot_timeout_s",
        "db_pool_size",
        "db_max_overflow",
        "db_echo",
        mode="before",
    )
    @classmethod
    def _blank_to_default(cls, v: object, info: object) -> object:
        """Treat a blank env var (``FMCP_DB_POOL_SIZE=``) as unset.

        Dokploy's UI writes an empty string for a variable that has been added
        but not filled in, and a typed field cannot parse ``""``. Falling back
        to the declared default is the intent in every such case.
        """
        if isinstance(v, str) and v.strip() == "":
            return cls.model_fields[info.field_name].default  # type: ignore[attr-defined]
        return v

    @field_validator("public_base_url", "fanmoot_api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        """No trailing slash, so ``f"{base}/v1/clubs"`` never yields a double slash.

        A stray slash here would propagate into the OAuth discovery documents,
        where an issuer that differs from the one a client stored by a single
        character is an authorization failure with no useful error.
        """
        return v.rstrip("/")

    @field_validator("database_url", mode="before")
    @classmethod
    def _require_postgres(cls, v: object) -> object:
        """Require a PostgreSQL URL, in every environment.

        ``docker-compose.yml`` injects ``FMCP_DATABASE_URL: ${FMCP_DATABASE_URL:-}``
        so an unset variable arrives as ``""`` rather than absent. That is
        refused here rather than defaulted: there is no second engine to fall
        back to, and a service that boots without the database holding its
        OAuth grants is a service that will fail later, further from the cause.
        """
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                "FMCP_DATABASE_URL is required and must point at PostgreSQL. Expected: "
                "postgresql+asyncpg://USER:PASSWORD@HOST:5432/fanmoot_mcp "
                "(append ?ssl=require for a managed remote database)"
            )
        url = v.strip()
        if not url.startswith(("postgresql+asyncpg://", "postgres+asyncpg://")):
            driver = url.split("://")[0]
            raise ValueError(
                f"FMCP_DATABASE_URL must use the postgresql+asyncpg driver, got {driver!r}. "
                "PostgreSQL is the only supported database. Expected: "
                "postgresql+asyncpg://USER:PASSWORD@HOST:5432/fanmoot_mcp"
            )
        return url

    # ── Derived views ────────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() == "production"

    @property
    def can_store_grants(self) -> bool:
        """Whether a fan's upstream credentials can be encrypted at rest.

        False means the authenticated half of the surface is closed. Reported by
        ``/healthz`` so "the write tools are off" is something an operator reads
        rather than discovers from a failed grant.
        """
        return bool(self.secret_encryption_key.strip())

    def url_for(self, path: str) -> str:
        """Absolute URL on *this* server — the OAuth documents are full of them."""
        return f"{self.public_base_url}/{path.lstrip('/')}"

    def fanmoot_url_for(self, path: str) -> str:
        """Absolute URL on the *upstream* Fanmoot API."""
        return f"{self.fanmoot_api_base_url}/{path.lstrip('/')}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook: drop the cached Settings so new env vars take effect."""
    get_settings.cache_clear()
