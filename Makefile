# fanmoot-mcp — development tasks.
#
# Everything runs through .venv, so no shell activation is needed and CI and a
# laptop run the identical command. Same targets as `fanmoot`, so muscle memory
# carries between the two repos.

VENV := .venv
PY   := $(VENV)/bin/python
PIP  := uv pip install --python $(PY)
COMPOSE := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.DEFAULT_GOAL := help
.PHONY: help install dev test lint fmt types check migrate revision downgrade record build up down logs shell clean

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

migrate:  ## Apply migrations
	$(VENV)/bin/alembic upgrade head

revision:  ## Autogenerate a migration: make revision m="oauth clients"
	@test -n "$(m)" || (echo 'usage: make revision m="what changed"'; exit 1)
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

downgrade:  ## Roll back one revision
	$(VENV)/bin/alembic downgrade -1

record:  ## Re-capture live /v1 responses into fixtures/ (needs network)
	$(PY) scripts/record_fixtures.py $(ARGS)

build:  ## Build the Docker image
	docker build -t fanmoot-mcp:local .

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
