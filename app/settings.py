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

import json
from functools import lru_cache
from typing import Any

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

    # Where a fan writes about their data. Named on `/privacy` and `/terms`
    # (`S7-12`). Fanmoot's own support address, since this is Fanmoot's service.
    support_email: str = "support@fanmoot.com"

    # The line `mcp-publisher login http` checks for at
    # /.well-known/mcp-registry-auth (`S7-11`): `v=MCPv1; k=ed25519; p=...`. A
    # public key. Empty serves a 404, and nothing is claimed.
    registry_auth: str = ""

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

    # ── MCP transport (S2) ───────────────────────────────────────────────────
    # Browser origins allowed to reach /mcp, comma-separated. A non-browser
    # client — Alexa+, Claude Desktop, Cursor, curl — sends no `Origin` header
    # and is unaffected; this list exists for the ones that do, which in
    # practice means the web demo at alexaplus.fanmoot.com and the MCP
    # Inspector. Empty is the safe default: every browser is refused with a 403
    # and every real client still connects.
    mcp_allowed_origins: str = ""

    # How long a Streamable HTTP session may sit idle before it is closed and
    # its id starts answering 404. Thirty minutes is longer than any voice
    # conversation and short enough that an abandoned session is not memory
    # this process holds all night.
    mcp_session_idle_timeout_s: float = 1800.0

    # Concurrent sessions. Past this a new one is refused with a 503 and the
    # open ones are untouched — a ceiling that sheds load rather than one that
    # takes the process down with it.
    mcp_max_sessions: int = 500

    # ── Cache warming (S7-2) ─────────────────────────────────────────────────
    # Fill the live digest, the next fortnight of fixtures and the catalogue at
    # startup, so the first call after a deploy is not the slow one, then keep
    # them fresh every `warm_interval_s` — under the ten-second live TTL, so a
    # fan never waits on a revalidation this loop could have made. Mostly `304`s.
    # Off in the test suite, which counts every upstream call it makes.
    warm_caches: bool = True
    # 0 warms once at startup and stops.
    warm_interval_s: float = 5.0

    # ── Voice (S3-1) ─────────────────────────────────────────────────────────
    # The zone kick-off times are spoken in. An MCP request carries no locale
    # and no timezone, so a voice surface has to choose one and say which:
    # Fanmoot lists its fixtures in UK time, so that is the default.
    voice_timezone: str = "Europe/London"

    # ── Bedrock: match_mood's sentiment pass (S3-7, HAD-9) ───────────────────
    # `match_mood` is the one read tool this server *computes* rather than
    # fetches: it samples a public match thread and labels it with its own
    # Bedrock call. Credentials come from the standard AWS chain (environment,
    # profile, or the task role in production) rather than from FMCP_ variables,
    # so nothing secret is declared here.
    #
    # An empty model id turns the Bedrock pass off and leaves `match_mood`
    # answering from the offline lexicon in `app.mood` — which is what the test
    # suite runs against, and what a deployment without AWS credentials
    # degrades to rather than failing the tool outright.
    bedrock_region: str = "us-east-1"
    bedrock_model_id: str = "amazon.nova-lite-v1:0"
    # A voice turn cannot wait: past this the mood falls back to the lexicon
    # rather than spending the fan's patience on a better adjective.
    bedrock_timeout_s: float = 4.0
    # How many takes are sampled from a thread. Counts, not quotes — this is
    # the one number in the read surface that is not bounded by HAD-7's cap of
    # twenty, because none of these moots is read aloud.
    mood_sample_size: int = 40

    # ── OAuth 2.1 authorization server (S4, HAD-4) ───────────────────────────
    # The static, pre-seeded clients — tier 1 of HAD-5, which in practice means
    # Alexa. A JSON array, upserted by `client_id` at boot so a redeploy never
    # orphans credentials Alexa already holds:
    #
    #   [{"client_id": "amzn1...", "client_secret": "...", "client_name": "Alexa+",
    #     "redirect_uris": ["https://pitangui.amazon.com/api/skill/link/M2...", ...],
    #     "scopes": "fan.read moot.write predict.write fan.write"}]
    #
    # Empty is a working configuration: every other client self-registers
    # (`S4-16`), so a server with nothing here still serves Claude, ChatGPT,
    # Cursor and the Inspector — it just cannot serve Alexa, which has no DCR.
    oauth_clients: str = ""

    # Access token 1 h, refresh 90 days — `S4-7`. The refresh token is not
    # optional: Alexa's docs make it mandatory, and without one a fan re-links
    # their account every hour.
    oauth_access_token_ttl_s: int = 3600
    oauth_refresh_token_ttl_days: int = 90

    # An authorization code lives 60 seconds and is single-use (`S4-6`). It
    # travels through a browser redirect and is spent immediately by a client
    # that already has it, so a longer life is only ever a longer window for a
    # code left in someone's history to be replayed.
    oauth_code_ttl_s: int = 60

    # How long the browser leg — sign in, then consent — may take before the
    # pending authorization it is carrying is refused (`app.oauth.flow`). Long
    # enough to read a consent screen and find a password manager; short enough
    # that a form left open in a tab overnight cannot be submitted in the
    # morning.
    oauth_flow_ttl_s: int = 900

    # Dynamic Client Registration (`S4-16`). On by default, because a public
    # MCP server that cannot be self-registered against is an Alexa add-on
    # wearing an MCP costume (§2.2). Turn it off and tier 2 closes: only the
    # clients in FMCP_OAUTH_CLIENTS can connect.
    oauth_dcr_enabled: bool = True

    # A self-registered client that never completes a grant is reaped after
    # this many days. Open registration means anybody can create a row; this is
    # what stops the table being a place to leave litter (`S4-16`).
    oauth_dcr_client_ttl_days: int = 30

    # How long a Fanmoot access token minted by the exchange is held in process
    # before a fresh one is fetched (`S4-8`). Fanmoot's own access tokens live
    # 15 minutes; this is deliberately a little under, so a cached token is
    # never handed out inside its last few seconds and then rejected upstream
    # mid-turn.
    oauth_upstream_access_ttl_s: int = 840

    # Rate limits and lockout (`S4-14`), per client address, per window.
    # Deliberately generous for /token — a household behind one NAT refreshing
    # several sessions is legitimate — and tight for the login form and DCR,
    # which are where a password is guessed and where rows are created.
    oauth_login_limit: int = 10
    oauth_login_window_s: int = 900
    oauth_token_limit: int = 120
    oauth_token_window_s: int = 60
    oauth_register_limit: int = 5
    oauth_register_window_s: int = 3600

    # ── Secrets ──────────────────────────────────────────────────────────────
    # The Fernet key that wraps a fan's upstream Fanmoot refresh token at rest
    # (HAD-3). Empty is a *working* configuration for everything that does not
    # need a grant — the read tools, which take no authentication at all — and a
    # refusal for everything that does. That is the safe direction: a server
    # missing its key serves football answers and declines to post, rather than
    # writing credentials somewhere a leak would matter.
    #
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    #
    # **A comma-separated list rotates it** (`S7-4`). The first key encrypts;
    # every key in the list decrypts. So a rotation is: put the new key in
    # front, deploy, run `make rotate-keys` to re-wrap every stored session
    # under it, then drop the old key and deploy again. No fan reconnects. See
    # docs/runbook.md.
    secret_encryption_key: str = ""

    # ── Validators ───────────────────────────────────────────────────────────
    @field_validator(
        "fanmoot_timeout_s",
        "db_pool_size",
        "db_max_overflow",
        "db_echo",
        "mcp_session_idle_timeout_s",
        "mcp_max_sessions",
        "bedrock_timeout_s",
        "mood_sample_size",
        "oauth_access_token_ttl_s",
        "oauth_refresh_token_ttl_days",
        "oauth_code_ttl_s",
        "oauth_flow_ttl_s",
        "oauth_dcr_enabled",
        "oauth_dcr_client_ttl_days",
        "oauth_upstream_access_ttl_s",
        "oauth_login_limit",
        "oauth_login_window_s",
        "oauth_token_limit",
        "oauth_token_window_s",
        "oauth_register_limit",
        "oauth_register_window_s",
        "warm_caches",
        "warm_interval_s",
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

    @field_validator("oauth_clients")
    @classmethod
    def _parse_oauth_clients(cls, v: str) -> str:
        """Refuse malformed JSON at boot rather than at Alexa's first redirect.

        The *shape* of each entry is checked in ``app.oauth.clients``, which
        knows what a client is. What is checked here is only that the variable
        parses at all and is a list — because the failure mode this closes is a
        stray comma in a Dokploy text box producing a server that boots
        healthy, serves football, and cannot link a single account.
        """
        raw = v.strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"FMCP_OAUTH_CLIENTS is not valid JSON ({exc.msg} at position {exc.pos}). "
                'Expected a JSON array: [{"client_id": "...", "redirect_uris": ["..."]}]'
            ) from None
        if not isinstance(parsed, list):
            raise ValueError(
                "FMCP_OAUTH_CLIENTS must be a JSON *array* of client objects, "
                f"got {type(parsed).__name__}."
            )
        return raw

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
        return bool(self.encryption_keys)

    @property
    def encryption_keys(self) -> list[str]:
        """``FMCP_SECRET_ENCRYPTION_KEY`` as keys, newest first.

        One key is the ordinary case. More than one is a rotation in progress:
        the first wraps everything written from now on, and the rest are only
        ever used to open what they wrapped before.
        """
        return [key.strip() for key in self.secret_encryption_key.split(",") if key.strip()]

    @property
    def mood_uses_bedrock(self) -> bool:
        """Whether ``match_mood`` has a model to call.

        False is a working configuration, not a broken one: the tool answers
        from the lexicon and says so in its payload, which is what keeps the
        read surface whole on a laptop with no AWS credentials.
        """
        return bool(self.bedrock_model_id.strip())

    @property
    def mcp_allowed_origin_list(self) -> list[str]:
        """``FMCP_MCP_ALLOWED_ORIGINS`` as origins, with this server's own added.

        A page served from ``public_base_url`` is always allowed: the OAuth
        consent screen and the sign-up form live there, and an operator should
        not have to list their own host to make their own pages work.
        """
        configured = [part.strip() for part in self.mcp_allowed_origins.split(",")]
        return [self.public_base_url, *(origin for origin in configured if origin)]

    @property
    def oauth_client_specs(self) -> list[dict[str, Any]]:
        """``FMCP_OAUTH_CLIENTS`` as a list of raw objects, already known to parse.

        Entries that are not objects are dropped here rather than in the
        seeder: a bare string in that array is a typo, and the seeder's job is
        to validate fields, not to wonder what a string client is.
        """
        if not self.oauth_clients.strip():
            return []
        parsed = json.loads(self.oauth_clients)
        return [entry for entry in parsed if isinstance(entry, dict)]

    @property
    def resource_uri(self) -> str:
        """The canonical URI of the protected resource — RFC 8707's ``resource``.

        The MCP endpoint itself, not this server's root: that is what a client
        is asking for a token *for*, what ``addon.json`` names, and what the
        protected-resource metadata has to echo exactly (`S4-2`). A token
        issued for anything else is refused at the mount.
        """
        return self.url_for("/mcp")

    @property
    def resource_metadata_url(self) -> str:
        """Where a 401 tells a client to look — RFC 9728's path-insertion form.

        The resource is ``…/mcp``, so its metadata lives at
        ``/.well-known/oauth-protected-resource/mcp``. The bare path is served
        as well, because Alexa's account linking reads that one (`S4-2`), but
        the challenge names the spec-derived URL so a client that computes it
        rather than reading it finds the same document.
        """
        return self.url_for("/.well-known/oauth-protected-resource/mcp")

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
