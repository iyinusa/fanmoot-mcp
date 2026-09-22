# syntax=docker/dockerfile:1.7

# ─────────────────────────────────────────────────────────────────────────────
# One stage, and that is the point.
#
# `fanmoot`'s image has a Node stage because it builds two frontends. This
# server has no frontend to build: the OAuth consent, login and sign-up pages
# are server-rendered Jinja2 (HAD-4), and the MCP Apps views (HAD-8, S6) are
# hand-written HTML with their CSS and JS inlined, because an MCP client renders
# them in a sandboxed iframe with no bundler and no second request. So there is
# nothing to compile and nothing to collect.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential and libpq-dev build the wheels that have no aarch64 prebuilt;
# curl is the healthcheck's only requirement.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# source, so an edit to app/ reuses the cached install.
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY tests ./tests

# Drop privileges. This process writes to nothing on disk — its only state is
# in PostgreSQL — so it has no business running as root, and leaving the code
# tree root-owned means a compromised process cannot rewrite its own source.
RUN useradd --create-home --uid 10001 fanmoot
USER fanmoot

# 8080, not fanmoot's 8000: the two run side by side on one Dokploy host.
EXPOSE 8080

# Hits the container's own port over localhost, so it must answer on any Host
# header — nothing here redirects on the host, which is why it can.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

# Overridden by docker-compose (which runs migrations first). This CMD is what
# a bare `docker run` gets.
#
# **One worker, deliberately.** `fanmoot` runs two; this cannot, yet. An MCP
# Streamable HTTP session lives in the serving process's memory, so a second
# worker would answer a request carrying a session id it has never seen with a
# 404 — intermittently, and only under load, which is the worst way to find
# out. Scaling out needs either stateless mode or session affinity, and that is
# an `S7` decision made with the latency numbers in hand, not a default set here.
CMD ["gunicorn", "app.main:app", \
    "-k", "uvicorn.workers.UvicornWorker", \
    "-b", "0.0.0.0:8080", \
    "--workers", "1", \
    "--access-logfile", "-"]
