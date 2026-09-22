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
import sys
from typing import Any

import structlog


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
        processors=[*shared, structlog.processors.format_exc_info, renderer],
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
