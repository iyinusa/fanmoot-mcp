# fanmoot-mcp

A standards-compliant **Model Context Protocol** server that exposes
[Fanmoot](https://fanmoot.com) — where fans debate football — as tools,
resources and prompts, with its own OAuth 2.1 authorization server so a fan can
sign in *or sign up* from the client and then post in their own name.

Spec era **`2025-11-25`**, Streamable HTTP, on `POST|GET /mcp`.

> **Skeleton.** This README fills out as the milestones land. `S0` is the
> scaffold: package, settings, logging, health, Docker, CI. See
> `../Amazon Hackathon.md` §6 for the milestone list.

## What it talks to

The server is a **client of Fanmoot's public API** (`https://fanmoot.com/v1/*`),
not of its database. It holds no model of Fanmoot's domain and shares no
connection pool with it. Everything the server can possibly do, a fan could do
through the public API.

Its own small PostgreSQL holds **OAuth state and nothing else**.

## Run it locally

```bash
make install          # uv venv + dependencies
cp .env.example .env  # then set FMCP_DATABASE_URL
make migrate
make dev              # http://localhost:8080
```

Or in Docker, which is what production runs:

```bash
make up               # docker-compose.yml + docker-compose.dev.yml
curl -s localhost:8080/healthz | python3 -m json.tool
```

`make check` is what CI runs — `ruff`, `mypy --strict`, `pytest`. A milestone
task is ticked only when it passes.

## Connect a client

*(Arrives with `S2`. The endpoint is `https://mcp.fanmoot.com/mcp`; it speaks
Streamable HTTP and negotiates protocol version `2025-11-25`.)*

## Licence

MIT. See [LICENSE](LICENSE).
