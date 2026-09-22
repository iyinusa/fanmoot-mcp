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

## Tests run offline, against a recorded Fanmoot

`fixtures/` holds real `/v1` responses captured from production by `make
record`. The suite drives the actual client — real timeouts, real retry policy,
real error mapping — against a transport that replays them, so it is fast,
deterministic and still asserting against shapes the API genuinely produced.
Nothing in `make check` touches the network or needs a database.

```bash
make record           # re-capture (needs network); writes fixtures/manifest.json
make record ARGS=-v   # …and print what each call returned
```

The authenticated endpoints are not recorded live — doing so would write a real
fan's handle and email into this repository — so their fixtures are hand-built
from Fanmoot's own schemas and marked `synthetic` in the manifest.

## Connect a client

*(Arrives with `S2`. The endpoint is `https://mcp.fanmoot.com/mcp`; it speaks
Streamable HTTP and negotiates protocol version `2025-11-25`.)*

## Licence

MIT. See [LICENSE](LICENSE).
