# fanmoot-mcp — development tasks.
#
# Everything runs through .venv, so no shell activation is needed and CI and a
# laptop run the identical command. Same targets as `fanmoot`, so muscle memory
# carries between the two repos.

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := uv pip install --python $(PY)
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.dev.yml

# Where `make dev` serves the MCP endpoint. Override to point the Inspector at
# a deployed server: make inspect MCP_URL=https://mcp.fanmoot.com/mcp
MCP_URL ?= http://127.0.0.1:8080/mcp

.DEFAULT_GOAL := help
.PHONY: help local install dev test lint fmt types check inspect migrate revision downgrade record verify-live latency smoke rotate-keys build up down logs shell clean

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Create the venv and install dependencies
	uv venv --python 3.12 $(VENV)
	$(PIP) -e ".[dev]"

dev:  ## Run the server with reload
	$(VENV)/bin/uvicorn app.main:app --reload --port 8080

test:  ## Run the test suite (offline — recorded fixtures, no database needed)
	$(VENV)/bin/pytest -q

lint:  ## Check style and import order
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

fmt:  ## Autoformat and apply safe fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

types:  ## Type-check with mypy --strict
	$(VENV)/bin/mypy app

check: lint types test  ## Everything CI runs — must pass before ticking a milestone task

inspect:  ## Open MCP Inspector against a running server (needs `make dev` in another shell)
	@echo "Streamable HTTP → $(MCP_URL)"
	@echo "If the Inspector opens without the URL filled in, paste it and pick 'Streamable HTTP'."
	npx --yes @modelcontextprotocol/inspector --transport http --server-url $(MCP_URL)

migrate:  ## Apply migrations
	$(VENV)/bin/alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="oauth clients"
	@test -n "$(m)" || (echo 'usage: make revision m="what changed"'; exit 1)
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

downgrade:  ## Roll back one revision
	$(VENV)/bin/alembic downgrade -1

record:  ## Re-capture live /v1 responses into fixtures/ (needs network)
	$(PY) scripts/record_fixtures.py $(ARGS)

verify-live:  ## Replay the recordings against production; fail if a shape drifted (S7-8)
	$(PY) scripts/verify_live.py $(ARGS)

latency:  ## p50/p95 per tool from tool_calls; ARGS="--drive <url>" to measure a server (S7-1)
	$(PY) scripts/latency.py $(ARGS)

# The origin to smoke-test. `make smoke URL=http://127.0.0.1:8080` for `make dev`.
URL ?= https://mcp.fanmoot.com

smoke:  ## Check a deployed server from outside: TLS, discovery, handshake, 401 (S7-9)
	$(PY) scripts/smoke.py $(URL)

rotate-keys:  ## Re-wrap stored Fanmoot sessions under the newest key (S7-4, docs/runbook.md)
	$(PY) scripts/rotate_keys.py $(ARGS)

build:  ## Build the Docker image
	docker build -t fanmoot-mcp:local .

local:  ## Run the server and its own PostgreSQL on :8080 — nothing to configure
	docker compose -f docker-compose.local.yml up --build

up:  ## Start the stack with the dev overlay
	@docker network inspect dokploy-network >/dev/null 2>&1 \
	  || docker network create dokploy-network
	$(COMPOSE) up --build

down:  ## Stop the stack
	$(COMPOSE) down

logs:  ## Tail container logs
	$(COMPOSE) logs -f mcp

shell:  ## Shell into the running container
	$(COMPOSE) exec mcp /bin/bash

clean:  ## Remove caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache
