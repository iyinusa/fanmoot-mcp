"""Structured logging.

Lifted from ``fanmoot`` so a log line means the same thing in both services and
one aggregator query spans them. JSON in production, because that is what a log
aggregator can query; a human-readable console renderer in development, because
that is what a person can read.

``request_id`` is bound into a context variable by the middleware, which is what
lets every line emitted while handling one request — including every JSON-RPC
frame of an MCP call — be collected together without threading a logger through
every call. That matters more here than it did in ``fanmoot``: the trace *is*
the demo (HAD-12).
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

#: Keys whose value is a credential wherever they appear (`S7-4`). No code path
#: here logs one on purpose, and this is what keeps it that way: a future
#: ``log.info("token_issued", **body)`` has its secrets struck out before the
#: renderer sees them, rather than written to a log that an operator reads over
#: somebody's shoulder.
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "authorization",
        "client_secret",
        "code",
        "code_verifier",
        "cookie",
        "flow_token",
        "id_token",
        "password",
        "refresh_token",
        "registration_access_token",
        "secret",
        "set-cookie",
        "token",
    }
)

#: A bearer credential inside a free-text value: an error message that quoted a
#: header, say.
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")

REDACTED = "[redacted]"


def redact(_logger: Any, _method: str, event: MutableMapping[str, Any]) -> dict[str, Any]:
    """structlog processor: strike credentials out of one event, recursively."""
    return {key: _scrub(key, value) for key, value in event.items()}


def _scrub(key: str, value: Any) -> Any:
    if key.lower() in SENSITIVE_KEYS and value not in (None, ""):
        return REDACTED
    if isinstance(value, str):
        return _BEARER.sub(r"\1 " + REDACTED, value)
    if isinstance(value, Mapping):
        return {k: _scrub(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub("", item) for item in value]
    # Anything else — a tuple, an exception, a number — is passed through
    # untouched. `exc_info` is a tuple that structlog formats later, and a
    # processor that turned it into a list would break that formatting.
    return value


def configure_logging(env: str) -> None:
    is_prod = env.strip().lower() == "production"

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if is_prod
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        # `redact` last before rendering: after the traceback has become a
        # string, so a credential quoted in one is struck out as well, and after
        # everything else, so nothing added by an earlier processor escapes it.
        processors=[*shared, structlog.processors.format_exc_info, redact, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.INFO if is_prod else logging.DEBUG
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib loggers (uvicorn, sqlalchemy, alembic, the MCP SDK) through
    # the same handler so the stream stays one format rather than two
    # interleaved ones.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=logging.INFO, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine.Engine", "httpx", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
